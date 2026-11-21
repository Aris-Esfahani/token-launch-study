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
from agent.ingest.oldfaithful_backfill import (
    Backfill,
    BackfillError,
    measurements,
    parse_epochs,
    temporary_problem,
)
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
def screen() -> None:
    """Open the screen: live alerts, and the analysis of any mint you paste or copy.

    Keep it beside Padre. It needs the engine running (`agentd start`); it holds no
    data itself, so it can be closed and opened again at any time."""
    cfg = load_config()
    port = cfg.settings.local_api.port
    if not port:
        typer.echo("local_api.port is 0 in config/settings.yaml: the screen's connection is off.")
        raise typer.Exit(1)
    try:
        from agent.tui.app import run_screen
    except ModuleNotFoundError as exc:
        if exc.name != "textual":
            raise
        typer.echo("The screen needs the Textual package. Install it with:\n"
                   "  .venv\\Scripts\\pip install -e .")  # fmt: skip
        raise typer.Exit(1) from None
    run_screen(port, cfg.run_dir)


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
    typer.echo(f"Layout check:           {m['unknown_events']:,} unknown events, "
               f"{m['stale_idl_events']:,} events longer than our layout files")
    per_day = m.get("per_chain_day")
    if per_day:
        typer.echo(f"Per day of chain time:  {per_day['downloaded_gb']:,.0f} GB downloaded, "
                   f"{per_day['kept_gb']:.2f} GB kept, {per_day['cpu_hours']:.1f} CPU hours")


STOPPED_EXIT = 130  # stopped early (Ctrl+C, or the data drive almost full)
WAIT_S = 600


def _backfill_epoch(cfg, epoch: int, *, from_slot: int | None, to_slot: int | None,
                    workers: int | None, max_mb_s: float | None, status: bool, wait: bool,
                    ask: bool) -> str:  # fmt: skip
    """One epoch of `agent backfill`: 'done', 'failed' (chunks left in the gap log, tried
    again next run) or 'stopped'."""
    bf = _backfill(cfg, epoch, workers, max_mb_s)
    while True:
        try:
            with httpx.Client(timeout=60, follow_redirects=True) as client:
                index = bf.load_index(client)
                problem = None if status else bf.car_problem(client, index)
        except (BackfillError, httpx.HTTPError) as exc:
            if "not in the archive" in str(exc):
                problem = str(exc)
            else:  # when waiting anyway, a rate limit or network hiccup is waited out too
                problem = temporary_problem(exc) if wait else None
            if problem is None:
                typer.echo(f"Could not read the epoch's slot index: {exc}")
                raise typer.Exit(1) from None
        if problem is None:
            break
        if not wait:
            typer.echo(f"Epoch {epoch}: {problem}. Try later, or add --wait.")
            raise typer.Exit(1)
        typer.echo(f"{clock.clock(time.time())} epoch {epoch}: {problem}; checking again in "
                   "10 minutes (Ctrl+C to stop).")  # fmt: skip
        time.sleep(WAIT_S)
    if status:
        typer.echo(f"Epoch {epoch}:")
        _print_measurements(measurements(bf, index))
        return "done"
    pending = bf.pending(index, from_slot, to_slot)
    size = sum(c.size for c in pending)
    free = shutil.disk_usage(cfg.data_dir).free / 1e9
    done = len(bf.progress().done)
    typer.echo(f"Epoch {epoch}: slots {index.first_slot:,}-{index.last_slot:,}; "
               f"{done:,} chunks done earlier.")  # fmt: skip
    if not pending:
        typer.echo("Nothing left to do in that range.")
        _print_measurements(measurements(bf, index))
        return "done"
    limit = f"{bf.max_mb_s:g} MB/s" if bf.max_mb_s else "no limit"
    typer.echo(f"To do: {len(pending):,} chunks, {size / 1e9:,.1f} GB to download "
               f"({bf.workers} workers, speed limit {limit}).")  # fmt: skip
    typer.echo(f"Data drive: {free:,.1f} GB free. Only pump.fun rows are kept "
               "(a few GB per epoch).")  # fmt: skip
    if ask and not typer.confirm("Start the download?", default=False):
        raise typer.Exit(1)
    totals = bf.run(pending)
    m = measurements(bf, index)
    report = bf.folder / "report.json"
    report.write_text(json.dumps(m, indent=1), encoding="utf-8")
    typer.echo("")
    typer.echo(f"Epoch {epoch} stopped early." if totals.stopped
               else f"Epoch {epoch} finished.")  # fmt: skip
    _print_measurements(m)
    typer.echo(f"Details saved to: {report}")
    if totals.stopped:
        return "stopped"
    return "failed" if m["chunks_failed"] else "done"


