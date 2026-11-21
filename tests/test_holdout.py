"""A second held-out period (spec 15.4, `config/holdout.yaml`): its own data folder beside
the first dataset, the check on the validation day, scoring and simulating once, and the
verdict against the first test days."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")  # research packages: pip install -e ".[research]"
np = pytest.importorskip("numpy")
pytest.importorskip("lightgbm")

import pyarrow.parquet as pq  # noqa: E402

from agent.backtest import run as R  # noqa: E402
from agent.dataset.build import build_dataset  # noqa: E402
from agent.holdout import report as P  # noqa: E402
from agent.holdout import run as H  # noqa: E402
from agent.models.evaluate import DAY_S, load_plan  # noqa: E402
from agent.models.frame import load_frame  # noqa: E402
from agent.sim import backtest as B  # noqa: E402
from test_backtest_run import Fake, small_plan  # noqa: E402
from test_dataset import END, T0, market  # noqa: E402
from test_dataset import project as backfilled  # noqa: E402
from test_frame import D0, _dataset  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PLANS, PLANS_SHA = H.load_holdout(ROOT / "config" / "holdout.yaml")
STATE = {"holdout_sha": PLANS_SHA, "plan_sha": "p", "models_sha": "m", "cuts_sha": "c",
         "settings_sha": "s", "presets_sha": "t", "source_sha": "x",
         "source_is_the_simulating_code": False}  # fmt: skip


def plans_of(days: list[str]) -> H.HoldoutPlan:
    period = PLANS.periods["h2"].model_copy(update={"days": days})
    return PLANS.model_copy(update={"periods": {"h2": period}})


# --- the period file -----------------------------------------------------------------------


def test_the_period_file_names_whole_days_and_what_counts_as_held(tmp_path):
    period = H.get_period(PLANS, "h2")
    assert period.days == ["2026-11-20", "2026-11-21", "2026-11-22"]
    assert period.start == D0 + 8 * DAY_S and period.end == D0 + 11 * DAY_S
    assert (period.models, period.backtest) == ("r5", "r6")
    holds = PLANS.holds
    assert holds.model == "lightgbm" and holds.stages == ["bad", "grad_later", "hit_2x"]
    assert holds.beats == ["base_rate", "rules", "logistic"]
    assert holds.interval_above == ["base_rate", "rules"]
    assert holds.calibration_ratio == (0.75, 1.33)
    with pytest.raises(ValueError, match="unknown period"):
        H.get_period(PLANS, "h9")
    with pytest.raises(ValueError, match="one after another"):
        plans_of(["2026-11-20", "2026-11-22"]).periods["h2"].model_validate(
            {"title": "t", "days": ["2026-11-20", "2026-11-22"], "models": "r5",
             "backtest": "r6"})  # fmt: skip
    text = (ROOT / "config" / "holdout.yaml").read_bytes().replace(b"\r\n", b"\n")
    unknown = tmp_path / "holdout.yaml"
    unknown.write_bytes(text + b"loosened: true\n")
    with pytest.raises(ValueError):
        H.load_holdout(unknown)


def test_a_files_hash_does_not_depend_on_its_line_endings(tmp_path):
    unix, windows = tmp_path / "a.yaml", tmp_path / "b.yaml"
    unix.write_bytes(b"a: 1\nb: 2\n")
    windows.write_bytes(b"a: 1\r\nb: 2\r\n")
    assert H.content_sha(unix.read_bytes()) == H.content_sha(windows.read_bytes())
    for recorded in (hashlib.sha256(p.read_bytes()).hexdigest()[:12] for p in (unix, windows)):
        assert H.same_content(unix, recorded) and H.same_content(windows, recorded)
    windows.write_bytes(b"a: 1\r\nb: 3\r\n")
    assert not H.same_content(windows, hashlib.sha256(unix.read_bytes()).hexdigest()[:12])


# --- the period's data folder ----------------------------------------------------------------


def _first_dataset(tmp_path) -> tuple[Path, Path, dict]:
    """A backfill of made-up coins, the first dataset built from it, and a manifest of
    models whose test day is the day after the coins."""
    root, data = backfilled(tmp_path, market())
    build_dataset(root, data)
    first = json.loads((data / "dataset" / "manifest.json").read_text(encoding="utf-8"))
    models = data / "models" / "r5"
    models.mkdir(parents=True)
    (models / "manifest.json").write_text(
        json.dumps({"days": {"test": ["1970-01-13"]}}), encoding="utf-8")
    return root, data, first


def _more_backfill(data: Path, until: int) -> None:
    """A later epoch that reaches `until` (its chunks hold no coins)."""
    progress = data / "backfill" / "epoch-1085" / "progress.jsonl"
    progress.parent.mkdir(parents=True)
    progress.write_text(json.dumps({"kind": "commit", "chunks": [0], "failed": {}, "files": [],
                                    "times": {"0": [END + 1, until]}}) + "\n",
                        encoding="utf-8")  # fmt: skip


def test_the_period_gets_its_own_dataset_and_the_first_one_is_not_touched(tmp_path):
    root, data, first = _first_dataset(tmp_path)
    kept = {p: p.read_bytes() for p in [*(data / "dataset").iterdir(),
                                        *(data / "labels").iterdir()]}  # fmt: skip
    plans = plans_of(["1970-01-14", "1970-01-15"])
    with pytest.raises(H.NotAllowed, match="does not cover"):
        H.build(root, data, plans, "h2")  # the new epochs are not there yet
    assert not H.view_dir(data, "h2").exists()
    with pytest.raises(H.NotAllowed, match="not after the first test days"):
        H.build(root, data, plans_of(["1970-01-13", "1970-01-14"]), "h2")

    _more_backfill(data, 16 * DAY_S + 5_000)
    made = H.build(root, data, plans, "h2")
    view = H.view_dir(data, "h2")
    assert {p: p.read_bytes() for p in kept} == kept  # the first labels and dataset: as before
    assert (view / "raw_backfill").resolve() == (data / "raw_backfill").resolve()
    assert (view / "dataset" / "dataset.parquet").exists()
    assert (view / "labels" / "coins.parquet").exists()
    # The period's dataset sees the longer backfill; the first one still sees its own.
    new = json.loads((view / "dataset" / "manifest.json").read_text(encoding="utf-8"))
    assert new["backfill_spans"] == [[T0 - 100, 16 * DAY_S + 5_000]]
    assert first["backfill_spans"] == [[T0 - 100, END]]
    # Coins the first dataset knew completely are the same rows; the coin launched too
    # late for its labels then (now known) is left out of the comparison.
    same = made["same_as_first"]
    assert made["same"] and same["only_in_first"] == same["only_in_new"] == 0
    assert 0 < same["rows"] < first["rows"] == made["rows"]
    everything = H.compare_with_first(data, view, END)
    assert everything["only_in_first"] == everything["only_in_new"] > 0
    assert json.loads((view / "build.json").read_text(encoding="utf-8"))["same"] is True
    H.build(root, data, plans, "h2")  # again, before anything is scored: allowed

    # A changed value is found.
    path = view / "dataset" / "dataset.parquet"
    table = pq.read_table(path).to_pandas()
    table.loc[0, "trades"] += 1
    table.to_parquet(path)
    changed = H.compare_with_first(data, view, same["known_until"])
    assert changed["only_in_first"] == changed["only_in_new"] == 1

    # Once the period was scored, its dataset is not built again.
    (view / "score_report.json").write_text("{}", encoding="utf-8")
    with pytest.raises(H.NotAllowed, match="already scored"):
        H.build(root, data, plans, "h2")
    # Removing the period's folder removes the links, never the backfill behind them.
    shutil.rmtree(view)
    assert list((data / "raw_backfill").rglob("*.parquet"))
    assert (data / "backfill" / "epoch-1084" / "progress.jsonl").exists()


def test_the_periods_rows_come_from_the_same_loader_and_only_its_days(tmp_path):
    plan = load_plan(ROOT / "config" / "evaluation.yaml")[0]
    span = (D0 - 6_000, D0 + 12 * DAY_S + 6_000)  # Nov 12-23 whole, a part-day at each end
    rows = [
        {"mint": "old-test", "created_ts": D0 + 7 * DAY_S + 50},  # Nov 19
        {"mint": "a", "created_ts": D0 + 8 * DAY_S, "label_hit_2x": True},  # first second
        {"mint": "b", "created_ts": D0 + 9 * DAY_S + 5, "is_mayhem": True},
        {"mint": "c", "created_ts": D0 + 11 * DAY_S - 1},  # the period's last second
        {"mint": "later", "created_ts": D0 + 11 * DAY_S},  # Nov 23: after the period
        {"mint": "gap", "created_ts": D0 + 8 * DAY_S + 9, "has_gap": True},
        {"mint": "no-labels", "created_ts": D0 + 10 * DAY_S, "labels_complete": False},
        {"mint": "other-quote", "created_ts": D0 + 10 * DAY_S + 1, "is_sol_paired": False},
    ]
    view = _dataset(tmp_path, rows, span)
    period = H.get_period(PLANS, "h2")
    frame = H.period_frame(view, plan, period, backfill_only_signals=False)
    assert sorted(set(frame["mint"])) == ["a", "b", "c"] and len(frame) == 6
    assert set(frame["date"]) == {"2026-11-20", "2026-11-21", "2026-11-22"}
    assert frame.loc[frame["mint"] == "a", "y_hit_2x"].iloc[0] == 1.0
    assert set(frame.loc[frame["mint"] == "b", "group"]) == {"mayhem"}
    # The plan itself still gives its own split of this dataset: nothing was changed in it.
    assert sorted(set(load_frame(view, plan, ("test",), backfill_only_signals=False)["mint"])) \
        == ["b", "c", "later"]

    with pytest.raises(H.NotAllowed, match="whole days"):  # Nov 24 is a part-day here
        H.period_frame(view, plan, period.model_copy(
            update={"days": ["2026-11-23", "2026-11-24"]}), backfill_only_signals=False)


# --- what is frozen ----------------------------------------------------------------------------


@pytest.fixture
def frozen(tmp_path):
    """A project and a data folder as R5 and R6 leave them, with made-up model files."""
    root, data = tmp_path / "project", tmp_path / "data"
    shutil.copytree(ROOT / "config", root / "config")
    plan_sha = load_plan(root / "config" / "evaluation.yaml")[1]
    models = data / "models" / "r5"
    (models / "bad").mkdir(parents=True)
    (models / "bad" / "lightgbm.txt").write_text("a model", encoding="utf-8")
    files = {"bad/lightgbm.txt": hashlib.sha256(b"a model").hexdigest()}
    (models / "manifest.json").write_text(json.dumps(
        {"files": files, "plan_sha": plan_sha, "dataset_sha256": "d1",
         "days": {"test": ["2026-11-18", "2026-11-19"]}}), encoding="utf-8")  # fmt: skip
    (data / "dataset").mkdir()
    (data / "dataset" / "manifest.json").write_text(json.dumps({"file_sha256": "d1"}),
                                                    encoding="utf-8")  # fmt: skip
    presets, _ = B.load_presets(root / "config" / "padre_presets.yaml")
    settings, settings_sha = R.load_settings(root / "config" / "backtest.yaml")
    cuts = {"lightgbm_2x_5ph": {"30": {"cut": 1.5}}}
    folder = data / "backtest" / "r6"
    folder.mkdir(parents=True)
    (folder / "cuts.json").write_text(json.dumps(cuts), encoding="utf-8")
    (folder / "frozen.json").write_text(json.dumps({
        "cuts_sha": hashlib.sha256(json.dumps(cuts, sort_keys=True).encode()).hexdigest()[:12],
        "models_sha": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:12],
        "settings_sha": settings_sha, "presets_sha": "an earlier fee",
        "source_sha": "the simulating code"}), encoding="utf-8")  # fmt: skip
    (folder / "test_report.json").write_text(json.dumps({
        "presets": json.loads(presets.model_dump_json()),
        "settings": json.loads(settings.model_dump_json()),
        "summarized_again": {"code": {"source_sha": R.code_version(root)["source_sha"]}}}),
        encoding="utf-8")  # fmt: skip
    return root, data


def test_the_frozen_files_are_checked_and_each_change_is_named(frozen):
    root, data = frozen
    period = H.get_period(PLANS, "h2")
    state = H.frozen_state(root, data, "h", period)
    assert state["holdout_sha"] == "h" and not state["source_is_the_simulating_code"]
    assert state["source_sha"] == R.code_version(root)["source_sha"]

    def refused(path: Path, old: bytes, new: bytes, match: str) -> None:
        original = path.read_bytes()
        assert old in original
        path.write_bytes(original.replace(old, new))
        with pytest.raises(H.NotAllowed, match=match):
            H.frozen_state(root, data, "h", period)
        path.write_bytes(original)

    refused(data / "models" / "r5" / "bad" / "lightgbm.txt", b"a model", b"another",
            "model files differ")
    refused(data / "backtest" / "r6" / "cuts.json", b"1.5", b"1.4", "cuts.json differs")
    refused(root / "config" / "padre_presets.yaml", b"slippage_pct: 20", b"slippage_pct: 30",
            "padre_presets.yaml differs")
    refused(root / "config" / "padre_presets.yaml", b"platform_fee_pct: 1.0",
            b"platform_fee_pct: 0.5", "padre_presets.yaml differs")
    refused(root / "config" / "backtest.yaml", b"picks_per_hour: [5, 20]",
            b"picks_per_hour: [5, 10]", "backtest.yaml changed")
    refused(root / "config" / "evaluation.yaml", b"test_share: 0.25", b"test_share: 0.5",
            "not the plan the models were trained under")
    refused(data / "dataset" / "manifest.json", b"d1", b"d2", "first dataset is not the one")
    refused(data / "backtest" / "r6" / "test_report.json",
            R.code_version(root)["source_sha"].encode(), b"other code", "code changed")
    # The same files with the other line endings are the same files.
    for name in ("backtest.yaml", "evaluation.yaml", "padre_presets.yaml"):
        path = root / "config" / name
        unix = path.read_bytes().replace(b"\r\n", b"\n")
        path.write_bytes(unix)
        assert H.frozen_state(root, data, "h", period) == state
        path.write_bytes(unix.replace(b"\n", b"\r\n"))
        assert H.frozen_state(root, data, "h", period) == state


# --- the check, and the two runs ---------------------------------------------------------------

STAGES_STUB = {"hit_2x": {"title": "Stage 3", "kind": "pick", "cells": [
    {"offset_s": 30, "slice": "all", "model": "lightgbm", "n": 3,
     "pr_auc": {"value": 0.25, "low": 0.2, "high": 0.3}}]}}  # fmt: skip


@pytest.fixture
def ready(tmp_path, monkeypatch):
    """Made-up coins, models and backfill (as in the R6 tests), the first split's reports
    saved by R6's own run, and a period whose dataset was built."""
    root, data = tmp_path / "project", tmp_path / "data"
    shutil.copytree(ROOT / "config", root / "config")
    (root / "docs").mkdir()
    fake = Fake()
    fake.install(monkeypatch)
    plan = small_plan()
    R.run(root, data, plan, "validation")
    R.run(root, data, plan, "test")
    models = data / "models" / "r5"
    models.mkdir(parents=True)
    (models / "validation_report.json").write_text(json.dumps({"stages": STAGES_STUB}),
                                                   encoding="utf-8")  # fmt: skip
    (models / "test_report.json").write_text(
        json.dumps({"stages": STAGES_STUB, "rows": 6, "coins": 3}), encoding="utf-8")
    (models / "manifest.json").write_text(
        json.dumps({"days": {"test": ["2026-11-18", "2026-11-19"]}}), encoding="utf-8")
    view = H.view_dir(data, "h2")
    (view / "dataset").mkdir(parents=True)
    (view / "dataset" / "dataset.parquet").write_bytes(b"the period's dataset")
    sha = hashlib.sha256(b"the period's dataset").hexdigest()
    (view / "build.json").write_text(json.dumps({
        "same": True, "dataset_sha256": sha, "epochs": [1084, 1094], "coins": 9, "rows": 36,
        "same_as_first": {"rows": 24, "only_in_first": 0, "only_in_new": 0}}),
        encoding="utf-8")  # fmt: skip
    state = dict(STATE)
    monkeypatch.setattr(H, "frozen_state", lambda _r, _d, _h, _p: dict(state))
    monkeypatch.setattr(H, "load_models", lambda _folder: {})
    monkeypatch.setattr(H, "load_frame", lambda _d, _p, roles, **_k: fake.frames[roles[0]])
    monkeypatch.setattr(H, "dataset_split", lambda _d, _p: (
        type("Split", (), {"validation": [17 * 86_400]})(), {}))  # fmt: skip
    monkeypatch.setattr(H, "role_ranges", lambda _s, _o: {"validation": (17 * 86_400,
                                                                        18 * 86_400)})  # fmt: skip
    monkeypatch.setattr(H, "score_frame", lambda _f, _m, _p: copy.deepcopy(STAGES_STUB))
    monkeypatch.setattr(H, "verify_pool_trades", lambda _d, _a, _b: {})
    # The period's rows: the made-up coins of two other days.
    period_rows = fake.frames["test"]
    monkeypatch.setattr(H, "period_frame", lambda _v, _p, _period, **_k: period_rows)
    return root, data, plan, state


