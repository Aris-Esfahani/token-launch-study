"""The engine process (`agentd`).

Connects to the live feed, parses pump.fun events, records them to Parquet with a gap
log, prints them in foreground mode, and keeps a heartbeat file so `agentd status` and
`agentd stop` work. Live state, scoring and alerts arrive in later phases.
"""

from __future__ import annotations

import argparse
import asyncio
import faulthandler
import logging
import os
import shutil
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from agent import __version__
from agent.config import AppConfig, load_config
from agent.daemon.control import (
    HEARTBEAT_S,
    RunFiles,
    engine_state,
    process_started_at,
    write_status,
)
from agent.ingest import downloads
from agent.ingest.parsers import ParsedTx
from agent.ingest.pipeline import Pipeline
from agent.ingest.rpc import Rpc, RpcError
from agent.ingest.ws_client import LogNotification, LogsStream
from agent.logging_setup import redact, setup_logging
from agent.sim.chain_config import fetch_snapshot, save_snapshot

log = logging.getLogger("agentd")

TICK_S = 0.5  # how often the stop and pause flags are checked
RATE_WINDOW_S = 60.0
SUMMARY_EVERY_S = 300.0
STALL_AFTER_S = 10.0  # a tick this late means the PC slept or the engine froze


@dataclass
class Feed:
    started_at: float = field(default_factory=time.time)
    connected: bool = False
    source: str | None = None
    reconnects: int = 0
    last_message_at: float | None = None
    last_disconnect: dict | None = None
    # (time, messages, failed, tokens, trades, bytes) samples for the last minute
    samples: deque = field(default_factory=deque)


def _rates(feed: Feed, pipeline: Pipeline) -> dict:
    s = pipeline.stats
    now = time.monotonic()
    feed.samples.append(
        (now, s.messages, s.failed, s.rows_seen.get("tokens", 0), s.rows_seen.get("trades", 0),
         s.bytes)
    )  # fmt: skip
    while len(feed.samples) > 2 and now - feed.samples[0][0] > RATE_WINDOW_S:
        feed.samples.popleft()
    if len(feed.samples) < 2:
        return {}
    t0, m0, f0, c0, tr0, b0 = feed.samples[0]
    t1, m1, f1, c1, tr1, b1 = feed.samples[-1]
    dt = max(t1 - t0, 1e-9)
    msgs = m1 - m0
    return {
        "msgs_per_s": round(msgs / dt, 1),
        "failed_share": round((f1 - f0) / msgs, 3) if msgs else None,
        "trades_per_s": round((tr1 - tr0) / dt, 1),
        "creates_per_hr": round((c1 - c0) / dt * 3600),
        "gb_per_day": round((b1 - b0) / dt * 86400 / 1e9, 1),
    }


def _status(
    state: str, feed: Feed, pipeline: Pipeline, rates: dict, proc_started: float | None, cfg
) -> dict:
    now = time.time()
    s, sink = pipeline.stats, pipeline.sink
    last_gap = pipeline.gap_log.last
    return {
        "state": state,
        "pid": os.getpid(),
        "process_started_at": proc_started,
        "version": __version__,
        "started_at": feed.started_at,
        "updated_at": now,
        "source": feed.source,
        "connected": feed.connected,
        "last_message_age_s": (
            round(now - feed.last_message_at, 1) if feed.last_message_at else None
        ),
        "reconnects": feed.reconnects,
        "last_disconnect": feed.last_disconnect,
        "messages": s.messages,
        "mb_received": round(s.bytes / 1e6, 1),
        "bytes_received": s.bytes,
        "lag_s": s.lag_s,
        "creates": s.rows_seen.get("tokens", 0),
        "trades": s.rows_seen.get("trades", 0),
        "duplicates": s.duplicates,
        "simulated": s.simulated,
        "truncated": s.truncated,
        "unknown_events": s.unknown_events,
        "stale_idl_events": s.stale_idl_events,
        "decode_errors": s.decode_errors,
        "rows_dropped": s.rows_dropped,
        "recording": pipeline.recording,
        "paused": pipeline.paused,
        "open_gap": pipeline.gaps.open_reason,
        "rows_written": dict(sink.rows_written),
        "files_written": sink.files_written,
        "mb_written": round(sink.bytes_written / 1e6, 2),
        "last_write_at": sink.last_flush_at,
        "gaps_logged": pipeline.gap_log.count,
        "last_gap": (
            {
                "at": last_gap["recorded_at"],
                "reason": last_gap["reason"],
                "seconds": round(last_gap["end_ts"] - last_gap["start_ts"]),
            }
            if last_gap
            else None
        ),
        "disk_free_gb": round(shutil.disk_usage(cfg.data_dir).free / 1e9, 1),
        "data_dir": str(cfg.data_dir),
        **rates,
    }


