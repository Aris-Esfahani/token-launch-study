"""The saved R5 models, scoring one coin at one decision time (spec 15.3, step L1).

Analysis only. The backtest (R6) found no signal that makes money after costs, and
nothing here places a trade or reads a private key.

What is loaded, all from `<data folder>/models/`:
- `r5/<stage>/lightgbm.txt`: the LightGBM model of each stage (a text file), checked
  against the SHA-256 the R5 manifest recorded for it.
- `r5_live/bundle.json`: each stage's calibration map as plain numbers, written once by
  `agent export-live-models`. The maps are stored in the R5 folder inside
  `models.joblib`, which cannot be opened without loading the training code (and through
  it the labels); the bundle names the hash of the `models.joblib` it was made from, and
  that is checked against the manifest too.
- `r5/test_report.json`: only the usual rate of each outcome on the test days, to show
  beside a probability. Reading the report scores nothing.

A score is made only at the decision times the models were trained at (T+30/60/120/300 s),
for the rows they were trained on: SOL-paired coins, no recording gap, and for
"graduates later" only coins still on the bonding curve. Anything else gets no score and
the reason (`why`, or `missing` for one stage).

The engine must start without the research packages: `load_scorer` raises `ScoringOff`
with a reason to show, and never anything else for a missing package or file.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

MODEL_FOLDER, BUNDLE_FOLDER, BUNDLE_FILE = "r5", "r5_live", "bundle.json"
MODEL_KIND = "lightgbm"  # the model without backfill-only signals: the one a live tool can run
MIN_HOUR_COVERED = 0.95
INSTALL_HINT = 'pip install -e ".[research]"'


class ScoringOff(Exception):
    """Model scores cannot be made; the message says why, in words for the screen."""


@dataclass
class _Stage:
    name: str
    title: str
    on_curve_only: bool
    booster: object  # lightgbm.Booster
    cal_x: object  # numpy arrays: the calibration map's raw scores and probabilities
    cal_y: object


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path, what: str, fix: str) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ScoringOff(f"{what} not found ({path}): {fix}") from None
    except (OSError, ValueError) as exc:
        raise ScoringOff(f"{what} cannot be read ({path.name}: {exc})") from None


def _number(value: object) -> float:
    """A signal as the models saw it in training: a number, missing as NaN."""
    return float("nan") if value is None else float(value)


class LiveScorer:
    def __init__(self, stages: list[_Stage], features: list[str], info: dict,
                 usual: dict, min_hour_covered: float, np) -> None:  # fmt: skip
        self._stages = stages
        self.features = features
        self._info = info
        self._usual = usual  # stage -> decision time -> group -> rate on the test days
        self.min_hour_covered = min_hour_covered
        self._np = np
        self.name = f"{info['version']}/{MODEL_KIND}"
        self.bundle_sha = info["bundle_sha"]

    @property
    def stages(self) -> list[tuple[str, str]]:
        return [(s.name, s.title) for s in self._stages]

    def info(self) -> dict:
        """What the screen says about the models beside every score."""
        return dict(self._info, min_hour_covered=self.min_hour_covered)

    def usual(self, offset_s: int, is_mayhem: bool) -> dict:
        """Each stage's rate on the test days at this decision time, for the coin's group
        (ordinary or mayhem): what a probability is compared with. None if unknown."""
        group = "mayhem" if is_mayhem else "ordinary"
        return {s.name: self._usual.get(s.name, {}).get(str(offset_s), {}).get(group)
                for s in self._stages}  # fmt: skip

    def score(self, snapshot: dict, context: dict, has_gap: bool) -> dict:
        """The stages' calibrated probabilities (`p`) and raw scores (`raw`) for one coin
        at one decision time, or why there are none. `context` is what
        `LaunchHistory.context` gave for the coin; it is kept in the result."""
        out = {"model": self.name, "bundle": self.bundle_sha, "context": context}
        why = self._unscorable(snapshot, context, has_gap)
        if why:
            out["why"] = why
            return out
        values = {**snapshot, **context}
        try:
            row = [[_number(values[name]) for name in self.features]]
        except KeyError as exc:
            out["why"] = f"the models read a signal the live code does not make: {exc.args[0]}"
            return out
        np = self._np
        x = np.array(row, dtype=np.float64)
        p, raw, missing = {}, {}, {}
        for stage in self._stages:
            if stage.on_curve_only and snapshot.get("completed"):
                missing[stage.name] = "already graduated"
                continue
            score = float(stage.booster.predict(x, raw_score=True, num_threads=1)[0])
            raw[stage.name] = score
            p[stage.name] = float(np.interp(score, stage.cal_x, stage.cal_y))
        out["p"], out["raw"] = p, raw
        if missing:
            out["missing"] = missing
        return out

    def _unscorable(self, snapshot: dict, context: dict, has_gap: bool) -> str | None:
        if not snapshot.get("is_sol_paired"):
            return "not paired with SOL (the models cover SOL-paired coins only)"
        if has_gap:
            return "a recording gap touched this coin, so its signals may be incomplete"
        covered = context.get("hour_covered")
        if covered is None or covered < self.min_hour_covered:
            share = "none" if covered is None else f"{covered:.0%}"
            need = f"{self.min_hour_covered:.0%}"
            return (f"only {share} of the hour before its launch was recorded "
                    f"({need} needed to count the launches in it)")  # fmt: skip
        return None


def load_scorer(data_dir: Path, *, min_hour_covered: float = MIN_HOUR_COVERED) -> LiveScorer:
    """The saved models, ready to score. Raises `ScoringOff` when they cannot be used:
    a research package is not installed, a file is missing, or a file is not the one the
    R5 manifest lists."""
    try:
        import lightgbm
        import numpy as np
    except ImportError as exc:
        raise ScoringOff(
            f"{exc.name or 'a research package'} is not installed ({INSTALL_HINT})"
        ) from None
    folder = data_dir / "models" / MODEL_FOLDER
    bundle_path = data_dir / "models" / BUNDLE_FOLDER / BUNDLE_FILE
    manifest = _read_json(folder / "manifest.json", "the saved models' manifest",
                          "train them with `agent train`")  # fmt: skip
    bundle = _read_json(bundle_path, "the live model bundle",
                        "make it with `agent export-live-models`")  # fmt: skip
    try:
        return _build(folder, manifest, bundle, _sha256(bundle_path)[:12], min_hour_covered,
                      lightgbm, np)  # fmt: skip
    except (KeyError, TypeError, ValueError) as exc:
        raise ScoringOff(f"the live model bundle is not as expected ({exc!r}): make it "
                         "again with `agent export-live-models`") from None  # fmt: skip


def _build(folder: Path, manifest: dict, bundle: dict, bundle_sha: str,
           min_hour_covered: float, lightgbm, np) -> LiveScorer:  # fmt: skip
    listed = manifest["files"]
    source = bundle["source"]
    if source["manifest_sha256"] != _sha256(folder / "manifest.json"):
        raise ScoringOff("the live model bundle was made from other models than the ones "
                         "saved now: make it again with `agent export-live-models`")  # fmt: skip
    features = list(bundle["features"])
    if features != list(manifest["features"]):
        raise ScoringOff("the bundle's signals are not the ones the models were trained on")
    stages = []
    for name, saved in bundle["stages"].items():
        for file in (f"{name}/{MODEL_KIND}.txt", f"{name}/models.joblib"):
            path = folder / file
            if not path.is_file():
                raise ScoringOff(f"model file missing: {path}")
            if _sha256(path) != listed.get(file):
                raise ScoringOff(f"model file {file} is not the one the R5 manifest lists")
            if file.endswith(".joblib") and source["files"].get(file) != listed.get(file):
                raise ScoringOff(f"the bundle's calibration was not made from {file}")
        booster = lightgbm.Booster(model_file=str(folder / name / f"{MODEL_KIND}.txt"))
        if booster.feature_name() != features:
            raise ScoringOff(f"the {name} model reads other signals than the manifest lists")
        cal = saved["calibration"]
        cal_x, cal_y = np.array(cal["x"], dtype=np.float64), np.array(cal["y"], dtype=np.float64)
        if len(cal_x) == 0 or len(cal_x) != len(cal_y) or (np.diff(cal_x) <= 0).any():
            raise ScoringOff(f"the {name} calibration map is not valid")
        stages.append(_Stage(name, saved["title"], bool(saved["on_curve_only"]), booster,
                             cal_x, cal_y))  # fmt: skip
    if not stages:
        raise ScoringOff("the live model bundle holds no model")
    days = manifest["days"]
    info = {
        "version": manifest["version"], "bundle_sha": bundle_sha,
        "trained_days": days["train"], "validation_days": days["validation"],
        "test_days": days["test"],
        "stages": [{"name": s.name, "title": s.title} for s in stages],
    }  # fmt: skip
    return LiveScorer(stages, features, info, _usual_rates(folder, [s.name for s in stages]),
                      min_hour_covered, np)  # fmt: skip


def _usual_rates(folder: Path, stages: list[str]) -> dict:
    """stage -> decision time -> group -> the outcome's rate on the test days, from the
    saved test report. Empty when there is no report (rates are then shown as unknown)."""
    try:
        report = json.loads((folder / "test_report.json").read_text(encoding="utf-8"))
        out: dict = {}
        for name in stages:
            for cell in report["stages"][name]["cells"]:
                if cell["model"] == MODEL_KIND and cell["slice"] in ("all", "ordinary", "mayhem"):
                    by_time = out.setdefault(name, {}).setdefault(str(cell["offset_s"]), {})
                    by_time[cell["slice"]] = cell["rate"]
        return out
    except (OSError, ValueError, KeyError, TypeError):
        return {}
