"""`agentd` command: start, stop and inspect the engine."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Annotated

import typer

from agent import clock
from agent.config import load_config
from agent.daemon import main as engine
from agent.daemon.control import RunFiles, engine_state, process_started_at
from agent.daemon.launch import spawn_detached, windowless_python
from agent.ingest import downloads
from agent.ingest.coverage import (
    first_recorded_ts,
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
    show: Annotated[
        str,
        typer.Option(help="With --foreground: print 'events', every 'verdicts', or 'alerts' "
                          "(tokens that pass the rules)."),
    ] = "events",
) -> None:
    """Turn the engine on."""
    cfg = load_config()
    files = RunFiles(cfg.run_dir)
    files.run_dir.mkdir(parents=True, exist_ok=True)
    state, status = engine_state(files)
    if state != "stopped":
        typer.echo(f"agentd is already running (pid {status['pid']}).")
        raise typer.Exit(0)

    if show not in engine.SHOW:
        typer.echo(f"--show must be one of: {', '.join(engine.SHOW)}")
        raise typer.Exit(2)
    if foreground:
        typer.echo("agentd running in the foreground. Press Ctrl+C to stop.")
        try:
            code = asyncio.run(
                engine.run(cfg, foreground=True, creates_only=creates_only, show=show)
            )
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
def alerts(
    last: Annotated[int, typer.Option(help="Show this many recent alerts first.")] = 20,
    follow: Annotated[
        bool, typer.Option("--follow", help="Keep showing new alerts until Ctrl+C.")
    ] = False,
) -> None:
    """Tokens that passed the rules (config/rules.yaml), newest last, mint first."""
    cfg = load_config()
    path = RunFiles(cfg.run_dir).run_dir / "alerts.log"
    if not path.exists() and not follow:
        typer.echo("No alerts yet. The engine writes them once it runs with config/rules.yaml.")
        raise typer.Exit(1)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for line in lines[-last:] if last > 0 else []:
        typer.echo(line)
    if not follow:
        return
    typer.echo("Waiting for new alerts. Press Ctrl+C to stop.")
    position = path.stat().st_size if path.exists() else 0
    try:
        while True:
            time.sleep(0.5)
            if not path.exists():
                continue
            size = path.stat().st_size
            if size < position:
                position = 0  # the file was replaced
            if size > position:
                with open(path, encoding="utf-8") as fh:
                    fh.seek(position)
                    for line in fh.read().splitlines():
                        typer.echo(line)
                    position = fh.tell()
    except KeyboardInterrupt:
        pass


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
            typer.echo(f"uptime:   {_uptime(cfg, end)}")
            if cfg.settings.recorder.goal_hours > 0:
                typer.echo(f"burst:    {_burst(cfg, end, running=False)}")
            days_line, size_line = _history(cfg, end, s)
            typer.echo(f"history:  {days_line}")
            typer.echo(f"          {size_line}")
        raise typer.Exit(1)

    typer.echo(f"agentd:    {state} (pid {s['pid']})")
    run_time = _duration(time.time() - s["started_at"])
    typer.echo(f"running:   {run_time}, since {_when(s['started_at'])} ({clock.zone()})")
    if state == "unresponsive":
        age = int(time.time() - s["updated_at"])
        typer.echo(f"           no heartbeat for {age}s; the numbers below are old")
    for line in _feed_lines(s):
        typer.echo(line)
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
    typer.echo(f"uptime:    {_uptime(cfg, time.time())}")
    if cfg.settings.recorder.goal_hours > 0:
        typer.echo(f"burst:     {_burst(cfg, time.time(), running=True)}")
    days_line, size_line = _history(cfg, time.time(), s)
    typer.echo(f"history:   {days_line}")
    typer.echo(f"           {size_line}")
    typer.echo(f"verdicts:  {_verdicts(s.get('live'))}")
    lag = s.get("lag_s")
    covered = s.get("covered_lag_s")
    covered_text = f" (all data up to {covered}s ago)" if covered is not None else ""
    typer.echo(
        f"quality:   {f'{lag}s behind the chain' if lag is not None else 'lag n/a'}"
        f"{covered_text}; "
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


def _feed_lines(s: dict) -> list[str]:
    """One line per live connection (older status files have a single feed)."""
    connections = s.get("connections")
    if not connections:
        age = s.get("last_message_age_s")
        last = f"last message {age}s ago" if age is not None else "no messages yet"
        connected = "connected" if s.get("connected") else "NOT connected"
        return [f"feed:      {s.get('source') or '-'}, {connected}, {last}"]
    lines = []
    for c in connections:
        age = c.get("last_message_age_s")
        state = "connected" if c.get("connected") else "NOT connected"
        parts = [c.get("source") or "-", state]
        parts.append(f"last message {age}s ago" if age is not None else "no messages yet")
        if c.get("lag_s") is not None:
            parts.append(f"{c['lag_s']}s behind")
        if c.get("first_share") is not None:
            parts.append(f"first for {c['first_share']:.0%}")
        parts.append(f"{c.get('reconnects', 0)} drops")
        lines.append(f"feed {c['name']}:    " + ", ".join(parts))
    return lines


def _verdicts(live: dict | None) -> str:
    if not live:
        return "n/a (engine version without verdicts)"
    if not live.get("enabled"):
        return f"OFF ({live.get('rules_error') or 'no rules'})"
    counts = live.get("verdicts", {})
    total = sum(counts.values())
    by_level = ", ".join(f"{level} {counts[level]:,}" for level in sorted(counts)) or "none yet"
    wait = live.get("median_wait_s")
    wait_text = f"; median {wait:.1f}s after the cutoff" if wait is not None else ""
    alert = live.get("last_alert")
    alert_text = f"; last pass {_clock(alert['at'])}" if alert else "; no pass yet"
    error = f"; RULES PROBLEM: {live['rules_error']}" if live.get("rules_error") else ""
    return (
        f"{total:,} made ({by_level}); {live.get('verdicts_with_gap', 0):,} with gaps; "
        f"{live.get('tokens_tracked', 0):,} tokens tracked{wait_text}{alert_text}{error}"
    )


def _uptime(cfg, end: float, window_s: float = 86400) -> str:
    """Share of the last 24 hours (or of the time since recording began) recorded, against
    the target in settings.yaml (spec 15.1: at least 95%)."""
    first = first_recorded_ts(cfg.data_dir / "raw")
    if first is None or end <= first:
        return "nothing recorded yet"
    since = max(first, end - window_s)
    coverage = recorded_coverage(cfg.data_dir, end, since)
    if coverage is None or coverage.span_seconds <= 0:
        return "nothing recorded yet"
    share = coverage.recorded_seconds / coverage.span_seconds
    target = cfg.settings.recorder.uptime_target
    verdict = "meets" if share >= target else "BELOW"
    span = "the last 24 h" if since > first else f"the time since {_when(first)}"
    return f"{share:.1%} of {span} recorded; {verdict} the {target:.0%} target"


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
    """A moment in local time: '4:03 AM today', '11:10 PM yesterday' or 'Nov 10 4:03 AM'."""
    return clock.when(epoch)


def _clock(epoch: float, *, with_date: bool = False) -> str:
    """'9:05:27 PM', or 'Nov 12 9:05:27 PM' with the date."""
    return clock.stamp(epoch) if with_date else clock.clock(epoch, seconds=True)
