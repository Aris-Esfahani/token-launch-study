"""Backfill parity (spec 4.1, mandatory): the live recorder and the backfill must produce
identical rows for the same transactions.

Uses a real slot of the Old Faithful archive (the backfill path) and the RPC's copy of the
same transactions fed through the live pipeline (the recorder path: the RPC's log lines
are what the live feed delivers).
"""

from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path

import duckdb
import pytest

from agent.backfill_parity import compare, run_parity
from agent.ingest.gaps import GapLog
from agent.ingest.oldfaithful_extract import Extractor
from agent.ingest.pipeline import Pipeline
from agent.ingest.solana import b58encode
from agent.ingest.ws_client import LogNotification
from helpers import PUMP, car_fixture, run_fixture_backfill

NOT_COMPARED = {"recv_ts", "source", "date"}


def _mentions_pump(rpc: dict) -> bool:
    loaded = rpc["loadedAddresses"] or {}
    return PUMP in rpc["accountKeys"] + loaded.get("writable", []) + loaded.get("readonly", [])


def _notification(signature: str, rpc: dict, *, logs=None, slot=None) -> LogNotification:
    return LogNotification(
        program=PUMP, signature=signature, slot=slot or rpc["slot"], err=rpc["err"],
        logs=logs if logs is not None else rpc["logMessages"],
        recv_ts=rpc["blockTime"] + 1.5, size=1000,
    )  # fmt: skip


def record(data_dir: Path, *, skip: set[str] = frozenset(), extra: list[LogNotification] = ()):
    """Feed the fixture's transactions through the live pipeline, as the feed would: only
    those that mention pump.fun. Two more trades, far outside the backfilled slots, mark the
    recording as running before and after them."""
    _, info = car_fixture()
    pipeline = Pipeline(data_dir, data_dir / "run")
    pipeline.on_connection("connected")
    rpc_txs = sorted(info["rpc"].items())
    trade = next((s, r) for s, r in rpc_txs if r["err"] is None and _mentions_pump(r)
                 and any("Instruction: Buy" in line for line in r["logMessages"]))  # fmt: skip
    pipeline.handle(_notification(_fake_signature(1), trade[1], slot=info["slot"] - 5000))
    for signature, rpc in rpc_txs:
        if _mentions_pump(rpc) and signature not in skip:
            pipeline.handle(_notification(signature, rpc))
    for n in extra:
        pipeline.handle(n)
    pipeline.handle(_notification(_fake_signature(2), trade[1], slot=info["slot"] + 5000))
    pipeline.close()
    return pipeline


def _fake_signature(n: int) -> str:
    return b58encode(bytes([n]) * 64)


def _parity(data_dir: Path, bf, index, check=None):
    return asyncio.run(run_parity(data_dir, bf.epoch, chunk_slots=bf.chunk_slots,
                                  first_slot=index.first_slot, check=check))  # fmt: skip


def test_same_transaction_gives_identical_rows(tmp_path):
    """Row by row, before anything is written: the archive's bytes and the live log lines
    of the same transactions produce the same values in every shared column."""
    data, info = car_fixture()
    backfill = Extractor().extract(data, info["offset"], {info["slot"]})
    pipeline = Pipeline(tmp_path, tmp_path / "run")
    pipeline.on_connection("connected")
    live: dict[str, list[dict]] = {}
    for signature, rpc in sorted(info["rpc"].items()):
        parsed = pipeline.handle(_notification(signature, rpc))
        for table, rows in (parsed.rows if parsed else {}).items():
            live.setdefault(table, []).extend(rows)

    def keyed(rows):
        return {(r["signature"], r["event_index"]): r for r in rows}

    assert set(backfill.rows) - {"truncated_txs"} == set(live)
    compared = 0
    for table in live:
        b, r = keyed(backfill.rows[table]), keyed(live[table])
        assert set(b) == set(r), table
        for key, row in r.items():
            for column, value in row.items():
                if column not in NOT_COMPARED:
                    assert b[key][column] == value, (table, key, column)
                    compared += 1
            assert b[key]["source"] == "backfill" and b[key]["recv_ts"] is None
    assert compared > 1_000  # about 40 rows of 40-odd columns
    # Failed transactions are dropped on both paths.
    failed = {s for s, rpc in info["rpc"].items() if rpc["err"] is not None}
    assert failed
    assert not failed & {row["signature"] for rows in backfill.rows.values() for row in rows}


