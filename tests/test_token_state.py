"""Live signals (spec 8) and the no-lookahead rule (spec 6): a snapshot at a cutoff may
only use events with ts <= cutoff, and must not depend on the order events arrived in."""

from __future__ import annotations

import random

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from agent.features.replay import load_states, outcomes, snapshots
from agent.features.token_state import (
    EARLY_SLOTS,
    NATIVE_SOL,
    SNAPSHOT_OFFSETS_S,
    CreatorIndex,
    TokenState,
)
from agent.ingest.gaps import GapLog
from agent.sim.curve_math import Curve, FeeRates, buy_with_budget, sell_tokens

AGENT = "MayhemAgent1111111111111111111111111111111"
SUPPLY = 10**15
START = Curve(30 * 10**9, 1_073_000_000 * 10**6, 0, 793_100_000 * 10**6)
RATES = FeeRates(95, 30)
T0, SLOT0 = 1_000_000, 500_000


def create_row(mint="M", **changes) -> dict:
    return {
        "mint": mint, "creator": "DEV", "ts": T0, "slot": SLOT0, "signature": f"c-{mint}",
        "event_index": 0, "token_total_supply": SUPPLY, "quote_mint": NATIVE_SOL,
        "is_mayhem_mode": False, "virtual_quote_reserves": START.virtual_quote,
        "virtual_token_reserves": START.virtual_token, "real_token_reserves": START.real_token,
        **changes,
    }  # fmt: skip


class Chain:
    """Trade rows whose reserves follow the curve math, like real events. Signatures
    sort in the opposite order of the trades, so code cannot rely on them for order."""

    def __init__(self, mint="M") -> None:
        self.mint, self.curve, self.n, self.rows = mint, START, 0, []

    def buy(self, wallet, sol, *, slot, ts) -> dict:
        return self._add(wallet, True, buy_with_budget(self.curve, int(sol * 1e9), RATES),
                         slot, ts)  # fmt: skip

    def sell(self, wallet, tokens, *, slot, ts) -> dict:
        return self._add(wallet, False, sell_tokens(self.curve, tokens, RATES), slot, ts)

    def _add(self, wallet, is_buy, fill, slot, ts) -> dict:
        self.curve, self.n = fill.after, self.n + 1
        c = fill.after
        row = {
            "mint": self.mint, "slot": slot, "ts": ts, "signature": f"s{10_000 - self.n:05d}",
            "event_index": 0, "wallet": wallet, "is_buy": is_buy,
            "quote_amount": fill.curve_quote, "token_amount": fill.tokens,
            "virtual_quote_reserves": c.virtual_quote, "virtual_token_reserves": c.virtual_token,
            "real_quote_reserves": c.real_quote, "real_token_reserves": c.real_token,
        }  # fmt: skip
        self.rows.append(row)
        return row


