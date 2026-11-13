"""Live transaction logs from a Solana RPC WebSocket (`logsSubscribe`).

Keeps one connection open, reconnects with backoff when it drops or goes quiet, and moves
to the next configured source when one keeps failing. Works with any standard Solana RPC
WebSocket, so the live source can be swapped in settings.yaml without code changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from agent.config import Endpoint
from agent.logging_setup import redact

log = logging.getLogger(__name__)


@dataclass(slots=True)
class LogNotification:
    program: str  # address of the subscribed program this transaction mentions
    signature: str
    slot: int
    err: object | None  # None means the transaction succeeded
    logs: list[str]
    recv_ts: float  # wall-clock time we received it (time.time())
    size: int  # size of the websocket message, uncompressed


class StreamError(Exception):
    """The current connection is unusable; drop it and reconnect."""


# (event, source name, detail). event is "connected" or "disconnected".
StateCallback = Callable[[str, str, str], None]


class LogsStream:
    def __init__(
        self,
        endpoints: Sequence[Endpoint],
        programs: Sequence[str],
        commitment: str = "confirmed",
        *,
        reconnect_initial_s: float = 1.0,
        reconnect_max_s: float = 30.0,
        stale_after_s: float = 20.0,
        open_timeout_s: float = 15.0,
        on_state: StateCallback | None = None,
    ) -> None:
        if not endpoints:
            raise ValueError("at least one live source is required")
        if not programs:
            raise ValueError("at least one program to follow is required")
        self._endpoints = list(endpoints)
        self._programs = list(programs)
        self._commitment = commitment
        self._initial = reconnect_initial_s
        self._max = reconnect_max_s
        self._stale_after = stale_after_s
        self._open_timeout = open_timeout_s
        self._on_state = on_state
        self._delivered = 0  # notifications handled on the current connection

    async def run(
        self, handler: Callable[[LogNotification], None], stop: asyncio.Event
    ) -> None:
        """Deliver notifications to `handler` until `stop` is set. Never gives up."""
        delay = self._initial
        index = 0
        while not stop.is_set():
            endpoint = self._endpoints[index % len(self._endpoints)]
            self._delivered = 0
            try:
                await self._session(endpoint, handler, stop)
                reason = "stopped"
            except (OSError, WebSocketException, StreamError, ValueError, KeyError) as exc:
                # ValueError covers bad JSON; KeyError covers an unexpected message shape.
                reason = redact(f"{type(exc).__name__}: {exc}")
            if stop.is_set():
                break
            self._notify("disconnected", endpoint.name, reason)
            if self._delivered:
                delay = self._initial  # it was working: retry the same source quickly
            else:
                index += 1  # it never worked: try the next source, and wait longer
            log.warning("live feed %s dropped (%s); retry in %.1fs", endpoint.name, reason, delay)
            await _sleep_unless_stopped(stop, delay * random.uniform(0.75, 1.25))
            if not self._delivered:
                delay = min(delay * 2, self._max)

    async def _session(
        self, endpoint: Endpoint, handler: Callable[[LogNotification], None], stop: asyncio.Event
    ) -> None:
        async with connect(
            endpoint.url,
            open_timeout=self._open_timeout,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=1,  # public nodes are slow to confirm a close; do not wait for it
            max_size=8 * 1024 * 1024,
        ) as ws:
            # logsSubscribe accepts one address per subscription, so send one per program.
            for request_id, program in enumerate(self._programs, start=1):
                await ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": "logsSubscribe",
                            "params": [{"mentions": [program]}, {"commitment": self._commitment}],
                        }
                    )
                )
            subscriptions: dict[int, str] = {}  # subscription id -> program address
            last_message = time.monotonic()
            while not stop.is_set():
                try:
                    async with asyncio.timeout(1.0):
                        raw = await ws.recv()
                except TimeoutError:
                    if time.monotonic() - last_message > self._stale_after:
                        raise StreamError(f"no data for {self._stale_after:.0f}s") from None
                    continue
                last_message = time.monotonic()
                msg = json.loads(raw)
                if msg.get("method") == "logsNotification":
                    params = msg["params"]
                    program = subscriptions.get(params["subscription"])
                    if program is None:
                        continue
                    value = params["result"]["value"]
                    handler(
                        LogNotification(
                            program=program,
                            signature=value["signature"],
                            slot=params["result"]["context"]["slot"],
                            err=value["err"],
                            logs=value["logs"] or [],
                            recv_ts=time.time(),
                            size=len(raw),
                        )
                    )
                    self._delivered += 1
                elif "error" in msg:
                    raise StreamError(f"subscription rejected: {msg['error']}")
                elif "id" in msg and "result" in msg:
                    subscriptions[msg["result"]] = self._programs[msg["id"] - 1]
                    if len(subscriptions) == len(self._programs):
                        self._notify("connected", endpoint.name, "")

    def _notify(self, event: str, source: str, detail: str) -> None:
        if self._on_state is not None:
            self._on_state(event, source, detail)


async def _sleep_unless_stopped(stop: asyncio.Event, seconds: float) -> None:
    try:
        async with asyncio.timeout(seconds):
            await stop.wait()
    except TimeoutError:
        pass