def test_parity_passes_on_the_same_data(tmp_path):
    record(tmp_path)
    bf, index, totals = run_fixture_backfill(tmp_path)
    assert totals.chunks_done == 1
    result = _parity(tmp_path, bf, index)
    assert result.passed, result.to_json()
    assert result.compared_rows > 0 and result.mismatched_rows == 0
    assert {"trades", "amm_trades", "tokens"} <= set(result.tables)
    for t in result.tables.values():
        assert t.recorder_only == 0 and t.backfill_only == 0
    # PumpSwap rows from transactions that never mention pump.fun are not compared: the
    # live feed cannot have them.
    with duckdb.connect() as db:
        not_live = db.execute(
            "SELECT count(*) FROM read_parquet(?, union_by_name=true) WHERE NOT bf_mentions_pump",
            [f"{(tmp_path / 'raw_backfill' / 'amm_trades').as_posix()}/*/*.parquet"],
        ).fetchone()[0]
    assert not_live > 0


def test_missed_rows_are_counted_inside_and_outside_gaps(tmp_path):
    _, info = car_fixture()
    trades = [s for s, r in sorted(info["rpc"].items()) if r["err"] is None and _mentions_pump(r)
              and any("Instruction: Buy" in line or "Instruction: Sell" in line
                      for line in r["logMessages"])]  # fmt: skip
    record(tmp_path, skip=set(trades[:3]))
    bf, index, _ = run_fixture_backfill(tmp_path)
    result = _parity(tmp_path, bf, index)
    assert result.passed  # rows only the backfill has do not fail the test
    missed = sum(t.backfill_only for t in result.tables.values())
    assert missed >= 3
    assert sum(t.backfill_only_in_gaps for t in result.tables.values()) == 0
    assert result.tables["trades"].silent_miss_share > 0

    GapLog(tmp_path / "gaps").append({
        "start_ts": info["rpc"][trades[0]]["blockTime"] - 1.0,
        "end_ts": info["rpc"][trades[0]]["blockTime"] + 1.0,
        "start_slot": info["slot"] - 1, "end_slot": info["slot"] + 1,
        "reason": "disconnect", "detail": "",
    })  # fmt: skip
    result = _parity(tmp_path, bf, index)
    assert sum(t.backfill_only_in_gaps for t in result.tables.values()) == missed


def test_a_differing_value_fails(tmp_path):
    _, info = car_fixture()
    signature, rpc = next(
        (s, r) for s, r in sorted(info["rpc"].items())
        if r["err"] is None and _mentions_pump(r)
        and any("Instruction: Buy" in line for line in r["logMessages"])
    )  # fmt: skip
    logs = list(rpc["logMessages"])
    for i, line in enumerate(logs):
        payload = line.removeprefix("Program data: ")
        if line.startswith("Program data: ") and len(base64.b64decode(payload)) > 200:
            raw = bytearray(base64.b64decode(payload))
            raw[40] ^= 0x01  # a byte of the trade's quote amount
            logs[i] = "Program data: " + base64.b64encode(bytes(raw)).decode()
            break
    record(tmp_path, skip={signature}, extra=[_notification(signature, rpc, logs=logs)])
    bf, index, _ = run_fixture_backfill(tmp_path)
    result = _parity(tmp_path, bf, index)
    assert not result.passed
    assert result.mismatched_rows == 1
    trades = result.tables["trades"]
    assert trades.mismatch_examples[0]["signature"] == signature
    assert trades.mismatched_columns  # names the column(s) that differ


