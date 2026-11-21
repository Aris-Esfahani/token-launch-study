"""Backfill pump.fun history from Old Faithful, one epoch at a time (spec 4.1, Phase 3).

How a run works:

1. Read the epoch's slot index (about 5 MB, kept in `data/backfill/epoch-<E>/`).
2. Split the epoch into chunks of consecutive slots (64 by default, about 60 MB).
3. Worker processes each download one chunk with an HTTP range request, decode it in
   memory and keep only rows from pump.fun and PumpSwap transactions (see
   oldfaithful_extract.py). Nothing else is written to disk.
4. Every few minutes the rows of finished chunks are written to
   `data/raw_backfill/<table>/date=<UTC day>/e<E>-*.parquet`, and the chunks are marked
   done in `data/backfill/epoch-<E>/progress.jsonl` (one line per write, after the files
   are complete).

A stopped run (Ctrl+C, PC restart, network loss) continues where it left off: chunks
marked done are skipped. A chunk that keeps failing after several tries is left out and
written to the backfill gap log (`data/backfill/gaps/epoch-<E>.parquet`); the next run
tries it again. Files written after the last mark (a crash in between) are moved to
`data/raw_backfill/_orphans/` on the next start, never read and never deleted.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import time
from collections import Counter, deque
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pyarrow as pa

from agent.ingest import downloads
from agent.ingest.car import CarError, SlotIndex, SlotRange
from agent.ingest.gaps import GAP_SCHEMA
from agent.ingest.oldfaithful_extract import Extractor, backfill_columns
from agent.ingest.parsers import Parser
from agent.ingest.recorder import ParquetSink, write_parquet_atomic

ROWS_FOLDER = "raw_backfill"
ORPHANS = "_orphans"
ATTEMPTS = 6


class BackfillError(RuntimeError):
    pass


# Answers that mean "try again later" (rate limits, server trouble), not "this will never
# work". The archive sent a 429 once while we were waiting for an epoch to be published.
TEMPORARY_HTTP = frozenset({408, 425, 429, 500, 502, 503, 504})


def temporary_problem(exc: Exception) -> str | None:
    """A short reason if `exc` may clear up by itself (rate limit, server or network
    trouble); None if it will not."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in TEMPORARY_HTTP:
        return f"the archive answered HTTP {exc.response.status_code} (temporary)"
    if isinstance(exc, httpx.TransportError):  # timeouts, dropped connections
        return f"could not reach the archive ({type(exc).__name__})"
    return None


@dataclass(frozen=True)
class Chunk:
    id: int
    first_slot: int
    last_slot: int
    ranges: tuple[SlotRange, ...]  # slots with a block

    @property
    def start(self) -> int:
        return self.ranges[0].offset

    @property
    def end(self) -> int:
        return self.ranges[-1].offset + self.ranges[-1].length

    @property
    def size(self) -> int:
        return self.end - self.start if self.ranges else 0


