"""The recording pipeline offline: real transactions in, Parquet files and gap log out."""

import json

import duckdb

from agent.ingest.gaps import read_gaps
from agent.ingest.pipeline import Pipeline
from helpers import live, load_fixture


def make(tmp_path, **kwargs) -> Pipeline:
    return Pipeline(tmp_path / "data", tmp_path / "data" / "run", **kwargs)


def stored(tmp_path, table: str, columns: str = "signature") -> list[tuple]:
    folder = tmp_path / "data" / "raw" / table
    if not any(folder.glob("date=*/*.parquet")):
        return []
    with duckdb.connect() as db:
        return db.execute(
            f"SELECT {columns} FROM read_parquet('{folder.as_posix()}/*/*.parquet', "
            "union_by_name=true) ORDER BY ts, event_index"
        ).fetchall()


def gaps(tmp_path) -> list[dict]:
    return read_gaps(tmp_path / "data" / "gaps")


def test_records_events_and_drops_failed_and_repeated_transactions(tmp_path):
    p = make(tmp_path)
    create_and_buy, sell = live("pump-BuyV2"), live("pump-SellV3")
    assert p.handle(create_and_buy) is not None
    assert p.handle(sell) is not None
    assert p.handle(live("pump-Buy", err={"InstructionError": [2, "Custom"]})) is None
    assert p.handle(live("pump-SellV3")) is None  # the feed sometimes repeats itself
    p.flush()

    assert stored(tmp_path, "tokens", "mint, symbol, source") == [
        ("DYZocfkvb85zbc3M7VcEJtva6H96HycPzmF49Cv1pump", "STM", "recorder")
    ]
    trades = stored(tmp_path, "trades", "signature, is_buy, sol_amount, extra_hex, recv_ts > 0")
    assert sorted(trades) == sorted([
        (sell.signature, False, 18751289, "00" * 8, True),
        (create_and_buy.signature, True, 573955087, "00" * 8, True),
    ])  # fmt: skip
    s = p.stats
    assert (s.messages, s.failed, s.duplicates) == (4, 1, 1)
    assert s.rows_seen == {"tokens": 1, "trades": 2} and s.rows_dropped == 0
    assert s.last_event_ts == 1794482627 and s.lag_s is not None
    assert gaps(tmp_path) == [] and p.recording


def test_simulated_transactions_are_dropped(tmp_path):
    # Public nodes leak other people's simulations into the feed. They carry an all-zero
    # signature (64 "1" characters in base58) and never happened on chain.
    p = make(tmp_path)
    assert p.handle(live("pump-BuyV2", signature="1" * 64)) is None
    assert p.handle(live("pump-SellV3", signature="1" * 64)) is None
    p.flush()
    assert p.stats.simulated == 2 and p.stats.duplicates == 0
    assert stored(tmp_path, "tokens") == [] and stored(tmp_path, "trades") == []


def test_disconnect_is_logged_from_the_last_event_before_to_the_first_after(tmp_path):
    p = make(tmp_path)
    before, after = live("pump-BuyV3"), live("pump-BuyV2")
    p.handle(before)
    p.on_connection("disconnected", "ConnectionClosedError: no close frame")
    assert p.gaps.open_reason == "disconnect"
    p.on_connection("connected")
    p.handle(after)
    p.flush()

    recorded = {row[0] for row in stored(tmp_path, "trades")}
    assert recorded == {before.signature, after.signature}
    (gap,) = gaps(tmp_path)
    assert gap["reason"] == "disconnect" and "ConnectionClosedError" in gap["detail"]
    assert (gap["start_slot"], gap["end_slot"]) == (before.slot, after.slot)
    assert (gap["start_ts"], gap["end_ts"]) == (1794482629.0, 1794482633.0)


def test_two_connections_record_each_transaction_once_and_cover_each_other(tmp_path):
    p = make(tmp_path)
    # In chain order, as a live stream delivers them.
    first, second, third = (
        live(name, slot=1000 + i) for i, name in enumerate(("pump-BuyV3", "pump-Buy", "pump-BuyV2"))
    )
    for n in (first, second):
        assert p.handle(n, "1") is not None
        assert p.handle(n, "2") is None  # a repeat from the other connection
    p.on_connection("disconnected", "reset", "1")  # connection 2 is still up
    assert p.handle(third, "2") is not None
    p.flush()
    assert {row[0] for row in stored(tmp_path, "trades")} == {
        first.signature, second.signature, third.signature
    }
    assert gaps(tmp_path) == [] and p.gaps.open_reason is None
    s = p.stats
    assert s.duplicates == 2 and s.first_by == {"1": 2, "2": 1}
    assert s.messages_by == {"1": 2, "2": 3} and set(s.lag_by) == {"1", "2"}


