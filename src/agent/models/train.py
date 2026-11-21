"""Baselines and models for every stage, and their scores (spec 9, 15.3 step R5, 15.4).

For each stage (`frame.STAGES`), fitted on the training days and tuned on the validation
day only:
- `base_rate`: the training rate of the outcome at each decision time (a constant).
- `rules`: the hand-written rules' verdict (config/rules.yaml). As a probability, the
  training rate of the outcome for that verdict and decision time.
- `logistic`: logistic regression on the signals (squashed, missing values filled with
  the median and flagged, standardized); its strength chosen on the validation day.
- `lightgbm`: one gradient-boosted model per stage with the token's age as a feature
  (spec 9); a small grid chosen on the validation day, early stopping on it.
- `logistic_bf`, `lightgbm_bf`: the same with the backfill-only signals added (spec 4.1).
  The models without them are the ones a live tool could run.

Probabilities of the fitted models are calibrated on the validation day (isotonic).
Ranking metrics use the model's raw score, which the calibration only maps (it keeps the
order); the Brier score and the calibration curve use the calibrated probability.

`train` writes the models and a validation report. `score_test` reads the saved models
and scores the test days: once. It refuses when a test report exists, and `train`
refuses to replace models the test days were already scored with.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer, StandardScaler

from agent.dataset.build import _code_version as code_version
from agent.models.evaluate import (
    DAY_S,
    EvaluationPlan,
    Ranking,
    brier,
    calibration_curve,
    pr_auc,
    top_k_per_hour,
)
from agent.models.frame import (
    STAGES,
    Stage,
    dataset_split,
    feature_columns,
    load_frame,
    matrix,
    role_ranges,
)

OUT_FOLDER = "models"
VERSION = "r5"
MODELS = ("base_rate", "rules", "logistic", "lightgbm", "logistic_bf", "lightgbm_bf")
FITTED = ("logistic", "lightgbm", "logistic_bf", "lightgbm_bf")
VERDICT_RANK = {"pass": 0, "caution": 1, "avoid": 2}  # higher = the rules like it less
LOGIT_C = (0.01, 0.1, 1.0)
GBM_BASE = {
    "objective": "binary", "metric": "average_precision", "learning_rate": 0.05,
    "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 1.0,
    "seed": 0, "deterministic": True, "force_row_wise": True, "verbosity": -1,
}  # fmt: skip
GBM_GRID = [
    {"num_leaves": leaves, "min_data_in_leaf": least, "class_weight": weight}
    for leaves in (15, 63) for least in (100, 400) for weight in ("none", "balanced")
]  # fmt: skip
GBM_MAX_ROUNDS, GBM_PATIENCE = 2000, 50
SHAP_ROWS = 20_000
SHUFFLES, SHUFFLE_ROUNDS = 5, 100
TOP_K = (5, 20)


# --- the pieces --------------------------------------------------------------------------


def _squash(x: np.ndarray) -> np.ndarray:
    """Counts and SOL amounts have long tails: log scale, keeping the sign."""
    return np.sign(x) * np.log1p(np.abs(x))


def _logit(c: float) -> Pipeline:
    return Pipeline([
        ("squash", FunctionTransformer(_squash)),
        ("fill", SimpleImputer(strategy="median", add_indicator=True)),
        ("scale", StandardScaler()),
        ("model", LogisticRegression(C=c, max_iter=500)),
    ])  # fmt: skip


def _gbm_params(choice: dict, y: np.ndarray) -> dict:
    params = GBM_BASE | {k: v for k, v in choice.items() if k != "class_weight"}
    if choice["class_weight"] == "balanced":
        params["scale_pos_weight"] = float((1 - y).sum() / max(y.sum(), 1))
    return params


def mean_pr_auc(y: np.ndarray, score: np.ndarray, offsets: np.ndarray) -> float:
    """PR-AUC at each decision time, averaged: what settings are chosen by."""
    return float(np.mean([pr_auc(y[offsets == o], score[offsets == o])
                          for o in np.unique(offsets)]))  # fmt: skip


@dataclass
class StageModels:
    stage: str
    base: dict[int, float]  # decision time -> training rate
    rules: dict[str, float]  # "<verdict>|<decision time>" -> training rate
    logistic: dict = field(default_factory=dict)  # name -> {"model", "calibration", ...}
    lightgbm: dict = field(default_factory=dict)
    tuning: dict = field(default_factory=dict)

    def scores(self, frame: pd.DataFrame, stage: Stage) -> dict[str, tuple]:
        """model -> (score to rank by, probability) for the rows of `frame`."""
        offsets = frame["offset_s"].to_numpy()
        base = np.array([self.base[int(o)] for o in offsets])
        rank = frame["rules_verdict"].map(VERDICT_RANK).to_numpy(dtype=float)
        rules_p = np.array([self.rules.get(f"{v}|{int(o)}", self.base[int(o)]) for v, o in
                            zip(frame["rules_verdict"], offsets, strict=True)])  # fmt: skip
        out = {"base_rate": (base, base),
               "rules": (rank if stage.kind == "filter" else -rank, rules_p)}  # fmt: skip
        for name in FITTED:
            kind = self.logistic if name.startswith("logistic") else self.lightgbm
            if name not in kind:
                continue
            fitted = kind[name]
            x = matrix(frame, fitted["features"])
            if name.startswith("logistic"):
                raw = fitted["model"].decision_function(x)
            else:
                raw = fitted["model"].predict(x, raw_score=True)
            out[name] = (raw, fitted["calibration"].predict(raw))
        return out


def _eligible(frame: pd.DataFrame, stage: Stage) -> pd.DataFrame:
    return frame[frame[stage.target].notna()]


def fit_stage(stage: Stage, train: pd.DataFrame, validation: pd.DataFrame, *,
              progress=lambda _msg: None, grid: list[dict] | None = None,
              logit_c: tuple[float, ...] = LOGIT_C,
              max_rounds: int = GBM_MAX_ROUNDS) -> StageModels:  # fmt: skip
    """Fit every baseline and model of one stage on `train`, tuned on `validation`."""
    tr, va = _eligible(train, stage), _eligible(validation, stage)
    y_tr, y_va = tr[stage.target].to_numpy(), va[stage.target].to_numpy()
    o_va = va["offset_s"].to_numpy()
    by_offset = tr.groupby("offset_s")[stage.target].mean()
    by_verdict = tr.groupby(["rules_verdict", "offset_s"])[stage.target].mean()
    models = StageModels(
        stage.name, {int(o): float(r) for o, r in by_offset.items()},
        {f"{v}|{int(o)}": float(r) for (v, o), r in by_verdict.items()},
    )  # fmt: skip
    chosen_gbm = None
    for suffix, with_bf in (("", False), ("_bf", True)):
        columns = feature_columns(with_backfill_only=with_bf)
        if any(c not in tr.columns for c in columns):
            continue
        x_tr, x_va = matrix(tr, columns), matrix(va, columns)

        tried = []
        for c in logit_c:
            model = _logit(c).fit(x_tr, y_tr)
            raw = model.decision_function(x_va)
            tried.append({"C": c, "validation_pr_auc": mean_pr_auc(y_va, raw, o_va),
                          "model": model, "raw": raw})  # fmt: skip
            progress(f"  {stage.name} logistic{suffix} C={c}: validation PR-AUC "
                     f"{tried[-1]['validation_pr_auc']:.4f}")  # fmt: skip
        best = max(tried, key=lambda t: t["validation_pr_auc"])
        models.logistic[f"logistic{suffix}"] = {
            "model": best["model"], "features": columns, "C": best["C"],
            "calibration": _calibrate(best["raw"], y_va),
        }  # fmt: skip
        models.tuning[f"logistic{suffix}"] = [
            {k: v for k, v in t.items() if k not in ("model", "raw")} for t in tried]  # fmt: skip

        train_set = lgb.Dataset(x_tr, y_tr, feature_name=columns, free_raw_data=False,
                                params={"feature_pre_filter": False})  # fmt: skip
        val_set = lgb.Dataset(x_va, y_va, reference=train_set,
                              params={"feature_pre_filter": False})
        # The grid is searched without the backfill-only signals; with them, the chosen
        # settings are refitted (early stopping again).
        choices = (grid or GBM_GRID) if chosen_gbm is None else [chosen_gbm]
        tried = []
        for choice in choices:
            booster = lgb.train(
                _gbm_params(choice, y_tr), train_set, num_boost_round=max_rounds,
                valid_sets=[val_set],
                callbacks=[lgb.early_stopping(GBM_PATIENCE, verbose=False)],
            )
            raw = booster.predict(x_va, raw_score=True)
            tried.append({**choice, "rounds": booster.best_iteration,
                          "validation_pr_auc": mean_pr_auc(y_va, raw, o_va),
                          "model": booster, "raw": raw})  # fmt: skip
            progress(f"  {stage.name} lightgbm{suffix} {choice}: {booster.best_iteration} "
                     f"rounds, validation PR-AUC {tried[-1]['validation_pr_auc']:.4f}")  # fmt: skip
        best = max(tried, key=lambda t: t["validation_pr_auc"])
        if chosen_gbm is None:
            chosen_gbm = {k: best[k] for k in ("num_leaves", "min_data_in_leaf", "class_weight")}
        models.lightgbm[f"lightgbm{suffix}"] = {
            "model": best["model"], "features": columns, "params": chosen_gbm,
            "rounds": best["rounds"], "calibration": _calibrate(best["raw"], y_va),
        }  # fmt: skip
        models.tuning[f"lightgbm{suffix}"] = [
            {k: v for k, v in t.items() if k not in ("model", "raw")} for t in tried]  # fmt: skip
    return models


def _calibrate(raw: np.ndarray, y: np.ndarray) -> IsotonicRegression:
    return IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip").fit(raw, y)


# --- scoring -----------------------------------------------------------------------------


def score_frame(frame: pd.DataFrame, fitted: dict[str, StageModels],
                plan: EvaluationPlan) -> dict:  # fmt: skip
    """The plan's metrics for every stage, model and decision time: for all coins, for
    ordinary and mayhem coins, and for each day; intervals resample whole launch hours."""
    u = plan.uncertainty
    interval = {"level": u.level, "resamples": u.resamples, "seed": u.seed}
    dates = sorted(frame["date"].unique())
    report = {}
    for stage in STAGES:
        rows = _eligible(frame, stage)
        scores = fitted[stage.name].scores(rows, stage)
        y = rows[stage.target].to_numpy()
        hours = rows["hour_ts"].to_numpy()
        offsets = rows["offset_s"].to_numpy()
        slices = {"all": np.ones(len(rows), dtype=bool)}
        slices |= {g: (rows["group"] == g).to_numpy() for g in ("ordinary", "mayhem")}
        if len(dates) > 1:
            slices |= {d: (rows["date"] == d).to_numpy() for d in dates}
        cells = []
        for offset in sorted(np.unique(offsets)):
            for name, inside in slices.items():
                pick = inside & (offsets == offset)
                if not pick.any():
                    continue
                for model, (rank, probability) in scores.items():
                    cell = {"offset_s": int(offset), "slice": name, "model": model,
                            "n": int(pick.sum()), "rate": float(y[pick].mean())}  # fmt: skip
                    ranking = Ranking(y[pick], rank[pick], hours[pick])
                    if stage.kind == "filter":
                        got = ranking.estimates(fine_blocked=plan.metrics.max_fine_blocked,
                                                **interval)  # fmt: skip
                        cell["pr_auc"] = got["pr_auc"].as_dict()
                        cell["recall_at_max_fine_blocked"] = got["recall"].as_dict()
                    else:
                        cell["pr_auc"] = ranking.estimates(**interval)["pr_auc"].as_dict()
                        for k in TOP_K:
                            cell[f"precision_top_{k}_per_hour"] = top_k_per_hour(
                                y[pick], rank[pick], hours[pick], k, **interval).as_dict()
                        cell["brier_score"] = brier(y[pick], probability[pick], hours[pick],
                                                    **interval).as_dict()  # fmt: skip
                        if name == "all":
                            cell["calibration_curve"] = calibration_curve(
                                y[pick], probability[pick])
                    cells.append(cell)
        report[stage.name] = {"title": stage.title, "kind": stage.kind, "cells": cells}
    return report


# --- checks (spec 9: SHAP; 15.3: leakage checks) -------------------------------------------


def shap_importance(models: StageModels, validation: pd.DataFrame, stage: Stage,
                    name: str = "lightgbm") -> list[dict]:  # fmt: skip
    """Mean absolute SHAP value of each feature on a sample of validation rows (the tree
    model's own exact SHAP values), largest first."""
    fitted = models.lightgbm[name]
    rows = _eligible(validation, stage)
    if len(rows) > SHAP_ROWS:
        rows = rows.sample(SHAP_ROWS, random_state=0)
    contrib = fitted["model"].predict(matrix(rows, fitted["features"]), pred_contrib=True)
    size = np.abs(contrib[:, :-1]).mean(axis=0)
    total = size.sum() or 1.0
    order = np.argsort(-size)
    return [{"feature": fitted["features"][i], "mean_abs_shap": float(size[i]),
             "share": float(size[i] / total)} for i in order]  # fmt: skip


def _mean_roc_auc(y: np.ndarray, score: np.ndarray, offsets: np.ndarray) -> float:
    return float(np.mean([roc_auc_score(y[offsets == o], score[offsets == o])
                          for o in np.unique(offsets)]))  # fmt: skip


def shuffled_label_check(models: StageModels, train: pd.DataFrame, validation: pd.DataFrame,
                         stage: Stage, repeats: int = SHUFFLES) -> dict:  # fmt: skip
    """Train the chosen LightGBM settings on training labels put in a random order (within
    each decision time, so the rates stay). With no real link between features and labels
    left, the validation PR-AUC must fall to about the base rate; if it stays high,
    something other than the features carries the answer. Repeated, since one random
    order can land a little above or below."""
    fitted = models.lightgbm["lightgbm"]
    tr, va = _eligible(train, stage), _eligible(validation, stage)
    x_tr, x_va = matrix(tr, fitted["features"]), matrix(va, fitted["features"])
    y_tr, o_tr = tr[stage.target].to_numpy(), tr["offset_s"].to_numpy()
    y_va, o_va = va[stage.target].to_numpy(), va["offset_s"].to_numpy()
    rng = np.random.default_rng(0)
    got, rocs = [], []
    for _ in range(repeats):
        y = y_tr.copy()
        for o in np.unique(o_tr):
            y[o_tr == o] = rng.permutation(y_tr[o_tr == o])
        rounds = min(max(fitted["rounds"], 1), SHUFFLE_ROUNDS)
        booster = lgb.train(_gbm_params(fitted["params"], y), lgb.Dataset(x_tr, y),
                            num_boost_round=rounds)  # fmt: skip
        raw = booster.predict(x_va, raw_score=True)
        got.append(mean_pr_auc(y_va, raw, o_va))
        rocs.append(_mean_roc_auc(y_va, raw, o_va))
    real = fitted["model"].predict(x_va, raw_score=True)
    return {"shuffled_validation_pr_auc": float(np.mean(got)),
            "shuffled_lowest": float(min(got)), "shuffled_highest": float(max(got)),
            # ROC-AUC is 0.5 for a score that knows nothing, above or below it by chance.
            "shuffled_roc_auc": float(np.mean(rocs)), "shuffled_roc_lowest": float(min(rocs)),
            "shuffled_roc_highest": float(max(rocs)),
            "real_pr_auc": mean_pr_auc(y_va, real, o_va),
            "real_roc_auc": _mean_roc_auc(y_va, real, o_va),
            "repeats": repeats,
            "base_rate": float(np.mean([y_va[o_va == o].mean()
                                        for o in np.unique(o_va)]))}  # fmt: skip


def walk_forward(models: StageModels, train: pd.DataFrame, stage: Stage,
                 drop_label_overlap_s: int) -> list[dict]:  # fmt: skip
    """The chosen settings refitted on the training days before each later training day
    and scored on that day (spec 15.4: walk-forward inside the training days). Nothing is
    chosen from these; they show how much a result moves from day to day."""
    rows = _eligible(train, stage)
    days = sorted(rows["date"].unique())
    out = []
    for i in range(1, len(days)):
        day_start = int(rows.loc[rows["date"] == days[i], "created_ts"].min()) // DAY_S * DAY_S
        past = rows[(rows["date"] < days[i])
                    & (rows["created_ts"] + drop_label_overlap_s <= day_start)]  # fmt: skip
        now = rows[rows["date"] == days[i]]
        y, y_now, o_now = (past[stage.target].to_numpy(), now[stage.target].to_numpy(),
                           now["offset_s"].to_numpy())  # fmt: skip
        fold = {"scored_day": days[i], "trained_on_days": i, "n": len(now),
                "base_rate": float(np.mean([y_now[o_now == o].mean()
                                            for o in np.unique(o_now)]))}  # fmt: skip
        logit = models.logistic["logistic"]
        model = _logit(logit["C"]).fit(matrix(past, logit["features"]), y)
        fold["logistic"] = mean_pr_auc(
            y_now, model.decision_function(matrix(now, logit["features"])), o_now)
        gbm = models.lightgbm["lightgbm"]
        booster = lgb.train(_gbm_params(gbm["params"], y),
                            lgb.Dataset(matrix(past, gbm["features"]), y),
                            num_boost_round=max(gbm["rounds"], 1))  # fmt: skip
        fold["lightgbm"] = mean_pr_auc(
            y_now, booster.predict(matrix(now, gbm["features"]), raw_score=True), o_now)
        rank = now["rules_verdict"].map(VERDICT_RANK).to_numpy(dtype=float)
        fold["rules"] = mean_pr_auc(y_now, rank if stage.kind == "filter" else -rank, o_now)
        out.append(fold)
    return out


# --- saving and loading ------------------------------------------------------------------


def model_dir(data_dir: Path) -> Path:
    return data_dir / OUT_FOLDER / VERSION


def save_models(folder: Path, fitted: dict[str, StageModels]) -> dict[str, str]:
    """One folder per stage: LightGBM models as text files, the rest in one joblib file.
    Returns each file's SHA-256, for the manifest."""
    hashes = {}
    for name, models in fitted.items():
        stage_dir = folder / name
        stage_dir.mkdir(parents=True, exist_ok=True)
        rest = {"stage": models.stage, "base": models.base, "rules": models.rules,
                "tuning": models.tuning, "logistic": models.logistic, "lightgbm": {}}  # fmt: skip
        for key, gbm in models.lightgbm.items():
            path = stage_dir / f"{key}.txt"
            gbm["model"].save_model(str(path), num_iteration=gbm["rounds"])
            rest["lightgbm"][key] = {k: v for k, v in gbm.items() if k != "model"}
            hashes[f"{name}/{path.name}"] = _sha256(path)
        joblib.dump(rest, stage_dir / "models.joblib")
        hashes[f"{name}/models.joblib"] = _sha256(stage_dir / "models.joblib")
    return hashes


def load_models(folder: Path) -> dict[str, StageModels]:
    """The saved models. Only files this project wrote are read (joblib files run code
    when loaded: never load one from anywhere else)."""
    fitted = {}
    for stage in STAGES:
        stage_dir = folder / stage.name
        rest = joblib.load(stage_dir / "models.joblib")
        gbms = {key: {**meta, "model": lgb.Booster(model_file=str(stage_dir / f"{key}.txt"))}
                for key, meta in rest["lightgbm"].items()}  # fmt: skip
        fitted[stage.name] = StageModels(rest["stage"], rest["base"], rest["rules"],
                                         rest["logistic"], gbms, rest["tuning"])  # fmt: skip
    return fitted


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class AlreadyScored(RuntimeError):
    pass


def train(root: Path, data_dir: Path, plan: EvaluationPlan, plan_sha: str, *,
          progress=lambda _msg: None) -> dict:  # fmt: skip
    """Fit every stage on the training days, tune on the validation day, run the checks,
    and save the models with a manifest and a validation report. The test days are not
    read."""
    folder = model_dir(data_dir)
    if (folder / "test_report.json").exists():
        raise AlreadyScored(
            f"The test days were already scored with the models in {folder}. They are "
            "scored once (spec 15.4): training again is refused.")
    started = time.time()
    split, dataset = dataset_split(data_dir, plan)
    drop = plan.split.drop_label_overlap_s
    frame = load_frame(data_dir, plan, ("train", "validation"))
    tr, va = frame[frame["role"] == "train"], frame[frame["role"] == "validation"]
    progress(f"Training rows {len(tr):,} ({tr['mint'].nunique():,} coins), validation rows "
             f"{len(va):,} ({va['mint'].nunique():,} coins)")  # fmt: skip
    fitted, checks = {}, {}
    for stage in STAGES:
        progress(f"{stage.title}")
        fitted[stage.name] = fit_stage(stage, tr, va, progress=progress)
        checks[stage.name] = {
            "shap": shap_importance(fitted[stage.name], va, stage),
            "shap_bf": shap_importance(fitted[stage.name], va, stage, "lightgbm_bf"),
            "shuffled_labels": shuffled_label_check(fitted[stage.name], tr, va, stage),
            "walk_forward": walk_forward(fitted[stage.name], tr, stage, drop),
        }
        progress(f"  checks done ({time.time() - started:.0f} s so far)")
    folder.mkdir(parents=True, exist_ok=True)
    hashes = save_models(folder, fitted)
    progress("Scoring the validation day")
    report = {"set": "validation", "stages": score_frame(va, load_models(folder), plan)}
    ranges = role_ranges(split, drop)
    manifest = {
        "version": VERSION, "made_at": time.time(), "seconds": round(time.time() - started),
        "plan_sha": plan_sha, "dataset_sha256": dataset["file_sha256"],
        "code": code_version(root),
        "days": {"train": _dates(split.train), "validation": _dates(split.validation),
                 "test": _dates(split.test)},  # fmt: skip
        "launch_ranges": ranges, "drop_label_overlap_s": drop,
        "rows": {"train": len(tr), "validation": len(va)},
        "coins": {"train": int(tr["mint"].nunique()), "validation": int(va["mint"].nunique())},
        "coins_in_both": len(set(tr["mint"]) & set(va["mint"])),
        "features": feature_columns(), "features_backfill_only": [
            c for c in feature_columns(with_backfill_only=True) if c not in feature_columns()],
        "chosen": {
            name: {"logistic_C": m.logistic["logistic"]["C"],
                   "logistic_bf_C": m.logistic["logistic_bf"]["C"],
                   "lightgbm": m.lightgbm["lightgbm"]["params"],
                   "lightgbm_rounds": m.lightgbm["lightgbm"]["rounds"],
                   "lightgbm_bf_rounds": m.lightgbm["lightgbm_bf"]["rounds"]}
            for name, m in fitted.items()},  # fmt: skip
        "tuning": {name: m.tuning for name, m in fitted.items()},
        "files": hashes,
    }
    _write_json(folder / "manifest.json", manifest)
    _write_json(folder / "checks.json", checks)
    _write_json(folder / "validation_report.json", report)
    return {"manifest": manifest, "checks": checks, "validation": report, "folder": folder}


def score_test(data_dir: Path, plan: EvaluationPlan, plan_sha: str, *,
               progress=lambda _msg: None) -> dict:  # fmt: skip
    """Score the test days with the saved models: once. The models' files must be the
    ones the manifest lists, and the plan the one they were trained under."""
    folder = model_dir(data_dir)
    out = folder / "test_report.json"
    if out.exists():
        raise AlreadyScored(f"The test days were already scored: {out}. They are scored "
                            "once (spec 15.4).")  # fmt: skip
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if manifest["plan_sha"] != plan_sha:
        raise ValueError("config/evaluation.yaml changed since the models were trained.")
    changed = [f for f, sha in manifest["files"].items() if _sha256(folder / f) != sha]
    if changed:
        raise ValueError(f"Model files differ from the manifest: {', '.join(changed)}")
    fitted = load_models(folder)
    frame = load_frame(data_dir, plan, ("test",))
    progress(f"Test rows {len(frame):,} ({frame['mint'].nunique():,} coins)")
    report = {"set": "test", "scored_at": time.time(), "rows": len(frame),
              "coins": int(frame["mint"].nunique()),
              "stages": score_frame(frame, fitted, plan)}  # fmt: skip
    _write_json(out, report)
    return report


def _dates(days: list[int]) -> list[str]:
    return [time.strftime("%Y-%m-%d", time.gmtime(d)) for d in days]


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=_plain), encoding="utf-8")
    tmp.replace(path)


def _plain(value):
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(f"not JSON: {type(value)}")
