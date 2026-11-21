"""The three launch-context signals the models read, in their live form (spec 6, 8.8, 15.4).

The models were trained with three signals known at a coin's launch, computed in SQL over
the whole dataset (`agent.models.frame.launch_context`):
- `dev_launches_24h`: launches by the same creator in the 24 hours before this one,
- `launches_last_hour`: all launches in the hour before this one,
- `launch_hour_utc`: the hour of the day.

Here the same three are counted from launches as they arrive. `LaunchIndex.context` reads
only launches strictly before the coin's own (`ts < created_ts`), so a launch that is
added later, at or after that time, changes nothing (the no-lookahead rule, spec 6). A
test checks the counts against the training SQL.

The counts are only as complete as the history behind them. In training the history was
the unbroken backfill. Live it is what the recorder saw (minus the gaps in its gap log)
plus the backfill on disk, so `LaunchHistory` also says what share of the hour and of the
24 hours before the launch was covered. A count over a window that was not fully covered
is a lower bound.
"""

from __future__ import annotations

from bisect import bisect_left, insort
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from agent.features.token_state import CreatorIndex

DAY_S = 86_400
DAY_BACK_S, HOUR_BACK_S = 86_400, 3_600  # the same windows as agent.models.frame
CONTEXT_SIGNALS = ("dev_launches_24h", "launches_last_hour", "launch_hour_utc")
RAW_ROOTS = ("raw", "raw_backfill")  # the live recording and the backfill


class LaunchIndex(CreatorIndex):
    """Every launch seen, by creator (as `CreatorIndex`) and overall, in time order."""

    def __init__(self) -> None:
        super().__init__()
        self._all: list[int] = []

    def add(self, creator: str, ts: int, mint: str) -> None:
        if mint in self._seen:
            return
        super().add(creator, ts, mint)
        insort(self._all, ts)

    def __len__(self) -> int:
        return len(self._all)

    def context(self, creator: str, created_ts: int) -> dict:
        """The three signals for a coin launched by `creator` at `created_ts`, from
        launches with ts < created_ts only."""
        own = self._launches.get(creator, [])
        return {
            "dev_launches_24h": (
                bisect_left(own, created_ts) - bisect_left(own, created_ts - DAY_BACK_S)
            ),
            "launches_last_hour": (
                bisect_left(self._all, created_ts)
                - bisect_left(self._all, created_ts - HOUR_BACK_S)
            ),
            "launch_hour_utc": (created_ts % DAY_S) // 3600,
        }


@dataclass
class LaunchHistory:
    """The launches seen, and which stretches of chain time they are complete for.

    Covered time is the backfill's spans plus the recording: everything from the first
    recorded event on, except the gaps in the gap log. `gap_lists` are read each time, so
    a list that grows (the engine's gaps of this run) is followed.
    """

    launches: LaunchIndex
    recorded_from: float | None = None  # on-chain time of the first recorded event
    backfill: Sequence[tuple[float, float]] = ()
    gap_lists: Sequence[list[dict]] = field(default_factory=list)

    def context(self, creator: str, created_ts: int) -> dict:
        """The three signals plus `hour_covered` and `day_covered`: the share of the hour
        and of the 24 hours before the launch that the history covers."""
        out = self.launches.context(creator, created_ts)
        out["hour_covered"] = self.covered_share(created_ts - HOUR_BACK_S, created_ts)
        out["day_covered"] = self.covered_share(created_ts - DAY_BACK_S, created_ts)
        return out

    def covered_share(self, start: float, end: float) -> float:
        pieces = [(max(a, start), min(b, end)) for a, b in self.backfill if b > start and a < end]
        if self.recorded_from is not None and self.recorded_from < end:
            cursor = max(self.recorded_from, start)
            holes = sorted(
                (g["start_ts"], g["end_ts"])
                for gaps in self.gap_lists
                for g in gaps
                if g["end_ts"] > cursor and g["start_ts"] < end
            )
            for hole_start, hole_end in holes:
                if hole_start > cursor:
                    pieces.append((cursor, min(hole_start, end)))
                cursor = max(cursor, hole_end)
            if cursor < end:
                pieces.append((cursor, end))
        covered, reached = 0.0, start
        for a, b in sorted(pieces):  # overlapping pieces count once
            a = max(a, reached)
            if b > a:
                covered += b - a
                reached = b
        return round(covered / (end - start), 6)


def load_launch_index(data_dir: Path, *, roots: Iterable[str] = RAW_ROOTS,
                      before_ts: int | None = None) -> LaunchIndex:  # fmt: skip
    """Every launch on disk in `roots` (launched before `before_ts`, if given)."""
    import duckdb  # only needed at start-up and by the checks

    index = LaunchIndex()
    patterns = [
        f"{(data_dir / root / 'tokens').as_posix()}/*/*.parquet"
        for root in roots
        if any((data_dir / root / "tokens").glob("*/*.parquet"))
    ]
    if not patterns:
        return index
    where = "" if before_ts is None else f"WHERE ts < {int(before_ts)} "
    with duckdb.connect() as db:
        rows = db.execute(
            f"SELECT creator, ts, mint FROM read_parquet({patterns!r}, union_by_name=true) "
            f"{where}ORDER BY ts, mint"
        ).fetchall()
    for creator, ts, mint in rows:
        index.add(creator, ts, mint)
    return index


def first_recorded_ts(data_dir: Path) -> float | None:
    """On-chain time of the first event the recorder ever saved (its first trade)."""
    import duckdb

    if not any((data_dir / "raw" / "trades").glob("*/*.parquet")):
        return None
    pattern = f"{(data_dir / 'raw' / 'trades').as_posix()}/*/*.parquet"
    with duckdb.connect() as db:
        value = db.execute(
            f"SELECT min(ts) FROM read_parquet('{pattern}', union_by_name=true)"
        ).fetchall()[0][0]
    return None if value is None else float(value)
