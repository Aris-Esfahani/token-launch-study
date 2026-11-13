"""The engine's live verdicts (spec 15.0 step C): every new token is judged at each
cutoff once the recording has covered it, with the same snapshot a replay gives."""

from __future__ import annotations

import time

from agent.daemon.live_state import (
    SETTLE_S,
    LiveSignals,
    RulesFile,
    alert_line,
)
from agent.features.replay import load_states
from agent.features.token_state import SNAPSHOT_OFFSETS_S, CreatorIndex
from agent.ingest.pipeline import Pipeline
from agent.models.rules import parse_rules
from helpers import fixture_names
from helpers import live as live_tx
from test_token_state import AGENT, T0, create_row, launch, state_with

RULES = parse_rules({
    "avoid": [{"feature": "dev_sold_pct", "at_least": 50, "reason": "dev sold {value:.0f}%"}],
    "caution": [{"feature": "snipers", "at_least": 2, "reason": "{value} snipers"}],
})  # fmt: skip


def signals(gaps=None, **kwargs) -> LiveSignals:
    return LiveSignals(RULES, "test", CreatorIndex(), gaps if gaps is not None else [],
                       agent=AGENT, **kwargs)  # fmt: skip


def fed(chain=None, **kwargs) -> tuple[LiveSignals, list[dict]]:
    chain = chain or launch()
    live = signals(**kwargs)
    live.add("tokens", create_row())
    for row in chain.rows:
        live.add("trades", row)
    return live, chain.rows


def test_each_cutoff_is_judged_once_the_recording_has_passed_it():
    live, rows = fed()
    assert live.due(None) == []
    assert live.due(T0 + 30 + SETTLE_S - 1) == []  # events stamped T0+30 may still come
    (first,) = live.due(T0 + 30 + SETTLE_S, now=T0 + 40.0)
    assert (first["offset_s"], first["ts"], first["created_ts"]) == (30, T0 + 30, T0)
    assert first["wait_s"] == 10.0 and first["has_gap"] is False
    rest = live.due(T0 + 1_000)
    assert [v["offset_s"] for v in rest] == [60, 120, 300]
    assert live.due(T0 + 2_000) == []  # each cutoff once
    expected = state_with(rows)
    for verdict in [first, *rest]:
        assert verdict["snapshot"] == expected.snapshot(verdict["ts"], creators=live.creators)
    by_offset = {v["offset_s"]: v for v in [first, *rest]}
    assert by_offset[30]["level"] == "caution" and by_offset[30]["reasons"] == ["2 snipers"]
    assert by_offset[60]["level"] == "avoid"  # the dev sold half at T0+45
    assert by_offset[60]["reasons"] == ["dev sold 50%", "2 snipers"]
    assert dict(live.stats.verdicts) == {"caution": 1, "avoid": 3}


def test_trades_that_arrive_before_their_create_are_kept():
    # With two connections the create can come later than some of the token's trades.
    chain = launch()
    live = signals()
    for row in chain.rows[:3]:
        live.add("trades", row)
    live.add("tokens", create_row())
    for row in chain.rows[3:]:
        live.add("trades", row)
    for verdict in live.due(T0 + 1_000):
        assert verdict["snapshot"] == state_with(chain.rows).snapshot(
            verdict["ts"], creators=live.creators
        )


def test_held_trades_of_tokens_never_seen_are_let_go():
    live = signals()
    live.add("trades", {**launch().rows[0], "mint": "OLD"})  # created before the engine
    assert live.held_rows == 1
    live.due(T0 + 10)
    assert live.held_rows == 1
    live.due(T0 + 1_000)
    assert live.held_rows == 0


def test_trades_before_the_create_and_of_untracked_tokens_change_nothing():
    live, rows = fed()
    early = {**rows[1], "signature": "early", "ts": T0 - 5}
    live.add("trades", early)
    live.add("trades", {**rows[1], "mint": "OTHER", "signature": "other"})  # never created
    (verdict,) = live.due(T0 + 30 + SETTLE_S)
    assert verdict["snapshot"] == state_with(rows).snapshot(T0 + 30, creators=live.creators)