def _phantom(info) -> LogNotification:
    signature, rpc = next((s, r) for s, r in sorted(info["rpc"].items())
                          if r["err"] is None and _mentions_pump(r)
                          and any("Instruction: Sell" in line for line in r["logMessages"]))
    return _notification(_fake_signature(9), rpc)


@pytest.mark.parametrize(
    ("status", "passed"),
    [("missing", True), ("ok", False), (None, False)],
)
def test_rows_only_the_recorder_has_must_not_be_on_chain(tmp_path, status, passed):
    """The live feed sometimes delivers transactions that never landed. Those are fine;
    a recorder-only transaction the chain does have means the backfill lost it."""
    _, info = car_fixture()
    record(tmp_path, extra=[_phantom(info)])
    bf, index, _ = run_fixture_backfill(tmp_path)
    asked = []

    def make_check(result):
        assert result.controls  # signatures in both, for the RPC's own sanity check

        async def check(signatures):
            asked.extend(signatures)
            return {s: status for s in signatures}

        return check

    result = _parity(tmp_path, bf, index, check=make_check if status else None)
    assert result.passed is passed
    if status:
        assert asked == [_fake_signature(9)]
    else:
        assert result.unchecked == [_fake_signature(9)]


def test_the_chain_check_asks_in_small_batches_and_asks_missing_ones_again():
    from agent.backfill_parity import rpc_check

    class FakeRpc:
        def __init__(self):
            self.batches: list[list[str]] = []
            self.flaky = {"real-but-flaky"}  # on chain, reported missing the first time

        async def call(self, method, params):
            signatures, options = params
            assert method == "getSignatureStatuses" and options["searchTransactionHistory"]
            self.batches.append(signatures)
            values = []
            for s in signatures:
                if s.startswith("gone") or s in self.flaky:
                    self.flaky.discard(s)
                    values.append(None)
                else:
                    values.append({"err": {"x": 1} if s.startswith("failed") else None})
            return {"value": values}

    rpc = FakeRpc()
    wanted = [f"gone-{i}" for i in range(10)] + ["failed-1", "real-but-flaky"]
    out = asyncio.run(rpc_check(rpc, controls=["control"], batch=4)(wanted))
    assert all(len(b) <= 4 for b in rpc.batches)
    assert out["real-but-flaky"] == "ok" and out["failed-1"] == "failed"
    assert all(out[f"gone-{i}"] == "missing" for i in range(10))
    # A control the node cannot find even when asked twice: nothing counts as checked.
    rpc = FakeRpc()
    assert asyncio.run(rpc_check(rpc, controls=["gone-control"], batch=4)(wanted)) == {}


def test_nothing_to_compare(tmp_path):
    bf, index, _ = run_fixture_backfill(tmp_path)
    result = compare(tmp_path, bf.epoch, chunk_slots=bf.chunk_slots, first_slot=index.first_slot)
    assert not result.passed and "no recorded data" in result.note


def test_recorder_timestamps_are_on_chain_time(tmp_path):
    """`ts` is the event's own timestamp on both paths, never the time it was received
    (the no-lookahead rule depends on it)."""
    pipeline = record(tmp_path)
    assert pipeline.stats.lag_s is not None
    bf, index, _ = run_fixture_backfill(tmp_path)
    with duckdb.connect() as db:
        for root in ("raw", "raw_backfill"):
            pattern = f"{(tmp_path / root / 'trades').as_posix()}/*/*.parquet"
            ts = db.execute(f"SELECT DISTINCT ts FROM read_parquet('{pattern}', "
                            "union_by_name=true) WHERE slot = ?",
                            [car_fixture()[1]["slot"]]).fetchall()  # fmt: skip
            assert ts and all(abs(t[0] - time.time()) > 3600 for t in ts)
