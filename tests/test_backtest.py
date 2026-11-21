"""The backtest's simulator (spec 10; research track R6) on built markets: exact fills
against numbers worked out by hand, fees, exit plans, slippage, failed transactions, the
move to the pool, no lookahead, and the same result when built again."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

from agent.sim import backtest as B
from agent.sim.curve_math import (
    NO_CREATOR,
    Curve,
    FeeRates,
    buy_with_budget,
    mayhem_agent_address,
    sell_tokens,
)
from agent.sim.market import NATIVE_SOL, LaunchRates, Market, load_markets
from agent.sim.pool_math import Pool, PoolRates, pool_buy_with_budget, pool_sell_tokens

ROOT = Path(__file__).resolve().parents[1]
START = Curve(30 * 10**9, 1_073_000_000 * 10**6, 0, 793_100_000 * 10**6)
RATES = FeeRates(95, 30)
POOL_RATES = PoolRates(20, 5, 95)
T0 = 1_000_000
SOL = 10**9
BASE_FEE = 5_000
LAUNCH = LaunchRates([(T0 - 10, False, 95, 30), (T0 - 10, True, 95, 30)])
PRESETS = B.Presets(
    buy_sol=1.0, entry_delay_s=5, entry_delay_tests_s=[2, 5, 10], slippage_pct=20, quote_age_s=2,
    priority_fee_sol=0.0005, failed_tx_rate=0.05, failed_tx_rate_tests=[0.0, 0.05, 0.10],
    max_sell_attempts=4,
    exit_plans=[B.ExitPlan(name="double_or_half", take_profit_pct=100, stop_loss_pct=50,
                           time_stop_min=30),
                B.ExitPlan(name="hold_5_min", time_stop_min=5)],
)  # fmt: skip
DOUBLE, HOLD = PRESETS.exit_plans
# A 1 SOL buy on a new curve at 0.95% + 0.30%, by hand:
#   net = floor(1e9 * 10000 / 10125) = 987,654,320; fees ceil(net * 95 / 1e4) = 9,382,717
#   and ceil(net * 30 / 1e4) = 2,962,963; together exactly 1 SOL.
#   tokens = floor((net - 1) * 1.073e15 / (30e9 + net - 1)) = 34,199,203,120,618.
NET, TOKENS = 987_654_320, 34_199_203_120_618


class Book:
    """A coin's trades in order, each following the curve or pool math."""

    def __init__(self, *, mayhem: bool = False) -> None:
        self.mayhem = mayhem
        self.curve, self.pool, self.completed_ts = START, None, None
        # ts, quote, token, real_token, fee bps, creator bps, payer, real_quote
        self.c: list[tuple] = []
        self.p: list[tuple] = []  # ts, quote, token, lp bps, protocol bps, creator bps

    def buy(self, sol: float, ts: int, rates: FeeRates = RATES):
        return self._curve(buy_with_budget(self.curve, round(sol * SOL), rates), ts, rates)

    def sell(self, tokens: int, ts: int, rates: FeeRates = RATES):
        return self._curve(sell_tokens(self.curve, tokens, rates), ts, rates)

    def _curve(self, fill, ts: int, rates: FeeRates, payer: bool = True):
        self.curve = fill.after
        after = fill.after
        self.c.append((ts, after.virtual_quote, after.virtual_token, after.real_token,
                       rates.protocol_bps, rates.creator_bps, payer, after.real_quote))  # fmt: skip
        if fill.after.real_token == 0:
            self.completed_ts = ts
        return fill

    def agent(self, quote: int, token: int, ts: int) -> None:
        """A mayhem agent trade: free, and it leaves the reserves its program sets."""
        self.curve = Curve(quote, token, self.curve.real_quote, self.curve.real_token)
        self.c.append((ts, quote, token, self.curve.real_token, 0, 0, False,
                       self.curve.real_quote))  # fmt: skip

    def graduate(self, ts: int, pool: Pool | None = None) -> None:
        """Complete the curve at `ts`; with a pool, it trades from its first pool trade."""
        self.completed_ts = ts
        self.pool = pool

    def pool_buy(self, sol: float, ts: int, rates: PoolRates = POOL_RATES):
        return self._pool(pool_buy_with_budget(self.pool, round(sol * SOL), rates), ts, rates)

    def pool_sell(self, tokens: int, ts: int, rates: PoolRates = POOL_RATES):
        return self._pool(pool_sell_tokens(self.pool, tokens, rates), ts, rates)

    def _pool(self, fill, ts: int, rates: PoolRates):
        self.pool = fill.after
        self.p.append((ts, fill.after.quote, fill.after.token, rates.lp_bps,
                       rates.protocol_bps, rates.creator_bps))  # fmt: skip
        return fill

    def market(self) -> Market:
        c = np.array([r[:6] for r in self.c], dtype=np.int64).reshape(-1, 6)
        p = np.array(self.p, dtype=np.int64).reshape(-1, 6)
        return Market(
            mint="M", created_ts=T0, is_mayhem=self.mayhem, start=START,
            completed_ts=self.completed_ts, c_ts=c[:, 0], c_quote=c[:, 1], c_token=c[:, 2],
            c_real_token=c[:, 3], c_rates=c[:, 4:6],
            c_real_quote=np.array([r[7] for r in self.c], dtype=np.int64),
            c_payer=np.array([r[6] for r in self.c], dtype=bool), p_ts=p[:, 0],
            p_quote=p[:, 1], p_token=p[:, 2], p_rates=p[:, 3:6],
        )  # fmt: skip


