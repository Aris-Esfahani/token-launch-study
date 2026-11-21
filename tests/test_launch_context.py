"""The models' launch-context signals in their live form (spec 6, 15.3 step L1): counted
from launches strictly before the coin's own, the same numbers as the training SQL, and
honest about how much of the history behind them was recorded."""

from __future__ import annotations

import random

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from agent.daemon.live_state import LiveSignals
from agent.models.rules import parse_rules
from agent.scoring.launch_context import (
    CONTEXT_SIGNALS,
    DAY_BACK_S,
    HOUR_BACK_S,
    LaunchHistory,
    LaunchIndex,
    first_recorded_ts,
    load_launch_index,
)
from scoring_helpers import FakeScorer
from test_token_state import AGENT, T0, create_row, launch

RULES = parse_rules({"caution": [{"feature": "snipers", "at_least": 2, "reason": "x"}]})


def index_of(*launches: tuple[str, int]) -> LaunchIndex:
    index = LaunchIndex()
    for n, (creator, ts) in enumerate(launches):
        index.add(creator, ts, f"MINT{n}")
    return index


def test_counts_use_launches_strictly_before_inside_each_window():
    index = index_of(
        ("DEV", T0 - DAY_BACK_S - 1),  # a second too old for the 24 h window
        ("DEV", T0 - DAY_BACK_S),  # the oldest second that counts
        ("DEV", T0 - HOUR_BACK_S - 1),
        ("DEV", T0 - 1),
        ("DEV", T0),  # the same second as the coin: not before it
        ("DEV", T0 + 5),
        ("OTHER", T0 - HOUR_BACK_S),  # the oldest second of the hour window
        ("OTHER", T0 - HOUR_BACK_S - 1),
        ("OTHER", T0 - 10),
    )
    assert index.context("DEV", T0) == {
        "dev_launches_24h": 3,
        "launches_last_hour": 3,  # DEV at T0-1, OTHER at T0-3600 and T0-10
        "launch_hour_utc": (T0 % 86_400) // 3600,
    }
    assert index.context("NOBODY", T0)["dev_launches_24h"] == 0
    assert set(index.context("DEV", T0)) == set(CONTEXT_SIGNALS)
    assert index.launches_before("DEV", T0) == 4  # the CreatorIndex count still works


def test_a_launch_added_twice_counts_once_and_order_does_not_matter():
    rng = random.Random(3)
    launches = [(f"C{rng.randrange(6)}", T0 - rng.randrange(0, 2 * DAY_BACK_S)) for _ in range(300)]
    numbered = list(enumerate(launches))
    in_order, shuffled = LaunchIndex(), LaunchIndex()
    for n, (creator, ts) in numbered:
        in_order.add(creator, ts, f"M{n}")
    rng.shuffle(numbered)
    for n, (creator, ts) in numbered + numbered[:50]:  # some arrive twice
        shuffled.add(creator, ts, f"M{n}")
    assert len(shuffled) == len(in_order) == 300
    for creator in ("C0", "C3", "C5"):
        assert shuffled.context(creator, T0) == in_order.context(creator, T0)


def test_no_lookahead_launches_at_or_after_the_coins_own_change_nothing():
    """Spec 6 for the launch context: whatever is launched from the coin's own second on,
    by anyone, in any number, its three signals stay the same."""
    rng = random.Random(1)
    past = [(f"C{rng.randrange(4)}", T0 - rng.randrange(1, DAY_BACK_S)) for _ in range(200)]
    index = index_of(*past, ("DEV", T0 - 50), ("DEV", T0 - 7_000))
    before = index.context("DEV", T0)
    assert before["dev_launches_24h"] == 2
    for n in range(500):
        creator = "DEV" if n % 2 else f"C{n % 4}"
        index.add(creator, T0 + rng.randrange(0, 4_000), f"LATER{n}")
    assert index.context("DEV", T0) == before


def test_no_lookahead_in_the_engine_later_launches_never_reach_a_coins_scores():
    """The live form end to end: the context the engine hands the models for a coin is the
    same at every decision time and the same as if nothing had been launched after it,
    however many launches arrived before the verdicts were made."""

    def contexts(later_launches: int) -> list[dict]:
        history = LaunchHistory(LaunchIndex(), recorded_from=T0 - 2 * DAY_BACK_S)
        history.launches.add("DEV", T0 - 100, "OLD1")
        history.launches.add("SOMEONE", T0 - 900, "OLD2")
        scorer = FakeScorer()
        live = LiveSignals(RULES, "t", history.launches, [], agent=AGENT, scorer=scorer,
                           history=history)  # fmt: skip
        live.add("tokens", create_row())
        for row in launch().rows:
            live.add("trades", row)
        for n in range(later_launches):  # the same second, and every second after it
            live.add("tokens", create_row(f"LATER{n}", ts=T0 + n % 400,
                                          creator="DEV" if n % 3 else "SOMEONE"))  # fmt: skip
        verdicts = [v for v in live.due(T0 + 1_000) if v["mint"] == "M"]
        assert [v["offset_s"] for v in verdicts] == [30, 60, 120, 300]
        assert all(v["scores"]["context"] is c for v, (_, c, _) in
                   zip(verdicts, [c for c in scorer.calls if c[0]["mint"] == "M"], strict=True))
        return [v["scores"]["context"] for v in verdicts]

    quiet, busy = contexts(0), contexts(900)
    assert quiet == busy
    assert all(c == quiet[0] for c in quiet)
    assert quiet[0]["dev_launches_24h"] == 1 and quiet[0]["launches_last_hour"] == 2
    assert quiet[0]["hour_covered"] == 1.0 and quiet[0]["day_covered"] == 1.0


