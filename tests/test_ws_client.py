"""The live-feed client against a local fake Solana WebSocket (127.0.0.1 only)."""

import asyncio
import json
import socket

from websockets.asyncio.server import serve

from agent.config import Endpoint
from agent.ingest.ws_client import LogsStream
from helpers import PUMP, PUMP_AMM, acknowledge, notification


async def collect(endpoints, want: int, programs=(PUMP,), **kwargs):
    """Run the stream until `want` notifications arrived. Returns them and the state events."""
    got, states = [], []
    stop = asyncio.Event()

    def handler(n):
        got.append(n)
        if len(got) >= want:
            stop.set()

    stream = LogsStream(
        endpoints,
        list(programs),
        reconnect_initial_s=0.05,
        reconnect_max_s=0.2,
        on_state=lambda *event: states.append(event),
        **kwargs,
    )
    await asyncio.wait_for(stream.run(handler, stop), timeout=15)
    return got, states


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def url(server) -> str:
    return f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"


def test_subscribes_and_reconnects_after_the_server_drops_the_connection():
    requests, connections = [], 0

    async def handler(ws):
        nonlocal connections
        connections += 1
        base = connections * 10
        requests.append(await acknowledge(ws))
        for i in range(3):
            await ws.send(notification(7, f"sig{base + i}", ["line"], slot=base + i))
        # returning closes the connection

    async def scenario():
        async with serve(handler, "127.0.0.1", 0) as server:
            return await collect([Endpoint("fake", url(server))], want=6)

    got, states = asyncio.run(scenario())
    assert [n.signature for n in got] == ["sig10", "sig11", "sig12", "sig20", "sig21", "sig22"]
    assert got[0].program == PUMP and got[0].slot == 10 and got[0].err is None
    assert got[0].logs == ["line"] and got[0].size > 0
    assert requests[0]["method"] == "logsSubscribe"
    assert requests[0]["params"] == [{"mentions": [PUMP]}, {"commitment": "confirmed"}]
    assert [s[0] for s in states] == ["connected", "disconnected", "connected"]


def test_reconnects_when_the_feed_goes_quiet():
    connections = 0

    async def handler(ws):
        nonlocal connections
        connections += 1
        await acknowledge(ws)
        await ws.send(notification(7, f"sig{connections}", []))
        await ws.wait_closed()  # stay connected but send nothing more

    async def scenario():
        async with serve(handler, "127.0.0.1", 0) as server:
            return await collect([Endpoint("fake", url(server))], want=2, stale_after_s=0.3)

    got, states = asyncio.run(scenario())
    assert [n.signature for n in got] == ["sig1", "sig2"]
    assert any(s[0] == "disconnected" and "no data" in s[2] for s in states)


def test_fails_over_to_the_next_source():
    async def good(ws):
        await acknowledge(ws)
        await ws.send(notification(7, "ok", []))
        await ws.wait_closed()

    async def rejecting(ws):
        request = json.loads(await ws.recv())
        await ws.send(
            json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32000}})
        )
        await ws.wait_closed()

    async def scenario():
        async with serve(good, "127.0.0.1", 0) as ok, serve(rejecting, "127.0.0.1", 0) as bad:
            endpoints = [
                Endpoint("dead", f"ws://127.0.0.1:{closed_port()}/?api-key=SECRETKEY123"),
                Endpoint("rejects", url(bad)),
                Endpoint("good", url(ok)),
            ]
            return await collect(endpoints, want=1)

    got, states = asyncio.run(scenario())
    assert [n.signature for n in got] == ["ok"]
    assert [(s[0], s[1]) for s in states] == [
        ("disconnected", "dead"),
        ("disconnected", "rejects"),
        ("connected", "good"),
    ]
    assert "subscription rejected" in states[1][2]
    assert all("SECRETKEY123" not in s[2] for s in states)


def test_two_programs_and_a_failed_transaction():
    async def handler(ws):
        first = json.loads(await ws.recv())
        second = json.loads(await ws.recv())
        await ws.send(json.dumps({"jsonrpc": "2.0", "result": 11, "id": first["id"]}))
        await ws.send(json.dumps({"jsonrpc": "2.0", "result": 22, "id": second["id"]}))
        await ws.send(notification(22, "amm", [], err={"InstructionError": [0, "Custom"]}))
        await ws.send(notification(99, "unknown-subscription", []))
        await ws.send(notification(11, "curve", []))
        await ws.wait_closed()

    async def scenario():
        async with serve(handler, "127.0.0.1", 0) as server:
            return await collect([Endpoint("fake", url(server))], want=2, programs=(PUMP, PUMP_AMM))

    got, _ = asyncio.run(scenario())
    assert [(n.signature, n.program) for n in got] == [("amm", PUMP_AMM), ("curve", PUMP)]
    assert got[0].err == {"InstructionError": [0, "Custom"]}