@app.command()
def backfill(
    epoch: Annotated[str, typer.Option(
        help="Old Faithful epoch to read, or several one after another: 1085-1091 or "
             "1085,1087.")],  # fmt: skip
    from_slot: Annotated[int | None, typer.Option(help="Only chunks from this slot.")] = None,
    to_slot: Annotated[int | None, typer.Option(help="Only chunks up to this slot.")] = None,
    workers: Annotated[int | None, typer.Option(help="Parallel downloads.")] = None,
    max_mb_s: Annotated[
        float | None, typer.Option(help="Speed limit in MB/s for all downloads (0 = none).")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Start without asking.")] = False,
    status: Annotated[bool, typer.Option("--status", help="Only show what is done.")] = False,
    wait: Annotated[
        bool, typer.Option("--wait", help="If an epoch is not in the archive yet, check "
                           "every 10 minutes and start when it is.")
    ] = False,
) -> None:
    """Download epochs of history from Old Faithful and keep the pump.fun rows.

    Asks before downloading (an epoch is about 400 GB). Stop any time with Ctrl+C, which
    stops every epoch in the command; run the same command again to continue."""
    try:
        epochs = parse_epochs(epoch)
    except ValueError as exc:
        typer.echo(f"--epoch: {exc}")
        raise typer.Exit(2) from None
    if len(epochs) > 1 and (from_slot is not None or to_slot is not None):
        typer.echo("--from-slot and --to-slot work with a single epoch only.")
        raise typer.Exit(2)
    cfg = load_config()
    ask = not (yes or status)
    if ask and len(epochs) > 1:
        typer.echo(f"Epochs {', '.join(map(str, epochs))}, one after another (about 400 GB "
                   f"each, so about {len(epochs) * 0.4:.1f} TB). Stop any time with Ctrl+C; "
                   "run the same command to continue.")  # fmt: skip
        if not typer.confirm("Start the downloads?", default=False):
            raise typer.Exit(1)
        ask = False
    failed = []
    for e in epochs:
        try:
            outcome = _backfill_epoch(cfg, e, from_slot=from_slot, to_slot=to_slot,
                                      workers=workers, max_mb_s=max_mb_s, status=status,
                                      wait=wait, ask=ask)  # fmt: skip
        except KeyboardInterrupt:
            outcome = "stopped"
        if outcome == "stopped":
            left = epochs[epochs.index(e):]
            typer.echo(f"\nStopped. Not finished: epoch {', '.join(map(str, left))}. "
                       "Run the same command again to continue.")  # fmt: skip
            raise typer.Exit(STOPPED_EXIT)
        if outcome == "failed":
            failed.append(e)
    if len(epochs) > 1 and not status:
        typer.echo(f"\nAll {len(epochs)} epochs read."
                   + (f" Chunks failed in epoch {', '.join(map(str, failed))}: they are in "
                      "the gap log; run the same command again to retry them." if failed
                      else ""))  # fmt: skip
    raise typer.Exit(1 if failed else 0)


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
def labels(
    examples: Annotated[int, typer.Option(help="Show this many example coins per label.")] = 2,
) -> None:
    """Label every backfilled coin (spec 7; research track R1): dead, rugged, bundled
    (stand-in), graduated, and doubled before halving after a simulated entry. Settings
    in config/labels.yaml; results in <data folder>/labels/."""
    import random

    from agent.labels.make_labels import (
        GROUPS,
        build_labels,
        group_of,
        load_label_settings,
        summarize,
    )

    cfg = load_config()
    settings, sha = load_label_settings(cfg.root / "config" / "labels.yaml")
    try:
        run = build_labels(cfg.data_dir, settings, sha, progress=typer.echo)
    except FileNotFoundError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from None
    summary = summarize(run)
    first, last = run.spans[0][0], run.spans[-1][1]
    typer.echo("")
    typer.echo(f"Backfill ({clock.zone()}): {_local(first)} to {_local(last)}"
               f"{f' in {len(run.spans)} stretches' if len(run.spans) > 1 else ''}")  # fmt: skip
    typer.echo(f"Coins: {summary['coins']:,}; with every label known: {summary['complete']:,} "
               f"(the rest launched within {settings.horizon_s // 60} min of the end of the "
               "data)")  # fmt: skip
    typer.echo(f"Saved to {cfg.data_dir / 'labels'} in {run.seconds:.0f} s")
    typer.echo("")
    typer.echo("First hour, share of coins:")
    typer.echo(f"  {'':12s}{'coins':>8s}{'dead':>7s}{'dead*':>7s}{'rugged':>8s}"
               f"{'bundled':>9s}{'bad':>6s}{'graduated':>11s}")  # fmt: skip

    def pct(value: float | None, digits: int = 0) -> str:
        return "-" if value is None else f"{value:.{digits}%}"

    for name in GROUPS:
        g = summary["groups"][name]
        r = g["rates"]
        typer.echo(f"  {name:12s}{g['coins']:>8,}{pct(r['is_dead']):>7s}"
                   f"{pct(r['is_dead_without_agent']):>7s}{pct(r['is_rug'], 1):>8s}"
                   f"{pct(r['is_bundled_proxy'], 1):>9s}{pct(r['bad']):>6s}"
                   f"{pct(r['graduated_1h'], 1):>11s}")  # fmt: skip
    typer.echo("  (dead*: not counting the mayhem agent's trades; 'other quote': priced in "
               "another coin, so no price labels)")  # fmt: skip
    d = settings.double
    typer.echo("")
    typer.echo(f"Reached {d.up:g}x before {d.down:g}x within {d.within_s // 60} min, after a "
               f"{d.buy_sol:g} SOL buy {d.entry_delay_s} s after each decision time:")  # fmt: skip
    offsets = d.offsets_s
    typer.echo(f"  {'':12s}" + "".join(f"{f'T+{o} s':>10s}" for o in offsets))
    for name in ("ordinary", "mayhem"):
        hits = summary["groups"][name]["hit_2x"]
        typer.echo(f"  {name:12s}" + "".join(
            f"{pct(hits[o]['rate'], 1) if o in hits else '-':>10s}" for o in offsets))  # fmt: skip
        if name == "mayhem":
            typer.echo(f"  {'  with agent':12s}" + "".join(
                f"{pct(hits[o]['rate_with_agent_prices'], 1) if o in hits else '-':>10s}"
                for o in offsets))  # fmt: skip
    if examples:
        typer.echo("")
        typer.echo("Examples to check on Solscan (mint, group, launched):")
        rng = random.Random(0)
        complete = [c for c in run.coins if c["complete"]]
        hit = {e["mint"] for e in run.entries if e["offset_s"] == offsets[0] and e["hit_2x"]}
        for label, test in (("rugged", lambda c: c["is_rug"]),
                            ("bundled", lambda c: c["is_bundled_proxy"]),
                            ("graduated", lambda c: c["graduated_1h"]),
                            (f"{d.up:g}x from T+{offsets[0]} s", lambda c: c["mint"] in hit),
                            ("dead", lambda c: c["is_dead"])):  # fmt: skip
            picked = [c for c in complete if test(c)]
            for c in rng.sample(picked, min(examples, len(picked))):
                typer.echo(f"  {label:16s}{c['mint']}  {group_of(c):11s} {_local(c['created_ts'])}")


@app.command()
def dataset() -> None:
    """Build the research dataset (spec 15.3, R2): one row per backfilled coin and cutoff
    (T+30/60/120/300 s) with its signals, the rules' verdict and its labels. Saved in
    <data folder>/dataset/ with a manifest."""
    import duckdb

    from agent.dataset.build import build_dataset
    from agent.models.rules import RulesError

    cfg = load_config()
    try:
        run = build_dataset(cfg.root, cfg.data_dir, progress=typer.echo)
    except (FileNotFoundError, ValueError, RulesError) as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from None
    m = run.manifest
    spans = m["backfill_spans"]
    cols = m["columns"]
    offsets = m["offsets_s"]
    typer.echo("")
    typer.echo(f"Backfill ({clock.zone()}): {_local(spans[0][0])} to {_local(spans[-1][1])}, "
               f"epoch{'s' if len(m['epochs']) > 1 else ''} "
               f"{', '.join(map(str, m['epochs']))}; signals from the backfill only")  # fmt: skip
    typer.echo(f"Rows: {m['rows']:,} ({m['coins']:,} coins x {len(offsets)} cutoffs); "
               f"columns: {len(cols['ids'])} ids, {len(cols['features'])} signals, "
               f"{len(cols['rules'])} rules, {len(cols['labels'])} labels")  # fmt: skip
    typer.echo(f"Rows to model on (no gap, every label known): {m['usable_rows']:,}")
    typer.echo("")
    with duckdb.connect() as db:
        counts = db.execute(
            "SELECT offset_s, rules_verdict, count(*) FROM read_parquet(?) "
            "WHERE NOT has_gap AND labels_complete GROUP BY ALL", [run.path.as_posix()]
        ).fetchall()  # fmt: skip
    by_offset: dict[int, dict[str, int]] = {}
    for offset, verdict, n in counts:
        by_offset.setdefault(offset, {})[verdict] = n
    typer.echo("Rules verdicts on those rows (config/rules.yaml):")
    typer.echo(f"  {'':10s}{'rows':>9s}{'avoid':>8s}{'caution':>9s}{'pass':>7s}")
    for offset in offsets:
        got = by_offset.get(offset, {})
        total = sum(got.values())
        share = [f"{got.get(v, 0) / total:.0%}" if total else "-"
                 for v in ("avoid", "caution", "pass")]  # fmt: skip
        typer.echo(f"  {f'T+{offset} s':10s}{total:>9,}{share[0]:>8s}{share[1]:>9s}"
                   f"{share[2]:>7s}")  # fmt: skip
    code = m["code"]
    commit = (code["git_commit"] or "unknown")[:7]
    changes = " plus uncommitted changes" if code["uncommitted_changes"] else ""
    typer.echo("")
    typer.echo(f"Saved to {run.path} ({run.path.stat().st_size / 1e6:.0f} MB) in "
               f"{m['seconds']:.0f} s{'; labels were rebuilt first' if run.labels_rebuilt else ''}")
    typer.echo(f"Versions: code {commit}{changes} (source hash {code['source_sha']}), rules "
               f"{m['rules_sha']}, label settings {m['labels_settings_sha']}; file hash "
               f"{m['file_sha256'][:12]}")  # fmt: skip


@app.command()
def train() -> None:
    """Fit the baselines and models of research step R5 (spec 15.3, 15.4) on the training
    days, tune them on the validation day, run the checks (SHAP, shuffled labels,
    walk-forward) and save everything in <data folder>/models/r5/. The test days are not
    read. Results go to docs/R5_RESULTS.md."""
    from agent.models.evaluate import load_plan
    from agent.models.report import MODEL_NAMES, write_results
    from agent.models.train import AlreadyScored
    from agent.models.train import train as fit

    cfg = load_config()
    plan, sha = load_plan(cfg.root / "config" / "evaluation.yaml")
    try:
        run = fit(cfg.root, cfg.data_dir, plan, sha, progress=typer.echo)
    except (AlreadyScored, FileNotFoundError, ValueError) as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from None
    m = run["manifest"]
    typer.echo("")
    typer.echo(f"Validation day {', '.join(m['days']['validation'])}: PR-AUC by decision time")
    for stage in run["validation"]["stages"].values():
        typer.echo(f"  {stage['title']}")
        cells = [c for c in stage["cells"] if c["slice"] == "all"]
        for model, label in MODEL_NAMES.items():
            got = {c["offset_s"]: c["pr_auc"]["value"] for c in cells if c["model"] == model}
            typer.echo(f"    {label:26s}" + "".join(f"{got[o]:>9.3f}" for o in sorted(got)))
    path = write_results(cfg.root, run["folder"])
    typer.echo("")
    typer.echo(f"Models and reports saved to {run['folder']} in {m['seconds']} s; "
               f"results written to {path}")  # fmt: skip


@app.command("score-test")
def score_test(
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask before scoring.")] = False,
) -> None:
    """Score the test days with the saved R5 models: once (spec 15.4). Refused when a
    test report already exists. Results go to docs/R5_RESULTS.md."""
    from agent.models.evaluate import load_plan
    from agent.models.report import write_results
    from agent.models.train import AlreadyScored, model_dir
    from agent.models.train import score_test as score

    cfg = load_config()
    plan, sha = load_plan(cfg.root / "config" / "evaluation.yaml")
    if not yes and not typer.confirm("This scores the test days. It can be done once. Go on?"):
        raise typer.Exit(1)
    try:
        report = score(cfg.data_dir, plan, sha, progress=typer.echo)
    except (AlreadyScored, FileNotFoundError, ValueError) as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from None
    path = write_results(cfg.root, model_dir(cfg.data_dir))
    typer.echo(f"Scored {report['coins']:,} coins of the test days; results written to {path}")


@app.command()
def backtest(
    days: Annotated[str, typer.Argument(help="validation, train or test.")],
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask before the test days.")] = False,
    resummarize: Annotated[bool, typer.Option(
        "--resummarize", help="Build the report again from the saved trades, without "
        "simulating, after a change to Padre's fee.")] = False,
) -> None:
    """Backtest as analysis (spec 10, 15.3 step R6): simulate 1 SOL buys after each signal
    with exact curve and pool math, the fees recorded on chain and the exit plans in
    config/padre_presets.yaml. `validation` chooses the score cuts and freezes them;
    `train` reports the unfitted signals on the training days; `test` simulates the test
    days, once. Nothing is traded. Results go to docs/R6_RESULTS.md."""
    from agent.backtest.report import money, write_results
    from agent.backtest.run import ALL_TIMES, SETS, NotAllowed
    from agent.backtest.run import resummarize as again
    from agent.backtest.run import run as simulate
    from agent.models.evaluate import load_plan

    if days not in SETS:
        typer.echo(f"Unknown days {days!r}: one of {', '.join(SETS)}.")
        raise typer.Exit(1)
    cfg = load_config()
    plan, _ = load_plan(cfg.root / "config" / "evaluation.yaml")
    if days == "test" and not resummarize and not yes and not typer.confirm(
        "This simulates the test days. It can be done once. Go on?"
    ):
        raise typer.Exit(1)
    try:
        report = (again if resummarize else simulate)(
            cfg.root, cfg.data_dir, plan, days, progress=typer.echo)
    except (NotAllowed, FileNotFoundError, ValueError) as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from None
    presets = report["presets"]
    delay = presets["entry_delay_s"]
    typer.echo("")
    typer.echo(f"Mean profit or loss per signal in SOL ({delay} s delay, all four decision "
               "times, headline setting), by exit plan:")  # fmt: skip
    plans = [p["name"] for p in presets["exit_plans"]]
    typer.echo(f"  {'':42s}" + "".join(f"{p:>32s}" for p in plans))
    cells = {(c["signal"], c["plan"]): c for c in report["cells"]
             if c["time"] == ALL_TIMES and c["delay_s"] == delay and c["slice"] == "all"
             and c["variant"] == "headline"}  # fmt: skip
    for signal, info in report["signals"].items():
        typer.echo(f"  {info['label'][:40]:42s}" + "".join(
            f"{money(cells[(signal, p)].get('mean_pnl_sol')):>32s}" for p in plans))  # fmt: skip
    path = write_results(cfg.root, cfg.data_dir)
    typer.echo("")
    typer.echo(f"{report['ledger']['rows']:,} simulated trades in {report['seconds']} s; "
               f"results written to {path}")  # fmt: skip


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
    step C): each token's signals at each cutoff must be identical. Where the verdicts
    carry model scores (spec 15.3, step L1), those are made again from the recorded data
    with the same code and must be identical too, to the last digit."""
    # Imported here so this command's code is not loaded by other commands (the backfill's
    # worker processes import this module).
    from collections import Counter

    import duckdb

    from agent.features.replay import RECORDED, load_states
    from agent.scoring.replay_check import disk_history

    cfg = load_config()
    folder = cfg.data_dir / "live" / "verdicts"
    if not any(folder.glob("*/*.parquet")):
        typer.echo("No live verdicts yet. They come from `agentd start` with config/rules.yaml.")
        raise typer.Exit(1)
    since = time.time() - hours * 3600
    files = f"{folder.as_posix()}/*/*.parquet"
    with duckdb.connect() as db:
        columns = {row[0] for row in db.execute(
            "DESCRIBE SELECT * FROM read_parquet(?, union_by_name=true)", [files]).fetchall()}
        # Verdicts saved before model scores existed have no such column.
        scores = "scores" if "scores" in columns else "NULL"
        cursor = db.execute(
            f"SELECT mint, ts, created_ts, offset_s, has_gap, snapshot, {scores}, decided_at "
            "FROM read_parquet(?, union_by_name=true) WHERE decided_at >= ? ORDER BY ts",
            [files, since],
        )
        verdicts = cursor.fetchall()
    if not verdicts:
        typer.echo(f"No live verdicts in the last {hours:g} h.")
        raise typer.Exit(1)
    created = [v[2] for v in verdicts]
    # The recording only: it is what the engine saw. (Read together with the backfill,
    # every recorded event would first be matched against weeks of history.) Earlier
    # launches come from everything on disk, as they do when the engine starts.
    replay = load_states(cfg.data_dir, min(created), max(created) + 1,
                         max_offset_s=max(v[3] for v in verdicts), roots=(RECORDED,))  # fmt: skip
    history = disk_history(cfg.data_dir)
    states = {s.mint: s for s in replay.states}
    same, differ, missing, gap_flags = 0, [], 0, 0
    keys: Counter = Counter()
    for mint, ts, _, offset, has_gap, snapshot, _, _ in verdicts:
        state = states.get(mint)
        if state is None:
            missing += 1
            continue
        live = json.loads(snapshot)
        replayed = state.snapshot(ts, creators=history.launches)
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
    scores_ok = _check_live_scores(cfg, verdicts, replay, history, show)
    if differ or not scores_ok:
        raise typer.Exit(1)


def _check_live_scores(cfg, verdicts: list[tuple], replay, history, show: int) -> bool:
    """The model-score half of `check-live`. True when nothing differs."""
    from agent.scoring.live import ScoringOff, load_scorer
    from agent.scoring.replay_check import check_scores

    rows = [(mint, ts, offset, has_gap, decided_at, saved)
            for mint, ts, _, offset, has_gap, _, saved, decided_at in verdicts]  # fmt: skip
    typer.echo("")
    if not any(r[5] is not None for r in rows):
        typer.echo("Model scores: none in these verdicts (the engine made them without the "
                   "saved models; `agentd status` says why).")  # fmt: skip
        return True
    try:
        scorer = load_scorer(cfg.data_dir,
                             min_hour_covered=cfg.settings.live_models.min_hour_covered)
    except ScoringOff as exc:
        typer.echo(f"Model scores: saved with the verdicts, but they cannot be checked: {exc}")
        return False
    got = check_scores(rows, replay, scorer, history)
    typer.echo(f"Model scores ({scorer.name}, bundle {scorer.bundle_sha}): {got.records:,} "
               f"verdicts carry a score record; {got.scored:,} with probabilities, "
               f"{got.records - got.scored:,} without")  # fmt: skip
    for reason, n in got.reasons.most_common(4):
        typer.echo(f"    no score ({n:,}): {reason}")
    share = f" ({got.same / got.compared:.2%})" if got.compared else ""
    typer.echo(f"  made again from the recorded data: {got.compared:,}, over "
               f"{got.hours:.2f} h of verdicts")  # fmt: skip
    typer.echo(f"  identical to the last digit: {got.same:,}{share}")
    typer.echo(f"  different: {len(got.different):,}")
    if got.other_model:
        typer.echo(f"  not compared, made with another model bundle: {got.other_model:,}")
    if got.gap_flag_differs:
        typer.echo(f"  not compared, a gap was logged after the verdict: "
                   f"{got.gap_flag_differs:,}")  # fmt: skip
    if got.token_missing:
        typer.echo(f"  not compared, token not in the recorded tokens: {got.token_missing:,}")
    if got.parts:
        typer.echo("  parts that differ most often: "
                   + ", ".join(f"{k} ({n:,})" for k, n in got.parts.most_common(6)))  # fmt: skip
        for mint, offset, changed in got.different[:show]:
            typer.echo(f"    {mint}  T+{offset}s: {', '.join(changed)}")
    return not got.different


@app.command("export-live-models")
def export_live_models(
    check: Annotated[bool, typer.Option(
        help="After writing, compare the engine's scoring with R5's own on the validation "
        "day (never the test days).")] = True,
) -> None:
    """Prepare the saved R5 models for the engine (spec 15.3, step L1): write each
    stage's calibration map as plain numbers to <data folder>/models/r5_live/bundle.json.
    Nothing in the R5 folder is changed and nothing is scored on the test days. Needs the
    research packages."""
    from agent.export_live import check_bundle, export_bundle
    from agent.models.evaluate import load_plan

    cfg = load_config()
    try:
        path = export_bundle(cfg.data_dir)
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from None
    typer.echo(f"Live model bundle written: {path}")
    if not check:
        return
    plan, _ = load_plan(cfg.root / "config" / "evaluation.yaml")
    result = check_bundle(cfg.data_dir, plan, progress=typer.echo)
    if not result["ok"]:
        typer.echo("The engine's scoring does NOT match R5's. Do not use the bundle.")
        raise typer.Exit(1)
    typer.echo("The engine's scoring matches R5's to the last digit on every row checked.")
