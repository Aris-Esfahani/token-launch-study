"""Minimal Solana JSON-RPC client for lookups (audits now; gap repair and wallet history
later). Polite by design: a fixed request rate, and backoff when the node says slow down.
"""

from __future__ import annotations

import asyncio
import base64
import time

import httpx

from agent.logging_setup import redact


class RpcError(Exception):
    pass


class Rpc:
    def __init__(
        self,
        url: str,  # may contain an API key: never log or print it
        *,
        max_rps: float = 3.0,
        timeout_s: float = 30.0,
        max_attempts: int = 6,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = url
        self._client = httpx.AsyncClient(timeout=timeout_s, transport=transport)
        self._min_interval = 1.0 / max_rps
        self._max_attempts = max_attempts
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self.requests = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def call(self, method: str, params: list) -> object:
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        delay = 1.0
        problem = "no attempt made"
        for _ in range(self._max_attempts):
            async with self._lock:  # one request at a time, spaced out
                wait = self._min_interval - (time.monotonic() - self._last_request)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_request = time.monotonic()
            self.requests += 1
            try:
                response = await self._client.post(self._url, json=body)
            except httpx.HTTPError as exc:
                problem = redact(f"{type(exc).__name__}: {exc}")
            else:
                if response.status_code == 200:
                    payload = response.json()
                    if "error" not in payload:
                        return payload.get("result")
                    problem = f"RPC error: {payload['error']}"
                    if payload["error"].get("code") not in (-32005, -32004, 429):
                        raise RpcError(f"{method}: {problem}")
                else:
                    problem = f"HTTP {response.status_code}"
                    if response.status_code not in (408, 429, 500, 502, 503, 504):
                        raise RpcError(f"{method}: {problem}")
                    retry_after = response.headers.get("Retry-After", "")
                    if retry_after.isdigit():
                        delay = max(delay, float(retry_after))
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
        raise RpcError(f"{method}: gave up after {self._max_attempts} attempts ({problem})")

    async def signatures_for_address(
        self, address: str, *, before: str | None = None, limit: int = 1000
    ) -> list[dict]:
        """Newest first. Each item has signature, slot, blockTime and err."""
        options: dict = {"limit": limit}
        if before:
            options["before"] = before
        return await self.call("getSignaturesForAddress", [address, options]) or []

    async def account_info(self, address: str) -> tuple[bytes, int, str] | None:
        """An account's raw data, the slot it was read at, and its owner program."""
        result = await self.call("getAccountInfo", [address, {"encoding": "base64"}])
        value = (result or {}).get("value")
        if value is None:
            return None
        return base64.b64decode(value["data"][0]), result["context"]["slot"], value["owner"]

    async def transaction(self, signature: str) -> dict | None:
        return await self.call(
            "getTransaction",
            [signature, {"encoding": "json", "maxSupportedTransactionVersion": 1}],
        )
