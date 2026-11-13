"""Offline end-to-end run of the engine against a local fake feed, using real transactions.

The engine connects, parses, prints (mint first), records to Parquet, ignores failed,
repeated and fake events, and shuts down cleanly when asked.
"""

import asyncio
import base64
import json
import logging

import duckdb
import pytest
from websockets.asyncio.server import serve

from agent.config import load_config
from agent.daemon import main as engine
from agent.daemon.control import RunFiles, engine_state, read_status
from agent.ingest import downloads
from agent.ingest.logs import walk_logs
from helpers import OTHER, acknowledge, load_fixture, make_project, notification, program_block

MINT_A = "DYZocfkvb85zbc3M7VcEJtva6H96HycPzmF49Cv1pump"
MINT_B = "H6Q8QkjGnAKe8qoGg55WsgcCSQV6kenk3kuXZKHypump"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for name in ("HELIUS_API_KEY", "DATA_DIR", "AGENT_HOME"):
        monkeypatch.delenv(name, raising=False)
    yield
    root = logging.getLogger()
    for handler in root.handlers[:]:
        handler.close()
        root.removeHandler(handler)


def as_message(name: str, **overrides) -> str:
    tx = load_fixture(name)
    signature = overrides.pop("signature", tx["signature"])
    return notification(7, signature, tx["logMessages"], slot=tx["slot"], **overrides)


async def fake_feed(ws):
    await acknowledge(ws)
    await ws.send(as_message("pump-BuyV2"))  # a new token plus its creator's first buy
    await ws.send(as_message("pump-SellV3"))
    await ws.send(as_message("pump-Buy", err={"InstructionError": [2, "Custom"]}))  # failed
    await ws.send(as_message("pump-SellV3"))  # repeated by the feed
    lines, _ = walk_logs(load_fixture("pump-BuyV3")["logMessages"])
    real_trade = base64.b64decode(next(ln.text for ln in lines if ln.kind == "data"))
    # Another program printing a genuine-looking pump.fun trade event:
    await ws.send(notification(7, "FakeEventSig", program_block(OTHER, ["Buy"], [real_trade])))
    await ws.wait_closed()


