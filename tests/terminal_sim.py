"""A stand-in for a terminal window that ends everything started from it when it closes.

Some Windows terminals put the shell in a "job" that kills all of its members on close and
does not let them leave (no breakaway). `closing_terminal` reproduces that: it runs some
Python code inside such a job, waits for the code to report a process id, then "closes
the window" by closing the job. Windows only.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import time
import uuid
from ctypes import wintypes
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
KILL_ON_JOB_CLOSE = 0x2000


class _Basic(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]  # fmt: skip


class _Extended(ctypes.Structure):
    _fields_ = [
        ("Basic", _Basic), ("Io", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]  # fmt: skip


def _kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
    ]  # fmt: skip
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    return k


# Runs inside the fake terminal: join the job, then run the caller's code.
_JOIN = """
import ctypes, sys
from ctypes import wintypes
k = ctypes.WinDLL("kernel32", use_last_error=True)
k.OpenJobObjectW.restype = wintypes.HANDLE
k.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
job = k.OpenJobObjectW(0x1F001F, False, sys.argv[1])
assert job and k.AssignProcessToJobObject(job, wintypes.HANDLE(-1)), ctypes.get_last_error()
k.CloseHandle(wintypes.HANDLE(job))
REPORT = sys.argv[2]
"""


def closing_terminal(code: str, timeout_s: float = 60) -> tuple[int, subprocess.Popen]:
    """Run `code` in a fake terminal, then close the terminal.

    `code` must write a process id to the file named by the variable REPORT. Returns that
    process id and the (now closed) terminal process.
    """
    k = _kernel32()
    name = f"Local\\agent-test-terminal-{uuid.uuid4().hex}"
    job = k.CreateJobObjectW(None, name)
    limits = _Extended()
    limits.Basic.LimitFlags = KILL_ON_JOB_CLOSE  # and no breakaway allowed
    assert k.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
    report = Path(tempfile.gettempdir()) / f"agent-test-terminal-{uuid.uuid4().hex}.txt"
    script = _JOIN + code + "\nimport time\ntime.sleep(600)\n"
    # The base interpreter, not the virtual environment's launcher, so the fake terminal is
    # one ordinary process. The code under test only needs the standard library and src/.
    python = getattr(sys, "_base_executable", sys.executable)
    terminal = subprocess.Popen(
        [python, "-c", script, name, str(report)],
        env={**os.environ, "PYTHONPATH": str(SRC)},
    )
    try:
        deadline = time.monotonic() + timeout_s
        while not (report.exists() and report.read_text().strip()):
            if terminal.poll() is not None:
                raise RuntimeError(f"the fake terminal exited early ({terminal.returncode})")
            if time.monotonic() > deadline:
                raise TimeoutError("the code in the fake terminal did not report a process id")
            time.sleep(0.1)
        pid = int(report.read_text().strip())
    finally:
        k.CloseHandle(job)  # close the window: everything still in the job is ended
        report.unlink(missing_ok=True)
    terminal.wait(timeout=10)
    return pid, terminal


def end_process(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
