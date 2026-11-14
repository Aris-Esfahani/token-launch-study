"""The engine's local API, for the screen (spec 3 and 12): one WebSocket on 127.0.0.1.

Each message is a JSON object with an "op":

- `{"op": "analyze", "id": 1, "mint": "<address>"}` gets back
  `{"op": "analysis", "id": 1, ...}`: the token's signals and verdict, from memory.
- `{"op": "subscribe"}` gets back `{"op": "alerts", "alerts": [...]}` (the latest ones),
  then `{"op": "alert", ...}` for every new alert while the connection stays open.
- `{"op": "status", "id": 2}` gets back `{"op": "status", "id": 2, ...}`: the same
  numbers `agentd status` shows.

It listens on 127.0.0.1 only, so nothing outside this PC can reach it, and it refuses
connections from web pages: a browser always sends an Origin header and the screen
sends none. It only reads the engine's memory and never changes what the engine does.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from collections.abc import Callable

from websockets.asyncio.server import Server, ServerConnection, broadcast, serve
from websockets.exceptions import ConnectionClosed

from agent.ingest.solana import is_address

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
KEEP_ALERTS = 50  # alerts a newly opened screen gets straight away
MAX_MESSAGE = 4096  # requests are tiny; refuse anything bigger


class LocalApi:
    def __init__(
        self,
        analyze: Callable[[str], dict],
        status: Callable[[], dict],
        *,
        keep_alerts: int = KEEP_ALERTS,
    ) -> None:
        self._analyze = analyze
        self._status = status
        self._subscribers: set[ServerConnection] = set()
        self._server: Server | None = None
        self.alerts: deque[dict] = deque(maxlen=keep_alerts)
        self.port: int | None = None
        self.requests = 0
        self.analyze_ms: deque[float] = deque(maxlen=500)  # engine-side time per lookup

    async def start(self, port: int) -> int:
        """Start listening; returns the port (useful with port 0, which picks a free one)."""
        self._server = await serve(
            self._handle, HOST, port, origins=[None], max_size=MAX_MESSAGE, compression=None
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def clients(self) -> int:
        return len(self._server.connections) if self._server is not None else 0

    def publish(self, alert: dict) -> None:
        """Send a new alert to every open screen (does not wait for slow ones)."""
        self.alerts.append(alert)
        if self._subscribers:
            broadcast(self._subscribers, json.dumps({"op": "alert", **alert}))

    def stats(self) -> dict:
        times = sorted(self.analyze_ms)
        return {
            "port": self.port,
            "clients": self.clients,
            "requests": self.requests,
            "median_analyze_ms": round(times[len(times) // 2], 2) if times else None,
        }

    async def _handle(self, ws: ServerConnection) -> None:
        try:
            async for raw in ws:
                reply = self.reply(raw, ws)
                if reply is not None:
                    await ws.send(json.dumps(reply))
        except ConnectionClosed:
            pass
        finally:
            self._subscribers.discard(ws)

    def reply(self, raw: str | bytes, ws: ServerConnection | None = None) -> dict | None:
        try:
            message = json.loads(raw)
            op = message.get("op")
        except (ValueError, AttributeError):
            return {"op": "error", "error": "send a JSON object with an op"}
        self.requests += 1
        request_id = message.get("id")
        if op == "analyze":
            mint = message.get("mint")
            if not is_address(mint):
                return {"op": "error", "id": request_id, "error": "not a Solana address"}
            began = time.perf_counter()
            try:
                result = self._analyze(mint)
            except Exception:  # noqa: BLE001 - a failed lookup must not touch recording
                log.exception("local API: analyze failed")
                return {"op": "error", "id": request_id, "error": "the engine could not analyze it"}
            self.analyze_ms.append((time.perf_counter() - began) * 1000)
            return {"op": "analysis", "id": request_id, **result}
        if op == "subscribe":
            if ws is not None:
                self._subscribers.add(ws)
            return {"op": "alerts", "alerts": list(self.alerts)}
        if op == "status":
            return {"op": "status", "id": request_id, **self._status()}
        return {"op": "error", "id": request_id, "error": f"unknown op {op!r}"}
