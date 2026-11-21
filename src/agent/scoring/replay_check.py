"""Live model scores against a replay of the recorded data (spec 15.3, step L1).

The engine saves each verdict's scores in `live/verdicts/`. `check_scores` makes them
again from the Parquet files the recorder wrote: the token's replayed snapshot, the
launch context counted from the launches on disk, and the same scoring code. A live
score must equal the replayed one to the last digit. Nothing here reads an outcome.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from agent.features.replay import Replay
from agent.ingest.backfill_coverage import backfill_spans
from agent.ingest.gaps import read_gaps
from agent.scoring.launch_context import LaunchHistory, first_recorded_ts, load_launch_index
from agent.scoring.live import LiveScorer


@dataclass
class ScoreCheck:
    records: int = 0  # live verdicts that carry a score record
    scored: int = 0  # of those, with probabilities
    same: int = 0
    different: list[tuple[str, int, list[str]]] = field(default_factory=list)
    parts: Counter = field(default_factory=Counter)  # which part differs, how often
    other_model: int = 0  # made with another model bundle than the one on disk now
    gap_flag_differs: int = 0  # a gap logged after the verdict: not compared
    token_missing: int = 0
    reasons: Counter = field(default_factory=Counter)  # why verdicts were not scored
    first_at: float | None = None  # wall-clock time of the first and last compared verdict
    last_at: float | None = None

    @property
    def compared(self) -> int:
        return self.same + len(self.different)

    @property
    def hours(self) -> float:
        if self.first_at is None or self.last_at is None:
            return 0.0
        return (self.last_at - self.first_at) / 3600


def disk_history(data_dir: Path) -> LaunchHistory:
    """The launch history as the files on disk give it: what the engine had built up by
    the time it made any verdict that is on disk now."""
    return LaunchHistory(
        load_launch_index(data_dir),
        recorded_from=first_recorded_ts(data_dir),
        backfill=backfill_spans(data_dir),
        gap_lists=[read_gaps(data_dir / "gaps")],
    )


def _plain(value: dict) -> dict:
    """As it comes back from the saved JSON."""
    return json.loads(json.dumps(value))


def _reason(why: str) -> str:
    """A reason without its numbers, so reasons can be counted."""
    if why.startswith("only "):
        return "too little of the hour before its launch was recorded"
    return why.split(" (")[0]


def check_scores(verdicts: list[tuple], replay: Replay, scorer: LiveScorer,
                 history: LaunchHistory) -> ScoreCheck:  # fmt: skip
    """`verdicts`: (mint, cutoff ts, offset_s, has_gap, decided_at, scores JSON or None)
    of the live verdicts to check; `replay`: their tokens, read from the recording."""
    states = {s.mint: s for s in replay.states}
    out = ScoreCheck()
    for mint, ts, offset, has_gap, decided_at, saved in verdicts:
        if saved is None:
            continue
        live = json.loads(saved)
        out.records += 1
        if "p" in live:
            out.scored += 1
        else:
            out.reasons[_reason(live.get("why") or "?")] += 1
        if live.get("bundle") != scorer.bundle_sha or live.get("model") != scorer.name:
            out.other_model += 1
            continue
        state = states.get(mint)
        if state is None:
            out.token_missing += 1
            continue
        gap_now = replay.has_gap(state, ts)
        if gap_now != has_gap:
            out.gap_flag_differs += 1
            continue
        snapshot = state.snapshot(ts, creators=history.launches)
        replayed = _plain(scorer.score(snapshot, history.context(state.creator, state.created_ts),
                                       gap_now))  # fmt: skip
        out.first_at = decided_at if out.first_at is None else min(out.first_at, decided_at)
        out.last_at = decided_at if out.last_at is None else max(out.last_at, decided_at)
        if live == replayed:
            out.same += 1
            continue
        changed = sorted(k for k in set(live) | set(replayed) if live.get(k) != replayed.get(k))
        out.parts.update(changed)
        out.different.append((mint, offset, changed))
    return out
