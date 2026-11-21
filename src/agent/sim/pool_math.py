"""PumpSwap pool math: what a buy or a sell in a graduated coin's pool pays and receives.

All amounts are integers in base units (lamports, raw token units), as on chain. No fee
rate or reserve is hardcoded: they come from recorded pool trades.

The rules, as found in the backfill's pool trades and checked against every one of them
(`verify_pool_trades`; `agent backtest` runs it on the days it simulates):

- Price is a constant product of the pool's token reserve and its *effective* quote
  reserve: the quote the pool holds plus its virtual quote reserve (pools of graduated
  coins start with part of their quote counted as virtual; the sum is what moves with
  trades).
- Sell:                gross = floor(tokens * quote / (token + tokens))
- Buy exact tokens:    net = floor(tokens * quote / (token - tokens)) + 1
- Buy with a budget:   net = floor(budget * 10_000 / (10_000 + total bps)); fees on that
  net; if net + fees exceed the budget the net is reduced by the excess; then
  tokens = floor((net - 1) * token / (quote + net - 1)). The same rule as on the curve.
- Three fees, each ceil(amount * bps / 10_000) on the net (buy) or gross (sell): the
  liquidity fee, which stays in the pool, the protocol fee and the coin creator's fee.
  A buyer pays net + fees; a seller receives gross - fees.
- After a buy the pool holds net + liquidity fee more quote and `tokens` fewer tokens;
  after a sell, gross - liquidity fee less quote and `tokens` more tokens.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

from agent.sim.curve_math import BPS, ceil_fee


@dataclass(frozen=True, slots=True)
class PoolRates:
    """Fee rates of a pool trade in basis points. A cashback fee, where a coin has one,
    is counted with the creator fee (it is charged on top in the same way)."""

    lp_bps: int
    protocol_bps: int
    creator_bps: int

    @property
    def total_bps(self) -> int:
        return self.lp_bps + self.protocol_bps + self.creator_bps

    def on(self, amount: int) -> tuple[int, int, int]:
        """(liquidity fee, protocol fee, creator fee) charged on `amount`."""
        return (ceil_fee(amount, self.lp_bps), ceil_fee(amount, self.protocol_bps),
                ceil_fee(amount, self.creator_bps))  # fmt: skip


@dataclass(frozen=True, slots=True)
class Pool:
    """A pool's reserves: `quote` is real plus virtual quote, `token` the coin."""

    quote: int
    token: int

    def price(self) -> float:
        """Marginal price: quote base units per raw token unit."""
        return self.quote / self.token


@dataclass(frozen=True, slots=True)
class PoolFill:
    side: str  # "buy" or "sell"
    tokens: int
    pool_quote: int  # net priced by the pool (buy) or gross out of it (sell)
    lp_fee: int
    protocol_fee: int
    creator_fee: int
    before: Pool
    after: Pool

    @property
    def fees(self) -> int:
        return self.lp_fee + self.protocol_fee + self.creator_fee

    @property
    def user_quote(self) -> int:
        """What the trader pays (buy) or receives (sell), fees included."""
        return self.pool_quote + self.fees if self.side == "buy" else self.pool_quote - self.fees


def _bought(pool: Pool, tokens: int, net: int, rates: PoolRates, fees=None) -> PoolFill:
    lp, protocol, creator = fees or rates.on(net)
    return PoolFill("buy", tokens, net, lp, protocol, creator, pool,
                    Pool(pool.quote + net + lp, pool.token - tokens))  # fmt: skip


def pool_buy_tokens(pool: Pool, tokens: int, rates: PoolRates) -> PoolFill:
    """Buy exactly `tokens`."""
    net = tokens * pool.quote // (pool.token - tokens) + 1
    return _bought(pool, tokens, net, rates)


def pool_buy_with_budget(pool: Pool, budget: int, rates: PoolRates) -> PoolFill:
    """Spend `budget` in total, fees included."""
    net = budget * BPS // (BPS + rates.total_bps)
    fees = rates.on(net)
    net -= max(net + sum(fees) - budget, 0)
    tokens = (net - 1) * pool.token // (pool.quote + net - 1) if net > 1 else 0
    return _bought(pool, tokens, net, rates, fees)


def pool_sell_tokens(pool: Pool, tokens: int, rates: PoolRates) -> PoolFill:
    """Sell exactly `tokens`."""
    gross = tokens * pool.quote // (pool.token + tokens)
    lp, protocol, creator = rates.on(gross)
    return PoolFill("sell", tokens, gross, lp, protocol, creator, pool,
                    Pool(pool.quote - (gross - lp), pool.token + tokens))  # fmt: skip


# --- the check against recorded pool trades ----------------------------------------------