def describe(parsed: ParsedTx, *, creates_only: bool = False) -> list[str]:
    """One line per event for the foreground view, mint address first (ready to paste
    into Padre)."""
    lines: list[str] = []
    for row in parsed.rows.get("tokens", []):
        lines.append(f"NEW    {row['mint']}  ${row['symbol']}  {row['name']}")
    if creates_only:
        return lines
    for row in parsed.rows.get("trades", []):
        side = "BUY " if row["is_buy"] else "SELL"
        lines.append(f"{side}   {row['mint']}  {_amount(row)}  by {row['wallet'][:6]}..")
    for row in parsed.rows.get("curve_completes", []):
        lines.append(f"DONE   {row['mint']}  bonding curve complete")
    for row in parsed.rows.get("migrations", []):
        lines.append(f"MOVED  {row['mint']}  to PumpSwap pool {row['pool']}")
    return lines


def _amount(trade: dict) -> str:
    """What the trade paid or received. Plain SOL for SOL-paired coins; for coins paired
    with something else, the raw amount and the other coin's address."""
    quote_mint = trade.get("quote_mint")
    if quote_mint in (None, _DEFAULT_PUBKEY, _WRAPPED_SOL):
        return f"{trade['sol_amount'] / 1e9:.4f} SOL"
    return f"{trade.get('quote_amount')} units of {quote_mint[:6]}.."


_DEFAULT_PUBKEY = "1" * 32  # an all-zero address: pump.fun's marker for "paired with SOL"
_WRAPPED_SOL = "So11111111111111111111111111111111111111112"


async def run(cfg: AppConfig, *, foreground: bool, creates_only: bool = False) -> int:
    files = RunFiles(cfg.run_dir)
    files.run_dir.mkdir(parents=True, exist_ok=True)
    state, previous = engine_state(files)
    if state != "stopped":
        print(f"agentd is already running (pid {previous['pid']}). Use `agentd stop` first.")
        return 2

    secrets = cfg.secret_values()
    setup_logging(files.log, console=foreground, secrets=secrets)
    # Keep the previous run's download count before this run's status replaces it.
    downloads.record_previous_run(files.run_dir, previous)

    endpoints, skipped = cfg.endpoints()
    if skipped:
        log.info("live sources skipped (no value set in .env): %s", ", ".join(skipped))
    if not endpoints:
        print("No live source is enabled. Check `live.sources` in config/settings.yaml.")
        return 1
    programs = cfg.subscribed_programs()

    recorder = cfg.settings.recorder
    pipeline = Pipeline(
        cfg.data_dir,
        cfg.run_dir,
        flush_interval_s=recorder.flush_interval_s,
        min_free_gb=recorder.min_free_gb,
    )
    # The parser's layouts (IDL files) must belong to the programs named in settings.
    for name, address in pipeline.parser.program_ids.items():
        if cfg.settings.programs.get(name) != address:
            print(f"settings.yaml program '{name}' does not match the IDL address {address}.")
            return 1

    feed = Feed()
    stop = asyncio.Event()
    proc_started = process_started_at(os.getpid())

    def on_state(event: str, source: str, detail: str) -> None:
        detail = redact(detail, secrets)
        pipeline.on_connection(event, detail)
        if event == "connected":
            feed.connected, feed.source = True, source
            log.info("live feed connected: %s", source)
        else:
            feed.connected = False
            feed.reconnects += 1
            feed.last_disconnect = {"at": time.time(), "source": source, "reason": detail}

    def handle(n: LogNotification) -> None:
        feed.last_message_at = n.recv_ts
        parsed = pipeline.handle(n)
        if parsed is None or not foreground:
            return
        clock = datetime.fromtimestamp(n.recv_ts).strftime("%H:%M:%S")
        for line in describe(parsed, creates_only=creates_only):
            print(f"{clock}  {line}", flush=True)

    live = cfg.settings.live
    stream = LogsStream(
        endpoints,
        list(programs),
        live.commitment,
        reconnect_initial_s=live.reconnect.initial_s,
        reconnect_max_s=live.reconnect.max_s,
        stale_after_s=live.stale_after_s,
        on_state=on_state,
    )

    files.stop_flag.unlink(missing_ok=True)
    files.pid.write_text(str(os.getpid()), encoding="utf-8")
    log.info("agentd %s starting (pid %s), data dir %s", __version__, os.getpid(), cfg.data_dir)
    exit_code = 0
    rates: dict = {}
    stream_task = asyncio.create_task(stream.run(handle, stop))
    snapshot_task = asyncio.create_task(_snapshot_loop(cfg, stop, secrets))
    try:
        last_beat = 0.0
        last_summary = time.monotonic()
        last_tick_wall = time.time()
        while not stop.is_set():
            now = time.monotonic()
            wall = time.time()
            if wall - last_tick_wall > STALL_AFTER_S:
                pipeline.note_stall(wall - last_tick_wall)
            last_tick_wall = wall
            pipeline.set_paused(files.pause_flag.exists())
            pipeline.tick()
            if now - last_beat >= HEARTBEAT_S:
                last_beat = now
                rates = _rates(feed, pipeline)
                write_status(files, _status("running", feed, pipeline, rates, proc_started, cfg))
            if now - last_summary >= SUMMARY_EVERY_S and rates:
                last_summary = now
                written = pipeline.sink.rows_written
                log.info(
                    "feed %s: %s msg/s, %s trades/s, %s tokens/hr, lag %ss; written: %s tokens, "
                    "%s trades; gaps this run: %s",
                    feed.source, rates.get("msgs_per_s"), rates.get("trades_per_s"),
                    rates.get("creates_per_hr"), pipeline.stats.lag_s,
                    written.get("tokens", 0), written.get("trades", 0), pipeline.gap_log.count,
                )  # fmt: skip
            if files.stop_flag.exists():
                log.info("stop requested")
                break
            if stream_task.done():
                break
            await asyncio.sleep(TICK_S)
    finally:
        stop.set()
        try:
            await asyncio.wait_for(stream_task, timeout=10)
        except (asyncio.CancelledError, TimeoutError):
            stream_task.cancel()
        except Exception:  # noqa: BLE001 - report any crash of the feed task, then exit
            log.exception("live feed task crashed")
            exit_code = 1
        snapshot_task.cancel()
        try:
            pipeline.close()  # final write, and mark the shutdown as clean
        except Exception:  # noqa: BLE001 - never let a failed final write block shutdown
            log.exception("final write failed")
            exit_code = 1
        feed.connected = False
        write_status(files, _status("stopped", feed, pipeline, rates, proc_started, cfg))
        files.pid.unlink(missing_ok=True)
        files.stop_flag.unlink(missing_ok=True)
        log.info("agentd stopped")
    return exit_code