def ledger(book: Book, offset: int = 30, delay: int = 5, presets: B.Presets = PRESETS) -> dict:
    """{plan name: ledger row} for one decision time and delay."""
    rows = B.simulate_signal(book.market(), offset, delay, presets, LAUNCH)
    assert [list(r) for r in rows] == [B.ledger_columns(presets)] * len(rows)
    return {r["plan"]: r for r in rows}


def pnl(row: dict, *, rate: float = 0.0, footprint: str = "none", fee_pct: float = 0.0,
        presets: B.Presets = PRESETS) -> dict:  # fmt: skip
    got = B.outcomes({k: [v] for k, v in row.items()}, presets, failed_rate=rate,
                     footprint=footprint, base_fee_lamports=BASE_FEE,
                     platform_fee_pct=fee_pct)  # fmt: skip
    return {k: v[0] for k, v in got.items()}


def after_fees(gross: int, rates=RATES) -> int:
    return gross - sum(-(-gross * bps // 10_000) for bps in (rates.protocol_bps,
                                                             rates.creator_bps))  # fmt: skip


# --- settings ----------------------------------------------------------------------------


def test_the_presets_file_loads_and_typos_are_refused(tmp_path):
    presets, sha = B.load_presets(ROOT / "config" / "padre_presets.yaml")
    assert presets.budget == SOL and presets.delays == [2, 5, 10] and len(sha) == 12
    assert presets.failed_rates == [0.0, 0.05, 0.10] and presets.platform_fee_pct == 1.0
    assert [p.name for p in presets.exit_plans] == ["double_or_half", "quick_50_25", "hold_5_min"]
    # The longest wait: a 10 s delay, a 30 min time stop, then five sells 10 s apart.
    assert presets.horizon_s == 10 + 1800 + 50
    bad = tmp_path / "presets.yaml"
    text = (ROOT / "config" / "padre_presets.yaml").read_text(encoding="utf-8")
    bad.write_text(text.replace("slippage_pct:", "slipage_pct:"), encoding="utf-8")
    with pytest.raises(ValidationError):
        B.load_presets(bad)


# --- fills and fees ----------------------------------------------------------------------


def test_a_buy_on_a_new_curve_matches_the_numbers_worked_out_by_hand():
    buy = B.simulate_buy(Book().market(), T0 + 35, PRESETS, LAUNCH)
    assert (buy.status, buy.venue, buy.ts) == (B.FILLED, "curve", T0 + 35)
    assert (buy.net, buy.tokens, buy.spent) == (NET, TOKENS, SOL)
    assert buy.spent - buy.net == 9_382_717 + 2_962_963  # pump.fun's two fees
    assert buy.price == NET / TOKENS and buy.fee_bps == 125 and not buy.capped