def test_recorded_rows_reach_the_listener_in_stored_form(tmp_path):
    seen = []
    p = make(tmp_path, on_row=lambda table, row: seen.append((table, row)))
    p.handle(live("pump-BuyV2"))
    p.flush()
    tables = [table for table, _ in seen]
    assert tables.count("tokens") == 1 and "trades" in tables
    create = next(row for table, row in seen if table == "tokens")
    with duckdb.connect() as db:
        cursor = db.execute(
            f"SELECT * FROM read_parquet('{(tmp_path / 'data' / 'raw' / 'tokens').as_posix()}"
            "/*/*.parquet')"
        )
        names = [d[0] for d in cursor.description]
        on_disk = dict(zip(names, cursor.fetchone(), strict=True))
    on_disk.pop("date")  # the folder name (date=YYYY-MM-DD), not a stored column
    assert create == on_disk  # the live path sees exactly what a replay reads back
    p.set_paused(True)
    seen.clear()
    p.handle(live("pump-SellV3"))
    assert seen == []  # not recorded, so not passed on


def test_pause_and_resume(tmp_path):
    p = make(tmp_path)
    p.handle(live("pump-BuyV3"))
    p.set_paused(True)
    p.handle(live("pump-Buy"))
    p.set_paused(False)
    p.handle(live("pump-BuyV2"))
    p.flush()
    assert len(stored(tmp_path, "trades")) == 2
    (gap,) = gaps(tmp_path)
    assert gap["reason"] == "paused"


def test_stops_recording_when_the_disk_is_nearly_full(tmp_path):
    p = make(tmp_path, min_free_gb=10**9)  # no real disk has this much free
    p.handle(live("pump-BuyV3"))
    p.flush()
    assert not p.recording and p.gaps.has("disk_full") and p.gaps.open_reason == "disk_full"
    assert stored(tmp_path, "trades") == [] and p.stats.rows_dropped == 1
    p.handle(live("pump-Buy"))
    assert p.sink.buffered_rows == 0  # nothing is buffered while the disk is full


def test_write_failure_is_logged_as_a_gap_and_recording_recovers(tmp_path, monkeypatch):
    p = make(tmp_path)
    first, lost, later = live("pump-BuyV3"), live("pump-Buy"), live("pump-BuyV2")
    p.handle(first)
    p.flush()

    real_flush = p.sink.flush

    def refuse():
        raise OSError("disk refused the write")

    monkeypatch.setattr(p.sink, "flush", refuse)
    p.handle(lost)
    p.flush()
    assert not p.recording and p.gaps.open_reason == "write_error"

    monkeypatch.setattr(p.sink, "flush", real_flush)
    p.flush()
    assert p.recording
    p.handle(later)
    p.flush()

    assert {row[0] for row in stored(tmp_path, "trades")} == {first.signature, later.signature}
    (gap,) = gaps(tmp_path)
    assert gap["reason"] == "write_error" and "disk refused" in gap["detail"]
    assert (gap["start_slot"], gap["end_slot"]) == (first.slot, later.slot)


def test_restart_logs_the_time_the_recorder_was_off(tmp_path):
    first = make(tmp_path)
    before = live("pump-BuyV3")
    first.handle(before)
    first.close()
    state = json.loads((tmp_path / "data" / "run" / "recorder_state.json").read_text())
    assert state["clean"] is True and state["flushed_slot"] == before.slot

    second = make(tmp_path)
    assert second.gaps.open_reason == "recorder_off"
    after = live("pump-BuyV2")
    second.handle(after)
    second.close()
    (gap,) = gaps(tmp_path)
    assert gap["reason"] == "recorder_off"
    assert (gap["start_slot"], gap["end_slot"]) == (before.slot, after.slot)
    assert len(stored(tmp_path, "trades")) == 2


def test_stall_is_logged(tmp_path):
    p = make(tmp_path)
    p.handle(live("pump-BuyV3"))
    p.note_stall(900)
    p.handle(live("pump-BuyV2"))
    (gap,) = gaps(tmp_path)
    assert gap["reason"] == "stall" and "900s" in gap["detail"]


def test_transactions_with_cut_off_logs_are_listed_for_later_repair(tmp_path):
    p = make(tmp_path)
    logs = load_fixture("pump-BuyV3")["logMessages"]
    p.handle(live("pump-BuyV3", logs=[*logs, "Log truncated"], signature="CutOffSignature"))
    p.flush()
    folder = tmp_path / "data" / "raw" / "truncated_txs"
    with duckdb.connect() as db:
        rows = db.execute(
            f"SELECT signature, source FROM read_parquet('{folder.as_posix()}/*/*.parquet')"
        ).fetchall()
    assert rows == [("CutOffSignature", "recorder")]
    assert p.stats.truncated == 1
    assert len(stored(tmp_path, "trades")) == 1  # the events before the cut are still kept