# One row per recorded pool trade, with the amounts the formulas above need. Shared with
# the backtest's loader, so the check and the simulator read the pool the same way.
POOL_TRADE_SQL = """
SELECT pool, slot, coalesce(bf_tx_index, 0) AS tx_index, event_index, ts, is_buy, ix_name,
  pool_base_token_reserves::HUGEINT AS token_before,
  pool_quote_token_reserves::HUGEINT
    + coalesce(try_cast(virtual_quote_reserves AS HUGEINT), 0) AS quote_before,
  coalesce(base_amount_out, base_amount_in)::HUGEINT AS tokens,
  CASE WHEN is_buy THEN quote_amount_in_with_lp_fee::HUGEINT - lp_fee::HUGEINT
       ELSE quote_amount_out::HUGEINT END AS pool_quote,
  CASE WHEN is_buy THEN quote_amount_in_with_lp_fee::HUGEINT
       ELSE -quote_amount_out_without_lp_fee::HUGEINT END AS quote_change,
  CASE WHEN ix_name LIKE 'buy_exact%' THEN quote_amount_in::HUGEINT END AS budget,
  CASE WHEN is_buy THEN user_quote_amount_in::HUGEINT
       ELSE user_quote_amount_out::HUGEINT END AS user_quote_recorded,
  lp_fee_basis_points::HUGEINT AS lp_bps, protocol_fee_basis_points::HUGEINT AS protocol_bps,
  coin_creator_fee_basis_points::HUGEINT
    + coalesce(cashback_fee_basis_points, 0)::HUGEINT AS creator_bps,
  lp_fee::HUGEINT AS lp_fee, protocol_fee::HUGEINT AS protocol_fee,
  coin_creator_fee::HUGEINT + coalesce(cashback, 0)::HUGEINT AS creator_fee
FROM {source}
"""


def verify_pool_trades(data_dir: Path, start_ts: int, end_ts: int) -> dict:
    """How many recorded trades in the pools of graduated pump.fun coins, with
    `start_ts <= ts < end_ts`, the formulas above reproduce exactly: the pool amount, the
    tokens of a buy with a budget, and that each trade starts from the reserves the
    previous one left. Trades routed through several pools (`multi_hop_swap`) are counted
    apart: they charge fees once for the whole route."""
    root = (data_dir / "raw_backfill").as_posix()
    with duckdb.connect() as db:
        tmp = data_dir / "duckdb_tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        db.execute(f"SET temp_directory = '{tmp.as_posix()}'")
        graduated = f"read_parquet('{root}/migrations/*/*.parquet', union_by_name=true)"
        source = (f"read_parquet('{root}/amm_trades/*/*.parquet', union_by_name=true) "
                  f"WHERE ts >= {int(start_ts)} AND ts < {int(end_ts)} "
                  f"AND pool IN (SELECT pool FROM {graduated})")  # fmt: skip
        db.execute("CREATE TEMP TABLE t AS " + POOL_TRADE_SQL.format(source=source))
        db.execute(f"""
            CREATE TEMP TABLE checked AS
            WITH f AS (
              SELECT *, lp_bps + protocol_bps + creator_bps AS bps,
                budget * {BPS} // ({BPS} + lp_bps + protocol_bps + creator_bps) AS net0,
                lag(token_before + CASE WHEN is_buy THEN -tokens ELSE tokens END) OVER w
                  AS token_left,
                lag(quote_before + quote_change) OVER w AS quote_left
              FROM t WINDOW w AS (PARTITION BY pool ORDER BY slot, tx_index, event_index)),
            g AS (
              SELECT *, (net0 * lp_bps + {BPS - 1}) // {BPS} + (net0 * protocol_bps + {BPS - 1})
                // {BPS} + (net0 * creator_bps + {BPS - 1}) // {BPS} AS fees0 FROM f)
            SELECT ix_name = 'multi_hop_swap' AS routed, is_buy, budget IS NOT NULL AS by_budget,
              CASE
                WHEN NOT is_buy THEN pool_quote = tokens * quote_before // (token_before + tokens)
                WHEN budget IS NULL
                  THEN pool_quote = tokens * quote_before // (token_before - tokens) + 1
                ELSE pool_quote = net0 - greatest(net0 + fees0 - budget, 0)
                  AND tokens = (pool_quote - 1) * token_before // (quote_before + pool_quote - 1)
              END AS amounts_ok,
              lp_fee = (pool_quote * lp_bps + {BPS - 1}) // {BPS}
                AND protocol_fee = (pool_quote * protocol_bps + {BPS - 1}) // {BPS}
                AND creator_fee = (pool_quote * creator_bps + {BPS - 1}) // {BPS} AS fees_ok,
              token_left IS NOT NULL AS has_previous,
              token_left = token_before AND quote_left = quote_before AS continues
            FROM g""")
        out = {}
        for routed, is_buy, by_budget, n, amounts, fees, linked, continues in db.execute(
            "SELECT routed, is_buy, by_budget, count(*), sum(amounts_ok::INT), "
            "sum(fees_ok::INT), sum(has_previous::INT), sum((has_previous AND continues)::INT) "
            "FROM checked GROUP BY ALL ORDER BY ALL"
        ).fetchall():
            kind = ("routed through several pools" if routed else "sell" if not is_buy
                    else "buy with a budget" if by_budget else "buy, exact tokens")  # fmt: skip
            got = out.setdefault(kind, {"trades": 0, "amounts_exact": 0, "fees_exact": 0,
                                        "with_previous": 0, "continues": 0})  # fmt: skip
            for key, value in zip(got, (n, amounts, fees, linked, continues), strict=True):
                got[key] += int(value or 0)
    return out