def launch() -> Chain:
    """A typical early life, in time order: dev buys at launch, two snipers, buyers, the
    dev sells half, more buyers, one buyer sells."""
    chain = Chain()
    dev = chain.buy("DEV", 1.0, slot=SLOT0, ts=T0)
    chain.buy("SNIPER1", 2.0, slot=SLOT0 + 1, ts=T0)
    chain.buy("SNIPER2", 0.5, slot=SLOT0 + EARLY_SLOTS, ts=T0 + 1)
    for i in range(5):
        chain.buy(f"BUYER{i}", 0.2 + i / 10, slot=SLOT0 + 20 + i * 40, ts=T0 + 5 + i * 9)
    chain.sell("DEV", dev["token_amount"] // 2, slot=SLOT0 + 200, ts=T0 + 45)
    for i in range(5, 12):
        chain.buy(f"BUYER{i}", 0.2 + i / 10, slot=SLOT0 + 220 + (i - 5) * 40,
                  ts=T0 + 50 + (i - 5) * 9)  # fmt: skip
    chain.sell("BUYER3", 10**12, slot=SLOT0 + 600, ts=T0 + 110)
    return chain


def keep_alive(chain: Chain) -> dict:
    """One trade after the token's first hour, so the recording is known to reach it."""
    late = Chain(chain.mint)
    late.curve = chain.curve
    row = late.buy("Z", 0.1, slot=SLOT0 + 90_000, ts=T0 + 3_700)
    return {**row, "signature": "keepalive", "source": "recorder"}


def state_with(rows, **create) -> TokenState:
    state = TokenState(create_row(**create), agent=AGENT)
    for row in rows:
        state.add_trade(row)
    return state


def test_signals_of_a_launch():
    chain = launch()
    dev_tokens = chain.rows[0]["token_amount"]
    upto = [r for r in chain.rows if r["ts"] <= T0 + 60]
    buys = [r for r in upto if r["is_buy"]]
    dev_sell = next(r for r in upto if not r["is_buy"])
    s = state_with(chain.rows).snapshot(T0 + 60)
    assert s["token_age_s"] == 60 and s["is_sol_paired"] and not s["completed"]
    assert (s["buys"], s["sells"], s["unique_buyers"]) == (len(buys), 1, len(buys)) == (10, 1, 10)
    assert s["dev_buy_pct"] == pytest.approx(dev_tokens * 100 / SUPPLY)
    assert s["dev_sold_pct"] == pytest.approx(50, abs=1e-6)
    assert s["dev_hold_pct"] == pytest.approx((dev_tokens - dev_tokens // 2) * 100 / SUPPLY)
    assert s["snipers"] == 2 and s["launch_slot_buyers"] == 0  # the dev is not a sniper
    assert s["sniper_hold_pct"] == pytest.approx(
        (chain.rows[1]["token_amount"] + chain.rows[2]["token_amount"]) * 100 / SUPPLY)
    assert s["holders"] == 10 and 0 < s["top1_hold_pct"] <= s["top10_hold_pct"] <= 100
    assert s["buy_sol"] == pytest.approx(sum(r["quote_amount"] for r in buys) / 1e9)
    assert s["sell_sol"] == pytest.approx(dev_sell["quote_amount"] / 1e9)
    assert s["curve_progress_pct"] > 0 and s["price_change_pct"] > 0
    last = Curve.after_trade(upto[-1])
    assert s["market_cap_sol"] == pytest.approx(last.market_cap(SUPPLY) / 1e9)
    assert s["largest_sell_pct"] == pytest.approx(dev_sell["token_amount"] * 100 / SUPPLY)
    assert s["buys_last_30s"] == sum(r["ts"] > T0 + 30 for r in buys)
    assert s["mayhem_agent_buy_share"] == 0 and s["dev_prior_launches"] is None


def test_nothing_after_the_cutoff_is_used():
    """The no-lookahead test (spec 6): adding any later event to the state changes no
    snapshot taken before it."""
    rng = random.Random(7)
    chain = launch()
    for i in range(300):  # a busy future: buys, sells and the curve completing
        ts, slot = T0 + 111 + i * 3, SLOT0 + 700 + i * 14
        if i % 3:
            chain.buy(f"LATE{i % 40}", rng.uniform(0.05, 3), slot=slot, ts=ts)
        else:
            chain.sell("SNIPER1", 10**10, slot=slot, ts=ts)
    future = [r for r in chain.rows if r["ts"] > T0 + 110]
    assert future
    for cutoff in [T0 + k for k in (0, 1, 30, 45, 60, 110)]:
        past = [r for r in chain.rows if r["ts"] <= cutoff]
        before = state_with(past).snapshot(cutoff)
        full = state_with(chain.rows)
        full.add_complete({"ts": T0 + 900})
        assert full.snapshot(cutoff) == before, cutoff


def test_arrival_order_and_repeats_do_not_matter():
    chain = launch()
    expected = state_with(chain.rows).snapshot(T0 + 300)
    rows = list(chain.rows) + chain.rows[:5]  # the live feed can repeat a transaction
    random.Random(1).shuffle(rows)
    assert state_with(rows).snapshot(T0 + 300) == expected


def test_last_trade_in_a_slot_is_found_by_chaining_reserves():
    """Several trades in the final slot: the curve state is the one after the trade that
    really came last, not the one whose signature sorts last."""
    chain = Chain()
    chain.buy("A", 1.0, slot=SLOT0, ts=T0)
    for i in range(4):
        chain.buy(f"B{i}", 0.3, slot=SLOT0 + 10, ts=T0 + 2)
    chain.sell("A", 10**12, slot=SLOT0 + 10, ts=T0 + 2)
    s = state_with(chain.rows).snapshot(T0 + 30)
    assert s["market_cap_sol"] == pytest.approx(chain.curve.market_cap(SUPPLY) / 1e9)


def test_mayhem_agent_trades_stay_in():
    chain = Chain()
    chain.buy(AGENT, 1.0, slot=SLOT0, ts=T0)
    chain.buy("X", 1.0, slot=SLOT0 + 1, ts=T0)
    s = state_with(chain.rows, is_mayhem_mode=True).snapshot(T0 + 30)
    assert s["is_mayhem"] and s["buys"] == 2 and s["holders"] == 2
    assert 0.4 < s["mayhem_agent_buy_share"] < 0.6 and s["mayhem_agent_hold_pct"] > 0
    assert s["snipers"] == 1 and s["launch_slot_buyers"] == 0  # the agent is neither


def test_coins_not_paired_with_sol():
    chain = launch()
    s = state_with(chain.rows, quote_mint="SomeOtherCoin11111111111111111111111111111").snapshot(
        T0 + 60)
    assert not s["is_sol_paired"]
    assert s["buy_sol"] is None and s["market_cap_sol"] is None and s["median_buy_sol"] is None
    assert s["dev_hold_pct"] > 0  # supply shares do not need SOL


def test_earlier_launches_by_the_same_dev():
    creators = CreatorIndex()
    for i, ts in enumerate([T0 - 500, T0 - 100, T0, T0 + 50]):
        creators.add("DEV", ts, f"m{i}")
    creators.add("DEV", T0 - 500, "m0")  # seen twice: counted once
    state = state_with(launch().rows)
    assert state.snapshot(T0 + 30, creators=creators)["dev_prior_launches"] == 2


def test_a_trade_of_another_token_is_refused():
    with pytest.raises(ValueError):
        state_with([{**launch().rows[0], "mint": "OTHER"}])


# --- replay from stored files ---------------------------------------------------------


def _write(folder, table, rows):
    path = folder / table / "date=2026-11-12" / "part-0.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)


def _record(tmp_path, chains_by_mint, *, extra_trades=()):
    tokens = [{**create_row(mint), "source": "recorder"} for mint in chains_by_mint]
    trades = [{**r, "source": "recorder"} for c in chains_by_mint.values() for r in c.rows]
    _write(tmp_path / "raw", "tokens", tokens)
    _write(tmp_path / "raw", "trades", trades + list(extra_trades))


def test_replay_matches_the_live_path(tmp_path):
    """Signals replayed from Parquet equal those of a state fed one event at a time, as
    the engine will feed it."""
    chain = launch()
    _record(tmp_path, {"M": chain}, extra_trades=[keep_alive(chain)])
    replay = load_states(tmp_path, T0 - 10, T0 + 10, after_s=3_600)
    rows = {r["offset_s"]: r for r in snapshots(replay)}
    assert set(rows) == set(SNAPSHOT_OFFSETS_S)
    live = TokenState(create_row(), agent=None)
    for row in chain.rows:
        live.add_trade(row)
    for offset, row in rows.items():
        expected = live.snapshot(T0 + offset, creators=replay.creators)
        assert {k: row[k] for k in expected} == expected
        assert row["has_gap"] is False
    assert outcomes(replay)["M"] == {"is_dead": True, "graduated_1h": False}


def test_gaps_are_flagged_and_long_ones_hide_outcomes(tmp_path):
    chain = launch()
    _record(tmp_path, {"M": chain}, extra_trades=[keep_alive(chain)])
    log = GapLog(tmp_path / "gaps")
    log.append({"start_ts": T0 + 40.0, "end_ts": T0 + 50.0, "start_slot": 0, "end_slot": 0,
                "reason": "disconnect", "detail": ""})  # fmt: skip
    replay = load_states(tmp_path, T0 - 10, T0 + 10, after_s=3_600)
    flags = {r["offset_s"]: r["has_gap"] for r in snapshots(replay)}
    assert flags == {30: False, 60: True, 120: True, 300: True}
    assert "M" in outcomes(replay)  # 10 s missing out of an hour: still known
    log.append({"start_ts": T0 + 1_000.0, "end_ts": T0 + 1_500.0, "start_slot": 0,
                "end_slot": 0, "reason": "disconnect", "detail": ""})  # fmt: skip
    assert "M" not in outcomes(load_states(tmp_path, T0 - 10, T0 + 10, after_s=3_600))


def test_snapshots_stop_where_the_recording_ends(tmp_path):
    chain = Chain()
    chain.buy("A", 1.0, slot=SLOT0, ts=T0)
    chain.buy("B", 1.0, slot=SLOT0 + 300, ts=T0 + 65)  # the newest recorded event
    _record(tmp_path, {"M": chain})
    replay = load_states(tmp_path, T0 - 10, T0 + 10)
    assert [r["offset_s"] for r in snapshots(replay)] == [30, 60]
    assert outcomes(replay) == {}
