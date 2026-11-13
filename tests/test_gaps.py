"""Gap log: every period when recording was off or unreliable must be written down."""

from agent.ingest.gaps import GapLog, GapTracker, read_gaps


def tracker(tmp_path) -> GapTracker:
    return GapTracker(GapLog(tmp_path / "gaps"), tmp_path / "recorder_state.json")


def gaps(tmp_path) -> list[dict]:
    return read_gaps(tmp_path / "gaps")


def test_no_gap_while_everything_works(tmp_path):
    t = tracker(tmp_path)
    for i in range(5):
        t.on_event(1000 + i, 50 + i)
    assert gaps(tmp_path) == [] and t.active and t.open_reason is None


def test_disconnect_gap_runs_from_last_event_before_to_first_event_after(tmp_path):
    t = tracker(tmp_path)
    t.on_event(1000, 50)
    t.begin("disconnect", "connection reset")
    assert not t.active and t.open_reason == "disconnect"
    t.on_event(1005, 55)  # must be ignored: we are not connected
    t.clear("disconnect")
    assert gaps(tmp_path) == []  # still open until an event arrives
    t.on_event(1030, 80)
    (gap,) = gaps(tmp_path)
    assert (gap["start_ts"], gap["end_ts"]) == (1000.0, 1030.0)
    assert (gap["start_slot"], gap["end_slot"]) == (50, 80)
    assert gap["reason"] == "disconnect" and gap["detail"] == "connection reset"
    assert gap["source"] == "recorder" and gap["recorded_at"] > 0
    assert t.active and t.open_reason is None


def test_pause_and_disconnect_overlapping_make_one_gap(tmp_path):
    t = tracker(tmp_path)
    t.on_event(1000, 50)
    t.begin("paused")
    t.begin("disconnect", "no data for 20s")
    t.clear("disconnect")
    t.on_event(1010, 60)  # connected again but still paused: not recorded
    assert gaps(tmp_path) == []
    t.clear("paused")
    t.on_event(1020, 70)
    (gap,) = gaps(tmp_path)
    assert (gap["start_ts"], gap["end_ts"], gap["reason"]) == (1000.0, 1020.0, "paused")
    assert "then disconnect (no data for 20s)" in gap["detail"]


def test_restart_after_a_clean_stop(tmp_path):
    first = tracker(tmp_path)
    first.on_event(1000, 50)
    first.on_flush()
    first.on_event(1001, 51)
    first.on_flush()
    first.save_state(clean=True)

    second = tracker(tmp_path)
    second.startup()
    assert second.open_reason == "recorder_off"
    second.on_event(5000, 900)
    (gap,) = gaps(tmp_path)
    assert (gap["start_ts"], gap["end_ts"], gap["reason"]) == (1001.0, 5000.0, "recorder_off")
    assert (gap["start_slot"], gap["end_slot"]) == (51, 900)


def test_restart_after_a_crash_starts_the_gap_at_the_last_event_on_disk(tmp_path):
    first = tracker(tmp_path)
    first.on_event(1000, 50)
    first.on_flush()  # 1000 is on disk
    first.on_event(1040, 90)  # buffered only; lost when the engine is killed

    second = tracker(tmp_path)
    second.startup()
    second.on_event(5000, 900)
    (gap,) = gaps(tmp_path)
    assert (gap["start_ts"], gap["start_slot"], gap["reason"]) == (1000.0, 50, "crash")