def test_the_live_counts_equal_the_training_sql():
    pytest.importorskip("pandas")  # agent.models.frame is research code
    import duckdb

    from agent.models.frame import launch_context

    rng = random.Random(7)
    rows = [(f"M{n}", f"C{rng.randrange(40)}", T0 + rng.randrange(0, 3 * DAY_BACK_S))
            for n in range(4_000)]  # fmt: skip
    index = LaunchIndex()
    for mint, creator, ts in rows:
        index.add(creator, ts, mint)
    with duckdb.connect() as db:
        db.execute("CREATE TABLE launches (mint VARCHAR, creator VARCHAR, created_ts BIGINT)")
        db.executemany("INSERT INTO launches VALUES (?, ?, ?)", rows)
        expected = {r[0]: r[1:] for r in db.execute(launch_context(db, "launches")).fetchall()}
    for mint, creator, ts in rows:
        got = index.context(creator, ts)
        assert tuple(got[name] for name in CONTEXT_SIGNALS) == expected[mint], mint


def test_covered_share_counts_the_backfill_and_the_recording_outside_its_gaps():
    hour = (T0 - HOUR_BACK_S, T0)
    assert LaunchHistory(LaunchIndex()).covered_share(*hour) == 0.0  # nothing recorded
    assert LaunchHistory(LaunchIndex(), recorded_from=T0 - 900).covered_share(*hour) == 0.25
    assert LaunchHistory(LaunchIndex(), recorded_from=T0 + 5).covered_share(*hour) == 0.0
    gaps = [{"start_ts": T0 - 1_800.0, "end_ts": T0 - 1_440.0},
            {"start_ts": T0 - 1_500.0, "end_ts": T0 - 1_440.0},  # overlaps: counted once
            {"start_ts": T0 - 9_000.0, "end_ts": T0 - 3_420.0}]  # reaches into the hour
    history = LaunchHistory(LaunchIndex(), recorded_from=T0 - 50_000, gap_lists=[gaps])
    assert history.covered_share(*hour) == 0.85  # 360 s + 180 s missed of 3,600
    # The backfill covers time the recorder missed; time both cover counts once.
    history.backfill = [(T0 - 4_000, T0 - 3_300), (T0 - 1_700, T0 - 1_600)]
    assert history.covered_share(*hour) == pytest.approx(1 - (360 - 100) / 3_600, abs=1e-6)
    # A gap logged later (the engine's list grows) is seen without rebuilding anything.
    this_run: list[dict] = []
    history = LaunchHistory(LaunchIndex(), recorded_from=T0 - 200_000, gap_lists=[[], this_run])
    assert history.context("DEV", T0)["hour_covered"] == 1.0
    this_run.append({"start_ts": T0 - 360.0, "end_ts": T0 - 180.0})
    context = history.context("DEV", T0)
    assert context["hour_covered"] == 0.95
    assert context["day_covered"] == round(1 - 180 / 86_400, 6)


def test_the_launches_on_disk_are_read_from_the_recording_and_the_backfill(tmp_path):
    def write(root: str, table: str, rows: list[dict]) -> None:
        folder = tmp_path / root / table / "date=2026-11-12"
        folder.mkdir(parents=True)
        pq.write_table(pa.Table.from_pylist(rows), folder / "part-1.parquet")

    assert len(load_launch_index(tmp_path)) == 0 and first_recorded_ts(tmp_path) is None
    write("raw", "tokens", [{"creator": "DEV", "ts": T0 - 10, "mint": "A"},
                            {"creator": "DEV", "ts": T0 - 20, "mint": "B"}])  # fmt: skip
    write("raw_backfill", "tokens", [{"creator": "DEV", "ts": T0 - 20, "mint": "B"},
                                     {"creator": "X", "ts": T0 - 5_000, "mint": "C"}])  # fmt: skip
    write("raw", "trades", [{"mint": "A", "ts": T0 - 9}, {"mint": "B", "ts": T0 - 15}])
    index = load_launch_index(tmp_path)
    assert len(index) == 3  # B is in both and counts once
    assert index.context("DEV", T0)["dev_launches_24h"] == 2
    assert len(load_launch_index(tmp_path, roots=("raw_backfill",))) == 2
    assert len(load_launch_index(tmp_path, before_ts=T0 - 15)) == 2
    assert first_recorded_ts(tmp_path) == T0 - 15
