"""R5 models on made-up rows: every baseline and model is fitted, saved and scored; the
checks run; and the test days can be scored once."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")  # research packages: pip install -e ".[research]"
np = pytest.importorskip("numpy")
pytest.importorskip("lightgbm")
pytest.importorskip("sklearn")

from agent.models import train as T  # noqa: E402
from agent.models.evaluate import load_plan  # noqa: E402
from agent.models.frame import STAGES, feature_columns  # noqa: E402
from agent.models.report import render_checks, render_report  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GRID = [{"num_leaves": 7, "min_data_in_leaf": 20, "class_weight": "none"},
        {"num_leaves": 7, "min_data_in_leaf": 20, "class_weight": "balanced"}]  # fmt: skip


def rows(n_coins: int, day: int, role: str, seed: int) -> pd.DataFrame:
    """Coins whose `net_sol` decides how likely each outcome is, 4 cutoffs each."""
    rng = np.random.default_rng(seed)
    columns = feature_columns(with_backfill_only=True)
    frames = []
    for offset in (30, 60, 120, 300):
        f = pd.DataFrame(rng.normal(size=(n_coins, len(columns))), columns=columns)
        signal = rng.normal(size=n_coins)
        f["net_sol"] = signal
        f["token_age_s"] = offset
        f["is_mayhem"] = (rng.random(n_coins) < 0.4).astype(float)
        f.loc[rng.random(n_coins) < 0.1, "median_buy_sol"] = np.nan  # some missing values
        chance = 1 / (1 + np.exp(-2 * signal))
        f["y_hit_2x"] = (rng.random(n_coins) < chance * 0.5).astype(float)
        f["y_grad_later"] = (rng.random(n_coins) < chance * 0.4).astype(float)
        f.loc[rng.random(n_coins) < 0.05, "y_grad_later"] = np.nan  # already graduated
        f["y_bad"] = (rng.random(n_coins) < 1 - chance * 0.6).astype(float)
        f["y_dead_or_rug"] = f["y_bad"]
        f["rules_verdict"] = np.where(signal < -0.5, "avoid",
                                      np.where(signal < 1.5, "caution", "pass"))  # fmt: skip
        f["mint"] = [f"{role}{day}-{i}" for i in range(n_coins)]
        f["created_ts"] = day * 86_400 + rng.integers(0, 86_400, n_coins)
        f["hour_ts"] = f["created_ts"] // 3600 * 3600
        f["date"] = f"2026-11-{day:02d}"
        f["offset_s"], f["role"] = offset, role
        f["group"] = np.where(f["is_mayhem"] == 1.0, "mayhem", "ordinary")
        frames.append(f)
    return pd.concat(frames, ignore_index=True)


@pytest.fixture(scope="module")
def market():
    train = pd.concat([rows(1500, d, "train", d) for d in (12, 13, 14)], ignore_index=True)
    return train, rows(1500, 17, "validation", 17), pd.concat(
        [rows(1200, d, "test", d) for d in (18, 19)], ignore_index=True)


@pytest.fixture(scope="module")
def fitted(market):
    train, validation, _ = market
    return {s.name: T.fit_stage(s, train, validation, grid=GRID, logit_c=(0.1, 1.0),
                                max_rounds=60) for s in STAGES}  # fmt: skip


def small_plan():
    plan, _ = load_plan(ROOT / "config" / "evaluation.yaml")
    return plan.model_copy(update={
        "uncertainty": plan.uncertainty.model_copy(update={"resamples": 40})})


def test_every_baseline_and_model_is_fitted_and_tuned_on_validation(fitted, market):
    _, validation, _ = market
    for stage in STAGES:
        m = fitted[stage.name]
        assert set(m.base) == {30, 60, 120, 300} and "avoid|60" in m.rules
        assert set(m.logistic) == {"logistic", "logistic_bf"}
        assert set(m.lightgbm) == {"lightgbm", "lightgbm_bf"}
        assert not any(c.startswith("bf_") for c in m.lightgbm["lightgbm"]["features"])
        assert any(c.startswith("bf_") for c in m.lightgbm["lightgbm_bf"]["features"])
        assert len(m.tuning["lightgbm"]) == len(GRID) and len(m.tuning["lightgbm_bf"]) == 1
        best = max(m.tuning["lightgbm"], key=lambda t: t["validation_pr_auc"])
        assert m.lightgbm["lightgbm"]["params"]["class_weight"] == best["class_weight"]
        eligible = validation[validation[stage.target].notna()]
        scores = m.scores(eligible, stage)
        assert list(scores) == list(T.MODELS)
        y, o = eligible[stage.target].to_numpy(), eligible["offset_s"].to_numpy()
        for name, (rank, probability) in scores.items():
            assert len(rank) == len(probability) == len(eligible)
            assert ((probability >= 0) & (probability <= 1)).all(), name
        base = T.mean_pr_auc(y, scores["base_rate"][0], o)
        for name in ("rules", "logistic", "lightgbm"):  # the made-up signal is found
            assert T.mean_pr_auc(y, scores[name][0], o) > base + 0.03, (stage.name, name)


def test_scores_cover_the_plan_and_survive_saving(tmp_path, fitted, market):
    _, _, test = market
    hashes = T.save_models(tmp_path, fitted)
    assert {f"{s.name}/lightgbm.txt" for s in STAGES} <= set(hashes)
    loaded = T.load_models(tmp_path)
    plan = small_plan()
    report = T.score_frame(test, loaded, plan)
    assert report == T.score_frame(test, fitted, plan)  # the saved files give the same scores
    for stage in STAGES:
        cells = report[stage.name]["cells"]
        assert {c["slice"] for c in cells} == {"all", "ordinary", "mayhem", "2026-11-18",
                                               "2026-11-19"}  # fmt: skip
        assert {c["model"] for c in cells} == set(T.MODELS)
        assert {c["offset_s"] for c in cells} == {30, 60, 120, 300}
        wanted = set(plan.metrics.stage1 if stage.kind == "filter" else plan.metrics.stage2_3)
        for c in cells:
            got = set(c) - {"offset_s", "slice", "model", "n", "rate"}
            assert got == (wanted if c["slice"] == "all" else wanted - {"calibration_curve"})
            assert c["pr_auc"]["low"] <= c["pr_auc"]["value"] <= c["pr_auc"]["high"]
        one = {c["model"]: c for c in cells if c["slice"] == "all" and c["offset_s"] == 60}
        assert one["base_rate"]["pr_auc"]["value"] == pytest.approx(one["base_rate"]["rate"])
        assert one["lightgbm"]["pr_auc"]["value"] > one["base_rate"]["pr_auc"]["value"]
    text = "\n".join(render_report({"stages": report}, "Test"))
    assert "Stage 3: 2x before 0.5x" in text and "LightGBM + backfill-only" in text
    assert "mayhem coins" in text and "2026-11-19" in text


def test_checks_shap_shuffled_labels_and_walk_forward(fitted, market):
    train, validation, _ = market
    stage = STAGES[3]
    m = fitted[stage.name]
    shap = T.shap_importance(m, validation, stage)
    assert shap[0]["feature"] == "net_sol"  # the one feature that carries the outcome
    assert sum(f["share"] for f in shap) == pytest.approx(1.0)
    shuffled = T.shuffled_label_check(m, train, validation, stage, repeats=2)
    real = T.mean_pr_auc(*(lambda e: (e[stage.target].to_numpy(),
                                       m.scores(e, stage)["lightgbm"][0],
                                       e["offset_s"].to_numpy()))(validation))  # fmt: skip
    assert shuffled["shuffled_validation_pr_auc"] < shuffled["base_rate"] + 0.05 < real
    folds = T.walk_forward(m, train, stage, 3905)
    assert [f["scored_day"] for f in folds] == ["2026-11-13", "2026-11-14"]
    assert [f["trained_on_days"] for f in folds] == [1, 2]
    assert all(f["lightgbm"] > f["base_rate"] for f in folds)
    manifest = {"chosen": {stage.name: {
        "logistic_C": 1.0, "lightgbm": m.lightgbm["lightgbm"]["params"],
        "lightgbm_rounds": 10}}}  # fmt: skip
    checks = {stage.name: {"shap": shap, "shap_bf": shap, "shuffled_labels": shuffled,
                           "walk_forward": folds}}  # fmt: skip
    assert "Shuffled labels" in "\n".join(render_checks(checks, manifest))


def test_the_test_days_are_scored_once(tmp_path):
    plan, sha = load_plan(ROOT / "config" / "evaluation.yaml")
    folder = T.model_dir(tmp_path)
    folder.mkdir(parents=True)
    (folder / "test_report.json").write_text("{}", encoding="utf-8")
    with pytest.raises(T.AlreadyScored):
        T.score_test(tmp_path, plan, sha)
    with pytest.raises(T.AlreadyScored):  # nor are the scored models replaced
        T.train(ROOT, tmp_path, plan, sha)


def test_scoring_refuses_models_or_a_plan_that_changed(tmp_path, fitted):
    plan, sha = load_plan(ROOT / "config" / "evaluation.yaml")
    folder = T.model_dir(tmp_path)
    hashes = T.save_models(folder, fitted)
    (folder / "manifest.json").write_text(json.dumps({"plan_sha": "another", "files": hashes}),
                                          encoding="utf-8")  # fmt: skip
    with pytest.raises(ValueError, match="evaluation.yaml changed"):
        T.score_test(tmp_path, plan, sha)
    (folder / "manifest.json").write_text(json.dumps({"plan_sha": sha, "files": hashes}),
                                          encoding="utf-8")  # fmt: skip
    with (folder / "hit_2x" / "lightgbm.txt").open("a", encoding="utf-8") as fh:
        fh.write("\n")
    with pytest.raises(ValueError, match="differ from the manifest"):
        T.score_test(tmp_path, plan, sha)
    assert not (folder / "test_report.json").exists()
