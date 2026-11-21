"""Scoring live coins with the saved models (spec 15.3 step L1): the engine's scorer gives
R5's numbers exactly, refuses model files that are not the manifest's, says why a coin has
no score, and stays behind the wall between labels and live code."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent.scoring.live import ScoringOff, load_scorer
from scoring_helpers import make_models

ROOT = Path(__file__).resolve().parents[1]
COVERED = {"hour_covered": 1.0, "day_covered": 1.0}
LABEL_SIDE = ("agent.labels", "agent.dataset", "agent.models.frame", "agent.models.train",
              "agent.models.report", "agent.backtest", "agent.export_live")  # fmt: skip


# --- the wall (no research package needed) -------------------------------------------------


def _imports(folder: Path) -> set[str]:
    names = set()
    for path in folder.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
            elif isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
    return names


def test_live_code_never_imports_the_label_side():
    """What runs live (signals, engine, screen, scoring) names no module that can read an
    outcome: labels, the dataset, the training frame, the training code, the backtest."""
    src = ROOT / "src" / "agent"
    for part in ("scoring", "daemon", "tui", "features"):
        found = sorted(n for n in _imports(src / part) if n.startswith(LABEL_SIDE))
        assert not found, f"{part} imports {found}"


def test_starting_the_engine_loads_no_label_code_and_no_research_package():
    """The same wall, checked on what is really loaded: a fresh Python imports the engine,
    the screen and the scoring code, and then holds no label-side module (also not through
    another module) and none of the research packages the engine must start without."""
    code = (
        "import sys\n"
        "import agent.daemon.main, agent.daemon.live_state, agent.tui.app\n"
        "import agent.scoring.live, agent.scoring.launch_context, agent.scoring.replay_check\n"
        f"label_side = {LABEL_SIDE!r}\n"
        "bad = sorted(m for m in sys.modules if m.startswith(label_side))\n"
        "research = sorted(m for m in ('lightgbm', 'sklearn', 'joblib', 'pandas') "
        "if m in sys.modules)\n"
        "print(bad, research)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env=env, timeout=120)  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[] []"


def test_without_the_models_or_the_packages_scoring_is_off_with_a_reason(tmp_path, monkeypatch):
    with pytest.raises(ScoringOff, match="agent train"):
        load_scorer(tmp_path)  # nothing saved here
    monkeypatch.setitem(sys.modules, "lightgbm", None)  # as if it were not installed
    with pytest.raises(ScoringOff, match=r"lightgbm is not installed \(pip install"):
        load_scorer(tmp_path)


# --- the scorer (needs the research packages) ---------------------------------------------


@pytest.fixture(scope="module")
def saved(tmp_path_factory):
    pytest.importorskip("lightgbm")  # research packages: pip install -e ".[research]"
    pytest.importorskip("sklearn")
    pytest.importorskip("pandas")
    data_dir = tmp_path_factory.mktemp("models")
    return {"data_dir": data_dir, **make_models(data_dir)}


def _snapshot(record: dict) -> dict:
    values = {k: (None if isinstance(v, float) and v != v else v) for k, v in record.items()}
    return {**values, "is_sol_paired": True, "completed": bool(values["completed"])}


def _context(record: dict) -> dict:
    return {k: record[k] for k in ("dev_launches_24h", "launches_last_hour",
                                   "launch_hour_utc")} | COVERED  # fmt: skip


def test_the_live_scorer_gives_r5s_scores_to_the_last_digit(saved):
    """Every row through R5's own scoring code and through the engine's scorer: the raw
    score and the calibrated probability are the same numbers, not close ones."""
    from agent.models.frame import STAGES

    scorer = load_scorer(saved["data_dir"])
    assert [name for name, _ in scorer.stages] == ["bad", "grad_later", "hit_2x"]
    assert scorer.features == saved["columns"] and len(scorer.features) == 36
    frame = saved["frame"]
    stages = {s.name: s for s in STAGES}
    expected = {name: saved["fitted"][name].scores(frame, stages[name])["lightgbm"]
                for name, _ in scorer.stages}  # fmt: skip
    checked = 0
    for i, record in enumerate(frame.to_dict("records")):
        got = scorer.score(_snapshot(record), _context(record), has_gap=False)
        assert got["model"] == "r5/lightgbm" and got["bundle"] == scorer.bundle_sha
        for name, _ in scorer.stages:
            if name == "grad_later" and record["completed"]:
                assert got["missing"] == {"grad_later": "already graduated"}
                assert name not in got["p"] and name not in got["raw"]
                continue
            raw, probability = expected[name]
            assert got["raw"][name] == float(raw[i])
            assert got["p"][name] == float(probability[i])
            assert 0.0 <= got["p"][name] <= 1.0
            checked += 1
    assert checked > 1_500
    # The saved record survives the trip through JSON unchanged (how verdicts are stored).
    assert json.loads(json.dumps(got)) == got


def test_a_coin_the_models_were_not_trained_for_gets_a_reason_instead_of_a_score(saved):
    scorer = load_scorer(saved["data_dir"])
    record = saved["frame"].to_dict("records")[1]
    snapshot, context = _snapshot(record), _context(record)
    assert "p" in scorer.score(snapshot, context, has_gap=False)

    not_sol = scorer.score({**snapshot, "is_sol_paired": False}, context, has_gap=False)
    assert "p" not in not_sol and "not paired with SOL" in not_sol["why"]
    gap = scorer.score(snapshot, context, has_gap=True)
    assert "p" not in gap and "recording gap" in gap["why"]
    short = scorer.score(snapshot, {**context, "hour_covered": 0.4}, has_gap=False)
    assert short["why"].startswith("only 40% of the hour before its launch was recorded (95%")
    # A short 24 h history does not stop a score: it is kept with it, to be shown.
    partial = scorer.score(snapshot, {**context, "day_covered": 0.3}, has_gap=False)
    assert "p" in partial and partial["context"]["day_covered"] == 0.3
    lost = {k: v for k, v in snapshot.items() if k != "holders"}
    assert "does not make: holders" in scorer.score(lost, context, has_gap=False)["why"]
    lenient = load_scorer(saved["data_dir"], min_hour_covered=0.0)
    assert "p" in lenient.score(snapshot, {**context, "hour_covered": 0.0}, has_gap=False)


def test_usual_rates_come_from_the_test_report_by_time_and_kind_of_coin(saved):
    scorer = load_scorer(saved["data_dir"])
    assert scorer.usual(60, is_mayhem=False) == {"bad": 0.25, "grad_later": 0.25, "hit_2x": 0.25}
    assert scorer.usual(60, is_mayhem=True)["hit_2x"] == 0.75
    assert scorer.usual(45, is_mayhem=False) == {"bad": None, "grad_later": None, "hit_2x": None}
    info = scorer.info()
    assert info["trained_days"] == ["2026-11-12", "2026-11-16"] and info["version"] == "r5"
    assert info["test_days"] == ["2026-11-18", "2026-11-19"] and info["min_hour_covered"] == 0.95


def test_model_files_that_are_not_the_manifests_are_refused(saved, tmp_path):
    import shutil

    data_dir = tmp_path / "data"
    shutil.copytree(saved["data_dir"] / "models", data_dir / "models")
    assert load_scorer(data_dir).name == "r5/lightgbm"
    folder = data_dir / "models" / "r5"
    bundle = data_dir / "models" / "r5_live" / "bundle.json"

    model = folder / "hit_2x" / "lightgbm.txt"
    original = model.read_bytes()
    model.write_bytes(original + b"\n")
    with pytest.raises(ScoringOff, match="hit_2x/lightgbm.txt is not the one the R5 manifest"):
        load_scorer(data_dir)
    model.write_bytes(original)

    joblib_file = folder / "bad" / "models.joblib"
    original = joblib_file.read_bytes()
    joblib_file.write_bytes(original + b"x")
    with pytest.raises(ScoringOff, match="bad/models.joblib is not the one"):
        load_scorer(data_dir)
    joblib_file.write_bytes(original)

    saved_bundle = json.loads(bundle.read_text(encoding="utf-8"))
    for change, message in (
        (lambda b: b["source"].update(manifest_sha256="0" * 64), "made from other models"),
        (lambda b: b["source"]["files"].update({"bad/models.joblib": "0" * 64}),
         "calibration was not made from bad/models.joblib"),
        (lambda b: b["features"].reverse(), "not the ones the models were trained on"),
        (lambda b: b["stages"]["bad"]["calibration"].update(x=[1.0, 1.0], y=[0.1, 0.2]),
         "bad calibration map is not valid"),
        (lambda b: b.pop("stages"), "not as expected"),
    ):  # fmt: skip
        changed = json.loads(json.dumps(saved_bundle))
        change(changed)
        bundle.write_text(json.dumps(changed), encoding="utf-8")
        with pytest.raises(ScoringOff, match=message):
            load_scorer(data_dir)
    bundle.unlink()
    with pytest.raises(ScoringOff, match="agent export-live-models"):
        load_scorer(data_dir)


def test_the_export_refuses_changed_model_files_and_leaves_the_r5_folder_alone(saved, tmp_path):
    import shutil

    from agent.export_live import export_bundle
    from scoring_helpers import sha256

    data_dir = tmp_path / "data"
    shutil.copytree(saved["data_dir"] / "models", data_dir / "models")
    folder = data_dir / "models" / "r5"
    before = {p.relative_to(folder).as_posix(): sha256(p) for p in folder.rglob("*") if p.is_file()}
    path = export_bundle(data_dir)
    after = {p.relative_to(folder).as_posix(): sha256(p) for p in folder.rglob("*") if p.is_file()}
    assert before == after and path.parent.name == "r5_live"
    bundle = json.loads(path.read_text(encoding="utf-8"))
    assert set(bundle["stages"]) == {"bad", "grad_later", "hit_2x"}
    assert bundle["stages"]["grad_later"]["on_curve_only"] is True
    assert bundle["stages"]["bad"]["on_curve_only"] is False
    # Any file the manifest lists, also one the engine does not read.
    (folder / "dead_or_rug" / "lightgbm.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="differ from the manifest"):
        export_bundle(data_dir)