def test_crash_right_after_a_restart_is_still_logged_as_a_crash(tmp_path):
    # A clean stop, a restart, then the engine is killed before its first write
    # (2026-11-12: the terminal it was started from was closed 20 seconds in).
    first = tracker(tmp_path)
    first.on_event(1000, 50)
    first.on_flush()
    first.save_state(clean=True)

    second = tracker(tmp_path)
    second.startup()
    second.on_event(1100, 150)  # closes the "recorder off" gap; nothing written yet
    # ...killed here...

    third = tracker(tmp_path)
    third.startup()
    assert third.open_reason == "crash"
    third.on_event(2000, 900)
    found = {g["reason"]: (g["start_ts"], g["end_ts"]) for g in gaps(tmp_path)}
    # The crash gap starts at the last event on disk, so it covers the lost events too
    # (it overlaps the first gap; gaps are read as a union).
    assert found == {"recorder_off": (1000.0, 1100.0), "crash": (1000.0, 2000.0)}


def test_first_ever_start_logs_no_gap(tmp_path):
    t = tracker(tmp_path)
    t.startup()
    t.on_event(1000, 50)
    assert gaps(tmp_path) == []


def test_lost_buffer_moves_the_gap_start_back_to_the_last_event_on_disk(tmp_path):
    t = tracker(tmp_path)
    t.on_event(1000, 50)
    t.on_flush()
    t.on_event(1040, 90)
    t.begin("write_error", "disk refused", lost_buffer=True)
    t.clear("write_error")
    t.on_flush()  # a later, empty write must not pretend the lost rows are on disk
    assert t.flushed_ts == 1000.0
    t.on_event(1100, 150)
    (gap,) = gaps(tmp_path)
    assert (gap["start_ts"], gap["end_ts"], gap["reason"]) == (1000.0, 1100.0, "write_error")


def test_stall_mark_closes_at_the_next_event_without_blocking_recording(tmp_path):
    t = tracker(tmp_path)
    t.on_event(1000, 50)
    t.mark("stall", "engine was suspended for about 600s")
    assert t.active
    t.on_event(1600, 3000)
    (gap,) = gaps(tmp_path)
    assert (gap["start_ts"], gap["end_ts"], gap["reason"]) == (1000.0, 1600.0, "stall")


def test_gaps_are_read_back_in_time_order(tmp_path):
    t = tracker(tmp_path)
    t.on_event(1000, 50)
    for start in (1000, 2000, 3000):
        t.begin("disconnect")
        t.clear("disconnect")
        t.on_event(start + 500, 60)
        t.on_event(start + 1000, 70)
    found = gaps(tmp_path)
    assert [g["end_ts"] for g in found] == [1500.0, 2500.0, 3500.0]
    assert read_gaps(tmp_path / "does-not-exist") == []


# --- several live connections at once (step B) ---------------------------------------


def test_one_connection_dropping_while_another_runs_is_no_gap(tmp_path):
    t = tracker(tmp_path)
    for i in range(5):
        t.on_event(1000 + i, 50 + i, "1")
        t.on_event(1000 + i, 50 + i, "2")
    t.connection_dropped("1", "ConnectionClosedError")
    for i in range(5, 10):
        t.on_event(1000 + i, 50 + i, "2")
    t.on_event(1012, 62, "1")  # back
    t.on_event(1012, 62, "2")
    assert gaps(tmp_path) == [] and t.open_reason is None
    assert (t.last_ts, t.last_slot) == (1012.0, 62)


def test_both_connections_dropping_is_a_gap_from_the_last_event_either_delivered(tmp_path):
    t = tracker(tmp_path)
    for i in range(3):
        t.on_event(1000 + i, 50 + i, "1")
        t.on_event(1000 + i, 50 + i, "2")
    t.on_event(1003, 53, "2")  # connection 2 got a little further
    t.connection_dropped("2", "reset 2")
    t.connection_dropped("1", "reset 1")
    assert t.open_reason == "disconnect"
    t.on_event(1020, 70, "1")
    (gap,) = gaps(tmp_path)
    assert (gap["start_ts"], gap["end_ts"]) == (1003.0, 1020.0)
    assert (gap["start_slot"], gap["end_slot"]) == (53, 70)
    assert gap["detail"] == "reset 1"


