"""`agent` command: offline tools (audit, chain snapshots, curve check, backfill)."""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from datetime import UTC, datetime
from typing import Annotated

import httpx
import typer

from agent import clock
from agent.audit import AuditResult, recorded_span, run_audit
from agent.backfill_parity import ParityResult, rpc_check, run_parity
from agent.config import load_config
from agent.ingest.borsh_idl import Idl
from agent.ingest.car import SlotIndex
from agent.ingest.oldfaithful_backfill import Backfill, BackfillError, measurements
from agent.ingest.parsers import Parser
from agent.ingest.rpc import Rpc, RpcError
from agent.sim.chain_config import QuoteKinds, fetch_snapshot, load_snapshots, save_snapshot
from agent.sim.verify import (
    MIN_EXACT_SHARE,
    MIN_WITHIN_TOLERANCE_SHARE,
    QUOTE_TOLERANCE,
    TOKEN_TOLERANCE,
    verify,
)

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Agent tools.")

TARGET_MISSED_SHARE = 0.01  # Phase 1: fewer than 1% of tokens missed
EDGE_MARGIN_S = 5


@app.callback()
def _root() -> None:
    """Agent tools."""


@app.command()
def audit(
    hours: Annotated[float, typer.Option(help="How far back to check.")] = 24.0,
    trades_sample: Annotated[
        int, typer.Option(help="Also check every trade of this many random tokens.")
    ] = 0,
) -> None:
    """Compare what was recorded with what is on chain, and report anything missed."""
    cfg = load_config()
    span = recorded_span(cfg.data_dir / "raw")
    if span is None:
        typer.echo("Nothing has been recorded yet. Run `agentd start` first.")
        raise typer.Exit(1)
    # Stay inside what was recorded (a few seconds in from each end: the first and last
    # seconds of a recording are only partly covered), and 2 minutes behind now so the
    # chain has settled.
    end = min(time.time() - 120, span[1] - EDGE_MARGIN_S)
    start = max(end - hours * 3600, span[0] + EDGE_MARGIN_S)
    if end - start < 60:
        typer.echo("Less than a minute of settled data so far. Try again in a few minutes.")
        raise typer.Exit(1)
    marker = Idl.load("pump").account_address("create_v2", "mint_authority")

    async def go() -> AuditResult:
        rpc = Rpc(cfg.rpc_url(), max_rps=cfg.settings.rpc.max_rps)
        try:
            return await run_audit(
                cfg.data_dir, rpc, Parser(), marker,
                start=start, end=end, trades_sample=trades_sample, progress=typer.echo,
            )  # fmt: skip
        finally:
            await rpc.aclose()

    try:
        result = asyncio.run(go())
    except RpcError as exc:
        typer.echo(f"The audit could not finish: {exc}")
        raise typer.Exit(1) from None

    folder = cfg.data_dir / "audits"
    folder.mkdir(parents=True, exist_ok=True)
    saved = folder / f"audit-{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
    saved.write_text(json.dumps(result.to_json(), indent=1), encoding="utf-8")

    typer.echo("")
    typer.echo(f"Window ({clock.zone()}):                {_local(start)} to {_local(end)}"
               f"  ({(end - start) / 3600:.2f} h)")  # fmt: skip
    typer.echo(f"Creates on chain:            {result.onchain_creates:,}")
    typer.echo(f"Recorded:                    {result.recorded_creates:,}")
    typer.echo(
        f"Missed outside logged gaps:  {len(result.missed_outside_gaps):,}"
        f"  ({result.missed_share:.2%}; target under {TARGET_MISSED_SHARE:.0%})"
    )
    typer.echo(f"Missed inside logged gaps:   {len(result.missed_inside_gaps):,}")
    typer.echo(f"Recorded but not on chain:   {len(result.recorded_not_onchain):,}")
    typer.echo(f"Could not be checked:        {len(result.unfetched):,}")
    gap_seconds = sum(g["end_ts"] - g["start_ts"] for g in result.gaps)
    typer.echo(f"Gaps in window:              {len(result.gaps)}  ({gap_seconds:.0f}s in total)")
    if trades_sample:
        typer.echo(
            f"Trades, {result.trade_sample_tokens} sample tokens:    "
            f"{result.onchain_trades:,} on chain, {len(result.missed_trades):,} missed"
            f"  ({result.missed_trade_share:.2%}); "
            f"{len(result.recorded_trades_not_onchain):,} recorded but not on chain"
        )
    typer.echo(f"RPC requests used:           {result.rpc_requests:,}")
    typer.echo(f"Details saved to:            {saved}")
    passed = result.missed_share < TARGET_MISSED_SHARE and not result.unfetched
    typer.echo(f"Result: {'PASS' if passed else 'NOT PASSED'}")
    raise typer.Exit(0 if passed else 1)


