"""The screen's connection to the engine's local API (daemon/local_api.py).

Keeps one WebSocket to 127.0.0.1 open, subscribes to alerts, and reconnects by itself
when the engine restarts. `analyze` and `status` send a request and wait for its answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from collections.abc import Callable

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from agent.daemon.local_api import HOST

RETRY_S = 2.0


class EngineOffline(Exception):
    """The engine is not running, or its local API is off."""


class EngineClient:
    def __init__(
        self,
        port: int,
        *,
        on_alert: Callable[[dict, bool], None] | None = None,
        on_connection: Callable[[bool], None] | None = None,
    ) -> None:
        self.url = f"ws://{HOST}:{port}"
        self._on_alert = on_alert or (lambda alert, old: None)
        self._on_connection = on_connection or (lambda up: None)
        self._ws: ClientConnection | None = None
        self._waiting: dict[int, asyncio.Future] = {}
        self._ids = itertools.count(1)

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def run(self, stop: asyncio.Event) -> None:
        """Stay connected until `stop` is set."""
        while not stop.is_set():
            try:
                async with connect(self.url, open_timeout=3, compression=None) as ws:
                    self._ws = ws
                    self._on_connection(True)
                    await ws.send(json.dumps({"op": "subscribe"}))
                    reader = asyncio.ensure_future(self._read(ws))
                    stopper = asyncio.ensure_future(stop.wait())
                    try:
                        await asyncio.wait({reader, stopper}, return_when=asyncio.FIRST_COMPLETED)
                    finally:  # also when the screen closes and cancels this
                        stopper.cancel()
                        reader.cancel()
                        await asyncio.gather(reader, stopper, return_exceptions=True)
            except (OSError, InvalidHandshake, TimeoutError, ConnectionClosed):
                pass
            if self._ws is not None:
                self._ws = None
                self._on_connection(False)
            self._fail_waiting()
            if not stop.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=RETRY_S)

    async def _read(self, ws: ClientConnection) -> None:
        async for raw in ws:
            try:
                message = json.loads(raw)
                op = message.get("op")
            except (ValueError, AttributeError):
                continue  # not from our engine; ignore it
            if op == "alert":
                self._on_alert(message, False)
            elif op == "alerts":
                for alert in message.get("alerts", []):
                    self._on_alert(alert, True)
            else:
                future = self._waiting.pop(message.get("id"), None)
                if future is not None and not future.done():
                    future.set_result(message)

    def _fail_waiting(self) -> None:
        for future in self._waiting.values():
            if not future.done():
                future.set_exception(EngineOffline("the engine went away"))
        self._waiting.clear()

    async def request(self, message: dict, timeout: float = 5.0) -> dict:
        ws = self._ws
        if ws is None:
            raise EngineOffline("the engine is not running")
        request_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._waiting[request_id] = future
        try:
            await ws.send(json.dumps({**message, "id": request_id}))
            return await asyncio.wait_for(future, timeout)
        except ConnectionClosed as exc:
            raise EngineOffline("the engine went away") from exc
        except TimeoutError as exc:
            raise EngineOffline(f"no answer from the engine in {timeout:.0f} s") from exc
        finally:
            self._waiting.pop(request_id, None)

    async def analyze(self, mint: str) -> dict:
        return await self.request({"op": "analyze", "mint": mint})

    async def status(self) -> dict:
        return await self.request({"op": "status"})