def test_engine_records_prints_and_stops_on_request(tmp_path, capsys):
    async def scenario():
        async with serve(fake_feed, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            make_project(tmp_path, ws_url=f"ws://127.0.0.1:{port}")
            cfg = load_config(tmp_path)
            files = RunFiles(cfg.run_dir)
            task = asyncio.create_task(engine.run(cfg, foreground=True))
            status = None
            for _ in range(150):
                status = read_status(files)
                if status and status["messages"] == 5:
                    break
                await asyncio.sleep(0.1)
            running_state = engine_state(files)[0]
            second_start = await engine.run(cfg, foreground=False)  # must refuse
            files.stop_flag.write_text("stop", encoding="utf-8")
            code = await asyncio.wait_for(task, timeout=15)
            return cfg, files, status, running_state, second_start, code

    cfg, files, status, running_state, second_start, code = asyncio.run(scenario())

    assert running_state == "running" and second_start == 2 and code == 0
    assert status["connected"] and status["source"] == "test" and status["recording"]
    assert (status["messages"], status["creates"], status["trades"]) == (5, 1, 2)
    assert status["duplicates"] == 1 and status["decode_errors"] == 0

    out = capsys.readouterr().out
    events = [ln.split("  ", 1)[1] for ln in out.splitlines() if "pump" in ln and "  " in ln]
    assert events == [
        f"NEW    {MINT_A}  $STM  StonkMarket",
        f"BUY    {MINT_A}  0.5740 SOL  by Dt6ANE..",
        f"SELL   {MINT_B}  0.0188 SOL  by ED955o..",
    ]
    assert "FakeEventSig" not in out and "already running" in out

    # A clean stop writes everything to disk and marks the shutdown as clean.
    raw = cfg.data_dir / "raw"
    with duckdb.connect() as db:
        tokens = db.execute(
            f"SELECT mint FROM read_parquet('{raw.as_posix()}/tokens/*/*.parquet')"
        ).fetchall()
        trades = db.execute(
            f"SELECT mint, is_buy FROM read_parquet('{raw.as_posix()}/trades/*/*.parquet') "
            "ORDER BY is_buy"
        ).fetchall()
    assert tokens == [(MINT_A,)]
    assert trades == [(MINT_B, False), (MINT_A, True)]
    final = read_status(files)
    assert final["state"] == "stopped" and final["rows_written"]["trades"] == 2
    assert engine_state(files)[0] == "stopped"
    assert not files.pid.exists() and not files.stop_flag.exists()
    assert json.loads((cfg.run_dir / "recorder_state.json").read_text())["clean"] is True


def test_engine_pauses_and_resumes_recording_from_the_flag_file(tmp_path):
    async def feed(ws):
        await acknowledge(ws)
        await ws.send(as_message("pump-BuyV3"))
        await ws.wait_closed()

    async def scenario():
        async with serve(feed, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            make_project(tmp_path, ws_url=f"ws://127.0.0.1:{port}")
            cfg = load_config(tmp_path)
            files = RunFiles(cfg.run_dir)
            files.run_dir.mkdir(parents=True, exist_ok=True)
            files.pause_flag.write_text("paused", encoding="utf-8")  # start paused
            # A previous run's final status: its download count must reach the ledger.
            files.status.write_text(json.dumps(
                {"state": "stopped", "pid": 1, "started_at": 123.0, "bytes_received": 5000}
            ), encoding="utf-8")
            task = asyncio.create_task(engine.run(cfg, foreground=False))
            seen = []
            for want_paused in (True, False):
                for _ in range(150):
                    status = read_status(files)
                    if status and status.get("messages") == 1 and status["paused"] == want_paused:
                        seen.append((status["paused"], status["recording"]))
                        break
                    await asyncio.sleep(0.1)
                files.pause_flag.unlink(missing_ok=True)  # resume
            files.stop_flag.write_text("stop", encoding="utf-8")
            await asyncio.wait_for(task, timeout=15)
            return seen, read_status(files)

    seen, final = asyncio.run(scenario())
    assert seen == [(True, False), (False, True)]
    assert final["rows_dropped"] == 1 and final["rows_written"]["trades"] == 0
    assert final["bytes_received"] > 0
    ledger = downloads.read_ledger(tmp_path / "data" / "run")
    assert [(e["run_started_at"], e["bytes"]) for e in ledger] == [(123.0, 5000)]


def test_engine_with_two_connections_records_each_transaction_once(tmp_path):
    async def first(ws):
        await acknowledge(ws)
        await ws.send(as_message("pump-BuyV2"))
        await ws.send(as_message("pump-SellV3"))
        await ws.wait_closed()

    async def second(ws):
        await acknowledge(ws)
        await ws.send(as_message("pump-SellV3"))  # also sent by the first connection
        await ws.send(as_message("pump-BuyV2"))
        await ws.send(as_message("pump-BuyV3"))  # only this connection has it
        await ws.wait_closed()

    async def scenario():
        async with serve(first, "127.0.0.1", 0) as one, serve(second, "127.0.0.1", 0) as two:
            port_one = one.sockets[0].getsockname()[1]
            port_two = two.sockets[0].getsockname()[1]
            make_project(tmp_path, ws_url=f"ws://127.0.0.1:{port_one}", extra_sources=f"""\
                - name: second
                  ws_url: ws://127.0.0.1:{port_two}
                  connection: 2
                """)  # fmt: skip
            (tmp_path / "config" / "rules.yaml").write_text(
                "caution:\n  - {feature: snipers, at_least: 2, reason: snipers}\n",
                encoding="utf-8",
            )
            cfg = load_config(tmp_path)
            files = RunFiles(cfg.run_dir)
            task = asyncio.create_task(engine.run(cfg, foreground=False))
            status = None
            for _ in range(150):
                status = read_status(files)
                if status and status["messages"] == 5 and len(status["connections"]) == 2 \
                        and all(c["connected"] for c in status["connections"]):
                    break
                await asyncio.sleep(0.1)
            files.stop_flag.write_text("stop", encoding="utf-8")
            await asyncio.wait_for(task, timeout=15)
            return cfg, status, read_status(files)

    cfg, status, final = asyncio.run(scenario())
    assert [c["name"] for c in status["connections"]] == ["1", "2"]
    assert {c["source"] for c in status["connections"]} == {"test", "second"}
    assert status["connected"] and status["duplicates"] == 2
    assert sum(c["first_share"] for c in status["connections"]) == pytest.approx(1.0)
    assert status["live"]["enabled"] and status["live"]["tokens_seen"] == 1
    assert final["rows_written"]["trades"] == 3 and final["gaps_logged"] == 0
    assert (cfg.run_dir / "alerts.log").exists()