def test_the_validation_day_built_again_must_give_the_saved_reports(ready):
    root, data, plan, _ = ready
    view = H.view_dir(data, "h2")
    with pytest.raises(H.NotAllowed, match="check"):  # no check yet: nothing may run
        H.score(root, data, plan, PLANS, PLANS_SHA, "h2")
    with pytest.raises(H.NotAllowed, match="check"):
        H.backtest(root, data, plan, PLANS, PLANS_SHA, "h2")

    got = H.check(root, data, plan, PLANS, PLANS_SHA, "h2")
    assert got["passed"] and got["validation_days"] == ["1970-01-18"]
    assert got["r5_validation_scores_largest_difference"] == 0.0
    assert got["r6_cuts_largest_difference"] == 0.0 and got["r6_validation_ledger_same_files"]
    assert got["r6_validation_cells_largest_difference"] == 0.0
    assert got["r6_validation_ledger_files"] > 0 and not (view / "check_ledger").exists()
    assert json.loads((view / "check.json").read_text(encoding="utf-8"))["passed"] is True

    # A saved ledger file that today's code does not reproduce fails the check.
    saved = data / "backtest" / "r6" / "validation_report.json"
    original = saved.read_text(encoding="utf-8")
    report = json.loads(original)
    first = next(iter(report["ledger"]["files"]))
    report["ledger"]["files"][first] = "0" * 64
    saved.write_text(json.dumps(report), encoding="utf-8")
    assert not H.check(root, data, plan, PLANS, PLANS_SHA, "h2")["passed"]
    with pytest.raises(H.NotAllowed, match="no passed check"):
        H.backtest(root, data, plan, PLANS, PLANS_SHA, "h2")
    saved.write_text(original, encoding="utf-8")
    # So does a saved score that differs.
    scores = data / "models" / "r5" / "validation_report.json"
    changed = copy.deepcopy(STAGES_STUB)
    changed["hit_2x"]["cells"][0]["pr_auc"]["value"] = 0.26
    scores.write_text(json.dumps({"stages": changed}), encoding="utf-8")
    again = H.check(root, data, plan, PLANS, PLANS_SHA, "h2")
    assert not again["passed"]
    assert again["r5_validation_scores_largest_difference"] == pytest.approx(0.01)
    scores.write_text(json.dumps({"stages": STAGES_STUB}), encoding="utf-8")
    assert H.check(root, data, plan, PLANS, PLANS_SHA, "h2")["passed"]
    # A period whose dataset does not agree with the first one is not run, checked or not.
    (view / "build.json").write_text(json.dumps({"same": False}), encoding="utf-8")
    for run in (H.score, H.backtest):
        with pytest.raises(H.NotAllowed, match="build"):
            run(root, data, plan, PLANS, PLANS_SHA, "h2")