def test_a_connection_starting_later_on_chain_covers_nothing_before_its_start(tmp_path):
    # Connection 2's first event is newer than anything connection 1 delivered, and
    # connection 1 drops before reaching it: the time in between was not watched.
    t = tracker(tmp_path)
    t.on_event(1000, 50, "1")
    t.on_event(1003, 53, "2")
    t.connection_dropped("1", "reset 1")
    (gap,) = gaps(tmp_path)
    assert (gap["start_slot"], gap["end_slot"]) == (50, 53) and t.open_reason is None


def two_feeds(t: GapTracker, steps: int) -> None:
    """A fast connection and one running 60 s behind, both up for `steps` seconds."""
    for w in range(steps):
        t.on_event(1000 + w, 100 + w, "fast")
        t.on_event(940 + w, 40 + w, "late")


def test_a_late_connection_covers_the_other_ones_reconnect(tmp_path):
    t = tracker(tmp_path)
    two_feeds(t, 70)  # fast reached slot 169, late slot 109
    t.connection_dropped("fast", "reset")
    t.on_event(1075, 175, "fast")  # reconnected; nothing from 169 to 175 from it
    for w in range(70, 80):  # the late one goes on through that stretch
        t.on_event(940 + w, 40 + w, "late")
    for w in range(80, 140):
        t.on_event(940 + w, 40 + w, "late")
    assert gaps(tmp_path) == [] and t.open_reason is None


def test_a_hole_no_connection_covered_is_logged_even_while_one_is_connected(tmp_path):
    # The late connection drops before it reaches the moment the fast one reconnected:
    # nothing delivered the time in between, though one connection was up all along.
    t = tracker(tmp_path)
    two_feeds(t, 70)
    t.connection_dropped("fast", "reset fast")
    t.on_event(1075, 175, "fast")
    t.on_event(1010, 110, "late")
    assert gaps(tmp_path) == []  # the late one may still get there
    t.connection_dropped("late", "reset late")
    (gap,) = gaps(tmp_path)
    assert (gap["start_slot"], gap["end_slot"]) == (169, 175)
    assert (gap["start_ts"], gap["end_ts"]) == (1069.0, 1075.0)
    assert gap["reason"] == "disconnect" and t.open_reason is None


def test_repeats_from_the_second_connection_count_as_coverage(tmp_path):
    # Connection 2 only ever delivers repeats (connection 1 is always first); it still
    # covered that time, so connection 1 dropping leaves no gap.
    t = tracker(tmp_path)
    for i in range(5):
        t.on_event(1000 + i, 50 + i, "1")
        t.on_event(1000 + i, 50 + i, "2")
    t.connection_dropped("1")
    t.on_event(1005, 55, "2")
    assert gaps(tmp_path) == []


def test_pause_breaks_every_connection(tmp_path):
    t = tracker(tmp_path)
    for i in range(2):
        t.on_event(1000 + i, 50 + i, "1")
        t.on_event(1000 + i, 50 + i, "2")
    t.begin("paused")
    t.on_event(1005, 55, "1")  # not recorded
    t.clear("paused")
    t.on_event(1010, 60, "2")
    t.on_event(1009, 59, "1")
    (gap,) = gaps(tmp_path)
    assert (gap["start_ts"], gap["end_ts"], gap["reason"]) == (1001.0, 1010.0, "paused")


def test_lost_rows_forget_every_connection_run(tmp_path):
    t = tracker(tmp_path)
    t.on_event(1000, 50, "1")
    t.on_event(1000, 50, "2")
    t.on_flush()
    t.on_event(1040, 90, "1")
    t.on_event(1041, 91, "2")
    t.begin("disk_full", "only 1 GB free", lost_buffer=True)
    t.clear("disk_full")
    t.on_event(1100, 150, "2")
    (gap,) = gaps(tmp_path)
    assert (gap["start_ts"], gap["end_ts"], gap["reason"]) == (1000.0, 1100.0, "disk_full")