def plan_chunks(index: SlotIndex, chunk_slots: int) -> list[Chunk]:
    """Every chunk of the epoch that has at least one block. Chunk ids are fixed by slot
    number (chunk k starts at first_slot + k * chunk_slots), so they are the same in every
    run."""
    chunks = []
    for k in range((index.slots + chunk_slots - 1) // chunk_slots):
        first = index.first_slot + k * chunk_slots
        last = min(first + chunk_slots - 1, index.last_slot)
        ranges = index.ranges(first, last)
        if not ranges:
            continue
        for a, b in zip(ranges, ranges[1:], strict=False):
            if b.offset != a.offset + a.length:
                raise CarError(f"slots {a.slot} and {b.slot} are not next to each other "
                               "in the archive file")  # fmt: skip
        chunks.append(Chunk(k, first, last, tuple(ranges)))
    return chunks


# --- worker processes --------------------------------------------------------------------


@dataclass
class ChunkResult:
    chunk_id: int
    ok: bool
    error: str = ""
    rows: dict[str, list[dict]] = field(default_factory=dict)
    stats: Counter = field(default_factory=Counter)
    problems: list[str] = field(default_factory=list)
    bytes: int = 0  # downloaded, including failed tries
    download_s: float = 0.0
    cpu_s: float = 0.0
    attempts: int = 0
    min_block_time: int | None = None
    max_block_time: int | None = None


_worker: dict = {}


def _init_worker(url: str, bytes_per_s: float, verify: bool) -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # the main process handles Ctrl+C
    _worker["url"] = url
    _worker["rate"] = bytes_per_s
    _worker["verify"] = verify
    _worker["client"] = httpx.Client(timeout=httpx.Timeout(60.0), follow_redirects=True)
    _worker["extractor"] = Extractor(Parser())


def fetch_range(client: httpx.Client, url: str, start: int, end: int,
                bytes_per_s: float = 0.0) -> bytes:
    """Bytes [start, end) of the file. Refuses anything but the exact range: a server that
    ignored the range header would otherwise send the whole archive."""
    headers = {"Range": f"bytes={start}-{end - 1}"}
    buf = bytearray()
    began = time.monotonic()
    with client.stream("GET", url, headers=headers) as response:
        if response.status_code != 206:
            raise BackfillError(f"HTTP {response.status_code} for a range request")
        for piece in response.iter_bytes(1 << 20):
            buf += piece
            if len(buf) > end - start:
                raise BackfillError("the server sent more than the requested range")
            if bytes_per_s:
                ahead = len(buf) / bytes_per_s - (time.monotonic() - began)
                if ahead > 0:
                    time.sleep(ahead)
    if len(buf) != end - start:
        raise BackfillError(f"got {len(buf)} of {end - start} bytes")
    return bytes(buf)


def run_chunk(chunk: Chunk) -> ChunkResult:
    """Download and decode one chunk (runs in a worker process)."""
    return process_chunk(
        chunk, _worker["client"], _worker["url"], _worker["extractor"],
        bytes_per_s=_worker["rate"], verify=_worker["verify"],
    )  # fmt: skip


def process_chunk(chunk: Chunk, client: httpx.Client, url: str, extractor: Extractor, *,
                  bytes_per_s: float = 0.0, verify: bool = True,
                  sleep: Callable[[float], None] | None = None) -> ChunkResult:
    sleep = sleep or time.sleep
    result = ChunkResult(chunk.id, ok=False)
    for attempt in range(1, ATTEMPTS + 1):
        result.attempts = attempt
        began = time.monotonic()
        try:
            data = fetch_range(client, url, chunk.start, chunk.end, bytes_per_s)
            result.bytes += len(data)
            result.download_s += time.monotonic() - began
            cpu = time.process_time()
            out = extractor.extract(data, chunk.start, {r.slot for r in chunk.ranges},
                                    verify=verify)  # fmt: skip
            result.cpu_s += time.process_time() - cpu
        except (httpx.HTTPError, BackfillError, CarError) as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            if attempt < ATTEMPTS:
                sleep(min(2.0**attempt, 60.0))
            continue
        result.ok = True
        result.error = ""
        result.rows, result.stats, result.problems = out.rows, out.stats, out.problems
        result.min_block_time, result.max_block_time = out.min_block_time, out.max_block_time
        return result
    return result


# --- the run -----------------------------------------------------------------------------


@dataclass
class Progress:
    """What earlier runs finished, read from progress.jsonl."""

    done: set[int] = field(default_factory=set)
    failed: dict[int, str] = field(default_factory=dict)  # failed and not done since
    files: set[str] = field(default_factory=set)
    times: dict[int, tuple[int | None, int | None]] = field(default_factory=dict)
    lines: list[dict] = field(default_factory=list)


def read_progress(path: Path) -> Progress:
    p = Progress()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return p
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # a line cut off by a crash: its chunks are simply done again
        p.lines.append(entry)
        if entry.get("kind") != "commit":
            continue
        for cid in entry.get("chunks", []):
            p.done.add(cid)
            p.failed.pop(cid, None)
        for cid, error in entry.get("failed", {}).items():
            if int(cid) not in p.done:
                p.failed[int(cid)] = error
        p.files.update(entry.get("files", []))
        for cid, span in entry.get("times", {}).items():
            p.times[int(cid)] = tuple(span)
    return p


@dataclass
class RunTotals:
    started: float = field(default_factory=time.time)
    chunks_done: int = 0
    chunks_failed: int = 0
    bytes: int = 0
    download_s: float = 0.0
    cpu_s: float = 0.0
    stats: Counter = field(default_factory=Counter)
    rows: Counter = field(default_factory=Counter)
    problems: list[str] = field(default_factory=list)
    stopped: bool = False


class Backfill:
    def __init__(self, data_dir: Path, epoch: int, *, base_url: str, workers: int = 6,
                 max_mb_s: float = 0.0, chunk_slots: int = 64, commit_chunks: int = 64,
                 commit_every_s: float = 180.0, min_free_gb: float = 5.0,
                 verify: bool = True, echo: Callable[[str], None] = print) -> None:
        self.data_dir = data_dir
        self.epoch = epoch
        self.base_url = base_url.rstrip("/")
        self.workers = max(1, workers)
        self.max_mb_s = max_mb_s
        self.chunk_slots = chunk_slots
        self.commit_chunks = commit_chunks
        self.commit_every_s = commit_every_s
        self.min_free = min_free_gb * 1e9
        self.verify = verify
        self.echo = echo
        self.folder = data_dir / "backfill" / f"epoch-{epoch}"
        self.rows_root = data_dir / ROWS_FOLDER
        self.progress_path = self.folder / "progress.jsonl"
        self.gaps_path = data_dir / "backfill" / "gaps" / f"epoch-{epoch}.parquet"
        self.prefix = f"e{epoch}"
        self.index: SlotIndex | None = None

    # where things are
    @property
    def car_url(self) -> str:
        return f"{self.base_url}/{self.epoch}/epoch-{self.epoch}.car"

    @property
    def index_url(self) -> str:
        return f"{self.base_url}/{self.epoch}/epoch-{self.epoch}-slot-ranges.raw"

    def load_index(self, client: httpx.Client) -> SlotIndex:
        """The slot index, downloaded once and kept."""
        path = self.folder / "slot-ranges.raw"
        if not path.exists():
            response = client.get(self.index_url)
            if response.status_code == 404:
                raise BackfillError(f"epoch {self.epoch} is not in the archive (yet)")
            response.raise_for_status()
            self.folder.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(response.content)
            os.replace(tmp, path)
        self.index = SlotIndex(self.epoch, path.read_bytes())
        return self.index

    def car_problem(self, client: httpx.Client, index: SlotIndex) -> str | None:
        """Why the epoch's archive file cannot be read yet (None if it can): it must exist
        and reach the end of the last slot in the index (the index can appear first)."""
        response = client.head(self.car_url)
        if response.status_code == 404:
            return "not in the archive yet"
        response.raise_for_status()
        size = int(response.headers.get("content-length") or 0)
        chunks = plan_chunks(index, self.chunk_slots)
        needed = chunks[-1].end if chunks else 0
        if size < needed:
            return f"still being uploaded ({size / 1e9:,.1f} of {needed / 1e9:,.1f} GB)"
        return None

    def progress(self) -> Progress:
        return read_progress(self.progress_path)

    def pending(self, index: SlotIndex, from_slot: int | None = None,
                to_slot: int | None = None) -> list[Chunk]:
        """Chunks still to do, limited to those overlapping [from_slot, to_slot]."""
        done = self.progress().done
        low = index.first_slot if from_slot is None else from_slot
        high = index.last_slot if to_slot is None else to_slot
        return [
            c for c in plan_chunks(index, self.chunk_slots)
            if c.id not in done and c.last_slot >= low and c.first_slot <= high
        ]  # fmt: skip

    def set_aside_orphans(self) -> int:
        """Move data files of this epoch that no progress line lists (written just before
        a crash) out of the way. Returns how many were moved."""
        listed = self.progress().files
        moved = 0
        for path in self.rows_root.glob(f"*/date=*/{self.prefix}-*.parquet"):
            relative = path.relative_to(self.rows_root).as_posix()
            if relative in listed:
                continue
            target = self.rows_root / ORPHANS / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, target)
            moved += 1
        return moved

    # running
    def run(self, chunks: list[Chunk], *, pool_factory=None) -> RunTotals:
        """Process the chunks. Ctrl+C stops after the chunks already started, writes what is
        finished and returns; the next run continues."""
        totals = RunTotals()
        if not chunks:
            return totals
        moved = self.set_aside_orphans()
        if moved:
            self.echo(f"Moved {moved} unlisted file(s) from an interrupted run to {ORPHANS}/.")
        parser = Parser()
        sink = ParquetSink(self.rows_root, backfill_columns(parser),
                           flush_interval_s=1e9, max_buffered_rows=10**9, prefix=self.prefix)
        rate = self.max_mb_s * 1e6 / self.workers if self.max_mb_s else 0.0
        factory = pool_factory or (lambda: ProcessPoolExecutor(
            max_workers=self.workers, initializer=_init_worker,
            initargs=(self.car_url, rate, self.verify),
        ))  # fmt: skip
        total_bytes = sum(c.size for c in chunks)
        batch: list[ChunkResult] = []
        last_commit = last_report = time.monotonic()
        queue = deque(chunks)
        inflight: dict[Future, Chunk] = {}

        def commit() -> None:
            nonlocal batch, last_commit
            last_commit = time.monotonic()
            if not batch:
                return
            self._commit(batch, sink)
            batch = []

        def collect(future: Future, chunk: Chunk) -> None:
            try:
                result = future.result()
            except Exception as exc:  # a bug in a worker: count the chunk as failed
                result = ChunkResult(chunk.id, ok=False, error=f"{type(exc).__name__}: {exc}")
            totals.bytes += result.bytes
            totals.download_s += result.download_s
            totals.cpu_s += result.cpu_s
            if result.ok:
                totals.chunks_done += 1
                totals.stats.update(result.stats)
                for table, rows in result.rows.items():
                    totals.rows[table] += len(rows)
                totals.problems.extend(result.problems[:20])
            else:
                totals.chunks_failed += 1
                self.echo(f"Chunk {chunk.id} (slots {chunk.first_slot}-{chunk.last_slot}) "
                          f"failed after {result.attempts} tries: {result.error}")  # fmt: skip
            batch.append(result)

        with factory() as pool:
            try:
                while queue or inflight:
                    while queue and len(inflight) < 2 * self.workers and not totals.stopped:
                        chunk = queue.popleft()
                        inflight[pool.submit(run_chunk, chunk)] = chunk
                    if not inflight:
                        break
                    finished, _ = wait(list(inflight), timeout=1.0, return_when=FIRST_COMPLETED)
                    for future in finished:
                        collect(future, inflight.pop(future))
                    now = time.monotonic()
                    if len(batch) >= self.commit_chunks or now - last_commit >= self.commit_every_s:
                        if shutil.disk_usage(self.data_dir).free < self.min_free:
                            self.echo("Stopping: the data drive is almost full.")
                            totals.stopped = True
                        commit()
                    if now - last_report >= 20:
                        last_report = now
                        self._report_progress(totals, total_bytes, len(chunks))
            except KeyboardInterrupt:
                totals.stopped = True
                self.echo("Stopping: finishing the chunks already started (Ctrl+C again "
                          "to stop at once; finished chunks are kept either way)...")  # fmt: skip
                try:
                    for future in list(inflight):
                        collect(future, inflight.pop(future))
                except KeyboardInterrupt:
                    pool.shutdown(wait=False, cancel_futures=True)
            commit()
        self._append({"kind": "run", "started": totals.started, "ended": time.time(),
                      "bytes": totals.bytes, "chunks_done": totals.chunks_done,
                      "chunks_failed": totals.chunks_failed, "workers": self.workers,
                      "max_mb_s": self.max_mb_s, "stopped": totals.stopped})  # fmt: skip
        self.write_gaps()
        return totals

    def _commit(self, batch: list[ChunkResult], sink: ParquetSink) -> None:
        ok = [r for r in batch if r.ok]
        for result in ok:
            for table, rows in result.rows.items():
                for row in rows:
                    sink.add(table, row)
        sink.flush()
        files = [p.relative_to(self.rows_root).as_posix() for p in sink.last_paths]
        stats: Counter = Counter()
        for result in ok:
            stats.update(result.stats)
        entry = {
            "kind": "commit",
            "at": time.time(),
            "chunks": [r.chunk_id for r in ok],
            "failed": {str(r.chunk_id): r.error for r in batch if not r.ok},
            "files": files,
            "file_bytes": sum((self.rows_root / f).stat().st_size for f in files),
            "bytes": sum(r.bytes for r in batch),
            "download_s": sum(r.download_s for r in batch),
            "cpu_s": sum(r.cpu_s for r in batch),
            "stats": dict(stats),
            "problems": [p for r in ok for p in r.problems][:50],
            "times": {str(r.chunk_id): [r.min_block_time, r.max_block_time] for r in ok},
        }
        self._append(entry)
        downloads.append(self.data_dir / "run", {
            "kind": "backfill", "epoch": self.epoch, "bytes": entry["bytes"],
            "estimated": False, "note": f"{len(ok)} chunks",
        })  # fmt: skip

    def _append(self, entry: dict) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        with open(self.progress_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _report_progress(self, totals: RunTotals, total_bytes: int, chunks: int) -> None:
        elapsed = max(time.time() - totals.started, 1e-9)
        rate = totals.bytes / elapsed
        left = max(total_bytes - totals.bytes, 0)
        eta = f"about {_duration(left / rate)} left" if rate > 0 else ""
        self.echo(
            f"{totals.chunks_done + totals.chunks_failed:,}/{chunks:,} chunks, "
            f"{totals.bytes / 1e9:.1f} of {total_bytes / 1e9:.1f} GB, {rate / 1e6:.0f} MB/s, "
            f"{eta}; {totals.stats['kept']:,} transactions kept, "
            f"{sum(totals.rows.values()):,} rows"
        )

    def write_gaps(self) -> None:
        """Rewrite the backfill gap log for this epoch from progress.jsonl: one gap per run
        of consecutive chunks that failed and were not done since."""
        p = self.progress()
        rows = []
        if p.failed:
            done_times = sorted((cid, span) for cid, span in p.times.items() if cid in p.done)
            for first, last in _runs(sorted(p.failed)):
                before = [s[1] for cid, s in done_times if cid < first and s[1] is not None]
                after = [s[0] for cid, s in done_times if cid > last and s[0] is not None]
                start_slot = self.epoch_first_slot() + first * self.chunk_slots
                end_slot = self.epoch_first_slot() + (last + 1) * self.chunk_slots - 1
                rows.append({
                    "start_ts": float(before[-1]) if before else None,
                    "end_ts": float(after[0]) if after else None,
                    "start_slot": start_slot, "end_slot": end_slot,
                    "reason": "backfill_missing",
                    "detail": p.failed[first][:500],
                    "recorded_at": time.time(), "source": "backfill",
                })  # fmt: skip
        write_parquet_atomic(pa.Table.from_pylist(rows, schema=GAP_SCHEMA), self.gaps_path)

    def epoch_first_slot(self) -> int:
        if self.index is None:
            self.index = SlotIndex(self.epoch, (self.folder / "slot-ranges.raw").read_bytes())
        return self.index.first_slot


def parse_epochs(text: str) -> list[int]:
    """'1085', '1085-1091' or '1085,1087' as epochs in the order given, without repeats."""
    out: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        low, _, high = part.partition("-")
        try:
            first, last = int(low), int(high or low)
        except ValueError:
            raise ValueError(f"not an epoch or a range of epochs: {part!r}") from None
        if last < first:
            raise ValueError(f"the range {part} goes backwards")
        out.extend(e for e in range(first, last + 1) if e not in out)
    if not out:
        raise ValueError("no epoch given")
    return out


def _runs(ids: list[int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for i in ids:
        if out and i == out[-1][1] + 1:
            out[-1] = (out[-1][0], i)
        else:
            out.append((i, i))
    return out


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600} h {seconds % 3600 // 60:02d} m"
    return f"{seconds // 60} m {seconds % 60:02d} s"


# --- measurements ------------------------------------------------------------------------


def measurements(backfill: Backfill, index: SlotIndex | None = None) -> dict:
    """Everything the pilot must report (spec 15, Phase 3), from progress.jsonl."""
    p = backfill.progress()
    commits = [e for e in p.lines if e.get("kind") == "commit"]
    runs = [e for e in p.lines if e.get("kind") == "run"]
    stats: Counter = Counter()
    for e in commits:
        stats.update(e.get("stats", {}))
    bytes_down = sum(e.get("bytes", 0) for e in commits)
    file_bytes = sum(e.get("file_bytes", 0) for e in commits)
    times = [t for cid, span in p.times.items() if cid in p.done for t in span if t]
    chain_s = (max(times) - min(times)) if len(times) > 1 else 0.0
    wall_s = sum(max(e["ended"] - e["started"], 0.0) for e in runs)
    out = {
        "epoch": backfill.epoch,
        "chunks_done": len(p.done),
        "chunks_failed": len(p.failed),
        "downloaded_gb": bytes_down / 1e9,
        "kept_gb": file_bytes / 1e9,
        "run_wall_hours": wall_s / 3600,
        "download_mb_s": bytes_down / 1e6 / wall_s if wall_s else None,
        "worker_download_hours": sum(e.get("download_s", 0) for e in commits) / 3600,
        "cpu_hours": sum(e.get("cpu_s", 0) for e in commits) / 3600,
        "decode_mb_s_per_core": (
            bytes_down / 1e6 / sum(e.get("cpu_s", 0) for e in commits)
            if any(e.get("cpu_s") for e in commits) else None
        ),
        "chain_hours_covered": chain_s / 3600,
        "transactions": stats.get("transactions", 0),
        "kept_transactions": stats.get("kept", 0),
        "failed_dropped": stats.get("failed (dropped)", 0),
        "unreadable": stats.get("unreadable", 0),
        "decode_errors": stats.get("decode_errors", 0),
        "truncated": stats.get("truncated", 0),
        "unknown_events": stats.get("unknown_events", 0),
        "stale_idl_events": stats.get("stale_idl_events", 0),
        "rows": {k[6:]: v for k, v in sorted(stats.items()) if k.startswith("rows: ")},
    }  # fmt: skip
    if chain_s:
        per_day = 86400 / chain_s
        out["per_chain_day"] = {
            "downloaded_gb": out["downloaded_gb"] * per_day,
            "kept_gb": out["kept_gb"] * per_day,
            "cpu_hours": out["cpu_hours"] * per_day,
        }
    if index is not None:
        all_chunks = plan_chunks(index, backfill.chunk_slots)
        out["epoch_chunks"] = len(all_chunks)
        out["epoch_gb"] = sum(c.size for c in all_chunks) / 1e9
    return out