def _local(epoch: float) -> str:
    return clock.stamp(epoch)


@app.command("snapshot-config")
def snapshot_config() -> None:
    """Save pump.fun's curve parameters and fee schedule, read from chain right now."""
    cfg = load_config()

    async def go() -> dict:
        rpc = Rpc(cfg.rpc_url(), max_rps=cfg.settings.rpc.max_rps)
        try:
            return await fetch_snapshot(rpc)
        finally:
            await rpc.aclose()

    try:
        snapshot = asyncio.run(go())
    except (RpcError, ValueError) as exc:
        typer.echo(f"Could not read the accounts: {exc}")
        raise typer.Exit(1) from None
    path = save_snapshot(cfg.data_dir, snapshot)
    g = snapshot["accounts"]["global"]["values"]
    fees = snapshot["accounts"]["fee_config"]["values"]
    typer.echo(f"Saved {path}")
    typer.echo(
        f"Curve: starts at {int(g['initial_virtual_sol_reserves']) / 1e9:g} SOL virtual, "
        f"{int(g['initial_real_token_reserves']):,} of {int(g['token_total_supply']):,} "
        f"token units for sale; migration fee {int(g['pool_migration_fee']) / 1e9:g} SOL"
    )
    for tier in fees["fee_tiers"]:
        f = tier["fees"]
        typer.echo(
            f"Fee tier from {int(tier['market_cap_lamports_threshold']) / 1e9:g} SOL market cap: "
            f"protocol {f['protocol_fee_bps']} bps, creator {f['creator_fee_bps']} bps"
        )
    typer.echo(
        f"Coins may set their own creator fee: {bool(g['creator_fee_configurable'])} "
        f"(up to {g['max_configurable_creator_fee_bps']} bps)"
    )


