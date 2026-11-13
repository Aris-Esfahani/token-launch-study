"""The gap log: every period when recording was off or unreliable (spec 3.2).

A gap is the hole between the last event safely recorded before a problem and the first
event recorded after it. Training and labeling must exclude or flag tokens that overlap a
gap. A gap never means "no tokens were launched"; it means "we were not watching".

    <data_dir>/gaps/gap-<time>-<n>.parquet     one small file per gap

Reasons: disconnect, paused, recorder_off (clean stop), crash (unclean stop, rows since
the last write were lost), stall (the PC slept or the engine froze), write_error, disk_full.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from agent.ingest.recorder import write_parquet_atomic

log = logging.getLogger(__name__)

GAP_SCHEMA = pa.schema(
    [
        pa.field("start_ts", pa.float64()),  # on-chain time of the last event before the gap
        pa.field("end_ts", pa.float64()),  # on-chain time of the first event after it
        pa.field("start_slot", pa.int64()),
        pa.field("end_slot", pa.int64()),
        pa.field("reason", pa.string()),
        pa.field("detail", pa.string()),
        pa.field("recorded_at", pa.float64()),  # wall-clock time the gap was written
        pa.field("source", pa.string()),
    ]
)


class GapLog:
    def __init__(self, folder: Path) -> None:
        self._folder = folder
        self._sequence = 0
        self._writer_id = uuid.uuid4().hex[:8]  # unique even for two logs in one process
        self.count = 0
        self.last: dict | None = None

    def append(self, gap: dict) -> None:
        row = {**gap, "recorded_at": time.time(), "source": "recorder"}
        self._sequence += 1
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        path = self._folder / f"gap-{stamp}-{self._writer_id}-{self._sequence:04d}.parquet"
        write_parquet_atomic(pa.Table.from_pylist([row], schema=GAP_SCHEMA), path)
        self.count += 1
        self.last = row
        log.warning(
            "gap logged: %s, %.0fs (%s)", row["reason"], row["end_ts"] - row["start_ts"],
            row["detail"] or "-",
        )  # fmt: skip


def read_gaps(folder: Path) -> list[dict]:
    files = sorted(folder.glob("gap-*.parquet")) if folder.is_dir() else []
    rows: list[dict] = []
    for file in files:
        rows.extend(pq.read_table(file).to_pylist())
    return sorted(rows, key=lambda g: g["start_ts"])


class GapTracker:
    """Decides when a gap opens and closes.

    Recording is trusted only while no "condition" is active (disconnected, paused, ...).
    A gap opens at the last trusted event when the first condition appears, and closes at
    the first event seen after the last condition clears.
    """

    def __init__(self, gap_log: GapLog, state_file: Path) -> None:
        self._log = gap_log
        self._state_file = state_file
        self._conditions: set[str] = set()
        self._open: dict | None = None
        self.last_ts: float | None = None  # last trusted event
        self.last_slot: int | None = None
        self.flushed_ts: float | None = None  # last event known to be on disk
        self.flushed_slot: int | None = None

    @property
    def active(self) -> bool:
        """True while events should be recorded."""
        return not self._conditions

    @property
    def open_reason(self) -> str | None:
        return self._open["reason"] if self._open else None

    def startup(self) -> None:
        """Open a gap covering the time since the previous run, if there was one."""
        try:
            previous = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return
        clean = previous.get("clean", False)
        ts = previous.get("last_ts") if clean else previous.get("flushed_ts")
        slot = previous.get("last_slot") if clean else previous.get("flushed_slot")
        if ts is None:
            return
        self._open = {
            "start_ts": ts,
            "start_slot": slot,
            "reason": "recorder_off" if clean else "crash",
            "detail": "" if clean else "engine did not shut down cleanly",
        }
        # Mark this run as "not stopped cleanly" right away, keeping the previous run's
        # last event on disk. If this run dies before its first write, the next start then
        # logs a crash gap from that point (it may overlap the gap logged above; read gaps
        # as a union).
        self.last_ts = self.flushed_ts = float(ts)
        self.last_slot = self.flushed_slot = slot
        self.save_state(clean=False)

    def begin(self, reason: str, detail: str = "", *, lost_buffer: bool = False) -> None:
        """A condition started. `lost_buffer`: rows not yet written were lost, so the gap
        starts at the last event on disk rather than the last event seen."""
        self._conditions.add(reason)
        self._ensure_open(reason, detail, lost_buffer)
        if lost_buffer:
            # Those events are gone, so the last trusted event is the last one on disk.
            self.last_ts, self.last_slot = self.flushed_ts, self.flushed_slot

    def has(self, reason: str) -> bool:
        return reason in self._conditions

    def clear(self, reason: str) -> None:
        self._conditions.discard(reason)

    def mark(self, reason: str, detail: str = "") -> None:
        """A one-off interruption (for example the PC slept): open a gap that closes at the
        next event, without holding recording back."""
        self._ensure_open(reason, detail, False)

    def _ensure_open(self, reason: str, detail: str, lost_buffer: bool) -> None:
        start_ts = self.flushed_ts if lost_buffer else self.last_ts
        start_slot = self.flushed_slot if lost_buffer else self.last_slot
        if self._open is None:
            self._open = {
                "start_ts": start_ts if start_ts is not None else time.time(),
                "start_slot": start_slot,
                "reason": reason,
                "detail": detail,
            }
            return
        if lost_buffer and start_ts is not None:
            self._open["start_ts"] = min(self._open["start_ts"], start_ts)
            if start_slot is not None:
                self._open["start_slot"] = min(self._open["start_slot"] or start_slot, start_slot)
        if reason != self._open["reason"]:
            note = f"then {reason}" + (f" ({detail})" if detail else "")
            if note not in self._open["detail"]:
                self._open["detail"] = "; ".join(p for p in (self._open["detail"], note) if p)

    def on_event(self, ts: float, slot: int) -> None:
        """Call for every event that is about to be recorded."""
        if self._conditions:
            return
        if self._open is not None:
            gap, self._open = self._open, None
            self._log.append({**gap, "end_ts": float(ts), "end_slot": slot})
        self.last_ts, self.last_slot = float(ts), slot

    def on_flush(self) -> None:
        """Everything seen so far is on disk."""
        self.flushed_ts, self.flushed_slot = self.last_ts, self.last_slot
        self.save_state(clean=False)

    def save_state(self, *, clean: bool) -> None:
        state = {
            "last_ts": self.last_ts,
            "last_slot": self.last_slot,
            "flushed_ts": self.flushed_ts,
            "flushed_slot": self.flushed_slot,
            "clean": clean,
            "updated_at": time.time(),
        }
        tmp = self._state_file.with_name(self._state_file.name + ".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, self._state_file)