def test_the_period_is_scored_once_and_simulated_once_on_what_the_check_passed_with(ready):
    root, data, plan, state = ready
    view = H.view_dir(data, "h2")
    assert H.check(root, data, plan, PLANS, PLANS_SHA, "h2")["passed"]

    # Anything that changed since the check blocks both runs.
    state["cuts_sha"] = "edited"
    for run in (H.score, H.backtest):
        with pytest.raises(H.NotAllowed, match="Changed since the check passed: cuts_sha"):
            run(root, data, plan, PLANS, PLANS_SHA, "h2")
    state["cuts_sha"] = STATE["cuts_sha"]
    dataset = view / "dataset" / "dataset.parquet"
    dataset.write_bytes(b"rebuilt")
    with pytest.raises(H.NotAllowed, match="not the file"):
        H.backtest(root, data, plan, PLANS, PLANS_SHA, "h2")
    dataset.write_bytes(b"the period's dataset")
    assert not (view / "score_report.json").exists() and not (view / "ledger").exists()

    simulated = H.backtest(root, data, plan, PLANS, PLANS_SHA, "h2")
    first = json.loads((data / "backtest" / "r6" / "test_report.json")
                       .read_text(encoding="utf-8"))  # fmt: skip
    # The made-up period holds the first test days' coins, so the frozen cuts and settings
    # must give the first test report's numbers and trades.
    assert simulated["cells"] == first["cells"] and simulated["signals"] == first["signals"]
    assert simulated["ledger"] == first["ledger"] and simulated["state"] == STATE
    assert simulated["presets"] == first["presets"]
    assert simulated["launch_range"] == [H.get_period(PLANS, "h2").start,
                                         H.get_period(PLANS, "h2").end]  # fmt: skip
    with pytest.raises(H.NotAllowed, match="simulated once"):
        H.backtest(root, data, plan, PLANS, PLANS_SHA, "h2")

    scored = H.score(root, data, plan, PLANS, PLANS_SHA, "h2")
    assert scored["stages"] == STAGES_STUB and scored["coins"] == 80 and scored["rows"] == 160
    assert scored["days"] == ["2026-11-20", "2026-11-21", "2026-11-22"]
    with pytest.raises(H.NotAllowed, match="scored once"):
        H.score(root, data, plan, PLANS, PLANS_SHA, "h2")
    # After a run, neither the check nor the dataset is made again.
    with pytest.raises(H.NotAllowed, match="already scored or simulated"):
        H.check(root, data, plan, PLANS, PLANS_SHA, "h2")
    with pytest.raises(H.NotAllowed, match="already scored or simulated"):
        H.build(root, data, PLANS, "h2")


