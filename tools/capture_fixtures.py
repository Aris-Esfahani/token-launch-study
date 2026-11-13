"""Capture real transactions from the live feed to use as parser test fixtures.

Listens to the public Solana WebSocket, keeps the first transaction seen for each
instruction name of the pump.fun and PumpSwap programs (plus rare events such as a
bonding-curve completion), downloads each one from the public RPC, and saves a trimmed
copy under tests/fixtures/tx/. Run it again whenever pump.fun changes its programs.

    .venv\\Scripts\\python tools\\capture_fixtures.py --seconds 300

Read-only: it needs no API key and sends nothing but public RPC requests.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx

from agent.config import load_config
from agent.ingest.logs import event_discriminator, event_payloads, instruction_names, walk_logs
from agent.ingest.ws_client import LogNotification, LogsStream

RARE_EVENTS = ["CompleteEvent", "PostCompleteBuyEvent", "CompletePumpAmmMigrationEvent"]
RPC_URL = "https://api.mainnet.solana.com"


def trim(tx: dict) -> dict:
    """Keep only what the parser (and the later backfill parity test) needs."""
    message, meta = tx["transaction"]["message"], tx["meta"]
    return {
        "signature": tx["transaction"]["signatures"][0],
        "slot": tx["slot"],
        "blockTime": tx.get("blockTime"),
        "version": tx.get("version"),
        "err": meta["err"],
        "fee": meta.get("fee"),
        "accountKeys": message["accountKeys"],
        "loadedAddresses": meta.get("loadedAddresses"),
        "instructions": message["instructions"],
        "innerInstructions": meta.get("innerInstructions"),
        "logMessages": meta["logMessages"],
    }


async def fetch(client: httpx.AsyncClient, signature: str) -> dict | None:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            signature,
            {"encoding": "json", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"},
        ],
    }
    for attempt in range(6):
        response = await client.post(RPC_URL, json=body)
        if response.status_code == 429:
            await asyncio.sleep(3 + attempt * 2)
            continue
        result = response.json().get("result")
        if result:
            return result
        await asyncio.sleep(2)
    return None


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=300, help="longest time to listen")
    parser.add_argument("--out", type=Path, default=Path("tests/fixtures/tx"))
    args = parser.parse_args()

    cfg = load_config()
    programs = {cfg.settings.programs["pump"]: "pump", cfg.settings.programs["pump_amm"]: "amm"}
    pump = cfg.settings.programs["pump"]
    rare_tags = {event_discriminator(name): name for name in RARE_EVENTS}

    wanted: dict[str, str] = {}  # fixture key -> signature
    volume = {name: [0, 0, 0, 0] for name in programs.values()}  # msgs, bytes, failed, truncated
    seen: set[str] = set()
    duplicates = 0
    started = time.monotonic()
    stop = asyncio.Event()

    def handle(n: LogNotification) -> None:
        nonlocal duplicates
        label = programs[n.program]
        counters = volume[label]
        counters[0] += 1
        counters[1] += n.size
        if (n.program, n.signature) in seen:
            duplicates += 1
        seen.add((n.program, n.signature))
        if n.err is not None:
            counters[2] += 1
            return
        lines, truncated = walk_logs(n.logs)
        if truncated:
            counters[3] += 1
            wanted.setdefault(f"{label}-truncated", n.signature)
        for name in instruction_names(lines, n.program):
            wanted.setdefault(f"{label}-{name}", n.signature)
        if n.program == pump:
            for payload in event_payloads(lines, pump):
                if payload[:8] in rare_tags:
                    wanted.setdefault(f"pump-event-{rare_tags[payload[:8]]}", n.signature)
        elapsed = time.monotonic() - started
        have_rare = all(f"pump-event-{name}" in wanted for name in RARE_EVENTS)
        if elapsed > args.seconds or (have_rare and elapsed > 120):
            stop.set()

    endpoints, _ = cfg.endpoints()
    stream = LogsStream(endpoints, list(programs), cfg.settings.live.commitment)
    await stream.run(handle, stop)

    elapsed = time.monotonic() - started
    print(f"listened {elapsed:.0f}s; duplicate notifications: {duplicates}")
    for label, (msgs, size, failed, truncated) in volume.items():
        ok = msgs - failed
        print(
            f"  {label}: {msgs / elapsed:.1f} msg/s, {size / elapsed / 1000:.0f} KB/s "
            f"({size / elapsed * 86400 / 1e9:.1f} GB/day), failed {failed / max(msgs, 1):.0%}, "
            f"truncated logs {truncated} of {ok} successful"
        )

    args.out.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=30) as client:
        for key, signature in sorted(wanted.items()):
            tx = await fetch(client, signature)
            if tx is None:
                print(f"  could not fetch {key} ({signature[:12]}...)")
                continue
            path = args.out / f"{key}.json"
            path.write_text(json.dumps(trim(tx), indent=1), encoding="utf-8")
            print(f"  saved {path.name}")
            await asyncio.sleep(0.4)


if __name__ == "__main__":
    asyncio.run(main())
