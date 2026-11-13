"""How much time the recording covers: from the first recorded event to now (or to the
last stop), minus the logged gaps. Overlapping gaps are counted once."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import duckdb

from agent.ingest.gaps import read_gaps

TABLES = ("tokens", "trades")


@dataclass(frozen=True)
class Coverage:
    start_ts: float  # on-chain time of the first recorded event
    end_ts: float
    gap_seconds: float
    gap_count: int

    @property
    def span_seconds(self) -> float:
        return max(self.end_ts - self.start_ts, 0.0)

    @property
    def recorded_seconds(self) -> float:
        return max(self.span_seconds - self.gap_seconds, 0.0)


def first_recorded_ts(raw: Path) -> float | None:
    """Time of the earliest recorded token or trade. Reads only the oldest day's files."""
    earliest: float | None = None
    for table in TABLES:
        for folder in sorted((raw / table).glob("date=*")):  # folder names sort by date
            if not any(folder.glob("*.parquet")):
                continue
            pattern = f"{folder.as_posix()}/*.parquet"
            with duckdb.connect() as db:
                value = db.execute(
                    f"SELECT min(ts) FROM read_parquet('{pattern}', union_by_name=true)"
                ).fetchone()[0]
            if value is not None:
                earliest = float(value) if earliest is None else min(earliest, float(value))
            break
    return earliest


def raw_bytes(raw: Path) -> int:
    """Size on disk of all recorded data files."""
    return sum(path.stat().st_size for path in raw.glob("*/date=*/*.parquet"))


def last_recorded_ts(run_dir: Path) -> float | None:
    """Time of the last event the recorder kept, from its state file."""
    try:
        state = json.loads((run_dir / "recorder_state.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    value = state.get("last_ts")
    return float(value) if value is not None else None


def merged_length(intervals: list[tuple[float, float]], low: float, high: float) -> float:
    """Total length of the intervals inside [low, high], counting overlaps once."""
    clipped = sorted((max(a, low), min(b, high)) for a, b in intervals if b > low and a < high)
    total = 0.0
    current: list[float] | None = None
    for start, end in clipped:
        if current is None or start > current[1]:
            if current is not None:
                total += current[1] - current[0]
            current = [start, end]
        else:
            current[1] = max(current[1], end)
    if current is not None:
        total += current[1] - current[0]
    return total


def session_start(data_dir: Path, end_ts: float, break_s: float) -> float | None:
    """Start of the current recording session: the end of the last logged gap at least
    `break_s` long (a real break between bursts), or else the first recorded event.
    Short interruptions such as a restart do not start a new session."""
    start = first_recorded_ts(data_dir / "raw")
    if start is None:
        return None
    for gap in read_gaps(data_dir / "gaps"):
        if gap["end_ts"] - gap["start_ts"] >= break_s and gap["end_ts"] <= end_ts:
            start = max(start, gap["end_ts"])
    return start


def recorded_coverage(
    data_dir: Path, end_ts: float, since: float | None = None
) -> Coverage | None:
    """Coverage from `since` (default: the first recorded event) to `end_ts`."""
    start = since if since is not None else first_recorded_ts(data_dir / "raw")
    if start is None or end_ts <= start:
        return None
    gaps = [
        g for g in read_gaps(data_dir / "gaps") if g["end_ts"] > start and g["start_ts"] < end_ts
    ]
    gap_seconds = merged_length([(g["start_ts"], g["end_ts"]) for g in gaps], start, end_ts)
    return Coverage(start, end_ts, gap_seconds, len(gaps))
