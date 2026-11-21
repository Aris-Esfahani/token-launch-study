"""Helpers for the live-scoring tests: a stand-in scorer that needs no research package,
and a tiny saved-model folder made with R5's own saving code (needs the research packages).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

STAGES = [{"name": "bad", "title": "Stage 1"}, {"name": "grad_later", "title": "Stage 2"},
          {"name": "hit_2x", "title": "Stage 3"}]  # fmt: skip
INFO = {"version": "r5", "bundle_sha": "testbundle00", "trained_days": ["2026-11-12", "2026-11-16"],
        "validation_days": ["2026-11-17"], "test_days": ["2026-11-18", "2026-11-19"],
        "stages": STAGES, "min_hour_covered": 0.95}  # fmt: skip


class FakeScorer:
    """Stands in for `LiveScorer`: remembers what it was asked and returns set numbers."""

    name, bundle_sha = "r5/lightgbm", "testbundle00"

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[dict, dict, bool]] = []
        self.fail = fail

    def score(self, snapshot: dict, context: dict, has_gap: bool) -> dict:
        self.calls.append((snapshot, context, has_gap))
        if self.fail:
            raise RuntimeError("boom")
        out = {"model": self.name, "bundle": self.bundle_sha, "context": context}
        if has_gap:
            return {**out, "why": "a recording gap touched this coin"}
        return {**out, "p": {"bad": 0.9, "grad_later": 0.02, "hit_2x": 0.05},
                "raw": {"bad": 2.0, "grad_later": -4.0, "hit_2x": -3.0}}  # fmt: skip

    def usual(self, offset_s: int, is_mayhem: bool) -> dict:
        return {"bad": 0.98 if is_mayhem else 0.87, "grad_later": 0.011, "hit_2x": 0.024}

    def info(self) -> dict:
        return dict(INFO)


def make_models(data_dir: Path, *, seed: int = 0, rows: int = 600) -> dict:
    """A small R5 model folder under `data_dir` (written by `train.save_models`), its
    manifest and test report, and the live bundle exported from it. Returns the fitted
    models and the rows they were fitted on. Needs lightgbm and scikit-learn."""
    import lightgbm as lgb
    import numpy as np
    import pandas as pd
    from sklearn.isotonic import IsotonicRegression

    from agent.export_live import export_bundle
    from agent.models.frame import STAGES as R5_STAGES
    from agent.models.frame import feature_columns
    from agent.models.train import StageModels, save_models

    rng = np.random.default_rng(seed)
    columns = feature_columns()
    frame = pd.DataFrame(rng.normal(size=(rows, len(columns))), columns=columns)
    frame.loc[frame.index % 7 == 0, "dev_sold_pct"] = np.nan
    frame["completed"] = (rng.random(rows) < 0.1).astype(float)
    frame["offset_s"] = rng.choice([30, 60, 120, 300], size=rows)
    frame["rules_verdict"] = "caution"
    x = frame[columns].to_numpy(dtype=np.float64)
    fitted = {}
    for i, stage in enumerate(R5_STAGES):
        y = (x[:, i] + rng.normal(size=rows) > 0.5).astype(float)
        booster = lgb.train(
            {"objective": "binary", "num_leaves": 7, "min_data_in_leaf": 10, "verbosity": -1,
             "seed": 0, "deterministic": True, "force_row_wise": True},
            lgb.Dataset(x, y, feature_name=columns), num_boost_round=12,
        )  # fmt: skip
        raw = booster.predict(x, raw_score=True)
        calibration = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip").fit(raw, y)
        fitted[stage.name] = StageModels(
            stage.name, {o: 0.5 for o in (30, 60, 120, 300)}, {},
            lightgbm={"lightgbm": {"model": booster, "features": columns, "rounds": 12,
                                   "params": {}, "calibration": calibration}},
        )  # fmt: skip
    folder = data_dir / "models" / "r5"
    hashes = save_models(folder, fitted)
    manifest = {
        "version": "r5", "features": columns, "files": hashes,
        "days": {"train": ["2026-11-12", "2026-11-16"], "validation": ["2026-11-17"],
                 "test": ["2026-11-18", "2026-11-19"]},
    }  # fmt: skip
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    report = {"stages": {
        stage.name: {"cells": [
            {"offset_s": offset, "slice": group, "model": model, "rate": rate}
            for offset in (30, 60, 120, 300)
            for group, rate in (("all", 0.5), ("ordinary", 0.25), ("mayhem", 0.75))
            for model in ("base_rate", "lightgbm")
        ]} for stage in R5_STAGES
    }}  # fmt: skip
    (folder / "test_report.json").write_text(json.dumps(report), encoding="utf-8")
    export_bundle(data_dir)
    return {"fitted": fitted, "frame": frame, "columns": columns, "folder": folder}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
