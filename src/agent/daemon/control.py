"""Small files the engine and the `agentd` command use to find each other.

    <data_dir>/run/agentd.pid    process id of the running engine
    <data_dir>/run/status.json   heartbeat, rewritten every 2 seconds
    <data_dir>/run/stop.flag     created by `agentd stop`; the engine exits when it sees it
    <data_dir>/run/pause.flag    exists while recording is paused (`agentd pause-recording`)
    <data_dir>/run/agentd.log    engine log (rotating)

Files are used instead of signals because a background process on Windows has no console
to receive Ctrl+C, and Windows has no catchable "please stop" signal.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

HEARTBEAT_S = 2.0
UNRESPONSIVE_AFTER_S = 15.0


@dataclass(frozen=True)
class RunFiles:
    run_dir: Path

    @property
    def pid(self) -> Path:
        return self.run_dir / "agentd.pid"

    @property
    def status(self) -> Path:
        return self.run_dir / "status.json"

    @property
    def stop_flag(self) -> Path:
        return self.run_dir / "stop.flag"

    @property
    def pause_flag(self) -> Path:
        return self.run_dir / "pause.flag"

    @property
    def log(self) -> Path:
        return self.run_dir / "agentd.log"

    @property
    def crash_output(self) -> Path:
        return self.run_dir / "agentd.out"


def process_started_at(pid: int) -> float | None:
    """When process `pid` started (epoch seconds), or None if it is not running.

    Never use os.kill(pid, 0) for this on Windows: there it terminates the process.
    """
    if pid <= 0:
        return None
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            exit_code = wintypes.DWORD()
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return None
            if exit_code.value != 259:  # 259 = STILL_ACTIVE
                return None
            created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [wintypes.LPFILETIME] * 4
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
            return (ticks - 116_444_736_000_000_000) / 10_000_000  # 100 ns ticks since 1601
        finally:
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    try:
        return os.stat(f"/proc/{pid}").st_ctime
    except OSError:
        return 0.0  # alive, start time unknown (macOS); the engine records 0.0 too


def write_status(files: RunFiles, status: dict) -> None:
    """Replace status.json in one step, so a reader never sees a half-written file."""
    tmp = files.status.with_name(files.status.name + ".tmp")
    try:
        tmp.write_text(json.dumps(status), encoding="utf-8")
        os.replace(tmp, files.status)
    except PermissionError:
        # Windows refuses the swap while a reader has the file open. It is open for a few
        # milliseconds at most, and the next heartbeat lands 2 seconds later.
        pass


def read_status(files: RunFiles) -> dict | None:
    for _ in range(3):
        try:
            return json.loads(files.status.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (PermissionError, json.JSONDecodeError):
            time.sleep(0.05)
    return None


def engine_state(files: RunFiles) -> tuple[str, dict | None]:
    """("running" | "unresponsive" | "stopped", last status).

    "unresponsive" means the engine's process exists but its heartbeat is old.
    """
    status = read_status(files)
    if not status or status.get("state") != "running":
        return "stopped", status
    started = process_started_at(int(status.get("pid", 0)))
    # The process must be the same one that wrote the status. If the engine died and
    # Windows gave its id to another program, the start times will not match.
    recorded = status.get("process_started_at")
    if started is None or (recorded is not None and abs(started - recorded) > 5):
        return "stopped", status
    if time.time() - status.get("updated_at", 0) > UNRESPONSIVE_AFTER_S:
        return "unresponsive", status
    return "running", status
