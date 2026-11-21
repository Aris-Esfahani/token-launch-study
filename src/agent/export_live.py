"""Prepare the saved R5 models for the engine, and check the result (spec 15.3, step L1).

Research side: this module loads the training code, so the engine never imports it.

`export_bundle` writes `<data folder>/models/r5_live/bundle.json`: each live stage's
calibration map (the points of the isotonic fit, as plain numbers) and the signals the
models read, with the hashes of the files it was made from. Nothing in the R5 folder is
changed. The engine reads the LightGBM text files from the R5 folder itself.

`check_bundle` compares the engine's scoring with R5's own on the validation day (never
the test days), two ways:
1. the same rows through both: R5's scoring code on the dataset's rows, and the live
   scorer on those rows' values;
2. the live path from raw events: coins of one stretch of the validation day replayed
   from the backfill through `TokenState`, `LaunchHistory` and the live scorer, against
   R5's scores for the same coins. This also checks that the live launch-context
   signals equal the dataset's.
Both must agree to the last digit.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import numpy as np

from agent.features.replay import BACKFILL_ONLY, load_states
from agent.features.token_state import SNAPSHOT_OFFSETS_S
from agent.ingest.backfill_coverage import backfill_spans
from agent.models.evaluate import EvaluationPlan
from agent.models.frame import STAGES, load_frame
from agent.models.train import load_models, model_dir
from agent.scoring.launch_context import LaunchHistory, load_launch_index
from agent.scoring.live import BUNDLE_FILE, BUNDLE_FOLDER, MODEL_KIND, LiveScorer, load_scorer

# The stages shown live (spec 7: stages 1-3). "Stage 1 without the bundled stand-in" is a
# second reading of stage 1 in the report, not a fourth score.
LIVE_STAGES = ("bad", "grad_later", "hit_2x")
# Stages predicted only for coins still on the bonding curve (frame.load_frame: the
# target is empty where `completed` is true).
ON_CURVE_ONLY = ("grad_later",)
SAME_ROWS = 4_000  # rows of the validation day for check 1
REPLAY_S = 20 * 60  # launches of this long a stretch of the validation day for check 2


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bundle_path(data_dir: Path) -> Path:
    return data_dir / "models" / BUNDLE_FOLDER / BUNDLE_FILE


def export_bundle(data_dir: Path) -> Path:
    """Write the live bundle from the saved models, whose files must be the ones the R5
    manifest lists."""
    folder = model_dir(data_dir)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    changed = [f for f, sha in manifest["files"].items() if _sha256(folder / f) != sha]
    if changed:
        raise ValueError(f"Model files differ from the manifest: {', '.join(changed)}")
    fitted = load_models(folder)
    titles = {s.name: s.title for s in STAGES}
    stages = {}
    for name in LIVE_STAGES:
        model = fitted[name].lightgbm[MODEL_KIND]
        if list(model["features"]) != list(manifest["features"]):
            raise ValueError(f"the {name} model reads other signals than the manifest lists")
        calibration = model["calibration"]
        stages[name] = {
            "title": titles[name],
            "on_curve_only": name in ON_CURVE_ONLY,
            "calibration": {"x": [float(v) for v in calibration.X_thresholds_],
                            "y": [float(v) for v in calibration.y_thresholds_]},  # fmt: skip
        }
    bundle = {
        "version": manifest["version"],
        "model": MODEL_KIND,
        "made_at": time.time(),
        "features": list(manifest["features"]),
        "source": {
            "manifest_sha256": _sha256(folder / "manifest.json"),
            "files": {f"{name}/models.joblib": manifest["files"][f"{name}/models.joblib"]
                      for name in LIVE_STAGES},  # fmt: skip
        },
        "stages": stages,
    }
    path = bundle_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(bundle, indent=1), encoding="utf-8")
    tmp.replace(path)
    return path


def _r5_scores(fitted: dict, frame) -> dict[tuple[str, int, str], tuple[float, float]]:
    """(mint, decision time, stage) -> (raw score, probability) from R5's own code."""
    out = {}
    stages = {s.name: s for s in STAGES}
    for name in LIVE_STAGES:
        rows = frame[frame[stages[name].target].notna()]
        raw, p = fitted[name].scores(rows, stages[name])[MODEL_KIND]
        for mint, offset, r, q in zip(rows["mint"], rows["offset_s"], raw, p, strict=True):
            out[(mint, int(offset), name)] = (float(r), float(q))
    return out


