"""The engine process (`agentd`).

Keeps one or more live connections open at once (spec 15.0 step B), parses pump.fun
events, records them to Parquet with a gap log, judges every new token at T+30/60/120/300
s with the rules in config/rules.yaml (step C), and keeps a heartbeat file so `agentd
status` and `agentd stop` work. In foreground mode it prints events, verdicts or alerts.

With the saved R5 models on disk and the research packages installed, each verdict also
gets the models' scores (spec 15.3, step L1; analysis only). Without them the engine runs
as before and says that model scores are off, and why.
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
from pathlib import Path

from agent import __version__, clock
from agent.config import AppConfig, load_config
from agent.daemon import live_state
from agent.daemon.control import (
    HEARTBEAT_S,
    RunFiles,
    engine_state,
    process_started_at,
    write_status,
)
from agent.daemon.local_api import HOST as API_HOST
from agent.daemon.local_api import LocalApi
from agent.ingest import downloads
from agent.ingest.backfill_coverage import backfill_spans
from agent.ingest.gaps import read_gaps
from agent.ingest.parsers import ParsedTx
from agent.ingest.pipeline import Pipeline
from agent.ingest.recorder import ParquetSink
from agent.ingest.rpc import Rpc, RpcError
from agent.ingest.ws_client import LogNotification, LogsStream
from agent.logging_setup import redact, setup_logging
from agent.scoring.launch_context import (
    LaunchHistory,
    LaunchIndex,
    first_recorded_ts,
    load_launch_index,
)
from agent.scoring.live import LiveScorer, ScoringOff, load_scorer
from agent.sim.chain_config import fetch_snapshot, save_snapshot

log = logging.getLogger("agentd")

TICK_S = 0.5  # how often the stop and pause flags are checked
RATE_WINDOW_S = 60.0
SUMMARY_EVERY_S = 300.0
STALL_AFTER_S = 10.0  # a tick this late means the PC slept or the engine froze
RULES_CHECK_S = 10.0  # how often config/rules.yaml is checked for edits
SHOW = ("events", "verdicts", "alerts")  # what the foreground view prints


@dataclass
class Connection:
    """One of the live connections that run at the same time."""

    name: str
    connected: bool = False
    source: str | None = None
    reconnects: int = 0
    last_message_at: float | None = None
    last_disconnect: dict | None = None


@dataclass
class Feed:
    started_at: float = field(default_factory=time.time)
    connections: dict[str, Connection] = field(default_factory=dict)
    # (time, messages, failed, tokens, trades, bytes) samples for the last minute
    samples: deque = field(default_factory=deque)

    @property
    def connected(self) -> bool:
        return any(c.connected for c in self.connections.values())

    @property
    def source(self) -> str | None:
        names = [c.source for c in self.connections.values() if c.connected and c.source]
        return " + ".join(names) or None

    @property
    def reconnects(self) -> int:
        return sum(c.reconnects for c in self.connections.values())

    @property
    def last_message_at(self) -> float | None:
        times = [c.last_message_at for c in self.connections.values() if c.last_message_at]
        return max(times) if times else None

    @property
    def last_disconnect(self) -> dict | None:
        drops = [c.last_disconnect for c in self.connections.values() if c.last_disconnect]
        return max(drops, key=lambda d: d["at"]) if drops else None


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
    state: str, feed: Feed, pipeline: Pipeline, rates: dict, proc_started: float | None, cfg,
    live: dict | None = None,
) -> dict:
    now = time.time()
    s, sink = pipeline.stats, pipeline.sink
    last_gap = pipeline.gap_log.last
    covered = pipeline.gaps.last_ts
    firsts = sum(s.first_by.values())
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
        # How far on chain the recording has covered everything, and how long ago that was.
        "covered_ts": covered,
        "covered_lag_s": round(now - covered, 1) if covered else None,
        "connections": [
            {
                "name": c.name,
                "source": c.source,
                "connected": c.connected,
                "reconnects": c.reconnects,
                "last_message_age_s": (
                    round(now - c.last_message_at, 1) if c.last_message_at else None
                ),
                "last_disconnect": c.last_disconnect,
                "lag_s": s.lag_by.get(c.name),
                "first_share": round(s.first_by.get(c.name, 0) / firsts, 3) if firsts else None,
            }
            for c in feed.connections.values()
        ],
        "live": live,
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


def _launch_history(data_dir: Path, gaps_this_run: list[dict]) -> LaunchHistory:
    """Every recorded or backfilled launch, so `dev_prior_launches` and the models'
    launch-context signals count the same live as in a replay, and the stretches of time
    those launches are complete for (the backfill's spans, and the recording outside its
    gaps). Missing data just means fewer launches counted, and says so in the coverage."""
    return LaunchHistory(
        load_launch_index(data_dir),
        recorded_from=first_recorded_ts(data_dir),
        backfill=backfill_spans(data_dir),
        gap_lists=[read_gaps(data_dir / "gaps"), gaps_this_run],
    )


def _load_models(cfg: AppConfig) -> tuple[LiveScorer | None, str | None]:
    """The saved models, or None and why model scores are off. Never raises: the engine
    records and gives rule verdicts without them."""
    settings = cfg.settings.live_models
    if not settings.enabled:
        return None, "turned off in config/settings.yaml (live_models.enabled)"
    try:
        return load_scorer(cfg.data_dir, min_hour_covered=settings.min_hour_covered), None
    except ScoringOff as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 - never let the models stop the engine
        log.exception("the saved models could not be loaded")
        return None, f"the saved models could not be loaded ({type(exc).__name__})"


def _live_status(live: live_state.LiveSignals | None, rules: live_state.RulesFile,
                 models_off: str | None = None) -> dict:  # fmt: skip
    if live is None:
        return {"enabled": False, "rules_error": rules.error,
                "models": {"on": False, "why": models_off or "live verdicts are off"}}  # fmt: skip
    st = live.stats
    models = live.models()
    models = {k: models[k] for k in ("on", "why", "version", "bundle_sha", "trained_days")
              if k in models}  # fmt: skip
    models |= {"scored": st.scored, "not_scored": st.not_scored,
               "last_not_scored": st.last_not_scored}  # fmt: skip
    return {
        "enabled": True,
        "tokens_tracked": st.tokens_tracked,
        "tokens_seen": st.tokens_seen,
        "verdicts": dict(st.verdicts),
        "verdicts_with_gap": st.verdicts_with_gap,
        "median_wait_s": live.median_wait_s(),
        "last_alert": st.last_alert,
        "rules_sha": live.rules_sha,
        "rules_error": rules.error,
        "models": models,
    }


async def run(
    cfg: AppConfig, *, foreground: bool, creates_only: bool = False, show: str = "events"
) -> int:
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

    groups, skipped = cfg.connections()
    if skipped:
        log.info("live sources skipped (no value set in .env): %s", ", ".join(skipped))
    if not groups:
        print("No live source is enabled. Check `live.sources` in config/settings.yaml.")
        return 1
    programs = cfg.subscribed_programs()

    # Live signals and verdicts. If the rules cannot be read, the engine still records.
    rules = live_state.RulesFile(cfg.root / "config" / "rules.yaml")
    rules.load()
    live: live_state.LiveSignals | None = None

    def on_row(table: str, row: dict) -> None:
        if live is not None:
            live.add(table, row)

    recorder = cfg.settings.recorder
    pipeline = Pipeline(
        cfg.data_dir,
        cfg.run_dir,
        flush_interval_s=recorder.flush_interval_s,
        min_free_gb=recorder.min_free_gb,
        on_row=on_row,
        row_tables=live_state.TABLES,
    )
    # The parser's layouts (IDL files) must belong to the programs named in settings.
    for name, address in pipeline.parser.program_ids.items():
        if cfg.settings.programs.get(name) != address:
            print(f"settings.yaml program '{name}' does not match the IDL address {address}.")
            return 1

    scorer, models_off = _load_models(cfg)
    if rules.rules:
        try:
            history = _launch_history(cfg.data_dir, pipeline.gap_log.logged)
        except Exception as exc:  # noqa: BLE001 - never let this stop recording
            log.warning("could not read earlier launches (%s); prior launches undercounted", exc)
            history = LaunchHistory(LaunchIndex(), gap_lists=[pipeline.gap_log.logged])
        live = live_state.LiveSignals(
            rules.rules, rules.sha, history.launches, pipeline.gap_log.logged,
            scorer=scorer, history=history, models_off=models_off,
        )
        log.info("live verdicts on (rules %s)", rules.sha)
    else:
        log.error("live verdicts off: %s", rules.error)
    if scorer is not None and live is not None:
        log.info("model scores on (%s, bundle %s; analysis only)", scorer.name, scorer.bundle_sha)
    else:
        log.warning("model scores off: %s", models_off or "live verdicts are off")
    verdict_sink = ParquetSink(
        cfg.data_dir / "live",
        {live_state.VERDICT_TABLE: live_state.VERDICT_COLUMNS},
        flush_interval_s=recorder.flush_interval_s,
        prefix="verdict",
    )
    alerts = open(files.run_dir / "alerts.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115

    # The screen's connection (spec 3): lookups from memory, the alert feed, the status.
    latest_status: dict = {}

    def analyze(mint: str) -> dict:
        if live is None:
            return {"mint": mint, "known": False, "why": f"live verdicts are off: {rules.error}"}
        return live.analyze(mint, pipeline.gaps.last_ts)

    api = LocalApi(analyze, lambda: latest_status)

    feed = Feed()
    stop = asyncio.Event()
    proc_started = process_started_at(os.getpid())

    def on_state(connection: Connection, event: str, source: str, detail: str) -> None:
        detail = redact(detail, secrets)
        pipeline.on_connection(event, detail, connection.name)
        if event == "connected":
            connection.connected, connection.source = True, source
            log.info("live feed %s connected: %s", connection.name, source)
        else:
            connection.connected = False
            connection.reconnects += 1
            connection.last_disconnect = {"at": time.time(), "source": source, "reason": detail}

    def handle(connection: Connection, n: LogNotification) -> None:
        connection.last_message_at = n.recv_ts
        parsed = pipeline.handle(n, connection.name)
        if parsed is None or not foreground or show != "events":
            return
        at = f"{clock.clock(n.recv_ts, seconds=True):>11s}"
        for line in describe(parsed, creates_only=creates_only):
            print(f"{at}  {line}", flush=True)

    settings = cfg.settings.live
    streams = []
    for number, endpoints in groups.items():
        connection = Connection(str(number))
        feed.connections[connection.name] = connection
        streams.append(LogsStream(
            endpoints,
            list(programs),
            settings.commitment,
            reconnect_initial_s=settings.reconnect.initial_s,
            reconnect_max_s=settings.reconnect.max_s,
            stale_after_s=settings.stale_after_s,
            on_state=lambda e, src, d, c=connection: on_state(c, e, src, d),
        ))  # fmt: skip
    log.info("live connections: %s", ", ".join(
        f"{n}: {' then '.join(e.name for e in eps)}" for n, eps in groups.items()
    ))  # fmt: skip

    def judge_due() -> None:
        if live is None:
            return
        for verdict in live.due(pipeline.gaps.last_ts):
            verdict_sink.add(live_state.VERDICT_TABLE, verdict)
            line = live_state.alert_line(verdict)
            if verdict["level"] == live_state.PASS:
                alerts.write(line + "\n")
                api.publish(live_state.alert_summary(verdict))
            if foreground and (show == "verdicts" or (
                show == "alerts" and verdict["level"] == live_state.PASS
            )):  # fmt: skip
                print(line, flush=True)

    def write_verdicts() -> None:
        try:
            verdict_sink.flush()
        except OSError as exc:
            dropped = verdict_sink.discard()
            log.error("could not write %s verdicts (%s)", dropped, exc)

    files.stop_flag.unlink(missing_ok=True)
    files.pid.write_text(str(os.getpid()), encoding="utf-8")
    log.info("agentd %s starting (pid %s), data dir %s", __version__, os.getpid(), cfg.data_dir)
    exit_code = 0
    rates: dict = {}
    stream_tasks = [
        asyncio.create_task(
            stream.run(lambda n, c=connection: handle(c, n), stop)
        )
        for stream, connection in zip(streams, feed.connections.values(), strict=True)
    ]
    snapshot_task = asyncio.create_task(_snapshot_loop(cfg, stop, secrets))
    api_port = cfg.settings.local_api.port
    if api_port:
        try:
            await api.start(api_port)
            log.info("local API for the screen on %s:%s", API_HOST, api.port)
        except OSError as exc:  # e.g. the port is taken; recording goes on without it
            log.warning("local API off: cannot listen on port %s (%s)", api_port, exc)
    try:
        last_beat = 0.0
        last_rules_check = time.monotonic()
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
            judge_due()
            if verdict_sink.due():
                write_verdicts()
            if now - last_rules_check >= RULES_CHECK_S:
                last_rules_check = now
                if rules.load() and live is not None:
                    live.rules, live.rules_sha = rules.rules, rules.sha
                    log.info("rules reloaded (%s)", rules.sha)
                elif rules.error:
                    log.warning(rules.error)
            if now - last_beat >= HEARTBEAT_S:
                last_beat = now
                rates = _rates(feed, pipeline)
                latest_status = _status(
                    "running", feed, pipeline, rates, proc_started, cfg,
                    _live_status(live, rules, models_off),
                )
                latest_status["local_api"] = api.stats()
                write_status(files, latest_status)
            if now - last_summary >= SUMMARY_EVERY_S and rates:
                last_summary = now
                written = pipeline.sink.rows_written
                lags = ", ".join(
                    f"{name} {lag}s" for name, lag in sorted(pipeline.stats.lag_by.items())
                )
                verdicts = dict(live.stats.verdicts) if live is not None else "off"
                log.info(
                    "feed %s: %s msg/s, %s trades/s, %s tokens/hr, lag %ss (%s); written: %s "
                    "tokens, %s trades; gaps this run: %s; verdicts: %s",
                    feed.source, rates.get("msgs_per_s"), rates.get("trades_per_s"),
                    rates.get("creates_per_hr"), pipeline.stats.lag_s, lags,
                    written.get("tokens", 0), written.get("trades", 0), pipeline.gap_log.count,
                    verdicts,
                )  # fmt: skip
            if files.stop_flag.exists():
                log.info("stop requested")
                break
            if any(task.done() for task in stream_tasks):
                break
            await asyncio.sleep(TICK_S)
    finally:
        stop.set()
        for task in stream_tasks:
            try:
                await asyncio.wait_for(task, timeout=10)
            except (asyncio.CancelledError, TimeoutError):
                task.cancel()
            except Exception:  # noqa: BLE001 - report any crash of a feed task, then exit
                log.exception("live feed task crashed")
                exit_code = 1
        snapshot_task.cancel()
        try:
            await api.close()
        except Exception:  # noqa: BLE001 - never let the screen's connection block shutdown
            log.exception("local API did not close cleanly")
        write_verdicts()
        alerts.close()
        try:
            pipeline.close()  # final write, and mark the shutdown as clean
        except Exception:  # noqa: BLE001 - never let a failed final write block shutdown
            log.exception("final write failed")
            exit_code = 1
        for connection in feed.connections.values():
            connection.connected = False
        write_status(files, _status(
            "stopped", feed, pipeline, rates, proc_started, cfg,
            _live_status(live, rules, models_off),
        ))  # fmt: skip
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
    parser.add_argument("--show", choices=SHOW, default="events", help="what to print")
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
        return asyncio.run(run(
            cfg, foreground=args.foreground, creates_only=args.creates_only, show=args.show
        ))  # fmt: skip
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
