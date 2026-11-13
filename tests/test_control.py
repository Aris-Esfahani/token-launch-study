import os
import subprocess
import sys
import time

from agent.daemon.control import (
    UNRESPONSIVE_AFTER_S,
    RunFiles,
    engine_state,
    process_started_at,
    read_status,
    write_status,
)


def _status(**overrides) -> dict:
    base = {
        "state": "running",
        "pid": os.getpid(),
        "process_started_at": process_started_at(os.getpid()),
        "updated_at": time.time(),
    }
    return {**base, **overrides}


def test_process_start_time_is_stable_for_a_live_process():
    first = process_started_at(os.getpid())
    assert isinstance(first, float)
    assert process_started_at(os.getpid()) == first


def test_checking_a_process_does_not_kill_it():
    # On Windows, os.kill(pid, 0) terminates the target. Make sure our check does not.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert process_started_at(child.pid) is not None
        time.sleep(0.2)
        assert child.poll() is None, "the liveness check must not end the process"
    finally:
        child.kill()
        child.wait()


def test_finished_process_is_reported_as_not_running():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert process_started_at(child.pid) is None
    assert process_started_at(0) is None


def test_status_round_trip(tmp_path):
    files = RunFiles(tmp_path)
    assert read_status(files) is None
    write_status(files, {"state": "running", "n": 1})
    write_status(files, {"state": "running", "n": 2})
    assert read_status(files) == {"state": "running", "n": 2}


def test_engine_state(tmp_path):
    files = RunFiles(tmp_path)
    assert engine_state(files)[0] == "stopped"  # no status file at all

    write_status(files, _status())
    assert engine_state(files)[0] == "running"

    write_status(files, _status(updated_at=time.time() - UNRESPONSIVE_AFTER_S - 5))
    assert engine_state(files)[0] == "unresponsive"

    write_status(files, _status(state="stopped"))
    assert engine_state(files)[0] == "stopped"


def test_reused_process_id_is_not_mistaken_for_the_engine(tmp_path):
    # Status left behind by an engine that died; its id now belongs to another program
    # (here: this test process), which started at a different time.
    files = RunFiles(tmp_path)
    write_status(files, _status(process_started_at=process_started_at(os.getpid()) - 3600))
    assert engine_state(files)[0] == "stopped"