# --- the verdict -----------------------------------------------------------------------------


def scores_report(*, lightgbm=0.30, logistic=0.25, rules=0.06, base=0.04, width=0.03,
                  predicted=0.040) -> dict:  # fmt: skip
    """An R5 report in which every stage and decision time has the same numbers."""
    stages = {}
    for name, kind in (("bad", "filter"), ("dead_or_rug", "filter"), ("grad_later", "pick"),
                       ("hit_2x", "pick")):  # fmt: skip
        cells = []
        for offset in (30, 60):
            for model, value in (("base_rate", base), ("rules", rules), ("logistic", logistic),
                                 ("lightgbm", lightgbm)):  # fmt: skip
                est = {"value": value, "low": value - width, "high": value + width}
                cell = {"offset_s": offset, "slice": "all", "model": model, "n": 1_000,
                        "rate": 0.04, "pr_auc": est}  # fmt: skip
                if kind == "filter":
                    cell["recall_at_max_fine_blocked"] = est
                else:
                    brier = 0.038 - value / 100
                    cell |= {"precision_top_5_per_hour": est, "precision_top_20_per_hour": est,
                             "brier_score": {"value": brier, "low": brier, "high": brier},
                             "calibration_curve": [
                                 {"n": 500, "predicted": predicted / 2, "observed": 0.02},
                                 {"n": 500, "predicted": predicted * 1.5,
                                  "observed": 0.06}]}  # fmt: skip
                cells.append(cell)
        stages[name] = {"title": f"Stage of {name}: its outcome", "kind": kind, "cells": cells}
    return {"set": "x", "rows": 8_000, "coins": 2_000, "stages": stages,
            "state": {"holdout_sha": PLANS_SHA}}  # fmt: skip


