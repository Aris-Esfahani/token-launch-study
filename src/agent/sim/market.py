"""A coin's market for the backtest (spec 10): the reserves after every trade, in exact
order, on its bonding curve and, after graduation, in its PumpSwap pool; and the fees each
trade was charged. Read from the backfill only (`raw_backfill/`), which has every trade's
position in its block.

What the backfill shows, and the simulator relies on (checked on Nov 12-17, 2026; see
`docs/PROGRESS.md`, "R6"):
- Every trade by a person starts exactly from the reserves the previous recorded trade
  reported, on ordinary and on mayhem coins alike. The mayhem program's reserve resets
  come with the mayhem agent's own trades, so the reserves after the last trade are the
  ones the next person trades on, whoever made that last trade.
- A pool's price follows its token reserve and its quote reserve *plus* its virtual quote
  reserve (`pool_math`).
- A curve's price follows its virtual reserves, but it can only pay out the quote it
  really holds (`real_quote`). On ordinary coins the two differ by a fixed amount; on
  mayhem coins the mayhem program sets the virtual reserves freely, far above or below.

Fee rates are never hardcoded. On the curve a simulated trade pays the rates of the last
trade a person made on that coin (not the mayhem agent, which trades free, and not a
routed trade, which is charged once for its whole route). A coin nobody has traded yet
pays what the most recent first trade of any other coin was charged (`LaunchRates`). In a
pool it pays the most common rates among the pool's last trades.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa

from agent.sim.curve_math import NO_CREATOR, Curve, FeeRates, mayhem_agent_address
from agent.sim.pool_math import POOL_TRADE_SQL, PoolRates

BACKFILL = "raw_backfill"
NATIVE_SOL = "1" * 32  # quote_mint of SOL-paired coins: the all-zero address
CURVE, POOL = "curve", "pool"
POOL_RATE_TRADES = 20  # a pool trade pays the most common rates of this many last trades
LAUNCH_RATES_BACK_S = 3600  # how far back first trades are read for coins without a trade
_EMPTY = np.zeros(0, dtype=np.int64)


@dataclass(frozen=True, slots=True)
class Spot:
    """Where and at what reserves a trade lands."""

    venue: str
    quote: int
    token: int
    real_token: int | None  # what the curve has left to sell; None in a pool
    rates: FeeRates | PoolRates | None  # None: no fee data for this moment
    ts: int  # the moment these reserves are from
    real_quote: int | None = None  # the quote the curve really holds; None in a pool


@dataclass
class Market:
    mint: str
    created_ts: int
    is_mayhem: bool
    start: Curve  # the curve at launch
    completed_ts: int | None = None  # when the bonding curve completed
    # Curve trades in order: time, reserves after the trade, the fees charged.
    c_ts: np.ndarray = field(default_factory=lambda: _EMPTY)
    c_quote: np.ndarray = field(default_factory=lambda: _EMPTY)
    c_token: np.ndarray = field(default_factory=lambda: _EMPTY)
    c_real_token: np.ndarray = field(default_factory=lambda: _EMPTY)
    c_real_quote: np.ndarray = field(default_factory=lambda: _EMPTY)
    c_rates: np.ndarray = field(default_factory=lambda: np.zeros((0, 2), dtype=np.int64))
    c_payer: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=bool))
    # Pool trades in order: time, reserves after the trade (quote = real + virtual), fees.
    p_ts: np.ndarray = field(default_factory=lambda: _EMPTY)
    p_quote: np.ndarray = field(default_factory=lambda: _EMPTY)
    p_token: np.ndarray = field(default_factory=lambda: _EMPTY)
    p_rates: np.ndarray = field(default_factory=lambda: np.zeros((0, 3), dtype=np.int64))

    def __post_init__(self) -> None:
        # The index of the last fee-paying curve trade at or before each curve trade.
        index = np.where(self.c_payer, np.arange(len(self.c_payer)), -1)
        self.c_last_payer = np.maximum.accumulate(index) if len(index) else index
        if self.completed_ts is None and len(self.c_ts):
            done = np.flatnonzero(self.c_real_token == 0)
            if len(done):
                self.completed_ts = int(self.c_ts[done[0]])
        # The price after every trade, curve then pool: what exits are triggered by.
        self.x_ts = np.concatenate([self.c_ts, self.p_ts])
        with np.errstate(divide="ignore", invalid="ignore"):
            self.x_price = np.concatenate([self.c_quote / self.c_token,
                                           self.p_quote / self.p_token])  # fmt: skip
        if len(self.x_ts) > 1 and (np.diff(self.x_ts) < 0).any():
            order = np.argsort(self.x_ts, kind="stable")
            self.x_ts, self.x_price = self.x_ts[order], self.x_price[order]

    def spot(self, ts: int, launch_rates=None, *, wait_for_pool: bool = False) -> Spot | None:
        """The reserves after the last trade at or before second `ts`, and the fees a
        trade there pays. None while the coin moves from its curve to its pool (no pool
        trade yet); with `wait_for_pool`, the pool's first trade instead (None if the data
        has none)."""
        if self.completed_ts is not None and self.completed_ts <= ts:
            j = int(np.searchsorted(self.p_ts, ts, side="right")) - 1
            if j < 0:
                if not (wait_for_pool and len(self.p_ts)):
                    return None
                j = 0
            return Spot(POOL, int(self.p_quote[j]), int(self.p_token[j]), None,
                        self.pool_rates(j), int(self.p_ts[j]))  # fmt: skip
        i = int(np.searchsorted(self.c_ts, ts, side="right")) - 1
        if i < 0:
            rates = launch_rates(ts, self.is_mayhem) if launch_rates else None
            return Spot(CURVE, self.start.virtual_quote, self.start.virtual_token,
                        self.start.real_token, rates, self.created_ts,
                        self.start.real_quote)  # fmt: skip
        payer = int(self.c_last_payer[i])
        if payer >= 0:
            rates = FeeRates(int(self.c_rates[payer, 0]), int(self.c_rates[payer, 1]))
        else:
            rates = launch_rates(ts, self.is_mayhem) if launch_rates else None
        return Spot(CURVE, int(self.c_quote[i]), int(self.c_token[i]),
                    int(self.c_real_token[i]), rates, int(self.c_ts[i]),
                    int(self.c_real_quote[i]))  # fmt: skip

    def pool_rates(self, j: int) -> PoolRates:
        """The most common fee rates among the pool's last trades up to trade `j` (the
        latest of them on a tie). Trades that paid no liquidity fee (fee-free wallets,
        such as the protocol's own buybacks) are left out when any trade paid one."""
        rows = self.p_rates[max(0, j - POOL_RATE_TRADES + 1) : j + 1].tolist()
        paying = [tuple(r) for r in rows if r[0] > 0] or [tuple(r) for r in rows]
        counts = Counter(paying)
        most = max(counts.values())
        return PoolRates(*next(r for r in reversed(paying) if counts[r] == most))


class LaunchRates:
    """The fees a first trade on a new coin pays: what the most recent first trade of any
    coin (of the same kind) at or before a moment was charged."""

    def __init__(self, rows: list[tuple[int, bool, int, int]]) -> None:
        self.by_kind = {}
        for mayhem in (False, True):
            kept = sorted((ts, fee, creator) for ts, m, fee, creator in rows if m == mayhem)
            self.by_kind[mayhem] = (np.array([r[0] for r in kept], dtype=np.int64), kept)

    def __call__(self, ts: int, is_mayhem: bool) -> FeeRates | None:
        times, rows = self.by_kind[bool(is_mayhem)]
        i = int(np.searchsorted(times, ts, side="right")) - 1
        return FeeRates(rows[i][1], rows[i][2]) if i >= 0 else None


# --- reading the backfill ----------------------------------------------------------------


def _source(root: Path, table: str, first_ts: int, last_ts: int) -> str | None:
    """SQL for the table's files of the UTC days from `first_ts` to `last_ts` (a day more
    on each side), or None when there are none."""
    day = datetime.fromtimestamp(first_ts, UTC).date() - timedelta(days=1)
    last = datetime.fromtimestamp(last_ts, UTC).date() + timedelta(days=1)
    files = []
    while day <= last:
        files += sorted((root / table / f"date={day.isoformat()}").glob("*.parquet"))
        day += timedelta(days=1)
    if not files:
        return None
    listed = ", ".join(f"'{f.as_posix()}'" for f in files)
    return f"read_parquet([{listed}], union_by_name=true)"


def _groups(mints: np.ndarray) -> list[tuple[str, int, int]]:
    """(mint, start, end) of each run of equal mints in an array sorted by mint."""
    if not len(mints):
        return []
    starts = np.flatnonzero(np.r_[True, mints[1:] != mints[:-1]])
    ends = np.r_[starts[1:], len(mints)]
    return [(mints[a], int(a), int(b)) for a, b in zip(starts, ends, strict=True)]


@dataclass
class Loaded:
    markets: dict[str, Market]
    launch_rates: LaunchRates
    base_fee_lamports: int | None  # the lowest fee per signature among the trades read


def load_markets(data_dir: Path, coins: list[tuple[str, int]], horizon_s: int) -> Loaded:
    """The markets of `coins` ((mint, launch time) pairs) up to `horizon_s` after each
    launch. Coins must be SOL-paired. The trades are read in exact order: slot, position
    in the block, event."""
    root = data_dir / BACKFILL
    agent = mayhem_agent_address()
    first = min(ts for _, ts in coins)
    last = max(ts for _, ts in coins) + horizon_s
    with duckdb.connect() as db:
        tmp = data_dir / "duckdb_tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        db.execute(f"SET temp_directory = '{tmp.as_posix()}'")
        db.register("wanted_rows", pa.table({
            "mint": [c[0] for c in coins], "created_ts": [int(c[1]) for c in coins]}))  # fmt: skip
        db.execute("CREATE TEMP TABLE wanted AS SELECT mint, created_ts::BIGINT AS created_ts "
                   "FROM wanted_rows")  # fmt: skip
        tokens = _source(root, "tokens", first - horizon_s, last)
        if tokens is None:
            raise FileNotFoundError("No backfilled tokens for these coins.")
        markets = {
            r[0]: Market(mint=r[0], created_ts=r[1], is_mayhem=bool(r[2]),
                         start=Curve(r[3], r[4], 0, r[5]))
            for r in db.execute(
                "SELECT w.mint, w.created_ts, coalesce(t.is_mayhem_mode, false), "
                "t.virtual_quote_reserves::BIGINT, t.virtual_token_reserves::BIGINT, "
                f"t.real_token_reserves::BIGINT FROM wanted w JOIN {tokens} t "
                "ON t.mint = w.mint "
                "QUALIFY row_number() OVER (PARTITION BY w.mint ORDER BY t.ts, t.signature) = 1"
            ).fetchall()
        }  # fmt: skip
        missing = [m for m, _ in coins if m not in markets]
        if missing:
            raise ValueError(f"{len(missing):,} coins are not in the backfill's tokens table, "
                             f"e.g. {missing[0]}")  # fmt: skip
        completes = _source(root, "curve_completes", first, last)
        if completes:
            for mint, ts in db.execute(
                f"SELECT mint, min(c.ts) FROM {completes} c JOIN wanted USING (mint) GROUP BY mint"
            ).fetchall():
                markets[mint].completed_ts = int(ts)

        curve = _source(root, "trades", first, last)
        base_fee, launch_rows = None, []
        curve_parts: dict[str, dict] = {}
        if curve:
            got = db.execute(
                "SELECT t.mint AS mint, t.ts AS ts, t.virtual_quote_reserves::BIGINT AS quote, "
                "t.virtual_token_reserves::BIGINT AS token, "
                "t.real_token_reserves::BIGINT AS real_token, "
                "coalesce(t.real_quote_reserves, 0)::BIGINT AS real_quote, "
                "coalesce(t.fee_basis_points, 0)::BIGINT AS fee_bps, "
                f"(CASE WHEN t.creator = '{NO_CREATOR}' THEN 0 "
                "  ELSE coalesce(t.creator_fee_basis_points, 0) END "
                " + coalesce(t.cashback_fee_basis_points, 0))::BIGINT AS creator_bps, "
                f"(coalesce(t.fee_basis_points, 0) > 0 AND t.wallet <> '{agent}' "
                " AND t.ix_name <> 'multi_hop_swap') AS payer "
                f"FROM {curve} t JOIN wanted w USING (mint) "
                "WHERE t.ts >= w.created_ts AND t.ts <= w.created_ts + ? "
                "ORDER BY t.mint, t.slot, coalesce(t.bf_tx_index, 0), t.event_index",
                [horizon_s],
            ).fetchnumpy()
            for mint, a, b in _groups(got["mint"]):
                curve_parts[mint] = {
                    "c_ts": got["ts"][a:b].astype(np.int64),
                    "c_quote": got["quote"][a:b].astype(np.int64),
                    "c_token": got["token"][a:b].astype(np.int64),
                    "c_real_token": got["real_token"][a:b].astype(np.int64),
                    "c_real_quote": got["real_quote"][a:b].astype(np.int64),
                    "c_rates": np.column_stack([got["fee_bps"][a:b], got["creator_bps"][a:b]])
                    .astype(np.int64),
                    "c_payer": got["payer"][a:b].astype(bool),
                }
            base_fee = db.execute(
                f"SELECT min(bf_fee // bf_signatures) FROM {curve} "
                "WHERE ts >= ? AND ts <= ? AND bf_signatures > 0", [first, last]
            ).fetchone()[0]  # fmt: skip
            # The first fee-paying trade of every SOL-paired coin launched around these.
            launch_rows = db.execute(
                "SELECT ts, m, fee_bps, creator_bps FROM ("
                " SELECT t.ts, coalesce(t.mayhem_mode, false) AS m, "
                " t.fee_basis_points::BIGINT AS fee_bps, "
                f" (CASE WHEN t.creator = '{NO_CREATOR}' THEN 0 "
                "   ELSE coalesce(t.creator_fee_basis_points, 0) END "
                "  + coalesce(t.cashback_fee_basis_points, 0))::BIGINT AS creator_bps, "
                " row_number() OVER (PARTITION BY t.mint "
                "   ORDER BY t.slot, coalesce(t.bf_tx_index, 0), t.event_index) AS rn "
                f" FROM {curve} t WHERE t.quote_mint = '{NATIVE_SOL}' "
                f" AND t.fee_basis_points > 0 AND t.wallet <> '{agent}' "
                " AND t.ix_name <> 'multi_hop_swap' AND t.ts >= ? AND t.ts <= ? "
                f" AND t.mint IN (SELECT mint FROM {tokens} WHERE ts >= ? AND ts <= ?)) "
                "WHERE rn = 1",
                [first - LAUNCH_RATES_BACK_S, last, first - LAUNCH_RATES_BACK_S, last],
            ).fetchall()

        pool_parts: dict[str, dict] = {}
        migrations = _source(root, "migrations", first, last)
        pool_trades = _source(root, "amm_trades", first, last)
        if migrations and pool_trades:
            db.execute(
                "CREATE TEMP TABLE pools AS SELECT m.mint, arg_min(m.pool, m.ts) AS pool, "
                f"any_value(w.created_ts) AS created_ts FROM {migrations} m "
                "JOIN wanted w USING (mint) GROUP BY m.mint"
            )
            inner = POOL_TRADE_SQL.format(
                source=f"{pool_trades} WHERE pool IN (SELECT pool FROM pools) "
                       f"AND ts >= {int(first)} AND ts <= {int(last)}")  # fmt: skip
            got = db.execute(
                "SELECT p.mint AS mint, a.ts AS ts, (a.quote_before + a.quote_change)::BIGINT "
                "AS quote, (a.token_before + CASE WHEN a.is_buy THEN -a.tokens ELSE a.tokens END)"
                "::BIGINT AS token, a.lp_bps::BIGINT AS lp_bps, "
                "a.protocol_bps::BIGINT AS protocol_bps, a.creator_bps::BIGINT AS creator_bps "
                f"FROM ({inner}) a JOIN pools p USING (pool) "
                "WHERE a.ts >= p.created_ts AND a.ts <= p.created_ts + ? "
                "ORDER BY p.mint, a.slot, a.tx_index, a.event_index",
                [horizon_s],
            ).fetchnumpy()
            for mint, a, b in _groups(got["mint"]):
                pool_parts[mint] = {
                    "p_ts": got["ts"][a:b].astype(np.int64),
                    "p_quote": got["quote"][a:b].astype(np.int64),
                    "p_token": got["token"][a:b].astype(np.int64),
                    "p_rates": np.column_stack([got["lp_bps"][a:b], got["protocol_bps"][a:b],
                                                got["creator_bps"][a:b]]).astype(np.int64),
                }  # fmt: skip
    for mint, market in markets.items():
        parts = curve_parts.get(mint, {}) | pool_parts.get(mint, {})
        if parts:
            markets[mint] = Market(mint=mint, created_ts=market.created_ts,
                                   is_mayhem=market.is_mayhem, start=market.start,
                                   completed_ts=market.completed_ts, **parts)  # fmt: skip
    rates = LaunchRates([(int(ts), bool(m), int(f), int(c)) for ts, m, f, c in launch_rows])
    return Loaded(markets, rates, None if base_fee is None else int(base_fee))
