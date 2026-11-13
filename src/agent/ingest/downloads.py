"""Running total of data downloaded for the dataset, across all engine runs (and, from
Phase 3, the backfill).

    <data_dir>/run/downloads.jsonl   one line per finished run: {"kind", "run_started_at",
                                     "pid", "bytes", "estimated", "note"}

The engine reports its own run's bytes in status.json while it runs. When the next run
starts, it adds the previous run's final count to this ledger, so a stop, crash or
restart never loses it. Byte counts are the size of the messages received; the network
adds a few percent on top (connection overhead), and the public feed is not compressed.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

LEDGER = "downloads.jsonl"


def _ledger(run_dir: Path) -> Path:
    return run_dir / LEDGER


def read_ledger(run_dir: Path) -> list[dict]:
    try:
        lines = _ledger(run_dir).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [json.loads(line) for line in lines if line.strip()]


def append(run_dir: Path, entry: dict) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    with open(_ledger(run_dir), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({**entry, "recorded_at": time.time()}) + "\n")


def status_bytes(status: dict) -> int:
    """Bytes a run reported in its status file (older versions only had megabytes)."""
    if status.get("bytes_received") is not None:
        return int(status["bytes_received"])
    return int(float(status.get("mb_received") or 0) * 1e6)


def _in_ledger(entries: list[dict], status: dict) -> bool:
    return any(
        e.get("kind") == "live"
        and e.get("run_started_at") == status.get("started_at")
        and e.get("pid") == status.get("pid")
        for e in entries
    )


def record_previous_run(run_dir: Path, previous: dict | None) -> None:
    """Called when a run starts: add the previous run's final count, unless already there.
    After a crash the last heartbeat (at most a few seconds old) is used."""
    if not previous or previous.get("started_at") is None:
        return
    if _in_ledger(read_ledger(run_dir), previous):
        return
    append(
        run_dir,
        {
            "kind": "live",
            "run_started_at": previous["started_at"],
            "pid": previous.get("pid"),
            "bytes": status_bytes(previous),
            "estimated": False,
            "note": "",
        },
    )


def total_bytes(run_dir: Path, current: dict | None) -> int:
    """Everything downloaded so far: the ledger plus the current (or last) run if the
    ledger does not hold it yet."""
    entries = read_ledger(run_dir)
    total = sum(int(e.get("bytes") or 0) for e in entries)
    if current and current.get("started_at") is not None and not _in_ledger(entries, current):
        total += status_bytes(current)
    return total