def failing(items: list[dict]) -> set[str]:
    return {i["check"] for i in items if i["ok"] is False}


def test_r5_held_means_beating_the_baselines_at_the_same_size_and_calibrated():
    holds, old = PLANS.holds, scores_report()
    items = P.judge_r5(scores_report(), old, holds)
    # 3 stages x 2 decision times: 3 baselines each; 2 or 3 metrics; 2 calibration checks
    # for the two pick stages.
    assert [sum(1 for i in items if i["check"] == c) for c in ("beats", "size", "calibration")] \
        == [18, 16, 8]
    assert all(i["ok"] for i in items) and P.verdict(items, "beats") == P.HELD
    assert not any("dead_or_rug" in i["where"] for i in items)  # not one of the plan's stages

    # Below logistic regression: the claim itself fails.
    worse = P.judge_r5(scores_report(lightgbm=0.24), old, holds)
    assert failing(worse) == {"beats"} and P.verdict(worse, "beats") == P.NOT_HELD
    # Above the rules, but the interval's low end is not.
    close = P.judge_r5(scores_report(lightgbm=0.30, rules=0.28, logistic=0.20), old, holds)
    assert failing(close) == {"beats"}
    assert {i["what"] for i in close if i["ok"] is False} == {
        "PR-AUC: above Rules, low end too", "caught at 30% blocked: above Rules, low end too"}
    # Still ahead of every baseline, but far from the first test days' numbers.
    higher = P.judge_r5(scores_report(lightgbm=0.50), old, holds)
    assert failing(higher) == {"size"} and P.verdict(higher, "beats") == P.DIRECTION
    # Predicting half as often as it happens.
    off = P.judge_r5(scores_report(predicted=0.02), old, holds)
    assert failing(off) == {"calibration"} and P.verdict(off, "beats") == P.DIRECTION
    assert {i["new"] for i in off if i["ok"] is False} == {"0.50"}
    # A Brier score no better than the base rate's.
    flat = scores_report()
    for stage in ("grad_later", "hit_2x"):
        for cell in flat["stages"][stage]["cells"]:
            cell["brier_score"] = {"value": 0.05, "low": 0.05, "high": 0.05}
    assert failing(P.judge_r5(flat, old, holds)) == {"calibration"}
    # A missing cell is not counted as passing.
    gone = scores_report()
    gone["stages"]["hit_2x"]["cells"] = [c for c in gone["stages"]["hit_2x"]["cells"]
                                         if c["model"] != "rules"]  # fmt: skip
    assert sum(1 for i in P.judge_r5(gone, old, holds) if i["ok"] is None) == 2


