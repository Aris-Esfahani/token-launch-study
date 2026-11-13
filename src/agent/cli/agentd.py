"""`agentd` command: start, stop and inspect the engine."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from agent.config import load_config
from agent.daemon import main as engine
from agent.daemon.control import RunFiles, engine_state, process_started_at
from agent.daemon.launch import spawn_detached, windowless_python
from agent.ingest import downloads
from agent.ingest.coverage import (
    last_recorded_ts,
    raw_bytes,
    recorded_coverage,
    session_start,
)

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Control the agent engine.")

START_TIMEOUT_S = 25
STOP_TIMEOUT_S = 20


@app.command()
def start(
    foreground: Annotated[
        bool, typer.Option("--foreground", help="Run in this window and print live events.")
    ] = False,
    creates_only: Annotated[
        bool, typer.Option("--creates-only", help="With --foreground: print new tokens only.")
    ] = False,
) -> None:
    """Turn the engine on."""
    cfg = load_config()
    files = RunFiles(cfg.run_dir)
    files.run_dir.mkdir(parents=True, exist_ok=True)
    state, status = engine_state(files)
    if state != "stopped":
        typer.echo(f"agentd is already running (pid {status['pid']}).")
        raise typer.Exit(0)

    if foreground:
        typer.echo("agentd running in the foreground. Press Ctrl+C to stop.")
        try:
            code = asyncio.run(engine.run(cfg, foreground=True, creates_only=creates_only))
        except KeyboardInterrupt:
            code = 0
        raise typer.Exit(code)

    spawned_at = time.time()
    launched = spawn_detached(
        [windowless_python(), "-m", "agent.daemon.main", "--home", str(cfg.root)],
        cfg.root,
        files.crash_output,
    )
    # The engine's own process id comes from its status file. It is not `launched.pid`:
    # in a Windows virtual environment python(w).exe is a small launcher that starts the
    # real interpreter as a second process and waits for it.
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        if process_started_at(launched.pid) is None:
            typer.echo(f"agentd failed to start. See {files.crash_output} and {files.log}")
            raise typer.Exit(1)
        state, status = engine_state(files)
        if state == "running" and status["started_at"] >= spawned_at - 1 and status["connected"]:
            typer.echo(f"agentd started (pid {status['pid']}), live feed: {status['source']}.")
            if files.pause_flag.exists():
                typer.echo("Recording is paused. Run `agentd resume-recording` to record.")
            break
        time.sleep(0.5)
    else:
        typer.echo("agentd started but the live feed has not connected yet. Check `agentd status`.")
    if launched.independent:
        typer.echo("You can close this window; the engine keeps running.")
    else:
        typer.echo(f"Note: {launched.note}.")
        typer.echo("Keep this window open: closing it may stop the engine.")


@app.command()
def stop() -> None:
    """Turn the engine off."""
    cfg = load_config()
    files = RunFiles(cfg.run_dir)
    state, status = engine_state(files)
    if state == "stopped":
        typer.echo("agentd is not running.")
        return
    files.stop_flag.write_text("stop", encoding="utf-8")
    deadline = time.monotonic() + STOP_TIMEOUT_S
    while time.monotonic() < deadline:
        if engine_state(files)[0] == "stopped":
            typer.echo("agentd stopped.")
            return
        time.sleep(0.5)
    typer.echo(
        f"agentd (pid {status['pid']}) did not stop within {STOP_TIMEOUT_S}s. "
        "If it stays stuck, end that process in Task Manager."
    )
    raise typer.Exit(1)


@app.command("pause-recording")
def pause_recording() -> None:
    """Keep the engine running but stop writing new events (logged as a gap)."""
    cfg = load_config()
    files = RunFiles(cfg.run_dir)
    files.run_dir.mkdir(parents=True, exist_ok=True)
    files.pause_flag.write_text("paused", encoding="utf-8")
    running = engine_state(files)[0] != "stopped"
    typer.echo("Recording paused." if running else "Recording will start paused next time.")


@app.command("resume-recording")
def resume_recording() -> None:
    """Start writing events again after `pause-recording`."""
    cfg = load_config()
    RunFiles(cfg.run_dir).pause_flag.unlink(missing_ok=True)
    typer.echo("Recording resumed.")


@app.command()
def status() -> None:
    """Show whether the engine is running and what the live feed is doing."""
    cfg = load_config()
    files = RunFiles(cfg.run_dir)
    state, s = engine_state(files)
    if state == "stopped":
        typer.echo("agentd: not running")
        if s and s.get("updated_at"):
            typer.echo(f"last ran: {_clock(s['updated_at'], with_date=True)}")
        end = last_recorded_ts(cfg.run_dir) or (s or {}).get("updated_at")
        if end:
            typer.echo(f"saved:    {_saved(cfg.data_dir, end)}")
            typer.echo(f"burst:    {_burst(cfg, end, running=False)}")
            days_line, size_line = _history(cfg, end, s)
            typer.echo(f"history:  {days_line}")
            typer.echo(f"          {size_line}")
        raise typer.Exit(1)

    typer.echo(f"agentd:    {state} (pid {s['pid']})")
    run_time = _duration(time.time() - s["started_at"])
    typer.echo(f"running:   {run_time}, since {_when(s['started_at'])}")
    if state == "unresponsive":
        age = int(time.time() - s["updated_at"])
        typer.echo(f"           no heartbeat for {age}s; the numbers below are old")
    age = s.get("last_message_age_s")
    last = f"last message {age}s ago" if age is not None else "no messages yet"
    connected = "connected" if s.get("connected") else "NOT connected"
    typer.echo(f"feed:      {s.get('source') or '-'}, {connected}, {last}")
    if s.get("msgs_per_s") is not None:
        failed = s.get("failed_share")
        failed_text = f"{failed:.0%} failed" if failed is not None else "failed share n/a"
        typer.echo(
            f"rates:     {s['msgs_per_s']} msg/s ({failed_text}), "
            f"{s['trades_per_s']} trades/s, {s['creates_per_hr']:,} tokens/hr"
        )
        typer.echo(
            f"network:   {s['mb_received']:,} MB received, about {s['gb_per_day']} GB/day "
            "at this rate (before any network compression)"
        )
    typer.echo(f"seen:      {s['creates']:,} tokens, {s['trades']:,} trades since start")
    drop = s.get("last_disconnect")
    last_drop = f", last {_clock(drop['at'])} ({drop['reason']})" if drop else ""
    typer.echo(f"drops:     {s['reconnects']} reconnects{last_drop}")

    if s.get("paused"):
        recording = "PAUSED (run `agentd resume-recording`)"
    elif s.get("recording"):
        recording = "on"
    else:
        recording = f"OFF ({s.get('open_gap') or 'waiting for the feed'})"
    written = s.get("rows_written", {})
    last_write = s.get("last_write_at")
    last_write_text = f"{int(time.time() - last_write)}s ago" if last_write else "none yet"
    typer.echo(
        f"recording: {recording}; written {written.get('tokens', 0):,} tokens, "
        f"{written.get('trades', 0):,} trades ({s.get('mb_written', 0)} MB in "
        f"{s.get('files_written', 0)} files), last write {last_write_text}"
    )
    typer.echo(f"saved:     {_saved(cfg.data_dir, time.time())}")
    typer.echo(f"burst:     {_burst(cfg, time.time(), running=True)}")
    days_line, size_line = _history(cfg, time.time(), s)
    typer.echo(f"history:   {days_line}")
    typer.echo(f"           {size_line}")
    lag = s.get("lag_s")
    typer.echo(
        f"quality:   {f'{lag}s behind the chain' if lag is not None else 'lag n/a'}; "
        f"{s.get('duplicates', 0)} repeats and {s.get('simulated', 0)} simulated dropped; "
        f"{s.get('truncated', 0)} cut-off logs; "
        f"{s.get('decode_errors', 0)} decode errors"
    )
    if s.get("stale_idl_events") or s.get("unknown_events"):
        typer.echo(
            f"           note: {s.get('stale_idl_events', 0):,} events carry fields newer than "
            f"the IDL files (kept as raw bytes) and {s.get('unknown_events', 0):,} are of a "
            "kind not in them. Check pump.fun's public docs for updated IDL files."
        )
    gap = s.get("last_gap")
    last_gap = f", last {_clock(gap['at'])} {gap['reason']} ({gap['seconds']}s)" if gap else ""
    open_gap = f"; one open now ({s['open_gap']})" if s.get("open_gap") else ""
    typer.echo(f"gaps:      {s.get('gaps_logged', 0)} logged this run{last_gap}{open_gap}")
    typer.echo(f"disk:      {s.get('disk_free_gb')} GB free at {s.get('data_dir')}")
    typer.echo(f"log:       {files.log}")


def _saved(data_dir: Path, end: float) -> str:
    """How much time the recording covers, from its first event to `end`."""
    coverage = recorded_coverage(data_dir, end)
    if coverage is None:
        return "nothing recorded yet"
    gaps = (
        f"{coverage.gap_count} gap{'' if coverage.gap_count == 1 else 's'}, "
        f"{_duration(coverage.gap_seconds)} off"
        if coverage.gap_count
        else "no gaps"
    )
    return (
        f"{_duration(coverage.recorded_seconds)} recorded out of "
        f"{_duration(coverage.span_seconds)} since {_when(coverage.start_ts)}; {gaps}"
    )


BAR_WIDTH = 30


def _bar(fraction: float) -> str:
    """A plain-text progress bar, e.g. [#########---------------------]."""
    filled = round(min(max(fraction, 0.0), 1.0) * BAR_WIDTH)
    return "[" + "#" * filled + "-" * (BAR_WIDTH - filled) + "]"


def _burst(cfg, end: float, *, running: bool) -> str:
    """Progress of the current recording burst toward the goal in settings.yaml."""
    recorder = cfg.settings.recorder
    goal_s = recorder.goal_hours * 3600
    goal = f"{recorder.goal_hours:g} h"
    since = session_start(cfg.data_dir, end, recorder.session_break_hours * 3600)
    coverage = recorded_coverage(cfg.data_dir, end, since) if since else None
    done_s = coverage.recorded_seconds if coverage else 0.0
    fraction = done_s / goal_s if goal_s else 1.0
    head = f"{_bar(fraction)} {min(fraction, 1.0):4.0%} of {goal}"
    if fraction >= 1:
        return f"{head}: done. Run `agent audit`, then `agentd stop`."
    left_s = goal_s - done_s
    if not running:
        return f"{head}; {_duration(left_s)} to go (not recording now)"
    return f"{head}; {_duration(left_s)} to go, done about {_when(time.time() + left_s)}"


def _history(cfg, end: float, status: dict | None = None) -> tuple[str, str]:
    """Progress toward the total history wanted for training (dataset.target_days):
    one line in days, one in GB downloaded."""
    target_days = cfg.settings.dataset.target_days
    coverage = recorded_coverage(cfg.data_dir, end)
    days = coverage.recorded_seconds / 86400 if coverage else 0.0
    fraction = days / target_days if target_days else 1.0
    days_line = (
        f"{_bar(fraction)} {min(fraction, 1.0):.1%} of {target_days:g} days; "
        f"{days:.2f} days done, {max(target_days - days, 0):.2f} days to go"
    )
    downloaded = downloads.total_bytes(cfg.run_dir, status) / 1e9
    # The days still missing are expected to come from the backfill archive.
    left_days = max(target_days - days, 0)
    needed = downloaded + left_days * cfg.settings.dataset.backfill_gb_per_day
    share = downloaded / needed if needed else 1.0
    kept = (raw_bytes(cfg.data_dir / "raw") + raw_bytes(cfg.data_dir / "raw_backfill")) / 1e9
    size_line = (
        f"downloaded {downloaded:,.1f} GB of about {needed:,.0f} GB ({share:.2%}); "
        f"kept {kept:.2f} GB on disk"
    )
    return days_line, size_line


def _duration(seconds: float) -> str:
    """A length of time in words a person reads at a glance: 45 s, 9 m 05 s, 3 h 07 m."""
    total = int(max(seconds, 0))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days} d {hours:02d} h"
    if hours:
        return f"{hours} h {minutes:02d} m"
    if minutes:
        return f"{minutes} m {secs:02d} s"
    return f"{secs} s"


def _when(epoch: float) -> str:
    """A moment in local time: '04:03 today', '23:10 yesterday' or 'Nov 10 04:03'."""
    moment = datetime.fromtimestamp(epoch)
    days_ago = (datetime.now().date() - moment.date()).days
    if days_ago == 0:
        return f"{moment:%H:%M} today"
    if days_ago == 1:
        return f"{moment:%H:%M} yesterday"
    if days_ago == -1:
        return f"{moment:%H:%M} tomorrow"
    return f"{moment:%b} {moment.day} {moment:%H:%M}"


def _clock(epoch: float, *, with_date: bool = False) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S")
