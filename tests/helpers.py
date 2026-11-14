"""Shared test helpers: fake transaction logs and a throwaway project folder."""

from __future__ import annotations

import base64
import json
import textwrap
import time
from pathlib import Path

from agent.ingest.ws_client import LogNotification

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
OTHER = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
TOKEN = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


def data_line(payload: bytes) -> str:
    return "Program data: " + base64.b64encode(payload).decode()


def program_block(
    program: str, names: list[str], payloads: list[bytes], depth: int = 1
) -> list[str]:
    """Log lines for one program call: instruction names, then its events."""
    lines = [f"Program {program} invoke [{depth}]"]
    lines += [f"Program log: Instruction: {name}" for name in names]
    lines += [data_line(p) for p in payloads]
    lines += [
        f"Program {program} consumed 30000 of 200000 compute units",
        f"Program {program} success",
    ]
    return lines


def notification(subscription: int, signature: str, logs: list[str], *, slot: int = 1000,
                 err: object | None = None) -> str:
    """A `logsNotification` message exactly as a Solana RPC WebSocket sends it."""
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "logsNotification",
            "params": {
                "result": {
                    "context": {"slot": slot},
                    "value": {"signature": signature, "err": err, "logs": logs},
                },
                "subscription": subscription,
            },
        }
    )


async def acknowledge(ws, subscription: int = 7) -> dict:
    """Answer the client's logsSubscribe request the way an RPC node does."""
    request = json.loads(await ws.recv())
    await ws.send(json.dumps({"jsonrpc": "2.0", "result": subscription, "id": request["id"]}))
    return request


def make_project(root: Path, *, ws_url: str = "ws://127.0.0.1:1", env: str = "",
                 extra_sources: str = "", api_port: int = 0) -> Path:
    """Create a minimal project folder (config/settings.yaml + .env) under `root`. The
    engine's local API is off unless `api_port` is given (never the real engine's port)."""
    (root / "config").mkdir(parents=True, exist_ok=True)
    settings = textwrap.dedent(
        f"""\
        programs:
          pump: {PUMP}
          pump_amm: {PUMP_AMM}
        live:
          commitment: confirmed
          subscribe: [pump]
          sources:
            - name: test
              ws_url: {ws_url}
        """
    )
    (root / "config" / "settings.yaml").write_text(
        settings + textwrap.indent(textwrap.dedent(extra_sources), "    ")
        + "  reconnect: {initial_s: 0.05, max_s: 0.2}\n  stale_after_s: 5\n"
        + f"local_api: {{port: {api_port}}}\n",
        encoding="utf-8",
    )
    (root / ".env").write_text(env, encoding="utf-8")
    return root


FIXTURES = Path(__file__).parent / "fixtures" / "tx"


def load_fixture(name: str) -> dict:
    """A real mainnet transaction captured by tools/capture_fixtures.py."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def fixture_names() -> list[str]:
    return sorted(path.stem for path in FIXTURES.glob("*.json"))


def live(name: str, *, err: object | None = None, signature: str | None = None,
         logs: list[str] | None = None, slot: int | None = None) -> LogNotification:
    """A fixture as the live feed would deliver it."""
    tx = load_fixture(name)
    return LogNotification(
        program=PUMP,
        signature=signature or tx["signature"],
        slot=slot or tx["slot"],
        err=err,
        logs=logs if logs is not None else tx["logMessages"],
        recv_ts=time.time(),
        size=1000,
    )


CAR_FIXTURES = Path(__file__).parent / "fixtures" / "car"
CAR_SLOT = 468072129  # epoch 1083; captured by tools/capture_car_fixture.py


def car_fixture(slot: int = CAR_SLOT) -> tuple[bytes, dict]:
    """One real slot of an Old Faithful archive, and the RPC's copy of its pump.fun and
    PumpSwap transactions."""
    import zstandard

    stem = CAR_FIXTURES / f"slot-{slot}"
    data = zstandard.ZstdDecompressor().decompress(stem.with_suffix(".car.zst").read_bytes())
    info = json.loads(stem.with_suffix(".json").read_text(encoding="utf-8"))
    return data, info


CAR_EPOCH = 1083


def fixture_index_bytes(info: dict) -> bytes:
    """A slot index for the fixture's epoch in which only the fixture slot has a block."""
    import struct

    slots = 432_000
    data = bytearray(12 * slots)
    struct.pack_into("<QI", data, 12 * (info["slot"] - info["epoch"] * slots),
                     info["offset"], info["length"])
    return bytes(data)


class FakeArchive:
    """An Old Faithful server holding the fixture slot, for httpx.MockTransport."""

    def __init__(self) -> None:
        self.data, self.info = car_fixture()
        self.index = fixture_index_bytes(self.info)
        self.mode = "ok"  # ok | ignore_range | corrupt | down
        self.requests = 0

    def __call__(self, request):
        import httpx

        self.requests += 1
        if request.method == "HEAD":
            size = self.info["offset"] + self.info["length"]
            return httpx.Response(200, headers={"content-length": str(size)})
        if request.url.path.endswith("slot-ranges.raw"):
            return httpx.Response(200, content=self.index)
        if self.mode == "down":
            return httpx.Response(503)
        if self.mode == "ignore_range":
            return httpx.Response(200, content=self.data)
        first, last = (int(v) for v in request.headers["range"][6:].split("-"))
        lo = first - self.info["offset"]
        body = self.data[lo : last - self.info["offset"] + 1]
        if self.mode == "corrupt":
            body = body[:100] + bytes([body[100] ^ 0xFF]) + body[101:]
        return httpx.Response(206, content=body)

    def client(self):
        import httpx

        return httpx.Client(transport=httpx.MockTransport(self))


def run_fixture_backfill(data_dir: Path, archive: FakeArchive | None = None, **kwargs):
    """Run the real backfill runner over the fixture epoch, in this process."""
    from concurrent.futures import ThreadPoolExecutor

    from agent.ingest import oldfaithful_backfill as ob
    from agent.ingest.oldfaithful_extract import Extractor

    archive = archive or FakeArchive()
    bf = ob.Backfill(data_dir, archive.info["epoch"], base_url="https://archive.test",
                     workers=1, echo=lambda _: None, **kwargs)
    client = archive.client()
    index = bf.load_index(client)
    ob._worker.update(client=client, url=bf.car_url, rate=0.0, verify=True,
                      extractor=Extractor())
    totals = bf.run(bf.pending(index), pool_factory=lambda: ThreadPoolExecutor(1))
    return bf, index, totals
