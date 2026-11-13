"""Capture one slot of an Old Faithful archive as a test fixture for the backfill reader
and the parity test (tests/test_car.py, tests/test_backfill_parity.py).

Saves, under tests/fixtures/car/:
    slot-<slot>.car.zst   the slot's bytes from the epoch's CAR file (zstd-compressed)
    slot-<slot>.json      where they came from, plus every pump.fun/PumpSwap transaction
                          of the slot as the public RPC returns it (getTransaction): the
                          same log lines the live feed delivers

    .venv\\Scripts\\python tools\\capture_car_fixture.py --epoch 1083 --slot 468072129

Downloads the epoch's slot index (about 5 MB) and the slot (about 1 MB). Read-only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import httpx
import zstandard

from agent.ingest.car import SlotIndex, decompress_meta, parse_meta, read_slots, tx_header
from agent.ingest.parsers import Parser
from agent.ingest.solana import b58decode, b58encode

BASE = "https://files.old-faithful.net"
RPC_URL = "https://api.mainnet.solana.com"
OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "car"


def rpc_transaction(client: httpx.Client, signature: str) -> dict:
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
        "params": [signature, {"encoding": "json", "maxSupportedTransactionVersion": 1}],
    }  # fmt: skip
    for attempt in range(8):
        reply = client.post(RPC_URL, json=body)
        if reply.status_code == 200 and "result" in reply.json():
            result = reply.json()["result"]
            if result is not None:
                return result
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"RPC did not return {signature}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epoch", type=int, required=True)
    ap.add_argument("--slot", type=int, required=True)
    ap.add_argument("--index", type=Path, help="a downloaded epoch-<E>-slot-ranges.raw")
    args = ap.parse_args()

    client = httpx.Client(timeout=60, follow_redirects=True)
    if args.index:
        index_bytes = args.index.read_bytes()
    else:
        index_bytes = client.get(f"{BASE}/{args.epoch}/epoch-{args.epoch}-slot-ranges.raw").content
    r = SlotIndex(args.epoch, index_bytes).get(args.slot)
    if r is None:
        raise SystemExit(f"slot {args.slot} was skipped (no block)")
    data = client.get(
        f"{BASE}/{args.epoch}/epoch-{args.epoch}.car",
        headers={"Range": f"bytes={r.offset}-{r.offset + r.length - 1}"},
    ).content
    assert len(data) == r.length, (len(data), r.length)

    ids = Parser().program_ids
    wanted = {b58decode(ids["pump"]), b58decode(ids["pump_amm"])}
    nodes = read_slots(data, r.offset)
    dctx = zstandard.ZstdDecompressor()
    signatures = []
    for tx in nodes.transactions:
        header = tx_header(nodes.join(tx.data_frame))
        meta = parse_meta(decompress_meta(nodes.join(tx.meta_frame), dctx))
        if wanted & (set(header.static_keys) | set(meta.loaded_addresses)):
            signatures.append(b58encode(header.signature))

    rpc = {}
    for signature in signatures:
        tx = rpc_transaction(client, signature)
        rpc[signature] = {
            "slot": tx["slot"],
            "blockTime": tx["blockTime"],
            "version": tx.get("version"),
            "err": tx["meta"]["err"],
            "fee": tx["meta"]["fee"],
            "computeUnitsConsumed": tx["meta"].get("computeUnitsConsumed"),
            "accountKeys": tx["transaction"]["message"]["accountKeys"],
            "loadedAddresses": tx["meta"].get("loadedAddresses"),
            "logMessages": tx["meta"]["logMessages"],
        }
        time.sleep(0.4)

    OUT.mkdir(parents=True, exist_ok=True)
    stem = OUT / f"slot-{args.slot}"
    stem.with_suffix(".car.zst").write_bytes(zstandard.ZstdCompressor(level=19).compress(data))
    info = {
        "epoch": args.epoch,
        "slot": args.slot,
        "offset": r.offset,
        "length": r.length,
        "sha256": hashlib.sha256(data).hexdigest(),
        "source": f"{BASE}/{args.epoch}/epoch-{args.epoch}.car",
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rpc": rpc,
    }
    stem.with_suffix(".json").write_text(json.dumps(info, indent=1), encoding="utf-8")
    print(f"saved {stem}.car.zst and .json: {len(nodes.transactions)} transactions, "
          f"{len(rpc)} mention our programs")  # fmt: skip


if __name__ == "__main__":
    main()
