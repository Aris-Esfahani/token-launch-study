"""The engine's local API and the screen's client (spec 3, 12): lookups, the alert feed,
the status, reconnecting, and refusing web pages and other machines."""

import asyncio
import shutil
import socket
from pathlib import Path

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import InvalidStatus

from agent.config import load_config
from agent.daemon import main as engine
from agent.daemon.control import RunFiles, read_status
from agent.daemon.local_api import LocalApi
from agent.tui import client as client_module
from agent.tui.client import EngineClient, EngineOffline
from helpers import make_project
from test_engine import MINT_A, fake_feed
from test_engine import isolated as isolated  # noqa: F401 - autouse fixture: clean env, logs

REPO = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def until(condition, timeout: float = 10.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


def fake_api(alerts: list | None = None) -> tuple[LocalApi, list]:
    calls: list[str] = []

    def analyze(mint: str) -> dict:
        calls.append(mint)
        return {"mint": mint, "known": False, "why": "test"}

    api = LocalApi(analyze, lambda: {"state": "running"})
    for alert in alerts or []:
        api.publish(alert)
    return api, calls


def test_lookups_status_and_bad_requests():
    async def scenario():
        api, calls = fake_api()
        port = await api.start(0)
        host = api._server.sockets[0].getsockname()[0]
        client = EngineClient(port)
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        try:
            await until(lambda: client.connected)
            answers = [
                await client.analyze(MINT_A),
                await client.status(),
                await client.request({"op": "analyze", "mint": "not a mint"}),
                await client.request({"op": "what"}),
            ]
        finally:
            stop.set()
            await task
            await api.close()
        return host, calls, answers, api.stats()

    host, calls, (analysis, status, bad, unknown), stats = asyncio.run(scenario())
    assert host == "127.0.0.1"  # this PC only
    assert calls == [MINT_A]
    assert analysis["op"] == "analysis" and analysis["mint"] == MINT_A and analysis["id"]
    assert status["op"] == "status" and status["state"] == "running"
    assert bad["op"] == "error" and bad["error"] == "not a Solana address"
    assert unknown["op"] == "error" and "unknown op" in unknown["error"]
    assert stats["requests"] == 5 and stats["median_analyze_ms"] is not None  # + subscribe


def test_a_screen_gets_the_latest_alerts_then_each_new_one():
    async def scenario():
        api, _ = fake_api(alerts=[{"mint": "OLD1"}, {"mint": "OLD2"}])
        port = await api.start(0)
        seen: list[tuple[str, bool]] = []
        client = EngineClient(port, on_alert=lambda a, old: seen.append((a["mint"], old)))
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        try:
            await until(lambda: len(seen) == 2)
            api.publish({"mint": "NEW"})
            await until(lambda: len(seen) == 3)
        finally:
            stop.set()
            await task
            await api.close()
        return seen

    assert asyncio.run(scenario()) == [("OLD1", True), ("OLD2", True), ("NEW", False)]


def test_web_pages_cannot_connect():
    async def scenario():
        api, _ = fake_api()
        port = await api.start(0)
        try:
            with pytest.raises(InvalidStatus) as refused:
                async with connect(f"ws://127.0.0.1:{port}", origin="https://example.com"):
                    pass
            async with connect(f"ws://127.0.0.1:{port}") as ws:  # no Origin: the screen
                await ws.send('{"op": "status", "id": 1}')
                answer = await ws.recv()
        finally:
            await api.close()
        return refused.value.response.status_code, answer

    code, answer = asyncio.run(scenario())
    assert code == 403 and '"state": "running"' in answer


def test_the_client_waits_for_the_engine_and_follows_its_restarts(monkeypatch):
    monkeypatch.setattr(client_module, "RETRY_S", 0.05)

    async def scenario():
        port = free_port()
        states: list[bool] = []
        client = EngineClient(port, on_connection=states.append)
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        await asyncio.sleep(0.2)
        offline = None
        try:
            await client.analyze(MINT_A)
        except EngineOffline as exc:
            offline = str(exc)
        api, _ = fake_api()
        await api.start(port)  # the engine starts
        await until(lambda: client.connected)
        await api.close()  # ...stops
        await until(lambda: not client.connected)
        await api.start(port)  # ...and starts again
        await until(lambda: client.connected)
        answer = await client.analyze(MINT_A)
        stop.set()
        await task
        await api.close()
        return offline, states, answer

    offline, states, answer = asyncio.run(scenario())
    assert offline == "the engine is not running"
    assert states == [True, False, True, False] and answer["mint"] == MINT_A


def test_the_engine_answers_the_screen_from_its_live_state(tmp_path):
    async def scenario():
        async with serve(fake_feed, "127.0.0.1", 0) as feed_server:
            feed_port = feed_server.sockets[0].getsockname()[1]
            api_port = free_port()
            make_project(tmp_path, ws_url=f"ws://127.0.0.1:{feed_port}", api_port=api_port)
            shutil.copy(REPO / "config" / "rules.yaml", tmp_path / "config" / "rules.yaml")
            cfg = load_config(tmp_path)
            files = RunFiles(cfg.run_dir)
            task = asyncio.create_task(engine.run(cfg, foreground=False))
            client = EngineClient(api_port)
            stop = asyncio.Event()
            client_task = asyncio.create_task(client.run(stop))
            try:
                await until(lambda: (read_status(files) or {}).get("messages") == 5)
                await until(lambda: client.connected)
                analysis = await client.analyze(MINT_A)
                status = await client.status()
            finally:
                stop.set()
                await client_task
                files.stop_flag.write_text("stop", encoding="utf-8")
                code = await asyncio.wait_for(task, timeout=15)
            return analysis, status, code, api_port

    analysis, status, code, api_port = asyncio.run(scenario())
    assert code == 0
    assert analysis["known"] and analysis["mint"] == MINT_A and analysis["symbol"] == "STM"
    assert analysis["level"] in ("avoid", "caution", "pass") and analysis["reasons"] is not None
    assert analysis["snapshot"]["buys"] == 1  # the creator's first buy
    assert status["state"] == "running" and status["local_api"]["port"] == api_port
