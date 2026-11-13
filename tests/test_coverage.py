"""Recorded time, gaps and the recording-burst progress shown by `agentd status`."""

import time
from datetime import datetime, timedelta

import pytest

from agent.cli.agentd import _bar, _duration, _when
from agent.ingest.coverage import (
    first_recorded_ts,
    last_recorded_ts,
    merged_length,
    raw_bytes,
    recorded_coverage,
    session_start,
)
from agent.ingest.gaps import GapLog
from agent.ingest.pipeline import Pipeline
from helpers import live

FIRST = 1794482629.0  # on-chain time of the pump-BuyV3 fixture trade
HOUR = 3600.0


@pytest.fixture
def data(tmp_path):
    """A data folder holding two recorded trades, the first at FIRST."""
    folder = tmp_path / "data"
    pipeline = Pipeline(folder, folder / "run")
    pipeline.handle(live("pump-BuyV3"))
    pipeline.handle(live("pump-BuyV2"))
    pipeline.close()
    return folder


def add_gap(folder, start, end, reason="disconnect"):
    GapLog(folder / "gaps").append(
        {"start_ts": start, "end_ts": end, "start_slot": 1, "end_slot": 2, "reason": reason,
         "detail": ""}  # fmt: skip
    )


def test_merged_length_counts_overlaps_once_and_clips_to_the_window():
    assert merged_length([], 0, 100) == 0
    assert merged_length([(10, 20), (15, 30), (40, 50)], 0, 100) == 30
    assert merged_length([(10, 20), (20, 25)], 0, 100) == 15
    assert merged_length([(-10, 5), (95, 120), (200, 300)], 0, 100) == 10


def test_first_and_last_recorded_times(data, tmp_path):
    assert first_recorded_ts(data / "raw") == FIRST
    assert last_recorded_ts(data / "run") == 1794482633.0
    assert first_recorded_ts(tmp_path / "nothing") is None
    assert last_recorded_ts(tmp_path / "nothing") is None


def test_coverage_subtracts_gaps(data):
    add_gap(data, FIRST + 100, FIRST + 160)
    add_gap(data, FIRST + 150, FIRST + 200, "crash")  # overlaps the first: counted once
    coverage = recorded_coverage(data, FIRST + 1000)
    assert coverage.span_seconds == 1000
    assert (coverage.gap_seconds, coverage.gap_count) == (100, 2)
    assert coverage.recorded_seconds == 900
    assert recorded_coverage(data, FIRST - 5) is None


def test_a_long_break_starts_a_new_burst_but_a_restart_does_not(data):
    add_gap(data, FIRST + 100, FIRST + 160, "recorder_off")  # a quick restart
    assert session_start(data, FIRST + 1000, break_s=HOUR) == FIRST
    add_gap(data, FIRST + 1000, FIRST + 1000 + 5 * HOUR, "recorder_off")  # off for 5 hours
    later = FIRST + 7 * HOUR
    start = session_start(data, later, break_s=HOUR)
    assert start == FIRST + 1000 + 5 * HOUR
    # Only the time since the burst began counts: 7 h after FIRST minus 1000 s minus 5 h.
    assert recorded_coverage(data, later, start).recorded_seconds == 2 * HOUR - 1000
    assert session_start(data.parent / "nothing", later, break_s=HOUR) is None


def test_bar():
    assert _bar(0) == "[" + "-" * 30 + "]"
    assert _bar(0.5) == "[" + "#" * 15 + "-" * 15 + "]"
    assert _bar(1.7) == "[" + "#" * 30 + "]"


def test_duration_and_when():
    assert [_duration(s) for s in (0, 45, 545, 11220, 90061)] == [
        "0 s", "45 s", "9 m 05 s", "3 h 07 m", "1 d 01 h",
    ]  # fmt: skip
    now = datetime.now()
    assert _when(now.timestamp()) == f"{now:%H:%M} today"
    assert _when((now - timedelta(days=1)).timestamp()).endswith("yesterday")
    assert _when((now + timedelta(days=1)).timestamp()).endswith("tomorrow")
    assert _when(time.mktime((2026, 11, 3, 9, 5, 0, 0, 0, -1))) == "Nov 3 09:05"


def test_history_progress_toward_the_target(data, monkeypatch, tmp_path):
    from agent.cli.agentd import _history
    from agent.config import load_config
    from helpers import make_project

    make_project(tmp_path)
    monkeypatch.delenv("DATA_DIR", raising=False)
    cfg = load_config(tmp_path)
    cfg = type(cfg)(cfg.root, cfg.settings, cfg.secrets, data)  # point at the test data
    assert cfg.settings.dataset.target_days == 28  # the default
    days_line, size_line = _history(cfg, FIRST + 7 * 86400)  # a week after the first event
    bar = "[" + "#" * 8 + "-" * 22 + "]"
    assert days_line == f"{bar} 25.0% of 28 days; 7.00 days done, 21.00 days to go"
    # Nothing counted as downloaded yet; the 21 missing days at 310 GB a day of backfill.
    kept = raw_bytes(data / "raw") / 1e9
    assert size_line == f"downloaded 0.0 GB of about 6,510 GB (0.00%); kept {kept:.2f} GB on disk"

