"""Start the engine so it keeps running after the window that started it is closed.

Windows terminals can end every program started from them when their window closes: they
put the shell in a "job" that kills its members on close and does not let them leave.
Detaching (the usual approach) is not enough there; on 2026-11-12 closing the user's
terminal stopped the engine 20 seconds after `agentd start`.

So on Windows the engine is started by Windows' own process service (WMI,
`Win32_Process.Create`). The engine then belongs to no window or job, runs as the same
user in the same session, and, started with pythonw.exe, has no window of its own. If that
service is unavailable, it falls back to a detached start and says so.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

WMI_TIMEOUT_S = 60

# PowerShell reads the command line and folder from environment variables, so paths with
# spaces or quotes never need escaping.
_WMI_SCRIPT = (
    "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
    "@{CommandLine = $env:AGENT_LAUNCH_COMMAND; CurrentDirectory = $env:AGENT_LAUNCH_CWD}; "
    '"$($r.ReturnValue) $($r.ProcessId)"'
)


@dataclass(frozen=True)
class Launched:
    pid: int
    independent: bool  # False: closing the window that started it may stop it
    note: str = ""


def windowless_python() -> str:
    """The Python that runs without a console window (pythonw.exe) where there is one."""
    exe = Path(sys.executable)
    if sys.platform == "win32" and exe.with_name("pythonw.exe").exists():
        return str(exe.with_name("pythonw.exe"))
    return str(exe)


def spawn_detached(command: list[str], cwd: Path, output: Path) -> Launched:
    """Start `command` in the background, independent of this window where possible.
    On the fallback path, its output goes to `output`."""
    if sys.platform != "win32":
        return Launched(_popen_detached(command, cwd, output), True)
    try:
        return Launched(_start_via_wmi(command, cwd), True)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        note = f"Windows' process service could not start it ({exc})"
        return Launched(_popen_detached(command, cwd, output), False, note)


def _start_via_wmi(command: list[str], cwd: Path) -> int:
    root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    powershell = root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    env = {
        **os.environ,
        "AGENT_LAUNCH_COMMAND": subprocess.list2cmdline(command),
        "AGENT_LAUNCH_CWD": str(cwd),
    }
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-Command", _WMI_SCRIPT],
        capture_output=True,
        text=True,
        env=env,
        timeout=WMI_TIMEOUT_S,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    parts = result.stdout.split()
    if result.returncode != 0 or len(parts) != 2 or parts[0] != "0":
        answer = result.stdout.strip() or result.stderr.strip()[:200] or "no answer"
        raise ValueError(f"WMI answered {answer!r}")
    return int(parts[1])


def _popen_detached(command: list[str], cwd: Path, output: Path) -> int:
    out = open(output, "ab")  # noqa: SIM115 - handed to the child process
    kwargs: dict = dict(cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
    try:
        if sys.platform != "win32":
            return subprocess.Popen(command, start_new_session=True, **kwargs).pid
        detached = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            flags = detached | subprocess.CREATE_BREAKAWAY_FROM_JOB
            return subprocess.Popen(command, creationflags=flags, **kwargs).pid
        except OSError:
            return subprocess.Popen(command, creationflags=detached, **kwargs).pid
    finally:
        out.close()
