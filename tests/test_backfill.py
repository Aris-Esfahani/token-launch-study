"""The backfill runner: chunk plan, downloads, resume, failures and the gap log."""

from __future__ import annotations

import json
import struct

import httpx
import pyarrow.parquet as pq
import pytest

from agent.ingest import downloads
from agent.ingest import oldfaithful_backfill as ob
from agent.ingest.car import CarError, SlotIndex
from agent.ingest.oldfaithful_extract import Extractor
from helpers import FakeArchive, run_fixture_backfill


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(ob.time, "sleep", lambda _s: None)


def test_chunks_have_fixed_ids():
    data = bytearray(12 * 10)
    struct.pack_into("<QI", data, 12 * 1, 100, 10)
    struct.pack_into("<QI", data, 12 * 2, 110, 20)
    struct.pack_into("<QI", data, 12 * 7, 130, 5)
    chunks = ob.plan_chunks(SlotIndex(3, bytes(data)), 4)
    assert [(c.id, c.first_slot, c.last_slot, c.start, c.end) for c in chunks] == [
        (0, 30, 33, 100, 130), (1, 34, 37, 130, 135),
    ]  # fmt: skip
    struct.pack_into("<QI", data, 12 * 2, 999, 20)  # not next to slot 31 in the file
    with pytest.raises(CarError):
        ob.plan_chunks(SlotIndex(3, bytes(data)), 4)


def _fixture_chunk(archive: FakeArchive) -> ob.Chunk:
    index = SlotIndex(archive.info["epoch"], archive.index)
    (chunk,) = ob.plan_chunks(index, 64)
    return chunk


def test_a_chunk_is_downloaded_and_decoded():
    archive = FakeArchive()
    chunk = _fixture_chunk(archive)
    result = ob.process_chunk(chunk, archive.client(), "https://a.test/x.car", Extractor())
    assert result.ok and result.attempts == 1
    assert result.bytes == archive.info["length"]
    assert result.stats["transactions"] == 301 and result.stats["kept"] > 0
    assert {"trades", "amm_trades", "tokens"} <= set(result.rows)
    assert result.min_block_time == result.max_block_time is not None


@pytest.mark.parametrize("mode", ["ignore_range", "corrupt", "down"])
def test_a_bad_download_is_retried_then_reported(mode):
    archive = FakeArchive()
    archive.mode = mode
    chunk = _fixture_chunk(archive)
    result = ob.process_chunk(chunk, archive.client(), "https://a.test/x.car", Extractor(),
                              sleep=lambda _s: None)  # fmt: skip
    assert not result.ok and result.attempts == ob.ATTEMPTS
    assert result.error
    if mode == "ignore_range":
        assert result.bytes == 0  # it never reads a whole archive by mistake


def test_fetch_range_refuses_short_or_long_answers():
    def short(request):
        return httpx.Response(206, content=b"abc")

    client = httpx.Client(transport=httpx.MockTransport(short))
    with pytest.raises(ob.BackfillError):
        ob.fetch_range(client, "https://a.test/x", 0, 10)
    with pytest.raises(ob.BackfillError):
        ob.fetch_range(client, "https://a.test/x", 0, 2)


def test_run_writes_rows_progress_and_downloads(tmp_path):
    bf, index, totals = run_fixture_backfill(tmp_path)
    assert totals.chunks_done == 1 and totals.chunks_failed == 0
    files = sorted((tmp_path / "raw_backfill").glob("*/date=*/e1083-*.parquet"))
    assert {f.parent.parent.name for f in files} >= {"trades", "amm_trades", "tokens"}
    rows = pq.read_table(next(f for f in files if f.parent.parent.name == "trades")).to_pylist()
    assert all(r["source"] == "backfill" and r["bf_car_offset"] for r in rows)
    progress = bf.progress()
    assert progress.done == {_fixture_chunk(FakeArchive()).id}
    assert {f"{f.parent.parent.name}/{f.parent.name}/{f.name}" for f in files} == progress.files
    ledger = downloads.read_ledger(tmp_path / "run")
    assert [e["kind"] for e in ledger] == ["backfill"]
    assert ledger[0]["bytes"] == bf.index.get(bf.index.first_slot + 216129).length
    assert pq.read_table(bf.gaps_path).num_rows == 0
    m = ob.measurements(bf, index)
    assert m["chunks_done"] == 1 and m["epoch_chunks"] == 1
    assert m["downloaded_gb"] > 0 and m["kept_gb"] > 0 and m["rows"]["trades"] == len(rows)