@app.command("verify-curve")
def verify_curve(
    hours: Annotated[float, typer.Option(help="How far back to check.")] = 24.0,
) -> None:
    """Re-run recorded trades with our curve math and compare with what the chain did."""
    cfg = load_config()
    span = recorded_span(cfg.data_dir / "raw")
    if span is None:
        typer.echo("Nothing has been recorded yet. Run `agentd start` first.")
        raise typer.Exit(1)
    end = span[1]
    start = max(end - hours * 3600, span[0])
    chain = cfg.settings.chain
    kinds = QuoteKinds(frozenset(chain.sol_like_quote_mints), frozenset(chain.stable_quote_mints))
    snapshots = load_snapshots(cfg.data_dir)
    result = verify(cfg.data_dir, start, end, kinds, snapshots)

    folder = cfg.data_dir / "audits"
    folder.mkdir(parents=True, exist_ok=True)
    saved = folder / f"verify-curve-{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
    saved.write_text(json.dumps(result.to_json(), indent=1), encoding="utf-8")

    typer.echo(f"Window ({clock.zone()}): {_local(start)} to {_local(end)}  "
               f"({(end - start) / 3600:.2f} h)")  # fmt: skip
    typer.echo("")
    typer.echo(f"{'Trade kind':28s}{'trades':>10s}{'exact':>10s}{'in tolerance':>14s}")
    rows = sorted(result.buckets.items(), key=lambda kv: -kv[1].trades)
    for name, b in [*rows, ("all", result.total)]:
        typer.echo(
            f"{name:28s}{b.trades:>10,}{b.exact_share:>10.3%}{b.within_share:>14.3%}"
        )
    total = result.total
    typer.echo("")
    typer.echo(
        f"Largest difference: {total.worst_quote_diff} base unit(s) of quote or fee, "
        f"{total.worst_token_diff:.2e} of the token amount"
    )
    typer.echo(
        f"Not compared: {result.agent_trades:,} trades by pump.fun's mayhem agent "
        "(its own program prices them)"
    )
    if snapshots:
        for kind, (checked, matched) in sorted(result.schedule.items()):
            share = matched / checked if checked else 1.0
            typer.echo(
                f"Fee rates from the chain snapshot vs. charged, {kind}: "
                f"{matched:,} of {checked:,} ({share:.2%})"
            )
        if result.multi_hop_fee_trades:
            typer.echo(
                f"  ({result.multi_hop_fee_trades:,} multi-hop route trades left out: a route "
                "charges each fee once, not per coin)"
            )
    else:
        typer.echo("Fee schedule not checked: no chain snapshot yet (run `agent snapshot-config`).")
    for kind, (checked, ok) in sorted(result.continuity.items()):
        share = ok / checked if checked else 1.0
        typer.echo(
            f"Continuity, {kind}: {ok:,} of {checked:,} trades ({share:.2%}) start where a "
            "recorded event left the curve"
        )
    typer.echo(f"Details saved to: {saved}")
    typer.echo(
        f"Result: {'PASS' if result.passed else 'NOT PASSED'} (needs {MIN_EXACT_SHARE:.1%} exact "
        f"and {MIN_WITHIN_TOLERANCE_SHARE:.2%} within {QUOTE_TOLERANCE} unit of quote and "
        f"{TOKEN_TOLERANCE:g} of tokens)"
    )
    raise typer.Exit(0 if result.passed else 1)



# --- backfill (Phase 3) ------------------------------------------------------------------


def _backfill(cfg, epoch: int, workers: int | None = None,
              max_mb_s: float | None = None) -> Backfill:
    b = cfg.settings.backfill
    return Backfill(
        cfg.data_dir, epoch, base_url=b.base_url,
        workers=workers or b.workers,
        max_mb_s=b.max_mb_s if max_mb_s is None else max_mb_s,
        chunk_slots=b.chunk_slots, commit_chunks=b.commit_chunks,
        min_free_gb=cfg.settings.recorder.min_free_gb, echo=typer.echo,
    )  # fmt: skip


def _print_measurements(m: dict) -> None:
    done, total = m["chunks_done"], m.get("epoch_chunks")
    failed = m["chunks_failed"]
    typer.echo(f"Chunks done:            {done:,}" + (f" of {total:,}" if total else "")
               + (f"; {failed:,} failed (in the gap log)" if failed else ""))
    if m.get("epoch_gb"):
        typer.echo(f"Epoch size:             {m['epoch_gb']:,.1f} GB")
    typer.echo(f"Downloaded:             {m['downloaded_gb']:,.2f} GB")
    typer.echo(f"Kept on disk:           {m['kept_gb']:,.3f} GB")
    if m["download_mb_s"]:
        typer.echo(f"Time:                   {m['run_wall_hours']:.2f} h "
                   f"({m['download_mb_s']:.0f} MB/s on average)")  # fmt: skip
    if m["decode_mb_s_per_core"]:
        typer.echo(f"Processing:             {m['cpu_hours']:.2f} CPU hours "
                   f"({m['decode_mb_s_per_core']:.0f} MB/s per core)")  # fmt: skip
    typer.echo(f"Chain time covered:     {m['chain_hours_covered']:.2f} h")
    typer.echo(f"Transactions read:      {m['transactions']:,}; kept {m['kept_transactions']:,} "
               f"(pump.fun or PumpSwap, successful); {m['failed_dropped']:,} failed ones dropped")
    rows = ", ".join(f"{k} {v:,}" for k, v in m["rows"].items())
    typer.echo(f"Rows:                   {rows or 'none'}")
    typer.echo(f"Problems:               {m['unreadable']:,} unreadable transactions, "
               f"{m['decode_errors']:,} undecodable events, {m['truncated']:,} cut-off logs")
    per_day = m.get("per_chain_day")
    if per_day:
        typer.echo(f"Per day of chain time:  {per_day['downloaded_gb']:,.0f} GB downloaded, "
                   f"{per_day['kept_gb']:.2f} GB kept, {per_day['cpu_hours']:.1f} CPU hours")