def test_a_coin_nobody_trades_costs_the_fees_and_shows_the_footprint_limit():
    row = ledger(Book())["hold_5_min"]
    assert (row["exit_reason"], row["trigger_ts"], row["sell_ts_1"]) == (
        B.TIME_STOP, T0 + 35 + 300, T0 + 35 + 300)  # fmt: skip
    # No trace: the tokens are sold from the starting reserves, which never held our SOL.
    gross = TOKENS * 30 * 10**9 // (1_073_000_000 * 10**6 + TOKENS)
    assert gross == 926_640_924
    assert row["sell_none_1"] == after_fees(gross) == 926_640_924 - 8_803_089 - 2_779_923
    # Our SOL stays: the sale gets back what the buy put in, to the lamport or two.
    quote = 30 * 10**9 + NET
    token = 30 * 10**9 * 1_073_000_000 * 10**6 // quote
    gross_stays = TOKENS * quote // (token + TOKENS)
    assert 0 <= NET - gross_stays <= 2
    assert row["sell_stays_1"] == after_fees(gross_stays)
    # Profit and loss: two transactions, each 0.0005 SOL priority fee + 5,000 lamports.
    none, stays = pnl(row), pnl(row, footprint="stays")
    assert none["pnl_sol"] == pytest.approx((915_057_912 - SOL) / SOL - 2 * 0.000505)
    assert none["pnl_sol"] == pytest.approx(-0.08595, abs=1e-5)
    assert stays["pnl_sol"] == pytest.approx(-0.02570, abs=1e-5)  # the true round trip
    assert none["sent"] == 2 and none["traded"] and none["sell_attempt"] == 1
    # Padre's fee, as a what-if: 1% of the SOL bought and of the SOL sold.
    assert pnl(row, fee_pct=1.0)["pnl_sol"] == pytest.approx(
        none["pnl_sol"] - 0.01 * (1 + 0.915057912))


def test_the_buy_uses_the_last_trade_at_or_before_its_second_and_that_coins_fees():
    book = Book()
    book.buy(2.0, T0 + 1, FeeRates(95, 0))  # a coin without a creator fee
    book.buy(0.5, T0 + 35)  # lands in the entry second: before our buy
    book.buy(3.0, T0 + 36)  # after it
    market = book.market()
    buy = B.simulate_buy(market, T0 + 35, PRESETS, LAUNCH)
    state = Curve(int(market.c_quote[1]), int(market.c_token[1]), 0, int(market.c_real_token[1]))
    want = buy_with_budget(state, SOL, RATES)  # the rates of the trade at T+35
    assert (buy.tokens, buy.net, buy.spent) == (want.tokens, want.curve_quote, SOL)
    early = B.simulate_buy(market, T0 + 34, PRESETS, LAUNCH)
    assert early.fee_bps == 95  # only the first trade's rates are known yet
    assert early.net == SOL * 10_000 // 10_095


def test_the_mayhem_agents_trade_sets_the_reserves_but_not_the_fees():
    book = Book(mayhem=True)
    book.buy(1.0, T0 + 2)
    book.agent(12 * 10**9, 900_000_000 * 10**6, T0 + 20)  # free, with a reserve reset
    buy = B.simulate_buy(book.market(), T0 + 35, PRESETS, LAUNCH)
    net = SOL * 10_000 // 10_125
    assert buy.net == net and buy.fee_bps == 125
    assert buy.tokens == (net - 1) * 900_000_000 * 10**6 // (12 * 10**9 + net - 1)
    only_agent = Book(mayhem=True)
    only_agent.agent(12 * 10**9, 900_000_000 * 10**6, T0 + 20)
    assert B.simulate_buy(only_agent.market(), T0 + 35, PRESETS, LAUNCH).fee_bps == 125
    # With no fee data at all there is no fill, rather than a guessed fee.
    assert B.simulate_buy(only_agent.market(), T0 + 35, PRESETS, None).status == B.NO_FEE_DATA


def test_a_coin_without_trades_pays_what_the_latest_first_trade_elsewhere_paid():
    rates = LaunchRates([(T0 + 10, False, 95, 30), (T0 + 34, False, 90, 25),
                         (T0 + 36, False, 50, 0), (T0 + 20, True, 0, 0)])  # fmt: skip
    assert rates(T0 + 35, False) == FeeRates(90, 25)  # nothing after the buy is read
    assert rates(T0 + 9, False) is None and rates(T0 + 35, True) == FeeRates(0, 0)
    assert B.simulate_buy(Book().market(), T0 + 35, PRESETS, rates).fee_bps == 115


