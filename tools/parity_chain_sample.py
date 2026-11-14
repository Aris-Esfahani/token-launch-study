"""Finish a backfill parity test's chain check on a random sample (spec 4.1, closing
Phase 3).

`agent parity` asks the chain about every transaction only the recorder has. On the
public RPC that takes about 1.3 s each with the full history, so for many thousands this
tool checks a random sample instead and reports the 95% upper bound on how many of all
of them could be on chain. It also checks every trade whose slot differs between the
recorder and the backfill (getTransaction, finalized: does the chain agree with the
backfill?), and control transactions both captured, before and after.

    .venv\\Scripts\\agent parity --epoch 1084 --no-chain-check
    .venv\\Scripts\\python tools\\parity_chain_sample.py --report <saved parity JSON>

Read-only apart from the result file it saves next to the report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import time
from collections import Counter
from pathlib import Path

import duckdb

from agent.backfill_parity import rpc_check
from agent.config import load_config
from agent.ingest.rpc import Rpc, RpcError

PART = 40  # signatures per call of the chain check, so a failed call loses little


def log(text: str) -> None:
    print(f"{time.strftime('%I:%M:%S %p')}  {text}", flush=True)


async def check(rpc, sample, controls, forks) -> tuple[dict, list, dict, dict]:
    control_before = await rpc_check(rpc)(controls)
    log(f"controls before: {dict(Counter(control_before.values()))}")
    out: dict[str, str] = {}
    for i in range(0, len(sample), PART):
        part = sample[i : i + PART]
        for _ in range(3):
            try:
                out.update(await rpc_check(rpc)(part))
                break
            except RpcError as exc:
                log(f"part {i // PART} failed ({exc}); trying again in 60 s")
                await asyncio.sleep(60)
        if (i // PART) % 5 == 4:
            log(f"{len(out):,} of {len(sample):,} checked: {dict(Counter(out.values()))}")
    fork_results = []
    for signature, rec_slot, bf_slot in forks:
        try:
            tx = await rpc.call("getTransaction", [signature, {
                "maxSupportedTransactionVersion": 1, "encoding": "json",
                "commitment": "finalized"}])  # fmt: skip
            chain = tx["slot"] if tx else None
        except RpcError as exc:
            chain = f"error: {exc}"
        fork_results.append({"signature": signature, "recorder_slot": rec_slot,
                             "backfill_slot": bf_slot, "chain_slot": chain})  # fmt: skip
    control_after = await rpc_check(rpc)(controls)
    log(f"controls after: {dict(Counter(control_after.values()))}")
    return out, fork_results, control_before, control_after


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--report", type=Path, required=True, help="agent parity's saved JSON")
    ap.add_argument("--sample", type=int, default=1000)
    ap.add_argument("--controls", type=int, default=10)
    ap.add_argument("--seed", type=int, default=20261112)
    args = ap.parse_args()

    cfg = load_config()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    only = sorted({s for t in report["tables"].values() for s in t["recorder_only_signatures"]})
    rng = random.Random(args.seed)
    sample = rng.sample(only, min(args.sample, len(only)))
    lo, hi = report["start_ts"], report["end_ts"]
    raw = (cfg.data_dir / "raw" / "trades").as_posix() + "/*/*.parquet"
    bf = (cfg.data_dir / "raw_backfill" / "trades").as_posix() + "/*/*.parquet"
    db = duckdb.connect()
    for name, pattern in (("r", raw), ("b", bf)):
        db.execute(
            f"CREATE TABLE {name} AS SELECT signature, event_index, slot FROM "
            f"read_parquet('{pattern}', union_by_name=true) WHERE ts BETWEEN {lo} AND {hi}"
        )
    forks = db.execute(
        "SELECT DISTINCT r.signature, r.slot, b.slot FROM r JOIN b "
        "USING (signature, event_index) WHERE r.slot <> b.slot ORDER BY 1"
    ).fetchall()
    query = "SELECT DISTINCT signature FROM r JOIN b USING (signature) ORDER BY 1"
    both = [s for (s,) in db.execute(query).fetchall()]
    db.close()
    controls = rng.sample(both, args.controls)
    log(f"{len(only):,} recorder-only transactions, sampling {len(sample):,}; "
        f"{len(forks)} trades with a different slot; {len(controls)} controls")  # fmt: skip

    async def run():
        rpc = Rpc(cfg.rpc_url(), max_rps=2, timeout_s=45, max_attempts=4)
        try:
            return await check(rpc, sample, controls, forks)
        finally:
            await rpc.aclose()

    out, fork_results, before, after = asyncio.run(run())
    counts = Counter(out.get(s, "unchecked") for s in sample)
    on_chain = counts.get("ok", 0)
    checked = sum(v for k, v in counts.items() if k != "unchecked")
    # 95% upper bound on the share on chain when none of `checked` were: 1 - 0.05 ** (1/n).
    bound = 1 - 0.05 ** (1 / checked) if checked and on_chain == 0 else None
    fork_ok = sum(1 for f in fork_results if f["chain_slot"] == f["backfill_slot"])
    controls_ok = (len(before) == len(after) == len(controls)
                   and all(v == "ok" for v in [*before.values(), *after.values()]))  # fmt: skip
    result = {
        "epoch": report["epoch"], "seed": args.seed, "population": len(only),
        "sample": len(sample), "counts": dict(counts), "upper_bound_95_on_chain_share": bound,
        "controls_ok": controls_ok, "forks": fork_results, "forks_chain_agrees": fork_ok,
        "on_chain_in_sample": [s for s in sample if out.get(s) == "ok"],
    }  # fmt: skip
    saved = args.report.with_name(
        f"parity-e{report['epoch']}-chain-sample-{time.strftime('%Y%m%dT%H%M%S')}.json"
    )
    saved.write_text(json.dumps(result, indent=1), encoding="utf-8")
    log(f"sample: {dict(counts)}; controls ok: {controls_ok}")
    if bound is not None:
        log(f"none on chain: at most {bound:.2%} of all {len(only):,} could be (95% confidence)")
    log(f"slot differences: the chain agrees with the backfill for {fork_ok} of "
        f"{len(fork_results)}")  # fmt: skip
    log(f"saved to {saved}")


if __name__ == "__main__":
    main()