def test_a_second_run_does_nothing(tmp_path):
    archive = FakeArchive()
    run_fixture_backfill(tmp_path, archive)
    before = archive.requests
    bf, _, totals = run_fixture_backfill(tmp_path, archive)
    assert totals.chunks_done == 0
    assert archive.requests == before + 0  # the index is kept, nothing is downloaded


def test_failed_chunks_go_to_the_gap_log_and_are_retried(tmp_path):
    archive = FakeArchive()
    archive.mode = "down"
    bf, index, totals = run_fixture_backfill(tmp_path, archive)
    assert totals.chunks_failed == 1
    gaps = pq.read_table(bf.gaps_path).to_pylist()
    assert len(gaps) == 1 and gaps[0]["reason"] == "backfill_missing"
    assert gaps[0]["start_slot"] <= archive.info["slot"] <= gaps[0]["end_slot"]
    assert gaps[0]["source"] == "backfill"
    assert bf.progress().failed

    archive.mode = "ok"
    bf, _, totals = run_fixture_backfill(tmp_path, archive)
    assert totals.chunks_done == 1
    assert not bf.progress().failed
    assert pq.read_table(bf.gaps_path).num_rows == 0


def test_files_from_an_interrupted_write_are_set_aside(tmp_path):
    bf, _, _ = run_fixture_backfill(tmp_path)
    stray = tmp_path / "raw_backfill" / "trades" / "date=2026-11-11" / "e1083-stray.parquet"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"not listed in progress.jsonl")
    assert bf.set_aside_orphans() == 1
    assert not stray.exists()
    assert (tmp_path / "raw_backfill" / "_orphans" / "trades" / "date=2026-11-11"
            / "e1083-stray.parquet").exists()  # fmt: skip
    assert bf.set_aside_orphans() == 0  # listed files stay where they are


def test_a_cut_off_progress_line_is_ignored(tmp_path):
    bf, _, _ = run_fixture_backfill(tmp_path)
    with open(bf.progress_path, "a", encoding="utf-8") as fh:
        fh.write('{"kind": "commit", "chunks": [99')
    assert bf.progress().done == {_fixture_chunk(FakeArchive()).id}


def test_an_epoch_not_in_the_archive_yet(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    bf = ob.Backfill(tmp_path, 9999, base_url="https://a.test", echo=lambda _: None)
    with pytest.raises(ob.BackfillError, match="not in the archive"):
        bf.load_index(client)


def test_backfill_rows_are_tagged_and_separate(tmp_path):
    """Backfill rows go to their own folder, never into the recorder's raw/ folder."""
    run_fixture_backfill(tmp_path)
    assert not (tmp_path / "raw").exists()
    report = json.dumps(ob.measurements(ob.Backfill(tmp_path, 1083, base_url="x")))
    assert "downloaded_gb" in report


def test_waits_for_the_whole_archive_file(tmp_path):
    archive = FakeArchive()
    bf = ob.Backfill(tmp_path, 1083, base_url="https://a.test", echo=lambda _: None)
    index = bf.load_index(archive.client())
    assert bf.car_problem(archive.client(), index) is None

    def partial(request):
        return httpx.Response(200, headers={"content-length": "1000"})

    problem = bf.car_problem(httpx.Client(transport=httpx.MockTransport(partial)), index)
    assert problem and "still being uploaded" in problem
    gone = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    assert bf.car_problem(gone, index) == "not in the archive yet"


def test_epochs_can_be_given_as_a_range_or_a_list():
    assert ob.parse_epochs("1084") == [1084]
    assert ob.parse_epochs("1085-1091") == list(range(1085, 1092))
    assert ob.parse_epochs("1085, 1087,1085,1086-1087") == [1085, 1087, 1086]
    for bad in ("", "1091-1085", "abc", "1085-x"):
        with pytest.raises(ValueError):
            ob.parse_epochs(bad)


def test_measurements_report_the_layout_check(tmp_path):
    run_fixture_backfill(tmp_path)
    m = ob.measurements(ob.Backfill(tmp_path, 1083, base_url="x"))
    # The fixture slot is real (epoch 1083): its 42 trade events carry the 8 bytes more than
    # pump.fun's published layout that Phase 1 found; nothing else is unknown.
    assert m["unknown_events"] == 0 and m["stale_idl_events"] == 42
