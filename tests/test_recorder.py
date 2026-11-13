import duckdb
import pyarrow.parquet as pq

from agent.ingest.borsh_idl import Field
from agent.ingest.recorder import ParquetSink

COLUMNS = {
    "trades": [
        Field("signature", "text"),
        Field("ts", "i64"),
        Field("amount", "u64"),
        Field("is_buy", "bool"),
        Field("huge", "big"),
        Field("holders", "json"),
        Field("recv_ts", "f64"),
    ],
    "tokens": [Field("signature", "text"), Field("ts", "i64")],
}
DAY1 = 1794482629  # 2026-11-12 UTC
DAY2 = DAY1 + 86400


def row(sig: str, ts: int, **extra) -> dict:
    return {"signature": sig, "ts": ts, "amount": 5, "is_buy": True, "huge": 7,
            "holders": [], "recv_ts": 1.25, **extra}  # fmt: skip


def read(root, table, columns="signature, ts, amount, is_buy, huge, holders, recv_ts"):
    path = f"{(root / table).as_posix()}/*/*.parquet"
    with duckdb.connect() as db:
        return db.execute(
            f"SELECT {columns} FROM read_parquet('{path}', union_by_name=true) ORDER BY signature"
        ).fetchall()


def test_rows_are_written_by_utc_date_and_read_back_exactly(tmp_path):
    sink = ParquetSink(tmp_path, COLUMNS)
    sink.add("trades", row("a", DAY1, amount=2**63 + 5, huge=2**100, holders=[{"k": "x", "v": 1}]))
    sink.add("trades", row("b", DAY2, is_buy=False, huge=None, ignored_extra_key=1))
    sink.add("tokens", {"signature": "t", "ts": DAY1})
    assert sink.buffered_rows == 3
    assert sink.flush() == 3
    assert sink.buffered_rows == 0

    files = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())
    assert [f.rsplit("/", 1)[0] for f in files] == [
        "tokens/date=2026-11-12", "trades/date=2026-11-12", "trades/date=2026-11-13",
    ]  # fmt: skip
    assert all(f.endswith(".parquet") for f in files), "no temporary files may be left behind"

    a, b = read(tmp_path, "trades")
    assert a == ("a", DAY1, 2**63 + 5, True, str(2**100), '[{"k":"x","v":1}]', 1.25)
    assert b == ("b", DAY2, 5, False, None, "[]", 1.25)
    # DuckDB also exposes the folder name as a `date` column, handy for filtering by day.
    assert [str(r[0]) for r in read(tmp_path, "trades", "date")] == ["2026-11-12", "2026-11-13"]
    assert sink.rows_written == {"trades": 2, "tokens": 1}
    assert sink.files_written == 3 and sink.bytes_written > 0 and sink.last_flush_at


def test_each_flush_adds_new_files_and_never_touches_old_ones(tmp_path):
    sink = ParquetSink(tmp_path, COLUMNS)
    sink.add("trades", row("a", DAY1))
    sink.flush()
    first = next(tmp_path.rglob("*.parquet"))
    before = first.read_bytes()
    sink.add("trades", row("b", DAY1))
    sink.flush()
    assert len(list(tmp_path.rglob("*.parquet"))) == 2
    assert first.read_bytes() == before
    assert [r[0] for r in read(tmp_path, "trades")] == ["a", "b"]
    assert pq.read_schema(first).names == [c.name for c in COLUMNS["trades"]]


def test_flush_timing_and_discard(tmp_path):
    sink = ParquetSink(tmp_path, COLUMNS, flush_interval_s=3600, max_buffered_rows=2)
    assert not sink.due()
    sink.add("trades", row("a", DAY1))
    assert not sink.due()
    sink.add("trades", row("b", DAY1))
    assert sink.due()  # buffer is full
    assert sink.discard() == 2 and sink.buffered_rows == 0
    assert sink.flush() == 0 and not list(tmp_path.rglob("*.parquet"))
    assert ParquetSink(tmp_path, COLUMNS, flush_interval_s=0).due()
