"""Live recording pipeline: notification -> drop failed/repeated -> parse -> Parquet.

Keeps the gap log honest: rows are only recorded while the feed is connected, recording
is not paused, and the disk is accepting writes. Used by the engine; has no network code,
so it can be tested offline.
"""

from __future__ import annotations

import logging
import shutil
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from agent.ingest.borsh_idl import Field
from agent.ingest.gaps import GapLog, GapTracker
from agent.ingest.parsers import ParsedTx, Parser, RawTx
from agent.ingest.recorder import ParquetSink
from agent.ingest.ws_client import LogNotification

log = logging.getLogger(__name__)

# Transactions whose logs the node cut short. Their events may be incomplete; the backfill
# (which reads full transactions) can repair them later.
TRUNCATED_TABLE = "truncated_txs"
_TRUNCATED_COLUMNS = [
    Field("signature", "text"),
    Field("slot", "i64"),
    Field("recv_ts", "f64"),
    Field("source", "text"),
]


# The signature of a transaction that was only simulated, never sent: 64 zero bytes,
# which print as 64 "1" characters. Public RPC nodes leak the logs of other people's
# simulations into the live feed (seen on 2026-11-12). They never happened on chain.
SIMULATED_SIGNATURE = "1" * 64


@dataclass
class PipelineStats:
    messages: int = 0
    bytes: int = 0
    failed: int = 0
    simulated: int = 0
    duplicates: int = 0
    events: int = 0
    truncated: int = 0
    unknown_events: int = 0
    stale_idl_events: int = 0
    decode_errors: int = 0
    rows_dropped: int = 0  # parsed but not recorded (paused, disconnected, disk problem)
    last_event_ts: int | None = None  # on-chain time of the newest event
    lag_s: float | None = None  # how far behind the chain the newest event arrived
    rows_seen: dict[str, int] = field(default_factory=dict)


class Pipeline:
    def __init__(
        self,
        data_dir: Path,
        run_dir: Path,
        *,
        parser: Parser | None = None,
        flush_interval_s: float = 60.0,
        min_free_gb: float = 5.0,
        dedup_size: int = 50_000,
    ) -> None:
        self.parser = parser or Parser()
        data_dir.mkdir(parents=True, exist_ok=True)
        run_dir.mkdir(parents=True, exist_ok=True)
        self._data_dir = data_dir
        self._min_free = min_free_gb * 1e9
        columns = {**self.parser.columns(), TRUNCATED_TABLE: _TRUNCATED_COLUMNS}
        self.sink = ParquetSink(data_dir / "raw", columns, flush_interval_s=flush_interval_s)
        self.gap_log = GapLog(data_dir / "gaps")
        self.gaps = GapTracker(self.gap_log, run_dir / "recorder_state.json")
        self.gaps.startup()
        self.stats = PipelineStats()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._dedup_size = dedup_size
        self.paused = False

    @property
    def recording(self) -> bool:
        return self.gaps.active

    def handle(self, n: LogNotification) -> ParsedTx | None:
        """Process one notification. Returns the parsed transaction, or None if it was a
        failed transaction or a repeat."""
        s = self.stats
        s.messages += 1
        s.bytes += n.size
        if n.err is not None:
            s.failed += 1
            return None
        if n.signature == SIMULATED_SIGNATURE:
            s.simulated += 1
            return None
        if n.signature in self._seen:
            s.duplicates += 1
            return None
        self._seen[n.signature] = None
        if len(self._seen) > self._dedup_size:
            self._seen.popitem(last=False)

        parsed = self.parser.parse(RawTx(n.signature, n.slot, n.logs, "recorder", n.recv_ts))
        s.events += parsed.events
        s.unknown_events += parsed.unknown_events
        s.stale_idl_events += parsed.stale_idl_events
        s.decode_errors += parsed.decode_errors
        ts = None
        for table, rows in parsed.rows.items():
            s.rows_seen[table] = s.rows_seen.get(table, 0) + len(rows)
            ts = ts if ts is not None else rows[0]["ts"]
        if ts is not None:
            s.last_event_ts = ts
            s.lag_s = round(n.recv_ts - ts, 1)
        if parsed.truncated:
            s.truncated += 1

        if not self.gaps.active:
            s.rows_dropped += sum(len(rows) for rows in parsed.rows.values())
            return parsed
        if ts is not None:
            self.gaps.on_event(ts, n.slot)
        for table, rows in parsed.rows.items():
            for row in rows:
                self.sink.add(table, row)
        if parsed.truncated:
            self.sink.add(
                TRUNCATED_TABLE,
                {"signature": n.signature, "slot": n.slot, "recv_ts": n.recv_ts,
                 "source": "recorder", "ts": ts},
            )  # fmt: skip
        return parsed

    def on_connection(self, event: str, detail: str = "") -> None:
        if event == "connected":
            self.gaps.clear("disconnect")
        else:
            self.gaps.begin("disconnect", detail)

    def set_paused(self, paused: bool) -> None:
        if paused == self.paused:
            return
        self.paused = paused
        if paused:
            self.gaps.begin("paused")
            log.info("recording paused")
        else:
            self.gaps.clear("paused")
            log.info("recording resumed")

    def note_stall(self, seconds: float) -> None:
        self.gaps.mark("stall", f"engine was suspended for about {seconds:.0f}s (PC asleep?)")

    def tick(self) -> None:
        """Write buffered rows when due. Call often; it does nothing most of the time."""
        if self.sink.due():
            self.flush()

    def flush(self) -> None:
        free = shutil.disk_usage(self._data_dir).free
        if free < self._min_free:
            detail = f"only {free / 1e9:.1f} GB free on the data drive"
            if not self.gaps.has("disk_full"):
                log.error("recording stopped: %s", detail)
            self._lose_buffer("disk_full", detail)
            return
        self.gaps.clear("disk_full")
        try:
            self.sink.flush()
        except OSError as exc:
            log.error("recording stopped: could not write data files (%s)", exc)
            self._lose_buffer("write_error", str(exc))
            return
        self.gaps.clear("write_error")
        self.gaps.on_flush()

    def _lose_buffer(self, reason: str, detail: str) -> None:
        self.stats.rows_dropped += self.sink.discard()
        self.gaps.begin(reason, detail, lost_buffer=True)
        self.sink.restart_timer()  # try again after the usual interval

    def close(self) -> None:
        """Final write on a clean shutdown."""
        self.flush()
        self.gaps.save_state(clean=True)
