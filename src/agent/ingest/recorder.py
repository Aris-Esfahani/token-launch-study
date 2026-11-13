"""Write parsed rows to append-only Parquet files, one folder per table and UTC date:

    <data_dir>/raw/<table>/date=YYYY-MM-DD/part-<time>-<writer id>-<n>.parquet

Rows are buffered in memory and written every `flush_interval_s` seconds. Each file is
written under a temporary name and renamed when complete, so a reader (DuckDB, the
trainer) never sees a half-written file and Windows never has to rewrite a file that is
open elsewhere. Files are never modified after they are written.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from agent.ingest.borsh_idl import Field
from agent.ingest.parsers import to_storage

_ARROW_TYPES = {
    "bool": pa.bool_(),
    "u8": pa.uint8(), "i8": pa.int8(), "u16": pa.uint16(), "i16": pa.int16(),
    "u32": pa.uint32(), "i32": pa.int32(), "u64": pa.uint64(), "i64": pa.int64(),
    "f32": pa.float32(), "f64": pa.float64(),
    "big": pa.string(), "text": pa.string(), "json": pa.string(), "bytes": pa.binary(),
}  # fmt: skip


def arrow_schema(columns: list[Field]) -> pa.Schema:
    return pa.schema([pa.field(c.name, _ARROW_TYPES[c.kind]) for c in columns])


def write_parquet_atomic(table: pa.Table, path: Path) -> int:
    """Write to a temporary file, then rename. Returns the file size in bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(table, tmp, compression="zstd")
    os.replace(tmp, path)
    return path.stat().st_size


class ParquetSink:
    def __init__(
        self,
        root: Path,
        columns: dict[str, list[Field]],
        *,
        flush_interval_s: float = 60.0,
        max_buffered_rows: int = 50_000,
        prefix: str = "part",
    ) -> None:
        self._root = root
        self._prefix = prefix
        self._columns = columns
        self._schemas = {table: arrow_schema(cols) for table, cols in columns.items()}
        self._buffers: dict[str, list[dict]] = {table: [] for table in columns}
        self._interval = flush_interval_s
        self._max_rows = max_buffered_rows
        self._last_flush = time.monotonic()
        self._sequence = 0
        self._writer_id = uuid.uuid4().hex[:8]  # unique even for two writers in one process
        self.rows_written: dict[str, int] = {table: 0 for table in columns}
        self.files_written = 0
        self.bytes_written = 0
        self.last_flush_at: float | None = None
        self.last_paths: list[Path] = []  # files written by the latest flush

    def add(self, table: str, row: dict) -> None:
        self._buffers[table].append(row)

    @property
    def buffered_rows(self) -> int:
        return sum(len(rows) for rows in self._buffers.values())

    def due(self) -> bool:
        return (
            time.monotonic() - self._last_flush >= self._interval
            or self.buffered_rows >= self._max_rows
        )

    def restart_timer(self) -> None:
        self._last_flush = time.monotonic()

    def discard(self) -> int:
        """Drop everything buffered (used when the rows cannot be written). Returns the count."""
        dropped = self.buffered_rows
        for rows in self._buffers.values():
            rows.clear()
        return dropped

    def flush(self) -> int:
        """Write all buffered rows. Returns how many were written. Raises OSError if the
        disk refuses; the buffer is then left untouched so nothing is lost silently."""
        self._last_flush = time.monotonic()
        self.last_paths = []
        written = 0
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        for table, rows in self._buffers.items():
            if not rows:
                continue
            by_date: dict[str, list[dict]] = {}
            for row in rows:
                by_date.setdefault(_utc_date(row.get("ts")), []).append(row)
            for date, group in by_date.items():
                arrays = [
                    pa.array([to_storage(r.get(c.name), c.kind) for r in group], type=field.type)
                    for c, field in zip(self._columns[table], self._schemas[table], strict=True)
                ]
                self._sequence += 1
                name = f"{self._prefix}-{stamp}-{self._writer_id}-{self._sequence:06d}.parquet"
                path = self._root / table / f"date={date}" / name
                self.bytes_written += write_parquet_atomic(
                    pa.Table.from_arrays(arrays, schema=self._schemas[table]), path
                )
                self.files_written += 1
                self.last_paths.append(path)
            self.rows_written[table] += len(rows)
            written += len(rows)
            rows.clear()
        self.last_flush_at = time.time()
        return written


def _utc_date(ts: object) -> str:
    moment = datetime.fromtimestamp(ts, UTC) if ts is not None else datetime.now(UTC)
    return moment.strftime("%Y-%m-%d")