@pytest.fixture
def trades(ready):
    """The first test days' R6 report, and a period's report with the same numbers."""
    root, data, plan, _ = ready
    old = json.loads((data / "backtest" / "r6" / "test_report.json").read_text(encoding="utf-8"))
    return old, copy.deepcopy(old)


def cells_of(report: dict, **want) -> list[dict]:
    key = {"time": R.ALL_TIMES, "delay_s": 5, "slice": "all", "variant": "headline"} | want
    return [c for c in report["cells"] if all(c[k] == v for k, v in key.items()) and c["n"]]


def test_r6_held_means_still_no_profit_at_the_same_size_with_the_same_pattern(trades):
    old, new = trades
    # Made-up coins are not real ones: give every cell a loss, mayhem coins a larger one.
    for report in (old, new):
        for c in report["cells"]:
            if c["n"]:
                value = -0.30 if c["slice"] == "mayhem" else -0.10
                c["mean_pnl_sol"] = {"value": value, "low": value - 0.05, "high": value + 0.05}
    items = P.judge_r6(new, old)
    assert {i["check"] for i in items} == {"no profit", "size", "pattern"}
    assert not failing(items) and P.verdict(items, "no profit") == P.HELD
    signals, plans = len(new["signals"]), len(new["presets"]["exit_plans"])
    assert sum(1 for i in items if i["check"] == "no profit") == 2 * signals * plans
    assert sum(1 for i in items if i["check"] == "size") == 2 * signals * plans
    # The two other delays, mayhem against ordinary, and each of the two days.
    assert sum(1 for i in items if i["check"] == "pattern") == 5 * signals * plans

    def with_change(change, **want) -> list[dict]:
        changed = copy.deepcopy(new)
        for c in cells_of(changed, signal="everything", plan="double_or_half", **want):
            c["mean_pnl_sol"] = change
        return P.judge_r6(changed, old)

    # A signal whose mean turns positive: the claim fails, even inside the old interval.
    profit = with_change({"value": 0.01, "low": -0.14, "high": 0.02})
    assert failing(profit) == {"no profit"} and P.verdict(profit, "no profit") == P.NOT_HELD
    assert [i["what"] for i in profit if i["ok"] is False] == ["strict view: mean below zero"]
    # Under the generous view only an interval entirely above zero fails.
    kind = with_change({"value": 0.01, "low": -0.14, "high": 0.02}, variant="footprint_stays")
    assert not failing(kind)
    kind = with_change({"value": 0.20, "low": 0.01, "high": 0.40}, variant="footprint_stays")
    assert failing(kind) == {"no profit", "size"}
    # Still a loss, but a much larger one than on the first test days.
    larger = with_change({"value": -0.50, "low": -0.55, "high": -0.45})
    assert failing(larger) == {"size"} and P.verdict(larger, "no profit") == P.DIRECTION
    # The same at another delay: compared with the first test days at that delay.
    slow = with_change({"value": -0.50, "low": -0.55, "high": -0.45}, delay_s=10)
    assert failing(slow) == {"pattern"}
    assert [i["what"] for i in slow if i["ok"] is False] == ["10 s delay: intervals overlap"]
    # One profitable day.
    day = with_change({"value": 0.30, "low": 0.10, "high": 0.50}, slice=new["dates"][0])
    assert failing(day) == {"pattern"}
    # Mayhem coins losing less than ordinary ones, but not clearly: the intervals overlap.
    mayhem = with_change({"value": -0.05, "low": -0.07, "high": -0.03}, slice="mayhem")
    assert not failing(mayhem)
    # Clearly better: the whole mayhem interval above the ordinary one (-0.15 to -0.05).
    mayhem = with_change({"value": -0.02, "low": -0.04, "high": -0.01}, slice="mayhem")
    assert failing(mayhem) == {"pattern"}
    assert [i["what"] for i in mayhem if i["ok"] is False] == [
        "mayhem coins not clearly better than ordinary ones"]
    # A signal that bought nothing is not counted as passing.
    empty = copy.deepcopy(new)
    for c in empty["cells"]:
        if c["signal"] == "lightgbm_2x_5ph":
            c["n"] = 0
            c.pop("mean_pnl_sol", None)
    unchecked = [i for i in P.judge_r6(empty, old) if i["ok"] is None]
    assert unchecked and all("LightGBM 2x, about 5 per hour" in i["where"] for i in unchecked)


