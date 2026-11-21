import os
import time

import pytest
from typer.testing import CliRunner

from agent.cli.agent import app as agent_app
from agent.cli.agentd import app as agentd_app
from agent.daemon.control import RunFiles, process_started_at, write_status
from helpers import make_project

runner = CliRunner()


@pytest.fixture
def project(tmp_path, monkeypatch):
    make_project(tmp_path)
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    return tmp_path


def test_status_and_stop_when_the_engine_is_not_running(project):
    result = runner.invoke(agentd_app, ["status"])
    assert result.exit_code == 1 and "not running" in result.output
    result = runner.invoke(agentd_app, ["stop"])
    assert result.exit_code == 0 and "not running" in result.output


def test_pause_and_resume_set_and_clear_the_flag(project):
    flag = RunFiles(project / "data" / "run").pause_flag
    result = runner.invoke(agentd_app, ["pause-recording"])
    assert result.exit_code == 0 and flag.exists()
    result = runner.invoke(agentd_app, ["resume-recording"])
    assert result.exit_code == 0 and not flag.exists()


def test_status_of_a_running_engine_shows_recording_and_gaps(project):
    files = RunFiles(project / "data" / "run")
    files.run_dir.mkdir(parents=True)
    now = time.time()
    write_status(files, {
        "state": "running", "pid": os.getpid(),
        "process_started_at": process_started_at(os.getpid()),
        "started_at": now - 3700, "updated_at": now, "source": "solana-public",
        "connected": True, "last_message_age_s": 0.1, "reconnects": 1,
        "last_disconnect": {"at": now - 600, "source": "solana-public", "reason": "no data"},
        "messages": 500_000, "mb_received": 900.5, "lag_s": 1.4, "creates": 1500,
        "trades": 98_000, "duplicates": 12, "simulated": 3, "truncated": 0, "unknown_events": 0,
        "stale_idl_events": 98_000, "decode_errors": 0, "rows_dropped": 40,
        "recording": True, "paused": False, "open_gap": None,
        "rows_written": {"tokens": 1490, "trades": 97_500}, "files_written": 180,
        "mb_written": 31.25, "last_write_at": now - 12, "gaps_logged": 1,
        "last_gap": {"at": now - 590, "reason": "disconnect", "seconds": 9},
        "disk_free_gb": 79.5, "data_dir": "D:\\\\memecoin-agent-data",
        "msgs_per_s": 131.2, "failed_share": 0.71, "trades_per_s": 27.4,
        "creates_per_hr": 1480, "gb_per_day": 24.3,
    })  # fmt: skip
    result = runner.invoke(agentd_app, ["status"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "agentd:    running" in out and "running:   1 h 01 m, since" in out
    assert "saved:     nothing recorded yet" in out
    assert "burst:     [" + "-" * 30 + "]" in out
    assert "0% of 24 h; 1 d 00 h to go, done about" in out
    assert "history:   [" + "-" * 30 + "] 0.0% of 28 days; 0.00 days done, 28.00 days to go" in out
    # 900.5 MB received in this run; the rest of the 28 days at 310 GB a day of backfill.
    assert "           downloaded 0.9 GB of about 8,681 GB (0.01%); kept 0.00 GB on disk" in out
    assert "recording: on; written 1,490 tokens, 97,500 trades (31.25 MB in 180 files)" in out
    assert "1.4s behind the chain; 12 repeats and 3 simulated dropped" in out
    assert "98,000 events carry fields newer than the IDL" in out
    assert "gaps:      1 logged this run" in out and "disconnect (9s)" in out
    assert "79.5 GB free" in out


def test_status_shows_when_recording_is_paused(project):
    files = RunFiles(project / "data" / "run")
    files.run_dir.mkdir(parents=True)
    now = time.time()
    write_status(files, {
        "state": "running", "pid": os.getpid(),
        "process_started_at": process_started_at(os.getpid()), "started_at": now - 5,
        "updated_at": now, "source": "test", "connected": True, "reconnects": 0,
        "creates": 0, "trades": 0, "recording": False, "paused": True, "open_gap": "paused",
    })  # fmt: skip
    result = runner.invoke(agentd_app, ["status"])
    assert result.exit_code == 0, result.output
    assert "PAUSED" in result.output and "one open now (paused)" in result.output


def test_status_when_stopped_shows_saved_time_and_burst_progress(project):
    from agent.ingest.pipeline import Pipeline
    from helpers import live

    data = project / "data"
    pipeline = Pipeline(data, data / "run")
    pipeline.handle(live("pump-BuyV3"))  # on-chain time 1794482629
    pipeline.handle(live("pump-BuyV2"))  # on-chain time 1794482633
    pipeline.close()
    result = runner.invoke(agentd_app, ["status"])
    assert result.exit_code == 1 and "not running" in result.output
    assert "saved:    4 s recorded out of 4 s since" in result.output
    assert "0% of 24 h; 23 h 59 m to go (not recording now)" in result.output
    assert "0.0% of 28 days; 0.00 days done, 28.00 days to go" in result.output
    assert "downloaded 0.0 GB of about 8,680 GB (0.00%)" in result.output


def test_status_says_whether_model_scores_are_on_and_why_not():
    from agent.cli.agentd import _models

    live = {"enabled": True, "verdicts": {}, "models": {
        "on": True, "version": "r5", "trained_days": ["2026-11-12", "2026-11-16"],
        "scored": 1200, "not_scored": 34, "last_not_scored": "not paired with SOL"}}  # fmt: skip
    assert _models(live) == (
        "on (r5, trained 2026-11-12 to 2026-11-16; analysis only: no signal made money in "
        "the backtest); 1,200 verdicts scored, 34 not; latest reason: not paired with SOL")
    off = {"enabled": True, "models": {"on": False, "why": "lightgbm is not installed"}}
    assert _models(off) == "OFF (lightgbm is not installed)"
    assert _models({"enabled": True}) == "n/a (engine version without model scores)"
    assert _models(None) == "n/a (engine version without model scores)"


def test_audit_explains_when_nothing_is_recorded_yet(project):
    result = runner.invoke(agent_app, ["audit"])
    assert result.exit_code == 1 and "Nothing has been recorded yet" in result.output


def _live_run(project, *, tamper: bool = False, models: bool = False,
              tamper_scores: bool = False):
    """Record real transactions and make live verdicts the way the engine does. With
    `models`, also score them with a small saved model, as the engine does at start-up."""
    from agent.daemon.live_state import VERDICT_COLUMNS, VERDICT_TABLE, LiveSignals
    from agent.features.token_state import CreatorIndex
    from agent.ingest.pipeline import Pipeline
    from agent.ingest.recorder import ParquetSink
    from agent.models.rules import parse_rules
    from helpers import fixture_names, live

    data = project / "data"
    rules = parse_rules({"caution": [{"feature": "snipers", "at_least": 1, "reason": "x"}]})
    signals = LiveSignals(rules, "t", CreatorIndex(), [])
    if models:
        from agent.daemon.main import _launch_history
        from agent.scoring.live import load_scorer
        from scoring_helpers import make_models

        make_models(data)
        # Nothing was recorded before these few seconds, so no hour is covered: turn
        # the minimum off, for the engine here and for `check-live` (settings.yaml).
        with open(project / "config" / "settings.yaml", "a", encoding="utf-8") as f:
            f.write("live_models: {min_hour_covered: 0}\n")
        history = _launch_history(data, [])
        signals = LiveSignals(rules, "t", history.launches, [],
                              scorer=load_scorer(data, min_hour_covered=0), history=history)
    pipeline = Pipeline(data, data / "run", on_row=signals.add)
    for name in fixture_names():
        if name.startswith("pump-"):
            pipeline.handle(live(name))
    pipeline.flush()
    sink = ParquetSink(data / "live", {VERDICT_TABLE: VERDICT_COLUMNS}, prefix="verdict")
    for verdict in signals.due(10**12):
        if tamper:
            verdict["snapshot"] = {**verdict["snapshot"], "holders": 999}
        if tamper_scores and "p" in verdict["scores"]:
            p = verdict["scores"]["p"]
            verdict["scores"] = {**verdict["scores"], "p": {**p, "bad": p["bad"] + 1e-12}}
        sink.add(VERDICT_TABLE, verdict)
    sink.flush()


def test_check_live_finds_live_and_replayed_signals_identical(project):
    _live_run(project)
    result = runner.invoke(agent_app, ["check-live", "--hours", "1"])
    assert result.exit_code == 0, result.output
    assert "identical: 12 (100.00%)" in result.output and "different: 0" in result.output


def test_check_live_says_when_the_verdicts_carry_no_model_scores(project):
    _live_run(project)
    result = runner.invoke(agent_app, ["check-live", "--hours", "1"])
    assert result.exit_code == 0 and "Model scores: none in these verdicts" in result.output


def test_check_live_finds_live_and_replayed_model_scores_identical(project):
    """Spec 15.3 step L1: scores made live equal the scores made again from the Parquet
    files the recorder wrote, with the same code, to the last digit."""
    pytest.importorskip("lightgbm")
    pytest.importorskip("sklearn")
    _live_run(project, models=True)
    result = runner.invoke(agent_app, ["check-live", "--hours", "1"])
    assert result.exit_code == 0, result.output
    assert "identical: 12 (100.00%)" in result.output
    # One of the three recorded coins is not paired with SOL: no score, and the reason.
    assert "12 verdicts carry a score record; 8 with probabilities, 4 without" in result.output
    assert "no score (4): not paired with SOL" in result.output
    assert "made again from the recorded data: 12" in result.output
    assert "identical to the last digit: 12 (100.00%)" in result.output
    assert result.output.count("different: 0") == 2


def test_check_live_reports_a_model_score_that_differs_in_the_last_digit(project):
    pytest.importorskip("lightgbm")
    pytest.importorskip("sklearn")
    _live_run(project, models=True, tamper_scores=True)
    result = runner.invoke(agent_app, ["check-live", "--hours", "1"])
    assert result.exit_code == 1
    assert "identical: 12 (100.00%)" in result.output  # the signals still match
    assert "identical to the last digit: 4 (33.33%)" in result.output  # the unscored ones
    assert "different: 8" in result.output and "p (8)" in result.output


def test_check_live_reports_a_difference(project):
    _live_run(project, tamper=True)
    result = runner.invoke(agent_app, ["check-live", "--hours", "1"])
    assert result.exit_code == 1
    assert "different: 12" in result.output and "holders (12)" in result.output


def test_alerts_shows_the_newest_alert_lines(project):
    files = RunFiles(project / "data" / "run")
    files.run_dir.mkdir(parents=True)
    result = runner.invoke(agentd_app, ["alerts"])
    assert result.exit_code == 1 and "No alerts yet" in result.output
    (files.run_dir / "alerts.log").write_text(
        "".join(f"12:00:0{i}  MINT{i}  $X  T+60s  PASS  no rule fired\n" for i in range(5)),
        encoding="utf-8",
    )
    result = runner.invoke(agentd_app, ["alerts", "--last", "2"])
    assert result.exit_code == 0
    assert [ln.split("  ")[1] for ln in result.output.splitlines()] == ["MINT3", "MINT4"]


def _fake_epochs(monkeypatch, outcomes):
    """Replace the per-epoch download with one that returns (or raises) `outcomes`."""
    import agent.cli.agent as cli

    called = []

    def fake(cfg, epoch, **kwargs):
        called.append((epoch, kwargs["ask"]))
        outcome = outcomes[epoch]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(cli, "_backfill_epoch", fake)
    return called


def test_backfill_runs_several_epochs_and_one_stop_stops_them_all(project, monkeypatch):
    called = _fake_epochs(monkeypatch, {1085: "done", 1086: "stopped", 1087: "done"})
    result = runner.invoke(agent_app, ["backfill", "--epoch", "1085-1087", "--yes"])
    assert [e for e, _ in called] == [1085, 1086]  # 1087 never starts
    assert result.exit_code == 130 and "Not finished: epoch 1086, 1087" in result.output

    # Ctrl+C outside a download (e.g. while waiting for an epoch to be published).
    called = _fake_epochs(monkeypatch, {1085: KeyboardInterrupt(), 1086: "done"})
    result = runner.invoke(agent_app, ["backfill", "--epoch", "1085-1086", "--yes"])
    assert [e for e, _ in called] == [1085] and result.exit_code == 130


def test_backfill_asks_once_for_several_epochs_and_reports_failures(project, monkeypatch):
    called = _fake_epochs(monkeypatch, {1085: "failed", 1086: "done"})
    result = runner.invoke(agent_app, ["backfill", "--epoch", "1085,1086"], input="y\n")
    assert called == [(1085, False), (1086, False)]  # asked once, up front
    assert result.exit_code == 1 and "failed in epoch 1085" in result.output

    called = _fake_epochs(monkeypatch, {1085: "done"})
    result = runner.invoke(agent_app, ["backfill", "--epoch", "1085-1086"], input="n\n")
    assert called == [] and result.exit_code == 1

    called = _fake_epochs(monkeypatch, {1085: "done"})
    assert runner.invoke(agent_app, ["backfill", "--epoch", "1085"]).exit_code == 0
    assert called == [(1085, True)]  # a single epoch asks with its exact size

    result = runner.invoke(agent_app, ["backfill", "--epoch", "1085-1086", "--from-slot", "5"])
    assert result.exit_code == 2


def _http_error(code: int):
    import httpx

    request = httpx.Request("GET", "https://files.old-faithful.net/1091/epoch-1091.car")
    return httpx.HTTPStatusError(
        f"HTTP {code}", request=request, response=httpx.Response(code, request=request)
    )


def test_backfill_wait_rides_out_rate_limits_but_not_real_errors(project, monkeypatch):
    import httpx

    import agent.cli.agent as cli
    from agent.ingest.oldfaithful_backfill import BackfillError

    class Archive:  # the slot index read fails in a given order
        def __init__(self, errors):
            self.errors = list(errors)

        def load_index(self, client):
            raise self.errors.pop(0)

    sleeps = []
    monkeypatch.setattr(cli.time, "sleep", sleeps.append)

    def run(errors, *flags):
        monkeypatch.setattr(cli, "_backfill", lambda *a, **k: Archive(errors))
        return runner.invoke(agent_app, ["backfill", "--epoch", "1091", "--yes", *flags])

    # What happened on 2026-11-19: a 429 while waiting for the epoch to be published.
    not_yet = BackfillError("epoch 1091 is not in the archive (yet)")
    errors = [not_yet, _http_error(429), httpx.ConnectError("dropped"), not_yet,
              _http_error(403)]  # fmt: skip
    result = run(errors, "--wait")
    assert len(sleeps) == 4 and result.exit_code == 1
    assert "HTTP 429 (temporary); checking again" in result.output
    assert "could not reach the archive (ConnectError); checking again" in result.output
    assert "Could not read the epoch's slot index: HTTP 403" in result.output  # not temporary

    sleeps.clear()  # without --wait, a temporary error still ends the command at once
    result = run([_http_error(429)])
    assert sleeps == [] and result.exit_code == 1