def test_a_gap_in_the_tokens_life_is_flagged_even_if_logged_after_it_was_tracked():
    gaps: list[dict] = []
    live, _ = fed(gaps=gaps)
    (v30,) = live.due(T0 + 32)
    gaps.append({"start_ts": T0 + 40.0, "end_ts": T0 + 50.0})  # the gap log grows
    flags = {v["offset_s"]: v["has_gap"] for v in live.due(T0 + 1_000)}
    assert v30["has_gap"] is False and flags == {60: True, 120: True, 300: True}
    assert live.stats.verdicts_with_gap == 3


def test_tokens_are_forgotten_after_the_keep_time():
    live, _ = fed(keep_s=400)
    live.due(T0 + 1_000)
    assert live.stats.tokens_tracked == 0 and live.stats.tokens_seen == 1


def test_prior_launches_count_the_creators_earlier_coins():
    live = signals()
    live.creators.add("DEV", T0 - 3_000, "OLD1")
    live.creators.add("DEV", T0 - 2_000, "OLD2")
    live.add("tokens", create_row())
    live.add("tokens", create_row())  # a repeated create changes nothing
    verdicts = live.due(T0 + 1_000)
    assert len(verdicts) == len(SNAPSHOT_OFFSETS_S)
    assert {v["snapshot"]["dev_prior_launches"] for v in verdicts} == {2}


def test_the_median_wait_and_last_alert():
    live, _ = fed()
    now = time.time()
    live.due(T0 + 1_000, now=now)
    assert live.median_wait_s(now=now) is not None
    assert live.stats.last_alert is None  # nothing passed


def test_the_alert_line_puts_the_mint_first():
    live, _ = fed()
    (verdict,) = live.due(T0 + 32, now=T0 + 47.0)
    line = alert_line({**verdict, "symbol": "STM"})
    clock, rest = line.split("  ", 1)
    assert len(clock) == 8 and rest.startswith("M  $STM  T+30s  CAUTION, 17s late  2 snipers")


def test_rules_are_read_again_when_the_file_changes(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(
        "caution:\n  - {feature: snipers, at_least: 2, reason: snipers}\n", encoding="utf-8"
    )
    rules = RulesFile(path)
    assert rules.load() and len(rules.rules) == 1 and rules.error is None
    first_sha = rules.sha
    assert not rules.load()  # unchanged
    path.write_text("caution:\n  - {feature: snipers}\n", encoding="utf-8")  # no test
    _touch_later(path)
    assert not rules.load() and len(rules.rules) == 1 and "keeping the previous" in rules.error
    path.write_text("avoid:\n  - {feature: snipers, at_least: 5, reason: x}\n", encoding="utf-8")
    _touch_later(path)
    assert rules.load() and rules.rules[0].level == "avoid" and rules.sha != first_sha


def _touch_later(path) -> None:
    import os

    stat = path.stat()
    os.utime(path, (stat.st_atime + 5, stat.st_mtime + 5))


def test_live_snapshots_equal_a_replay_of_what_was_recorded(tmp_path):
    """Real transactions through the recorder: the live verdicts' snapshots equal a
    replay of the Parquet files it wrote (spec 15.0 step C)."""
    data = tmp_path / "data"
    live = signals()
    pipeline = Pipeline(data, data / "run", on_row=live.add)
    for name in fixture_names():
        if name.startswith("pump-"):
            pipeline.handle(live_tx(name))
    pipeline.flush()
    verdicts = live.due(10**12)  # judge every cutoff now
    created = {v["mint"]: v["created_ts"] for v in verdicts}
    assert len(created) >= 1 and len(verdicts) == len(created) * len(SNAPSHOT_OFFSETS_S)
    replay = load_states(data, min(created.values()) - 1, max(created.values()) + 1)
    states = {state.mint: state for state in replay.states}
    assert set(states) == set(created)
    for verdict in verdicts:
        state = states[verdict["mint"]]
        assert verdict["snapshot"] == state.snapshot(verdict["ts"], creators=replay.creators)
    assert any(v["snapshot"]["trades"] >= 1 for v in verdicts)