def test_the_results_file_shows_both_periods_and_the_verdicts(ready):
    root, data, plan, _ = ready
    view = H.view_dir(data, "h2")
    with pytest.raises(H.NotAllowed, match="no report yet"):
        P.write_results(root, data, PLANS, PLANS_SHA, "h2")
    assert H.check(root, data, plan, PLANS, PLANS_SHA, "h2")["passed"]
    H.backtest(root, data, plan, PLANS, PLANS_SHA, "h2")
    path, verdicts = P.write_results(root, data, PLANS, PLANS_SHA, "h2")
    assert path == root / "docs" / "H2_RESULTS.md" and set(verdicts) == {"r6"}
    assert "**R5, the models:** not scored yet." in path.read_text(encoding="utf-8")

    # R5 reports with every stage, for the first test days and for the period.
    old = scores_report() | {"state": {"holdout_sha": PLANS_SHA}}
    (data / "models" / "r5" / "test_report.json").write_text(json.dumps(old), encoding="utf-8")
    (view / "score_report.json").write_text(json.dumps(scores_report(lightgbm=0.24)),
                                            encoding="utf-8")  # fmt: skip
    path, verdicts = P.write_results(root, data, PLANS, PLANS_SHA, "h2")
    assert verdicts["r5"] == P.NOT_HELD and verdicts["r6"] in (P.HELD, P.DIRECTION, P.NOT_HELD)
    text = path.read_text(encoding="utf-8")
    for part in ("# H2 results", "## Short answer", "**R5, the models: did not hold.**",
                 "**R6, the backtest: ", "- **Beats the baselines:** 12 of 18 cells pass.",
                 "  - Fails: Stage of hit_2x, T+60 s, PR-AUC: above Logistic regression: "
                 "0.240 (0.210 to 0.270) against 0.250.",
                 "## What was run", "byte for byte the saved ones",
                 "## R5: Nov 20-22 beside Nov 18-19", "| LightGBM | Nov 18-19 | 0.300 ",
                 "| LightGBM | Nov 20-22 | 0.240 ", "### Every R5 check",
                 "## R6: Nov 20-22 beside Nov 18-19", "### Exit plan `double_or_half`",
                 "| Signal | Strict, Nov 18-19 | Strict, Nov 20-22 | Generous, Nov 18-19 | "
                 "Generous, Nov 20-22 |", "**Delay sensitivity**", "**Per day**",
                 "**Ordinary vs. mayhem coins**", "**What happened to the buys**",
                 "### Every R6 check", "## Appendix: every R5 number of Nov 20-22",
                 "## Appendix: every R6 number of Nov 20-22"):  # fmt: skip
        assert part in text, part

    # What counts as "held" cannot be changed after the runs.
    with pytest.raises(H.NotAllowed, match="fixed beforehand"):
        P.write_results(root, data, PLANS, "an edited file", "h2")


def test_days_are_named_shortly():
    assert P.days_label(["2026-11-18", "2026-11-19"]) == "Nov 18-19"
    assert P.days_label(["2026-11-20", "2026-11-21", "2026-11-22"]) == "Nov 20-22"
    assert P.days_label(["2026-11-30", "2026-12-01"]) == "Nov 30, Dec 1"
    assert P.days_label(["2026-11-17"]) == "Nov 17"