async def _snapshot_loop(cfg: AppConfig, stop: asyncio.Event, secrets: list[str]) -> None:
    """Save pump.fun's curve parameters and fee schedule from chain at start-up and then
    every `chain.snapshot_every_hours` (spec 4: versioned, never hardcoded). A failure is
    logged and retried next time; it never affects recording."""
    every_s = cfg.settings.chain.snapshot_every_hours * 3600
    if every_s <= 0:
        return
    while not stop.is_set():
        rpc = Rpc(cfg.rpc_url(), max_rps=cfg.settings.rpc.max_rps)
        try:
            snapshot = await asyncio.wait_for(fetch_snapshot(rpc), timeout=120)
            path = save_snapshot(cfg.data_dir, snapshot)
            log.info("chain snapshot saved: %s", path.name)
        except (OSError, RpcError, ValueError, TimeoutError) as exc:
            reason = redact(f"{type(exc).__name__}: {exc}", secrets)
            log.warning("chain snapshot failed: %s", reason)
        finally:
            await rpc.aclose()
        try:
            async with asyncio.timeout(every_s):
                await stop.wait()
        except TimeoutError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentd-engine", description="Run the agent engine.")
    parser.add_argument("--foreground", action="store_true", help="print live events")
    parser.add_argument("--creates-only", action="store_true", help="print new tokens only")
    parser.add_argument("--home", help="project folder (holds config/settings.yaml)")
    args = parser.parse_args(argv)
    cfg = load_config(Path(args.home) if args.home else None)
    if not args.foreground:
        # In the background there is no console (pythonw.exe), so anything printed and any
        # crash report go to run/agentd.out instead of being lost.
        files = RunFiles(cfg.run_dir)
        files.run_dir.mkdir(parents=True, exist_ok=True)
        out = open(files.crash_output, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stdout = sys.stderr = out
        faulthandler.enable(out)
    try:
        return asyncio.run(run(cfg, foreground=args.foreground, creates_only=args.creates_only))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
