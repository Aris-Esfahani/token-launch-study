"""The running total of data downloaded across engine runs."""

from agent.ingest import downloads


def status(started_at: float, mb: float, *, pid: int = 7, exact: int | None = None) -> dict:
    s = {"started_at": started_at, "pid": pid, "mb_received": mb}
    if exact is not None:
        s["bytes_received"] = exact
    return s


def test_previous_run_is_added_once(tmp_path):
    first = status(1000.0, 250.0, exact=250_123_456)
    downloads.record_previous_run(tmp_path, first)
    downloads.record_previous_run(tmp_path, first)  # a second start must not count it twice
    (entry,) = downloads.read_ledger(tmp_path)
    assert entry["bytes"] == 250_123_456 and entry["run_started_at"] == 1000.0
    assert entry["estimated"] is False


def test_total_counts_finished_runs_and_the_current_one(tmp_path):
    downloads.record_previous_run(tmp_path, status(1000.0, 250.0))  # older status: MB only
    current = status(5000.0, 0, pid=8, exact=40_000_000)
    assert downloads.total_bytes(tmp_path, current) == 250_000_000 + 40_000_000
    # Once the next run has added it to the ledger it is not counted again.
    downloads.record_previous_run(tmp_path, current)
    assert downloads.total_bytes(tmp_path, current) == 290_000_000
    assert downloads.total_bytes(tmp_path, None) == 290_000_000


def test_estimated_entries_count_and_nothing_means_zero(tmp_path):
    assert downloads.total_bytes(tmp_path, None) == 0
    downloads.record_previous_run(tmp_path, None)
    downloads.record_previous_run(tmp_path, {"pid": 3})  # no start time: ignored
    assert downloads.read_ledger(tmp_path) == []
    downloads.append(tmp_path, {"kind": "live", "bytes": 1_000, "estimated": True})
    assert downloads.total_bytes(tmp_path, None) == 1_000
