"""The backtest's simulator (spec 10; research track step R6): what one buy after a signal,
and the sell an exit plan leads to, would have paid and received. Analysis only: nothing
here places a trade.

One simulated trade, for a coin, a decision time (T+k), a delay and an exit plan:

1. **Buy** `buy_sol` (pump.fun's fees included) at T+k+delay, on the reserves after the
   last trade at or before that second, with exact curve or pool math and the fees read
   from chain (`market`). The buy fails, as it would on Padre, if it gets fewer tokens
   than `slippage_pct` allows against the quote `quote_age_s` earlier.
2. **Watch** the price after every later trade (trades after the entry second only)
   against the price paid, before fees: the first one at or above the take-profit or at or
   below the stop-loss triggers the exit; else the time stop does.
3. **Sell** everything `delay` seconds after the trigger (a person reacts as slowly as
   when buying); a time stop sells at its planned time. A sell that would get less than
   the slippage limit allows fails and is tried again `delay` later. After
   `max_sell_attempts` failures the next attempt goes through whatever the price.

No lookahead: the buy reads trades up to its own second, the trigger reads trades after
it, each sell reads trades up to its own second (`tests/test_backtest.py` checks).

Failed transactions (`failed_tx_rate`): each attempt also fails by chance. The chance
numbers are fixed per coin, decision time, delay and plan (a hash, not a random seed), so
a run can be repeated exactly and a higher rate fails every attempt a lower one does. A
failed buy is not retried; a failed sell is. Every attempt that reaches the chain pays
the priority fee and Solana's base fee, failed or not.

Our own buy and the market (the limit that matters most; both versions are reported):
- `none`: the buy leaves no trace. The sell is priced on the reserves as recorded, which
  never held our SOL. Strict: on a new curve it understates a 1 SOL sale by about 6%.
- `stays`: the SOL our buy put in stays in the reserves, and everyone else is assumed to
  have moved the same SOL as they did. Exact when nobody else trades; generous when
  others sell after us (they would have taken part of our SOL).

A sale on the curve never pays out more than the curve holds (the quote it recorded plus
what our buy put in), in either view.

Other limits: a buy that would complete the curve takes what is left and stops there
(`capped`); a position whose coin left the curve but whose pool never traded in the data
is counted as worth nothing (`stuck`); the rent of a token account is not counted.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml
from pydantic import BaseModel, ConfigDict, Field

from agent.sim.curve_math import Curve, buy_with_budget
from agent.sim.market import CURVE, Market, Spot
from agent.sim.pool_math import Pool, pool_buy_with_budget

LAMPORTS_PER_SOL = 1_000_000_000
FOOTPRINTS = ("none", "stays")
# Why a buy did not happen. "slippage" reached the chain and failed there (it pays the
# transaction fees); the others never left Padre.
FILLED, SLIPPAGE, MIGRATING, NO_TOKENS, NO_FEE_DATA = (
    "filled", "slippage", "migrating", "no_tokens", "no_fee_data")  # fmt: skip
TAKE_PROFIT, STOP_LOSS, TIME_STOP = "take_profit", "stop_loss", "time_stop"


# --- settings (config/padre_presets.yaml) ------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExitPlan(_Strict):
    name: str
    take_profit_pct: float | None = None
    stop_loss_pct: float | None = None
    time_stop_min: float


class Presets(_Strict):
    buy_sol: float
    entry_delay_s: int
    entry_delay_tests_s: list[int]
    slippage_pct: float
    quote_age_s: int = Field(ge=0)
    priority_fee_sol: float
    platform_fee_pct: float | None = None
    platform_fee_tests_pct: list[float] = []
    failed_tx_rate: float = Field(ge=0, lt=1)
    failed_tx_rate_tests: list[float]
    max_sell_attempts: int = Field(ge=1, le=8)
    exit_plans: list[ExitPlan]

    @property
    def budget(self) -> int:
        return round(self.buy_sol * LAMPORTS_PER_SOL)

    @property
    def delays(self) -> list[int]:
        return sorted({self.entry_delay_s, *self.entry_delay_tests_s})

    @property
    def failed_rates(self) -> list[float]:
        return sorted({self.failed_tx_rate, *self.failed_tx_rate_tests})

    @property
    def horizon_s(self) -> int:
        """How long after a decision time the trades must be known."""
        longest = max(self.delays)
        return round(longest + max(p.time_stop_min for p in self.exit_plans) * 60
                     + (self.max_sell_attempts + 1) * longest)  # fmt: skip


def load_presets(path: Path) -> tuple[Presets, str]:
    """The settings and a short hash of the file."""
    raw = path.read_bytes()
    return Presets.model_validate(yaml.safe_load(raw) or {}), hashlib.sha256(raw).hexdigest()[:12]


# --- one trade ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Buy:
    status: str
    ts: int
    venue: str | None = None
    tokens: int = 0
    net: int = 0  # the quote our buy put into the reserves
    spent: int = 0  # what the buy cost, pump.fun's fees included
    price: float | None = None  # quote per token before fees: what exits compare with
    fee_bps: int | None = None
    capped: bool = False  # took everything the curve had left


def _bought(spot: Spot, budget: int) -> tuple[int, int, int, bool]:
    """(tokens, net into the reserves, spent in total, capped) for a buy at `spot`."""
    if spot.venue == CURVE:
        fill = buy_with_budget(Curve(spot.quote, spot.token, 0, spot.real_token), budget,
                               spot.rates)  # fmt: skip
        return fill.tokens, fill.curve_quote, fill.user_quote, fill.capped
    fill = pool_buy_with_budget(Pool(spot.quote, spot.token), budget, spot.rates)
    return fill.tokens, fill.pool_quote, fill.user_quote, False


def simulate_buy(market: Market, ts: int, presets: Presets, launch_rates=None) -> Buy:
    """A buy of `presets.buy_sol` landing at second `ts`."""
    spot = market.spot(ts, launch_rates)
    if spot is None:
        return Buy(MIGRATING, ts)
    if spot.rates is None:
        return Buy(NO_FEE_DATA, ts, spot.venue)
    if spot.venue == CURVE and spot.real_token <= 0:
        return Buy(MIGRATING, ts, spot.venue)
    tokens, net, spent, capped = _bought(spot, presets.budget)
    if tokens <= 0:
        return Buy(NO_TOKENS, ts, spot.venue)
    buy = Buy(FILLED, ts, spot.venue, tokens, net, spent, net / tokens,
              spot.rates.total_bps, capped)  # fmt: skip
    quoted = market.spot(ts - presets.quote_age_s, launch_rates)
    if quoted is not None and quoted.rates is not None and not (
        quoted.venue == CURVE and quoted.real_token <= 0
    ):
        expected = _bought(quoted, presets.budget)[0]
        if tokens < expected * (1 - presets.slippage_pct / 100):
            return Buy(SLIPPAGE, ts, spot.venue)
    return buy


def _sold(spot: Spot, tokens: int, own_net: int) -> tuple[int, int]:
    """What selling `tokens` at `spot` pays after pump.fun's fees: on the reserves as
    recorded ("none"), and with the quote our buy put in still there ("stays")."""
    gross = tokens * spot.quote // (spot.token + tokens)
    quote = spot.quote + own_net
    token = spot.quote * spot.token // quote
    gross_stays = tokens * quote // (token + tokens)
    if spot.real_quote is not None:
        # A curve cannot pay out more than it holds: what it recorded plus what our buy
        # put in. (It matters on mayhem coins, whose virtual reserves are set freely.)
        held = spot.real_quote + own_net
        gross, gross_stays = min(gross, held), min(gross_stays, held)
    return gross - sum(spot.rates.on(gross)), gross_stays - sum(spot.rates.on(gross_stays))


@dataclass(frozen=True, slots=True)
class Attempt:
    ts: int
    none: int  # lamports received, our buy leaving no trace
    stays: int  # lamports received, our SOL still in the reserves
    slipped: bool  # this sell would have failed on the slippage limit
    stuck: bool = False  # nowhere to sell in the data: counted as worth nothing


def simulate_sell(market: Market, ts: int, buy: Buy, presets: Presets, launch_rates=None,
                  *, forced: bool = False) -> Attempt:  # fmt: skip
    """A sell of the whole position landing at second `ts` (`forced`: without the
    slippage limit)."""
    spot = market.spot(ts, launch_rates, wait_for_pool=True)
    if spot is None or spot.rates is None:
        return Attempt(ts, 0, 0, False, stuck=True)
    none, stays = _sold(spot, buy.tokens, buy.net)
    slipped = False
    if not forced:
        quoted = market.spot(ts - presets.quote_age_s, launch_rates)
        if quoted is not None and quoted.rates is not None:
            expected = _sold(quoted, buy.tokens, buy.net)[0]
            slipped = none < expected * (1 - presets.slippage_pct / 100)
    return Attempt(max(spot.ts, ts), none, stays, slipped)


def find_exit(market: Market, buy: Buy, plan: ExitPlan) -> tuple[str, int]:
    """(why, when) the plan decides to sell: the first trade after the entry second whose
    price reaches the take-profit or the stop-loss, else the time stop."""
    stop_ts = buy.ts + round(plan.time_stop_min * 60)
    first = int(np.searchsorted(market.x_ts, buy.ts, side="right"))
    last = int(np.searchsorted(market.x_ts, stop_ts, side="right"))
    if last > first and (plan.take_profit_pct is not None or plan.stop_loss_pct is not None):
        prices = market.x_price[first:last]
        up = (np.inf if plan.take_profit_pct is None
              else buy.price * (1 + plan.take_profit_pct / 100))  # fmt: skip
        down = (-np.inf if plan.stop_loss_pct is None
                else buy.price * (1 - plan.stop_loss_pct / 100))  # fmt: skip
        hit = (prices >= up) | (prices <= down)
        if hit.any():
            i = int(np.argmax(hit))
            return (TAKE_PROFIT if prices[i] >= up else STOP_LOSS), int(market.x_ts[first + i])
    return TIME_STOP, stop_ts


def chances(*key: object, n: int) -> list[float]:
    """`n` numbers in [0, 1) fixed by `key`: an attempt fails by chance when its number
    is below the failed-transaction rate."""
    digest = hashlib.blake2b("|".join(map(str, key)).encode(), digest_size=8 * n).digest()
    return [int.from_bytes(digest[i * 8 : i * 8 + 8], "big") / 2**64 for i in range(n)]


# The ledger: one row per coin, decision time, delay and exit plan.
ATTEMPT_FIELDS = ("sell_ts", "sell_none", "sell_stays", "slipped", "chance")


def ledger_columns(presets: Presets) -> list[str]:
    last = presets.max_sell_attempts + 1
    return ["mint", "offset_s", "delay_s", "plan", "entry_ts", "buy_status", "venue",
            "capped", "tokens", "buy_net", "buy_spent", "entry_price", "buy_fee_bps",
            "buy_chance", "exit_reason", "trigger_ts", "stuck",
            *(f"{name}_{k}" for k in range(1, last + 1) for name in ATTEMPT_FIELDS)]  # fmt: skip


def simulate_signal(market: Market, offset_s: int, delay_s: int, presets: Presets,
                    launch_rates=None) -> list[dict]:  # fmt: skip
    """One ledger row per exit plan for a buy `delay_s` after decision time T+`offset_s`.

    Sell attempts are worked out as far as any tested failed-transaction rate can reach:
    the next one only when this one slipped or its chance number is below the highest
    rate. The last attempt is forced."""
    entry_ts = market.created_ts + offset_s + delay_s
    buy = simulate_buy(market, entry_ts, presets, launch_rates)
    highest = max(presets.failed_rates)
    last = presets.max_sell_attempts + 1
    base = {
        "mint": market.mint, "offset_s": offset_s, "delay_s": delay_s, "plan": None,
        "entry_ts": entry_ts, "buy_status": buy.status, "venue": buy.venue, "capped": buy.capped,
        "tokens": buy.tokens, "buy_net": buy.net, "buy_spent": buy.spent,
        "entry_price": buy.price, "buy_fee_bps": buy.fee_bps,
        "buy_chance": chances(market.mint, offset_s, delay_s, "buy", n=1)[0],
    }  # fmt: skip
    rows = []
    for plan in presets.exit_plans:
        row = base | {"plan": plan.name, "exit_reason": None, "trigger_ts": None, "stuck": False}
        row |= {f"{name}_{k}": None for k in range(1, last + 1) for name in ATTEMPT_FIELDS}
        if buy.status == FILLED:
            reason, trigger_ts = find_exit(market, buy, plan)
            row["exit_reason"], row["trigger_ts"] = reason, trigger_ts
            sell_ts = trigger_ts if reason == TIME_STOP else trigger_ts + delay_s
            numbers = chances(market.mint, offset_s, delay_s, plan.name, n=last)
            for k in range(1, last + 1):
                attempt = simulate_sell(market, sell_ts, buy, presets, launch_rates,
                                        forced=k == last)  # fmt: skip
                # A stuck position is settled at once (as worth nothing): it cannot fail.
                chance = 1.0 if attempt.stuck else numbers[k - 1]
                row |= {f"sell_ts_{k}": attempt.ts, f"sell_none_{k}": attempt.none,
                        f"sell_stays_{k}": attempt.stays, f"slipped_{k}": int(attempt.slipped),
                        f"chance_{k}": chance}  # fmt: skip
                row["stuck"] = row["stuck"] or attempt.stuck
                if not (attempt.slipped or chance < highest):
                    break
                sell_ts += delay_s
        rows.append(row)
    return rows


# --- profit and loss ---------------------------------------------------------------------


def outcomes(ledger, presets: Presets, *, failed_rate: float, footprint: str,
             base_fee_lamports: int, platform_fee_pct: float = 0.0
             ) -> dict[str, np.ndarray]:  # fmt: skip
    """Per ledger row, for one failed-transaction rate and one view of our own buy:
    - `pnl_sol`: SOL gained or lost, after pump.fun's fees, the priority and base fee of
      every attempt that reached the chain, and Padre's fee (`platform_fee_pct` of the
      SOL bought and sold);
    - `traded`: the buy went through; `sent`: transactions that reached the chain;
    - `sell_attempt`: which sell attempt went through (0 when nothing was bought).
    `ledger` is a table of `ledger_columns` (a pandas frame or a dict of arrays)."""
    if footprint not in FOOTPRINTS:
        raise ValueError(f"unknown footprint {footprint!r}")
    if failed_rate > max(presets.failed_rates):
        raise ValueError("the ledger was not worked out for a failed rate this high")

    def column(name: str, missing: float = np.nan) -> np.ndarray:
        values = np.asarray(ledger[name])
        if values.dtype == object:  # a column with empty cells that was not read as numbers
            values = np.array([np.nan if v is None else v for v in values], dtype=float)
        values = values.astype(float)
        return values if np.isnan(missing) else np.where(np.isnan(values), missing, values)

    status = np.asarray(ledger["buy_status"], dtype=object)
    n = len(status)
    filled = status == FILLED
    traded = filled & (column("buy_chance", 1.0) >= failed_rate)
    # A buy that reached the chain and failed there still paid its transaction fees.
    sent = (filled | (status == SLIPPAGE)).astype(float)
    last = presets.max_sell_attempts + 1
    received = np.zeros(n)
    sell_attempt = np.zeros(n, dtype=int)
    open_ = traded.copy()
    for k in range(1, last + 1):
        amount = column(f"sell_{footprint}_{k}")
        fails = (column(f"slipped_{k}", 0.0) > 0) | (column(f"chance_{k}", 1.0) < failed_rate)
        done = open_ & ~np.isnan(amount) & (~fails | (k == last))
        received[done] = amount[done]
        sell_attempt[done] = k
        open_ &= ~done
    if open_.any():
        raise ValueError("ledger rows without a completed sell attempt")
    stuck = traded & np.asarray(ledger["stuck"]).astype(bool)
    sent = sent + np.where(stuck, 0, sell_attempt)  # a stuck position sends no sell
    spent = np.where(traded, column("buy_spent", 0.0), 0.0)
    per_tx = presets.priority_fee_sol + base_fee_lamports / LAMPORTS_PER_SOL
    pnl = ((received - spent) / LAMPORTS_PER_SOL - sent * per_tx
           - platform_fee_pct / 100 * (spent + received) / LAMPORTS_PER_SOL)  # fmt: skip
    return {"pnl_sol": pnl, "traded": traded, "sent": sent, "sell_attempt": sell_attempt}