def test_a_sale_on_the_curve_cannot_take_more_than_the_curve_holds():
    book = Book(mayhem=True)
    book.buy(2.0, T0 + 10)
    held = book.curve.real_quote  # about 1.975 SOL: what the first buyer put in
    # The mayhem program sets virtual reserves that price the coin 500 times higher.
    book.agent(15_000 * 10**9, 1_073_000_000 * 10**6, T0 + 100)
    row = ledger(book)["hold_5_min"]
    buy = B.simulate_buy(book.market(), T0 + 35, PRESETS, LAUNCH)
    unlimited = buy.tokens * 15_000 * 10**9 // (1_073_000_000 * 10**6 + buy.tokens)
    assert unlimited > 400 * SOL  # what the virtual reserves alone would promise
    assert row["sell_none_1"] == row["sell_stays_1"] == after_fees(held + buy.net)
    # An ordinary sale stays well inside what the curve holds: nothing changes there.
    plain = Book()
    plain.buy(2.0, T0 + 10)
    row, buy = ledger(plain)["hold_5_min"], B.simulate_buy(plain.market(), T0 + 35, PRESETS, LAUNCH)
    gross = buy.tokens * plain.curve.virtual_quote // (plain.curve.virtual_token + buy.tokens)
    assert row["sell_none_1"] == after_fees(gross) and gross < plain.curve.real_quote + buy.net


# --- exit plans --------------------------------------------------------------------------


