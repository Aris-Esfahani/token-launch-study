"""Which stretches of chain time the backfill covers, from its progress files.

The labels (`agent.labels`) and the dataset's features (`agent.features.replay`) both use
this, so that a coin counts as covered in the same way for both. It is a fact about the
data on disk, not about any coin's future.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from agent.ingest.oldfaithful_backfill import read_progress

SPAN_JOIN_S = 60  # backfilled stretches this close together (e.g. two epochs) are one


def backfill_spans(data_dir: Path) -> list[tuple[int, int]]:
    """The stretches of chain time the backfill covers completely: runs of consecutive
    finished chunks in each epoch (a chunk not finished breaks a run), joined across
    epochs when they meet."""
    runs: list[tuple[int, int]] = []
    for path in sorted((data_dir / "backfill").glob("epoch-*/progress.jsonl")):
        p = read_progress(path)
        current: list[int] | None = None
        previous = None
        for cid in sorted(p.done):
            first, last = p.times.get(cid, (None, None))
            if previous is not None and cid != previous + 1 and current is not None:
                runs.append((current[0], current[1]))
                current = None
            previous = cid
            if first is None or last is None:
                continue  # a chunk with no pump.fun rows: it does not break the run
            if current is None:
                current = [first, last]
            else:
                current[1] = max(current[1], last)
        if current is not None:
            runs.append((current[0], current[1]))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(runs):
        if merged and start <= merged[-1][1] + SPAN_JOIN_S:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def covered(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    """True if one stretch covers all of [start, end]."""
    return any(a <= start and end <= b for a, b in spans)


def backfill_gaps(spans: list[tuple[int, int]]) -> list[dict]:
    """The time outside the stretches, as gap rows (`start_ts`, `end_ts`) like the
    recorder's gap log: before the first, between them, and after the last."""
    edges = [float("-inf")]
    for start, end in spans:
        edges += [start, end]
    edges.append(float("inf"))
    return [{"start_ts": edges[i], "end_ts": edges[i + 1]} for i in range(0, len(edges), 2)]


def backfill_epochs(data_dir: Path) -> list[int]:
    """Epochs with a backfill progress file."""
    epochs = []
    for path in (data_dir / "backfill").glob("epoch-*/progress.jsonl"):
        try:
            epochs.append(int(path.parent.name.removeprefix("epoch-")))
        except ValueError:
            continue
    return sorted(epochs)


def backfill_fingerprint(data_dir: Path) -> str:
    """A short hash of the backfill's files (names, sizes, write times). It changes when
    the backfill does, so what was made from it can tell it is out of date."""
    root = data_dir / "raw_backfill"
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.parquet")):
        st = path.stat()
        digest.update(f"{path.relative_to(root).as_posix()}|{st.st_size}|{st.st_mtime_ns};"
                      .encode())  # fmt: skip
    return digest.hexdigest()[:12]