def _compare(live: dict, r5: dict, key: tuple[str, int]) -> list[str]:
    """Stages of one coin and decision time where the live score is not R5's."""
    wrong = []
    for name in LIVE_STAGES:
        expected = r5.get((*key, name))
        got = ((live.get("raw") or {}).get(name), (live.get("p") or {}).get(name))
        if expected is None:
            if got != (None, None):
                wrong.append(name)
        elif got != expected:
            wrong.append(name)
    return wrong


def check_bundle(data_dir: Path, plan: EvaluationPlan, *, scorer: LiveScorer | None = None,
                 same_rows: int = SAME_ROWS, replay_s: int = REPLAY_S,
                 progress=lambda _msg: None) -> dict:  # fmt: skip
    """Compare the live scorer with R5's scoring on the validation day. Returns counts;
    `ok` is true when every score agrees to the last digit."""
    scorer = scorer or load_scorer(data_dir)
    fitted = load_models(model_dir(data_dir))
    # With the backfill-only signals: R5's scoring code scores every saved model.
    frame = load_frame(data_dir, plan, ("validation",))
    r5 = _r5_scores(fitted, frame)
    progress(f"Validation day: {len(frame):,} rows; R5 scores made")

    # 1. The same rows through both pieces of code.
    step = max(len(frame) // max(same_rows, 1), 1)
    sample = frame.iloc[::step]
    covered = {"hour_covered": 1.0, "day_covered": 1.0}
    same = {"rows": 0, "different": 0}
    for record in sample.to_dict("records"):
        values = {k: (None if isinstance(v, float) and np.isnan(v) else v)
                  for k, v in record.items()}  # fmt: skip
        snapshot = {**values, "is_sol_paired": True, "completed": bool(values["completed"])}
        context = {k: values[k] for k in ("dev_launches_24h", "launches_last_hour",
                                          "launch_hour_utc")} | covered  # fmt: skip
        live = scorer.score(snapshot, context, has_gap=False)
        same["rows"] += 1
        same["different"] += bool(_compare(live, r5, (record["mint"], int(record["offset_s"]))))
    progress(f"Same rows through both: {same['rows']:,} rows, {same['different']:,} differ")

    # 2. The live path from raw events, for one stretch of the day.
    start = int(frame["created_ts"].min()) + 6 * 3600
    end = start + replay_s
    replay = load_states(data_dir, start, end, roots=BACKFILL_ONLY)
    history = LaunchHistory(
        load_launch_index(data_dir, roots=BACKFILL_ONLY, before_ts=end),
        backfill=backfill_spans(data_dir),
    )
    modeled = {(m, int(o)) for m, o in zip(frame["mint"], frame["offset_s"], strict=True)}
    replayed = {"coins": 0, "rows": 0, "different": 0, "context_different": 0}
    by_key = frame.set_index(["mint", "offset_s"])
    for state in replay.states:
        context = history.context(state.creator, state.created_ts)
        counted = False
        for offset in SNAPSHOT_OFFSETS_S:
            key = (state.mint, offset)
            if key not in modeled:  # not SOL-paired, a gap, or a label not known
                continue
            counted = True
            snapshot = state.snapshot(state.created_ts + offset, creators=replay.creators)
            live = scorer.score(snapshot, context, has_gap=False)
            row = by_key.loc[key]
            replayed["rows"] += 1
            replayed["different"] += bool(_compare(live, r5, key))
            replayed["context_different"] += any(
                context[k] != row[k] for k in ("dev_launches_24h", "launches_last_hour",
                                               "launch_hour_utc"))  # fmt: skip
        replayed["coins"] += counted
    progress(f"Replayed from raw events: {replayed['coins']:,} coins, {replayed['rows']:,} "
             f"rows, {replayed['different']:,} differ")  # fmt: skip
    ok = (same["rows"] > 0 and replayed["rows"] > 0 and same["different"] == 0
          and replayed["different"] == 0 and replayed["context_different"] == 0)  # fmt: skip
    return {"ok": ok, "same_rows": same, "replayed": replayed,
            "replayed_from_ts": start, "replayed_to_ts": end}  # fmt: skip