def test_take_profit_sells_one_delay_after_the_trade_that_reached_it():
    book = Book()
    book.buy(1.0, T0 + 10)
    for i in range(8):
        book.buy(2.0, T0 + 100 + 10 * i)  # 16 SOL in: the price passes twice the entry price
    market = book.market()
    rows = ledger(book)
    row, buy = rows["double_or_half"], B.simulate_buy(market, T0 + 35, PRESETS, LAUNCH)
    # By hand: the price is (quote reserve)^2 / k, so twice the price paid needs the
    # reserve at sqrt(2) times its level around our buy: the seventh 2 SOL buy (T+160).
    hit = int(np.argmax(market.x_price >= 2 * buy.price))
    assert market.x_ts[hit] == T0 + 160 and market.x_price[hit - 1] < 2 * buy.price
    assert (row["exit_reason"], row["trigger_ts"], row["sell_ts_1"]) == (
        B.TAKE_PROFIT, T0 + 160, T0 + 165)  # fmt: skip
    # Sold on the reserves that trade left: the buy at T+170 comes after the sell.
    quote, token = int(market.c_quote[hit]), int(market.c_token[hit])
    assert row["sell_none_1"] == after_fees(buy.tokens * quote // (token + buy.tokens))
    assert pnl(row)["pnl_sol"] > 0.5
    # The plan without a take-profit holds for its five minutes.
    assert rows["hold_5_min"]["exit_reason"] == B.TIME_STOP
    assert rows["hold_5_min"]["sell_ts_1"] == T0 + 35 + 300


def test_stop_loss_and_time_stop():
    book = Book()
    first = book.buy(15.0, T0 + 10)  # the price is 2.2 times the start when we buy
    book.sell(first.tokens, T0 + 200)  # and falls back to the start: under half
    row = ledger(book)["double_or_half"]
    assert (row["exit_reason"], row["trigger_ts"], row["sell_ts_1"]) == (
        B.STOP_LOSS, T0 + 200, T0 + 205)  # fmt: skip
    assert pnl(row)["pnl_sol"] < -0.5
    quiet = Book()
    quiet.buy(3.0, T0 + 10)
    row = ledger(quiet)["double_or_half"]
    assert (row["exit_reason"], row["sell_ts_1"]) == (B.TIME_STOP, T0 + 35 + 1800)
    # A trade in the entry second is before the buy: it cannot trigger the exit.
    same_second = Book()
    first = same_second.buy(15.0, T0 + 10)
    same_second.sell(first.tokens, T0 + 35)
    assert ledger(same_second)["double_or_half"]["exit_reason"] == B.TIME_STOP


# --- slippage and failed transactions ------------------------------------------------------


def test_a_buy_fails_when_the_price_ran_more_than_the_slippage_limit():
    book = Book()
    book.buy(1.0, T0 + 10)
    book.buy(4.0, T0 + 34)  # between the quote (T+33) and the buy (T+35): about +27%
    row = ledger(book)["hold_5_min"]
    assert row["buy_status"] == B.SLIPPAGE and row["sell_ts_1"] is None
    got = pnl(row)
    assert got["pnl_sol"] == pytest.approx(-0.000505)  # it reached the chain: fees only
    assert not got["traded"] and got["sent"] == 1
    calm = Book()
    calm.buy(1.0, T0 + 10)
    calm.buy(4.0, T0 + 33)  # already in the quote
    assert ledger(calm)["hold_5_min"]["buy_status"] == B.FILLED


def test_a_sell_that_slips_is_tried_again_and_the_last_attempt_is_forced():
    book = Book()
    first = book.buy(20.0, T0 + 10)
    part = first.tokens // 3
    for i in range(3):  # a dump in three sales 5 s apart, each taking the price down 25%+
        book.sell(part, T0 + 35 + 300 - 1 + 5 * i)
    row = ledger(book)["hold_5_min"]
    slipped = [row[f"slipped_{k}"] for k in range(1, 6)]
    # Each of the first three sells lands a second after a sale its quote had not seen.
    assert slipped[:4] == [1, 1, 1, 0]
    k = slipped.index(0) + 1
    assert [row[f"sell_ts_{j}"] for j in range(1, k + 1)] == [
        T0 + 335 + 5 * j for j in range(k)]  # fmt: skip
    got = pnl(row)
    assert got["sell_attempt"] == k and got["sent"] == 1 + k
    assert got["pnl_sol"] == pytest.approx(
        (row[f"sell_none_{k}"] - SOL) / SOL - (1 + k) * 0.000505)
    one = PRESETS.model_copy(update={"max_sell_attempts": 1})
    row = ledger(book, presets=one)["hold_5_min"]
    assert row["slipped_1"] == 1 and row["slipped_2"] == 0 and "sell_ts_3" not in row
    assert pnl(row, presets=one)["sell_attempt"] == 2


def test_failed_transactions_are_fixed_numbers_and_higher_rates_fail_more():
    assert B.chances("M", 30, 5, "buy", n=3) == B.chances("M", 30, 5, "buy", n=3)
    assert B.chances("M", 30, 5, "buy", n=1) != B.chances("M", 60, 5, "buy", n=1)
    many = [x for i in range(2_000) for x in B.chances(i, n=1)]
    assert all(0 <= x < 1 for x in many) and 0.08 < np.mean(np.array(many) < 0.10) < 0.12
    row = ledger(Book())["hold_5_min"]
    # A second sell is only worked out when the first can fail at some tested rate.
    assert (row["sell_ts_2"] is None) == (row["chance_1"] >= 0.10)
    # A buy that fails by chance costs its fees and is not tried again.
    failed_buy = row | {"buy_chance": 0.03}
    assert pnl(failed_buy, rate=0.0)["traded"]
    got = pnl(failed_buy, rate=0.05)
    assert not got["traded"] and got["sent"] == 1 and got["pnl_sol"] == pytest.approx(-0.000505)
    # A sell that fails by chance is tried again one delay later.
    book = Book()
    book.buy(1.0, T0 + 10)
    retried = None
    for offset in range(30, 4_000):  # look for a case whose first sell fails at 10%
        candidate = B.simulate_signal(book.market(), offset, 5, PRESETS, LAUNCH)[1]
        if candidate["chance_1"] < 0.10 <= candidate["chance_2"] and candidate["buy_chance"] > 0.1:
            retried = candidate
            break
    assert retried is not None and retried["sell_ts_2"] == retried["sell_ts_1"] + 5
    low = 0.0 if retried["chance_1"] >= 0.05 else None
    assert pnl(retried, rate=0.10)["sell_attempt"] == 2 and pnl(retried, rate=0.10)["sent"] == 3
    assert pnl(retried, rate=0.0)["sell_attempt"] == 1
    if low is not None:
        assert pnl(retried, rate=0.05)["sell_attempt"] == 1  # 5% fails only what 10% fails
    with pytest.raises(ValueError):
        pnl(row, rate=0.2)  # the ledger was not worked out that far
    with pytest.raises(ValueError):
        pnl(row, footprint="half")


# --- graduation --------------------------------------------------------------------------


def graduated(pool_from: int = 400) -> Book:
    book = Book()
    book.buy(1.0, T0 + 10)
    book.graduate(T0 + 300, Pool(85 * 10**9, 206_900_000 * 10**6))
    book.pool_buy(0.5, T0 + pool_from)
    return book


def test_a_position_whose_coin_graduated_is_sold_in_the_pool():
    book = graduated()
    book.pool_buy(1.0, T0 + 500)
    market = book.market()
    row = ledger(book)["double_or_half"]
    buy = B.simulate_buy(market, T0 + 35, PRESETS, LAUNCH)
    # The pool trades far above the curve's early price: its first trade is the trigger.
    assert (row["exit_reason"], row["trigger_ts"], row["sell_ts_1"]) == (
        B.TAKE_PROFIT, T0 + 400, T0 + 405)  # fmt: skip
    pool = Pool(int(market.p_quote[0]), int(market.p_token[0]))  # after the trade at T+400
    want = pool_sell_tokens(pool, buy.tokens, POOL_RATES)
    assert row["sell_none_1"] == want.user_quote
    assert want.fees == sum(-(-want.pool_quote * bps // 10_000) for bps in (20, 5, 95))
    assert row["sell_stays_1"] > row["sell_none_1"]


def test_buying_a_graduated_coin_uses_the_pool_and_its_common_fees():
    book = graduated()
    book.pool_buy(0.2, T0 + 401, PoolRates(0, 5, 0))  # a fee-free wallet: not our rates
    book.pool_buy(0.2, T0 + 402, PoolRates(20, 5, 90))
    book.pool_buy(0.2, T0 + 403)
    market = book.market()
    assert market.pool_rates(0) == POOL_RATES and market.pool_rates(1) == POOL_RATES
    assert market.pool_rates(2) == PoolRates(20, 5, 90)  # a tie: the latest
    assert market.pool_rates(3) == POOL_RATES
    buy = B.simulate_buy(market, T0 + 405, PRESETS, LAUNCH)
    want = pool_buy_with_budget(Pool(int(market.p_quote[3]), int(market.p_token[3])), SOL,
                                POOL_RATES)  # fmt: skip
    assert (buy.venue, buy.tokens, buy.net, buy.spent) == (
        "pool", want.tokens, want.pool_quote, SOL)  # fmt: skip
    assert buy.fee_bps == 120 and buy.price == want.pool_quote / want.tokens


def test_between_the_curve_and_the_pool_nothing_can_be_bought_and_a_sell_waits():
    book = graduated(pool_from=2_000)
    market = book.market()
    assert B.simulate_buy(market, T0 + 305, PRESETS, LAUNCH).status == B.MIGRATING
    assert B.simulate_buy(market, T0 + 299, PRESETS, LAUNCH).status == B.FILLED
    row = ledger(book)["hold_5_min"]  # its sell is due at T+335: the pool trades at T+2000
    assert row["sell_ts_1"] == T0 + 2_000 and not row["stuck"] and row["sell_none_1"] > 0
    assert pnl(row)["sent"] == 2
    never = Book()
    never.buy(1.0, T0 + 10)
    never.graduate(T0 + 300)  # no pool trade in the data
    row = ledger(never)["hold_5_min"]
    assert row["stuck"] and row["sell_none_1"] == 0
    got = pnl(row, rate=0.10)
    assert got["pnl_sol"] == pytest.approx(-1.000505) and got["sent"] == 1


def test_a_buy_that_would_complete_the_curve_takes_what_is_left():
    book = Book()
    book.buy(85.5, T0 + 10)  # all the curve's tokens cost 85.005 SOL: about 0.56 SOL is left
    market = book.market()
    left = int(market.c_real_token[-1])
    assert 0 < left < TOKENS
    buy = B.simulate_buy(market, T0 + 35, PRESETS.model_copy(update={"slippage_pct": 99}), LAUNCH)
    assert buy.capped and buy.tokens == left and buy.spent < SOL
    done = Book()
    done.buy(90.0, T0 + 10)  # takes everything: the curve is complete
    assert done.market().completed_ts == T0 + 10
    assert B.simulate_buy(done.market(), T0 + 35, PRESETS, LAUNCH).status == B.MIGRATING


# --- no lookahead, and the same result twice -------------------------------------------------


def busy(extra_buy: tuple[float, int] | None = None) -> Book:
    """A coin with a few trades; `extra_buy` (SOL, time) is added in its place in time."""
    book = Book()
    steps = [(T0 + 10, "buy", 1.0), (T0 + 33, "buy", 0.3), (T0 + 50, "buy", 2.0),
             (T0 + 90, "sell", None), (T0 + 200, "buy", 0.7)]  # fmt: skip
    if extra_buy:
        steps.append((extra_buy[1], "buy", extra_buy[0]))
    first = None
    for ts, side, sol in sorted(steps, key=lambda step: step[0]):  # stable: ties keep order
        if side == "sell":
            book.sell(first.tokens, ts)
        else:
            fill = book.buy(sol, ts)
            first = first or fill
    return book


def test_no_lookahead_later_trades_change_nothing_earlier():
    base = busy()
    rows = ledger(base)
    # The buy at T+35 reads trades up to T+35: a trade at T+36 changes no part of it.
    later = busy((50.0, T0 + 36))
    buy_fields = ("buy_status", "venue", "tokens", "buy_net", "buy_spent", "entry_price",
                  "buy_fee_bps", "buy_chance", "capped")  # fmt: skip
    for plan in rows:
        assert {k: ledger(later)[plan][k] for k in buy_fields} == {
            k: rows[plan][k] for k in buy_fields}  # fmt: skip
    assert ledger(later)["double_or_half"]["exit_reason"] == B.TAKE_PROFIT  # it does matter later
    # The 5-minute plan sells at T+335 (a failed sell is tried again 5 s later): a trade
    # after its last sell changes nothing in its row.
    hold = rows["hold_5_min"]
    last_sell = max(hold[f"sell_ts_{k}"] for k in range(1, 6) if hold[f"sell_ts_{k}"])
    assert T0 + 335 <= last_sell <= T0 + 355
    after_sell = busy((50.0, last_sell + 1))
    assert ledger(after_sell)["hold_5_min"] == hold
    assert ledger(after_sell)["double_or_half"] != rows["double_or_half"]  # still holding
    # A trade in the sell's own second, or before it, does change what the sell gets.
    in_time = busy((50.0, T0 + 335))
    assert ledger(in_time)["hold_5_min"]["sell_none_1"] > rows["hold_5_min"]["sell_none_1"]
    # A trade before the buy changes the buy.
    before = busy((1.0, T0 + 34))
    assert ledger(before)["hold_5_min"]["tokens"] < rows["hold_5_min"]["tokens"]


def test_no_lookahead_the_market_at_a_second_ignores_everything_after_it():
    """Cutting the market's arrays at any second gives the same reserves and fees there as
    the whole market does."""
    book = graduated()
    book.c.insert(0, (T0 + 2, *book.c[0][1:]))
    book.pool_sell(10**12, T0 + 450)
    whole = book.market()
    for ts in range(T0, T0 + 600, 7):
        cut = Book()
        cut.c = [r for r in book.c if r[0] <= ts]
        cut.p = [r for r in book.p if r[0] <= ts]
        cut.completed_ts = book.completed_ts if book.completed_ts <= ts else None
        assert cut.market().spot(ts, LAUNCH) == whole.spot(ts, LAUNCH)


def test_delays_change_the_entry_and_every_plan_gets_a_row():
    book = busy()
    for delay in (2, 5, 10):
        rows = B.simulate_signal(book.market(), 30, delay, PRESETS, LAUNCH)
        assert [r["plan"] for r in rows] == ["double_or_half", "hold_5_min"]
        assert {r["entry_ts"] for r in rows} == {T0 + 30 + delay}
    assert ledger(book, delay=2)["hold_5_min"]["tokens"] > ledger(book)["hold_5_min"]["tokens"]


# --- from the backfill's files -------------------------------------------------------------


def write_backfill(root: Path, book: Book, mint: str = "M", day: str = "1970-01-12") -> None:
    """The book's trades as the backfill stores them (the columns the loader reads)."""
    agent = mayhem_agent_address()
    tokens = [{"mint": mint, "ts": T0, "signature": "s", "is_mayhem_mode": book.mayhem,
               "virtual_quote_reserves": START.virtual_quote,
               "virtual_token_reserves": START.virtual_token,
               "real_token_reserves": START.real_token}]  # fmt: skip
    trades = [{"mint": mint, "ts": ts, "slot": 10 + i, "bf_tx_index": 0, "event_index": 0,
               "virtual_quote_reserves": quote, "virtual_token_reserves": token,
               "real_token_reserves": real, "real_quote_reserves": real_quote,
               "fee_basis_points": fee,
               "creator_fee_basis_points": creator, "cashback_fee_basis_points": 0,
               "creator": "DEV", "wallet": "PERSON" if payer else agent, "ix_name": "buy",
               "mayhem_mode": book.mayhem, "quote_mint": NATIVE_SOL, "bf_fee": 5_000 + 77 * i,
               "bf_signatures": 1}
              for i, (ts, quote, token, real, fee, creator, payer, real_quote)
              in enumerate(book.c)]  # fmt: skip
    pool = [{"pool": "POOL", "ts": ts, "slot": 1_000 + i, "bf_tx_index": 0, "event_index": 0,
             "is_buy": True, "ix_name": "buy", "pool_base_token_reserves": token + 5,
             "pool_quote_token_reserves": quote - 10 - 7, "virtual_quote_reserves": "7",
             "base_amount_out": 5, "base_amount_in": None, "quote_amount_in": 9,
             "quote_amount_in_with_lp_fee": 10, "user_quote_amount_in": 12,
             "quote_amount_out": None, "quote_amount_out_without_lp_fee": None,
             "user_quote_amount_out": None, "lp_fee_basis_points": lp, "lp_fee": 1,
             "protocol_fee_basis_points": protocol, "protocol_fee": 1,
             "coin_creator_fee_basis_points": creator, "coin_creator_fee": 1,
             "cashback_fee_basis_points": 0, "cashback": 0}
            for i, (ts, quote, token, lp, protocol, creator) in enumerate(book.p)]  # fmt: skip
    tables = {"tokens": tokens, "trades": trades, "amm_trades": pool}
    if book.completed_ts is not None:
        tables["curve_completes"] = [{"mint": mint, "ts": book.completed_ts}]
        tables["migrations"] = [{"mint": mint, "pool": "POOL", "ts": book.completed_ts + 1}]
    for table, rows in tables.items():
        if rows:
            folder = root / "raw_backfill" / table / f"date={day}"
            folder.mkdir(parents=True, exist_ok=True)
            pq.write_table(pa.Table.from_pylist(rows), folder / "a.parquet")


def test_markets_read_from_the_backfill_match_and_give_the_same_ledger_twice(tmp_path):
    book = graduated()
    book.c.insert(1, (T0 + 20, 31 * 10**9, 1_040_000_000 * 10**6, START.real_token, 0, 0, False,
                      book.c[0][7]))  # fmt: skip
    book.pool_buy(1.0, T0 + 500)
    write_backfill(tmp_path, book)
    loaded = load_markets(tmp_path, [("M", T0)], horizon_s=3_000)
    got, want = loaded.markets["M"], book.market()
    for name in ("c_ts", "c_quote", "c_token", "c_real_token", "c_real_quote", "c_rates",
                 "c_payer", "p_ts",
                 "p_quote", "p_token", "p_rates", "x_ts", "x_price"):  # fmt: skip
        assert np.array_equal(getattr(got, name), getattr(want, name)), name
    assert (got.completed_ts, got.start, got.is_mayhem) == (T0 + 300, START, False)
    assert loaded.base_fee_lamports == 5_000  # the lowest fee per signature in the data
    assert loaded.launch_rates(T0 + 35, False) == RATES  # the coin's first paid trade
    assert loaded.launch_rates(T0 + 9, False) is None
    rows = [B.simulate_signal(got, 30, d, PRESETS, loaded.launch_rates) for d in (2, 5, 10)]
    again = load_markets(tmp_path, [("M", T0)], horizon_s=3_000)
    assert rows == [B.simulate_signal(again.markets["M"], 30, d, PRESETS, again.launch_rates)
                    for d in (2, 5, 10)]  # fmt: skip
    assert rows[1] == B.simulate_signal(want, 30, 5, PRESETS, LAUNCH)
    # Trades past the horizon are not read at all.
    short = load_markets(tmp_path, [("M", T0)], horizon_s=450).markets["M"]
    assert len(short.p_ts) == 1 and len(short.c_ts) == 2
    with pytest.raises(ValueError):
        load_markets(tmp_path, [("M", T0), ("NOT_THERE", T0)], horizon_s=450)


def test_a_coin_without_a_creator_pays_no_creator_fee(tmp_path):
    book = Book()
    book.buy(1.0, T0 + 10)
    write_backfill(tmp_path, book)
    path = tmp_path / "raw_backfill" / "trades" / "date=1970-01-12" / "a.parquet"
    rows = pq.read_table(path).to_pylist()
    pq.write_table(pa.Table.from_pylist([r | {"creator": NO_CREATOR} for r in rows]), path)
    market = load_markets(tmp_path, [("M", T0)], horizon_s=600).markets["M"]
    assert market.c_rates.tolist() == [[95, 0]]
