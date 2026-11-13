"""`agentd start` must leave an engine that survives closing the window it was typed in.

On 2026-11-12 closing the user's terminal stopped the engine 20 seconds after it started.
These tests reproduce that kind of terminal (see terminal_sim.py) with a dummy program in
place of the engine.
"""

import sys
import time
from pathlib import Path

import pytest

from agent.daemon.control import process_started_at
from agent.daemon.launch import spawn_detached, windowless_python

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows behaviour")

DUMMY = "import time; time.sleep(120)  # agent-test-dummy"


def started_in_closing_terminal(how: str, tmp_path: Path) -> int:
    from terminal_sim import closing_terminal

    code = f"""
import sys
from pathlib import Path
from agent.daemon import launch
command = [launch.windowless_python(), "-c", {DUMMY!r}]
if {how!r} == "plain":
    pid = launch._popen_detached(command, Path({str(tmp_path)!r}), Path({str(tmp_path / "out.txt")!r}))
else:
    result = launch.spawn_detached(command, Path({str(tmp_path)!r}), Path({str(tmp_path / "out.txt")!r}))
    assert result.independent, result.note
    pid = result.pid
Path(REPORT).write_text(str(pid))
"""  # noqa: E501
    pid, terminal = closing_terminal(code)
    assert terminal.returncode is not None, "the fake terminal should be gone"
    return pid


def test_plain_detached_start_dies_with_the_terminal(tmp_path):
    # Shows the fake terminal behaves like the real one did: this is the old start method.
    from terminal_sim import end_process

    pid = started_in_closing_terminal("plain", tmp_path)
    try:
        time.sleep(1)
        assert process_started_at(pid) is None
    finally:
        end_process(pid)


def test_engine_start_survives_closing_the_terminal(tmp_path):
    from terminal_sim import end_process

    pid = started_in_closing_terminal("agentd", tmp_path)
    try:
        time.sleep(2)
        assert process_started_at(pid) is not None, "the program ended with the terminal"
    finally:
        end_process(pid)


def test_background_engine_uses_the_windowless_python():
    assert Path(windowless_python()).name.lower() == "pythonw.exe"


def test_launch_reports_the_process_it_started(tmp_path):
    from terminal_sim import end_process

    result = spawn_detached([windowless_python(), "-c", DUMMY], tmp_path, tmp_path / "out.txt")
    try:
        assert result.independent and result.note == ""
        assert process_started_at(result.pid) is not None
    finally:
        end_process(result.pid)