@app.command()
def backfill(
    epoch: Annotated[int, typer.Option(help="Old Faithful epoch to read.")],
    from_slot: Annotated[int | None, typer.Option(help="Only chunks from this slot.")] = None,
    to_slot: Annotated[int | None, typer.Option(help="Only chunks up to this slot.")] = None,
    workers: Annotated[int | None, typer.Option(help="Parallel downloads.")] = None,
    max_mb_s: Annotated[
        float | None, typer.Option(help="Speed limit in MB/s for all downloads (0 = none).")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Start without asking.")] = False,
    status: Annotated[bool, typer.Option("--status", help="Only show what is done.")] = False,
    wait: Annotated[
        bool, typer.Option("--wait", help="If the epoch is not in the archive yet, check "
                           "every 10 minutes and start when it is.")
    ] = False,
) -> None:
    """Download one epoch of history from Old Faithful and keep the pump.fun rows.

    Asks before downloading (an epoch is about 400 GB). Stop any time with Ctrl+C; run
    the same command again to continue."""
    cfg = load_config()
    bf = _backfill(cfg, epoch, workers, max_mb_s)
    while True:
        try:
            with httpx.Client(timeout=60, follow_redirects=True) as client:
                index = bf.load_index(client)
                problem = None if status else bf.car_problem(client, index)
        except (BackfillError, httpx.HTTPError) as exc:
            problem = str(exc) if "not in the archive" in str(exc) else None
            if problem is None:
                typer.echo(f"Could not read the epoch's slot index: {exc}")
                raise typer.Exit(1) from None
        if problem is None:
            break
        if not wait:
            typer.echo(f"Epoch {epoch}: {problem}. Try later, or add --wait.")
            raise typer.Exit(1)
        typer.echo(f"{clock.clock(time.time())} epoch {epoch}: {problem}; checking again in "
                   "10 minutes (Ctrl+C to give up).")  # fmt: skip
        time.sleep(600)
    if status:
        _print_measurements(measurements(bf, index))
        return
    pending = bf.pending(index, from_slot, to_slot)
    size = sum(c.size for c in pending)
    free = shutil.disk_usage(cfg.data_dir).free / 1e9
    done = len(bf.progress().done)
    typer.echo(f"Epoch {epoch}: slots {index.first_slot:,}-{index.last_slot:,}; "
               f"{done:,} chunks done earlier.")  # fmt: skip
    if not pending:
        typer.echo("Nothing left to do in that range.")
        _print_measurements(measurements(bf, index))
        return
    limit = f"{bf.max_mb_s:g} MB/s" if bf.max_mb_s else "no limit"
    typer.echo(f"To do: {len(pending):,} chunks, {size / 1e9:,.1f} GB to download "
               f"({bf.workers} workers, speed limit {limit}).")  # fmt: skip
    typer.echo(f"Data drive: {free:,.1f} GB free. Only pump.fun rows are kept "
               "(a few GB per epoch).")  # fmt: skip
    if not yes and not typer.confirm("Start the download?", default=False):
        raise typer.Exit(1)
    totals = bf.run(pending)
    m = measurements(bf, index)
    report = bf.folder / "report.json"
    report.write_text(json.dumps(m, indent=1), encoding="utf-8")
    typer.echo("")
    typer.echo("Stopped early; run the same command to continue." if totals.stopped
               else "Finished.")  # fmt: skip
    _print_measurements(m)
    typer.echo(f"Details saved to: {report}")
    raise typer.Exit(1 if m["chunks_failed"] else 0)


@app.command()
def parity(
    epoch: Annotated[int, typer.Option(help="Backfilled epoch to compare.")],
    chain_check: Annotated[
        bool, typer.Option(help="Ask the RPC about rows only the recorder has.")
    ] = True,
) -> None:
    """Backfill parity test (spec 4.1): rows both the recorder and the backfill captured
    must be identical."""
    cfg = load_config()
    bf = _backfill(cfg, epoch)
    index_path = bf.folder / "slot-ranges.raw"
    if not index_path.exists():
        typer.echo(f"Epoch {epoch} has not been backfilled yet (agent backfill --epoch {epoch}).")
        raise typer.Exit(1)
    first_slot = SlotIndex(epoch, index_path.read_bytes()).first_slot

    async def go() -> ParityResult:
        rpc = Rpc(cfg.rpc_url(), max_rps=cfg.settings.rpc.max_rps) if chain_check else None
        try:
            return await run_parity(
                cfg.data_dir, epoch, chunk_slots=bf.chunk_slots, first_slot=first_slot,
                check=(lambda r: rpc_check(rpc, r.controls)) if rpc else None,
            )  # fmt: skip
        finally:
            if rpc:
                await rpc.aclose()

    try:
        result = asyncio.run(go())
    except RpcError as exc:
        typer.echo(f"The chain check could not finish: {exc}")
        raise typer.Exit(1) from None
    folder = cfg.data_dir / "audits"
    folder.mkdir(parents=True, exist_ok=True)
    saved = folder / f"parity-e{epoch}-{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
    saved.write_text(json.dumps(result.to_json(), indent=1, default=str), encoding="utf-8")
    if result.note:
        typer.echo(f"Nothing compared: {result.note}.")
        raise typer.Exit(1)
    if result.start_ts:
        typer.echo(f"Overlap ({clock.zone()}): {_local(result.start_ts)} to "
                   f"{_local(result.end_ts)}, "
                   f"{len(result.windows)} slot range(s)")  # fmt: skip
    typer.echo("")
    typer.echo(f"{'Table':20s}{'in both':>11s}{'differ':>8s}{'only rec.':>11s}"
               f"{'only backfill: in gaps':>24s}{'outside':>9s}{'(share)':>9s}")  # fmt: skip
    for name, t in result.tables.items():
        typer.echo(f"{name:20s}{t.both:>11,}{t.mismatched:>8,}{t.recorder_only:>11,}"
                   f"{t.backfill_only_in_gaps:>24,}{t.backfill_only_outside_gaps:>9,}"
                   f"{t.silent_miss_share:>9.2%}")  # fmt: skip
    typer.echo("")
    for name, t in result.tables.items():
        if t.mismatched:
            typer.echo(f"{name}: columns that differ: {t.mismatched_columns}")
        if t.duplicate_keys:
            typer.echo(f"{name}: repeated rows: {t.duplicate_keys}")
    status = result.chain_status
    if status or result.unchecked:
        missing = sum(1 for v in status.values() if v == "missing")
        typer.echo(f"Transactions only the recorder has: {len(status) + len(result.unchecked):,}; "
                   f"not on chain {missing:,}, on chain {len(result.recorder_only_on_chain):,}, "
                   f"not checked {len(result.unchecked):,}")  # fmt: skip
    typer.echo(f"Details saved to: {saved}")
    typer.echo(f"Result: {'PASS' if result.passed else 'NOT PASSED'}")
    raise typer.Exit(0 if result.passed else 1)


@app.command()
def signals(
    hours: Annotated[float, typer.Option(help="Tokens created in this many recent hours.")] = 2.0,
    at: Annotated[int, typer.Option(help="Seconds after creation to judge each token.")] = 60,
    show: Annotated[int, typer.Option(help="List this many of the newest tokens.")] = 15,
) -> None:
    """Replay recorded tokens through the live signals and the rules (config/rules.yaml),
    and show the verdicts, plus how often tokens with each verdict died or graduated."""
    # Imported here so this command's code is not loaded by other commands (the backfill's
    # worker processes import this module).
    from collections import Counter

    from agent.features.replay import OUTCOME_WINDOW_S, load_states, outcomes, snapshots
    from agent.models.rules import LEVELS, PASS, RulesError, judge, load_rules

    cfg = load_config()
    try:
        rules = load_rules(cfg.root / "config" / "rules.yaml")
    except (OSError, RulesError) as exc:
        typer.echo(f"Could not read the rules: {exc}")
        raise typer.Exit(1) from None
    span = recorded_span(cfg.data_dir / "raw")
    if span is None:
        typer.echo("Nothing has been recorded yet. Run `agentd start` first.")
        raise typer.Exit(1)
    end = int(span[1]) - at
    start = int(max(span[0], end - hours * 3600))
    replay = load_states(cfg.data_dir, start, end, max_offset_s=at, after_s=OUTCOME_WINDOW_S)
    rows = snapshots(replay, (at,))
    clean = [r for r in rows if not r["has_gap"]]
    try:
        verdicts = {r["mint"]: judge(r, rules) for r in clean}
    except RulesError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from None
    levels = (*LEVELS, PASS)
    typer.echo(f"Tokens created {clock.when(start)} to {clock.when(end)} "
               f"({(end - start) / 3600:.1f} h): "
               f"{len(replay.states):,}; judged at {at} s after creation: {len(clean):,} "
               f"({len(rows) - len(clean):,} more left out: a recording gap touched them)")
    if not clean:
        return
    typer.echo("")
    for level in levels:
        group = [v for v in verdicts.values() if v.level == level]
        reasons = Counter(v.fired[0].summary for v in group if v.fired)
        top = "; ".join(f"{r} ({n:,})" for r, n in reasons.most_common(3))
        typer.echo(f"  {level:8s}{len(group):>7,} ({len(group) / len(clean):>4.0%})"
                   f"{'   most often: ' + top if top else ''}")  # fmt: skip

    later = outcomes(replay)
    judged = {m: o for m, o in later.items() if m in verdicts}
    typer.echo("")
    if judged:
        typer.echo(f"What happened next, for the {len(judged):,} tokens whose first hour was "
                   "recorded (gaps under 10% of it):")  # fmt: skip
        typer.echo(f"  {'':8s}{'tokens':>8s}{'dead at 30 min':>17s}{'graduated in 1 h':>19s}")
        for level in levels:
            group = [o for m, o in judged.items() if verdicts[m].level == level]
            if group:
                dead = sum(o["is_dead"] for o in group) / len(group)
                grad = sum(o["graduated_1h"] for o in group) / len(group)
                typer.echo(f"  {level:8s}{len(group):>8,}{dead:>17.0%}{grad:>19.1%}")
        typer.echo("  (Rules are starting guesses; this is the first check of how they sort.)")
    else:
        typer.echo("No token's first hour is fully recorded yet; outcomes come later.")

    typer.echo("")
    typer.echo(f"Newest {min(show, len(clean))} (mint first, as Padre's search wants it):")
    for r in sorted(clean, key=lambda r: r["cutoff_ts"], reverse=True)[:show]:
        v = verdicts[r["mint"]]
        mcap = f"{r['market_cap_sol']:.0f} SOL" if r["market_cap_sol"] is not None else "-"
        typer.echo(f"  {r['mint']}  {v.level:7s} {clock.clock(r['cutoff_ts'] - at):>8s}  "
                   f"buyers {r['unique_buyers']:>3}  top10 {r['top10_hold_pct']:>3.0f}%  "
                   f"dev {r['dev_hold_pct']:>3.0f}%  snipers {r['sniper_hold_pct']:>3.0f}%  "
                   f"mcap {mcap}")  # fmt: skip
        if v.reasons:
            typer.echo(f"      {'; '.join(v.reasons)}")


@app.command("check-live")
def check_live(
    hours: Annotated[float, typer.Option(help="Verdicts made in this many recent hours.")] = 2.0,
    show: Annotated[int, typer.Option(help="List this many differences.")] = 5,
) -> None:
    """Check the engine's live verdicts against a replay of the recorded data (spec 15.0
    step C): each token's signals at each cutoff must be identical."""
    # Imported here so this command's code is not loaded by other commands (the backfill's
    # worker processes import this module).
    from collections import Counter

    import duckdb

    from agent.features.replay import load_states

    cfg = load_config()
    folder = cfg.data_dir / "live" / "verdicts"
    if not any(folder.glob("*/*.parquet")):
        typer.echo("No live verdicts yet. They come from `agentd start` with config/rules.yaml.")
        raise typer.Exit(1)
    since = time.time() - hours * 3600
    with duckdb.connect() as db:
        cursor = db.execute(
            "SELECT mint, ts, created_ts, offset_s, has_gap, snapshot FROM read_parquet(?, "
            "union_by_name=true) WHERE decided_at >= ? ORDER BY ts",
            [f"{folder.as_posix()}/*/*.parquet", since],
        )
        verdicts = cursor.fetchall()
    if not verdicts:
        typer.echo(f"No live verdicts in the last {hours:g} h.")
        raise typer.Exit(1)
    created = [v[2] for v in verdicts]
    replay = load_states(cfg.data_dir, min(created), max(created) + 1,
                         max_offset_s=max(v[3] for v in verdicts))  # fmt: skip
    states = {s.mint: s for s in replay.states}
    same, differ, missing, gap_flags = 0, [], 0, 0
    keys: Counter = Counter()
    for mint, ts, _, offset, has_gap, snapshot in verdicts:
        state = states.get(mint)
        if state is None:
            missing += 1
            continue
        live = json.loads(snapshot)
        replayed = state.snapshot(ts, creators=replay.creators)
        if has_gap != replay.has_gap(state, ts):
            gap_flags += 1
        if live == replayed:
            same += 1
            continue
        changed = sorted(k for k in replayed if live.get(k) != replayed[k])
        keys.update(changed)
        differ.append((mint, offset, changed))
    checked = same + len(differ)
    typer.echo(f"Live verdicts in the last {hours:g} h: {len(verdicts):,}; checked against a "
               f"replay: {checked:,}")  # fmt: skip
    typer.echo(f"  identical: {same:,} ({same / checked:.2%})" if checked else "  identical: -")
    typer.echo(f"  different: {len(differ):,}")
    if missing:
        typer.echo(f"  token not in the recorded tokens: {missing:,}")
    if gap_flags:
        typer.echo(f"  gap flag differs: {gap_flags:,} (a gap logged after the verdict was made)")
    if keys:
        typer.echo("  signals that differ most often: "
                   + ", ".join(f"{k} ({n:,})" for k, n in keys.most_common(6)))  # fmt: skip
        for mint, offset, changed in differ[:show]:
            typer.echo(f"    {mint}  T+{offset}s: {', '.join(changed)}")
    if differ:
        raise typer.Exit(1)
