"""A second held-out period (spec 15.4, note of 2026-11-21; `config/holdout.yaml`): the
frozen R5 models and the frozen R6 backtest on days that nothing has seen.

Nothing here trains, tunes or chooses anything. The steps of `agent holdout`:

1. `build`: the period's own data folder, `<data>/holdout/<name>/`. It reads the same
   backfill through folder links (no copy) and gets its own labels and dataset from the
   unchanged builders, so the dataset the models were trained on is never rebuilt and the
   first split (training, validation, test) stays where it is. Every row of a coin the
   first dataset knew completely must be the same in the new one.
2. `check` (before or after `build`): the validation day of the first split is scored
   and simulated again with the saved models, the frozen cuts and today's code, and must
   give the saved validation reports' numbers and trades. That shows the code and files
   are the ones the results were made with. No day of the period is read.
3. `score`: the R5 metrics on the period's days, once.
4. `backtest`: the R6 simulation on the period's days, once.

`score` and `backtest` refuse a second run, and both refuse when anything that decides a
result (this period's settings, the models, the cuts, the trade settings, the code)
differs from what `check` passed with, or when the period's dataset is not the file
`build` made. `build` and `check` refuse once a report exists.

This package is apart from `agent.models` and `agent.backtest` on purpose: the hashes of
those files were frozen with the models and the cuts, and stay as they are.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from agent.backtest import run as r6
from agent.dataset.build import FILE_NAME, build_dataset
from agent.dataset.build import OUT_FOLDER as DATASET_FOLDER
from agent.ingest.backfill_coverage import backfill_fingerprint, backfill_spans, covered
from agent.labels.make_labels import load_label_settings
from agent.models.evaluate import DAY_S, EvaluationPlan, split_days, whole_days
from agent.models.frame import dataset_split, load_frame, role_ranges
from agent.models.train import OUT_FOLDER as MODELS_FOLDER
from agent.models.train import load_models, score_frame
from agent.sim.backtest import load_presets
from agent.sim.pool_math import verify_pool_trades

OUT_FOLDER = "holdout"
LINKED = ("raw_backfill", "backfill")  # read through folder links: never copied or changed
SCORE_REPORT, BACKTEST_REPORT = "score_report.json", "backtest_report.json"
CHECK_FILE, BUILD_FILE = "check.json", "build.json"
TOLERANCE = 1e-9  # numbers built again must equal the saved ones this closely


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Period(_Strict):
    title: str
    days: list[str]  # whole UTC days, e.g. "2026-11-20"
    models: str
    backtest: str

    @model_validator(mode="after")
    def _consecutive(self) -> Period:
        starts = self.day_starts
        if not starts or any(b - a != DAY_S for a, b in zip(starts, starts[1:], strict=False)):
            raise ValueError("days must be whole UTC days, one after another")
        return self

    @property
    def day_starts(self) -> list[int]:
        return [int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())
                for d in self.days]  # fmt: skip

    @property
    def start(self) -> int:
        return self.day_starts[0]

    @property
    def end(self) -> int:
        """The first second after the period."""
        return self.day_starts[-1] + DAY_S


class Holds(_Strict):
    """What counts as "the results held" (see `config/holdout.yaml`)."""

    model: str
    stages: list[str]
    beats: list[str]
    interval_above: list[str]
    calibration_ratio: tuple[float, float]


class HoldoutPlan(_Strict):
    periods: dict[str, Period]
    holds: Holds


class NotAllowed(RuntimeError):
    """A run the plan forbids: a second time, or after a change."""


def content_sha(data: bytes) -> str:
    """A short hash of a text file's content, whatever its line endings (a fresh checkout
    on Windows writes CRLF where the file was first saved with LF)."""
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()[:12]


def same_content(path: Path, recorded: str) -> bool:
    """True if `recorded` is the short hash of the file as it is, or of the same text
    with the other line endings."""
    raw = path.read_bytes()
    unix = raw.replace(b"\r\n", b"\n")
    return recorded in {hashlib.sha256(b).hexdigest()[:12]
                        for b in (raw, unix, unix.replace(b"\n", b"\r\n"))}  # fmt: skip


def load_holdout(path: Path) -> tuple[HoldoutPlan, str]:
    """The periods and a short hash of the file's content."""
    raw = path.read_bytes()
    return HoldoutPlan.model_validate(yaml.safe_load(raw) or {}), content_sha(raw)


def get_period(holdout: HoldoutPlan, name: str) -> Period:
    if name not in holdout.periods:
        raise ValueError(f"unknown period {name!r}: config/holdout.yaml has "
                         f"{', '.join(holdout.periods)}")  # fmt: skip
    return holdout.periods[name]


def view_dir(data_dir: Path, name: str) -> Path:
    return data_dir / OUT_FOLDER / name


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def _plain(value):
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(f"not JSON: {type(value)}")


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=_plain), encoding="utf-8")
    tmp.replace(path)


def _as_saved(data):
    """`data` as it reads back from a JSON file."""
    return json.loads(json.dumps(data, default=_plain))


def _dates(days: list[int]) -> list[str]:
    return [time.strftime("%Y-%m-%d", time.gmtime(d)) for d in days]


# --- the period's data folder ------------------------------------------------------------


def _link(target: Path, link: Path) -> None:
    """A folder link: `link` shows what `target` holds, without a copy."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))  # needs no admin rights
    else:
        os.symlink(target, link, target_is_directory=True)


def make_view(data_dir: Path, name: str) -> Path:
    """The period's data folder, with links to the backfill. Its labels, dataset, ledger
    and reports are its own files."""
    view = view_dir(data_dir, name)
    view.mkdir(parents=True, exist_ok=True)
    for folder in LINKED:
        target, link = data_dir / folder, view / folder
        if not target.is_dir():
            raise FileNotFoundError(f"{target} is missing: there is no backfill to read.")
        if link.exists():
            if link.resolve() != target.resolve():
                raise NotAllowed(f"{link} exists and is not a link to {target}.")
            continue
        _link(target, link)
    return view


def _scored(view: Path) -> list[str]:
    return [f for f in (SCORE_REPORT, BACKTEST_REPORT) if (view / f).exists()]


def _first_test_days(data_dir: Path, period: Period) -> list[str]:
    manifest = json.loads((data_dir / MODELS_FOLDER / period.models / "manifest.json")
                          .read_text(encoding="utf-8"))  # fmt: skip
    return manifest["days"]["test"]


def compare_with_first(data_dir: Path, view: Path, known_until: int) -> dict:
    """Rows of coins launched up to `known_until` in the first dataset and in the
    period's: how many, and how many differ in any column. They must be the same rows:
    the same code on the same events, with later days changing nothing before them."""
    import duckdb

    old = (data_dir / DATASET_FOLDER / FILE_NAME).as_posix()
    new = (view / DATASET_FOLDER / FILE_NAME).as_posix()
    with duckdb.connect() as db:
        db.execute(f"SET temp_directory = '{(view / 'duckdb_tmp').as_posix()}'")
        names = [[d[0] for d in db.execute(f"SELECT * FROM read_parquet('{p}') LIMIT 0")
                  .description] for p in (old, new)]  # fmt: skip
        if names[0] != names[1]:
            return {"known_until": known_until, "same_columns": False, "rows": 0,
                    "rows_new": 0, "only_in_first": None, "only_in_new": None}  # fmt: skip
        sides = [f"SELECT * FROM read_parquet('{p}') WHERE created_ts <= {int(known_until)}"
                 for p in (old, new)]  # fmt: skip

        def count(sql: str) -> int:
            return db.execute(f"SELECT count(*) FROM ({sql})").fetchall()[0][0]

        rows, rows_new = count(sides[0]), count(sides[1])
        only_old = count(f"{sides[0]} EXCEPT ALL {sides[1]}")
        only_new = count(f"{sides[1]} EXCEPT ALL {sides[0]}")
    return {"known_until": known_until, "same_columns": True, "rows": rows,
            "rows_new": rows_new, "only_in_first": only_old, "only_in_new": only_new}  # fmt: skip


def build(root: Path, data_dir: Path, holdout: HoldoutPlan, name: str, *,
          progress=lambda _msg: None) -> dict:  # fmt: skip
    """Make the period's data folder and build its labels and dataset with the unchanged
    builders. Refused once the period was scored or simulated, when its days are not all
    after the first test days, and when the backfill does not cover them yet."""
    period = get_period(holdout, name)
    view = view_dir(data_dir, name)
    if _scored(view):
        raise NotAllowed(f"Period {name} was already scored or simulated "
                         f"({', '.join(_scored(view))}): its dataset is not built "
                         "again.")  # fmt: skip
    first_test = _first_test_days(data_dir, period)
    if period.days[0] <= first_test[-1]:
        raise NotAllowed(f"Period {name} starts on {period.days[0]}, not after the first "
                         f"test days (up to {first_test[-1]}).")  # fmt: skip
    first = json.loads((data_dir / DATASET_FOLDER / "manifest.json").read_text(encoding="utf-8"))
    first_span = first["backfill_spans"][0]
    settings, _ = load_label_settings(root / "config" / "labels.yaml")
    spans = backfill_spans(data_dir)
    # One unbroken stretch from where the first dataset starts to the end of the last
    # label of the period's last coin.
    if not covered(spans, first_span[0], period.end + settings.horizon_s):
        have = "; ".join(f"{_stamp(a)} to {_stamp(b)}" for a, b in spans) or "nothing"
        raise NotAllowed(
            f"The backfill does not cover period {name} yet. Needed: one unbroken stretch "
            f"up to {_stamp(period.end + settings.horizon_s)} UTC (the labels of the last "
            f"coins look {settings.horizon_s} s ahead). Covered (UTC): {have}.")
    if period.start not in whole_days(spans) or period.end - DAY_S not in whole_days(spans):
        raise NotAllowed(f"The days of period {name} are not whole days of the backfill.")
    make_view(data_dir, name)
    if backfill_fingerprint(view) != backfill_fingerprint(data_dir):
        raise NotAllowed("The period's folder does not show the same backfill files.")
    run = build_dataset(root, view, progress=progress)
    known_until = first_span[1] - settings.horizon_s
    same = compare_with_first(data_dir, view, known_until)
    made = {
        "period": name, "days": period.days, "made_at": time.time(),
        "dataset_sha256": run.manifest["file_sha256"],
        "first_dataset_sha256": first["file_sha256"],
        "rows": run.manifest["rows"], "coins": run.manifest["coins"],
        "usable_rows": run.manifest["usable_rows"], "epochs": run.manifest["epochs"],
        "backfill_spans": run.manifest["backfill_spans"],
        "code": run.manifest["code"], "labels_rebuilt": run.labels_rebuilt,
        "same_as_first": same,
        "same": bool(same["same_columns"] and same["rows"] == same["rows_new"]
                     and same["only_in_first"] == 0 and same["only_in_new"] == 0),
    }  # fmt: skip
    _write_json(view / BUILD_FILE, made)
    return made


def _stamp(ts: int) -> str:
    return time.strftime("%b %d %H:%M", time.gmtime(ts))


def period_frame(view: Path, plan: EvaluationPlan, period: Period, *,
                 backfill_only_signals: bool) -> pd.DataFrame:  # fmt: skip
    """The period's rows, built by R5's own loader (`models.frame.load_frame`): the same
    rows kept, the same features and targets. The loader takes its days from a split of
    the dataset's whole days, so it is given the plan with one change: the last set is
    "the days from the period's first day on". Rows after the period are dropped unread
    by any model."""
    manifest = json.loads((view / DATASET_FOLDER / "manifest.json").read_text(encoding="utf-8"))
    spans = [tuple(s) for s in manifest["backfill_spans"]]
    days = whole_days(spans)
    wanted = period.day_starts
    last = [d for d in days if d >= wanted[0]]
    if last[: len(wanted)] != wanted:
        raise NotAllowed(f"The period's dataset does not hold {', '.join(period.days)} as "
                         "whole days.")  # fmt: skip
    loader_plan = plan.model_copy(update={"split": plan.split.model_copy(
        update={"test_share": 0.0, "min_test_days": len(last)})})  # fmt: skip
    split = split_days(spans, loader_plan.split)
    if split.test != last:
        raise NotAllowed("The period's dataset has too few whole days before the period.")
    frame = load_frame(view, loader_plan, ("test",), backfill_only_signals=backfill_only_signals)
    frame = frame[(frame["created_ts"] >= period.start)
                  & (frame["created_ts"] < period.end)].reset_index(drop=True)  # fmt: skip
    if set(frame["date"]) - set(period.days):
        raise ValueError("rows outside the period's days were loaded")
    return frame


# --- what is frozen ----------------------------------------------------------------------


def frozen_state(root: Path, data_dir: Path, holdout_sha: str, period: Period) -> dict:
    """Short hashes of everything that decides a result. Raises `NotAllowed`, naming what
    differs, unless the models, the plan, the cuts and the settings are the ones the first
    test days were scored and simulated with."""
    problems = []
    models = data_dir / MODELS_FOLDER / period.models
    manifest = json.loads((models / "manifest.json").read_text(encoding="utf-8"))
    changed = [f for f, sha in manifest["files"].items()
               if hashlib.sha256((models / f).read_bytes()).hexdigest() != sha]  # fmt: skip
    if changed:
        problems.append(f"model files differ from their manifest: {', '.join(changed)}")
    if not same_content(root / "config" / "evaluation.yaml", manifest["plan_sha"]):
        problems.append("config/evaluation.yaml is not the plan the models were trained under")
    first = json.loads((data_dir / DATASET_FOLDER / "manifest.json").read_text(encoding="utf-8"))
    if first["file_sha256"] != manifest["dataset_sha256"]:
        problems.append("the first dataset is not the one the models were trained on")

    folder = data_dir / r6.OUT_FOLDER / period.backtest
    frozen, cuts = _read_json(folder / "frozen.json"), _read_json(folder / "cuts.json")
    test = _read_json(folder / "test_report.json")
    if frozen is None or cuts is None or test is None:
        raise NotAllowed(f"{folder} has no frozen cuts or no test report to compare with.")
    presets, _ = load_presets(root / "config" / "padre_presets.yaml")
    settings, _ = r6.load_settings(root / "config" / "backtest.yaml")
    if settings.models != period.models:
        problems.append("config/backtest.yaml names other models than the period")
    cuts_sha = hashlib.sha256(json.dumps(cuts, sort_keys=True).encode()).hexdigest()[:12]
    models_sha = hashlib.sha256(
        json.dumps(manifest["files"], sort_keys=True).encode()).hexdigest()[:12]
    if cuts_sha != frozen["cuts_sha"]:
        problems.append("cuts.json differs from the cuts that were frozen")
    if models_sha != frozen["models_sha"]:
        problems.append("the models are not the ones the cuts were chosen with")
    if not same_content(root / "config" / "backtest.yaml", frozen["settings_sha"]):
        problems.append("config/backtest.yaml changed since the cuts were frozen")
    # Padre's fee was changed after the cuts were frozen (spec 10: it changes no simulated
    # trade), so the settings to match are the ones the test report was last built with.
    if json.loads(presets.model_dump_json()) != test["presets"]:
        problems.append("config/padre_presets.yaml differs from the test report's settings")
    if json.loads(settings.model_dump_json()) != test["settings"]:
        problems.append("config/backtest.yaml differs from the test report's settings")
    source_sha = r6.code_version(root)["source_sha"]
    known = {frozen["source_sha"],
             (test.get("summarized_again") or {}).get("code", {}).get("source_sha")}  # fmt: skip
    if source_sha not in known:
        problems.append("the models' or the backtest's code changed since the test report "
                        f"was built (now {source_sha})")  # fmt: skip
    if problems:
        raise NotAllowed("Not what the first test days were run with: " + "; ".join(problems)
                         + ".")  # fmt: skip
    return {"holdout_sha": holdout_sha, "plan_sha": manifest["plan_sha"],
            "models_sha": models_sha, "cuts_sha": cuts_sha,
            "settings_sha": frozen["settings_sha"],
            "presets_sha": content_sha((root / "config" / "padre_presets.yaml").read_bytes()),
            "source_sha": source_sha,
            "source_is_the_simulating_code": source_sha == frozen["source_sha"]}  # fmt: skip


def difference(a, b) -> float:
    """The largest difference between two saved reports' numbers; infinite when they
    differ in anything else (keys, lengths, texts, a missing value)."""
    if isinstance(a, dict) and isinstance(b, dict):
        if a.keys() != b.keys():
            return float("inf")
        return max((difference(a[k], b[k]) for k in a), default=0.0)
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return float("inf")
        return max((difference(x, y) for x, y in zip(a, b, strict=True)), default=0.0)
    if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None:
        return 0.0 if a is b else float("inf")
    if isinstance(a, int | float) and isinstance(b, int | float):
        return abs(a - b)
    return 0.0 if a == b else float("inf")


def check(root: Path, data_dir: Path, plan: EvaluationPlan, holdout: HoldoutPlan,
          holdout_sha: str, name: str, *, progress=lambda _msg: None) -> dict:  # fmt: skip
    """Score and simulate the validation day of the first split again and compare with the
    saved validation reports. No test day and no day of the period is read, and no cut is
    chosen: the cuts worked out again must be the frozen ones."""
    period = get_period(holdout, name)
    view = view_dir(data_dir, name)
    if _scored(view):
        raise NotAllowed(f"Period {name} was already scored or simulated: the check is "
                         "not run again.")  # fmt: skip
    started = time.time()
    state = frozen_state(root, data_dir, holdout_sha, period)
    view.mkdir(parents=True, exist_ok=True)
    models = data_dir / MODELS_FOLDER / period.models
    folder = data_dir / r6.OUT_FOLDER / period.backtest
    fitted = load_models(models)
    presets, _ = load_presets(root / "config" / "padre_presets.yaml")
    settings, _ = r6.load_settings(root / "config" / "backtest.yaml")

    progress("Scoring the validation day again (R5)")
    frame = load_frame(data_dir, plan, ("validation",))
    saved = json.loads((models / "validation_report.json").read_text(encoding="utf-8"))
    scores_diff = difference(_as_saved(score_frame(frame, fitted, plan)), saved["stages"])

    progress("Simulating the validation day again (R6)")
    saved = json.loads((folder / "validation_report.json").read_text(encoding="utf-8"))
    frame = load_frame(data_dir, plan, ("validation",), backfill_only_signals=False)
    split, _ = dataset_split(data_dir, plan)
    start, end = role_ranges(split, plan.split.drop_label_overlap_s)["validation"]
    scores = r6.raw_scores(frame, fitted, settings)
    cuts = _read_json(folder / "cuts.json")
    cuts_diff = difference(
        _as_saved(r6.choose_cuts(frame, scores, settings, (end - start) / 3600)), cuts)
    scratch = view / "check_ledger"
    made = r6.build_ledger(data_dir, frame, presets, settings, scratch, progress=progress)
    ledger_same = made["files"] == saved["ledger"]["files"]
    kinds = r6.variants(presets, settings)
    aligned = r6.align(frame, scratch, presets, kinds, made["base_fee_lamports"])
    masks = r6.signal_masks(frame, scores, cuts, settings)
    cells_diff = difference(_as_saved(r6.summarize(frame, masks, aligned, kinds, plan)),
                            saved["cells"])  # fmt: skip
    shutil.rmtree(scratch)  # the scratch copy of the validation day's trades

    result = {
        "period": name, "checked_at": time.time(), "seconds": round(time.time() - started),
        "validation_days": _dates(split.validation), "state": state,
        "r5_validation_scores_largest_difference": scores_diff,
        "r6_cuts_largest_difference": cuts_diff,
        "r6_validation_ledger_files": len(made["files"]),
        "r6_validation_ledger_same_files": ledger_same,
        "r6_validation_cells_largest_difference": cells_diff,
        "passed": bool(scores_diff <= TOLERANCE and cuts_diff == 0.0 and ledger_same
                       and cells_diff <= TOLERANCE),
    }  # fmt: skip
    _write_json(view / CHECK_FILE, result)
    return result


def _ready(root: Path, data_dir: Path, holdout_sha: str, name: str, period: Period) -> dict:
    """The passed check of the period, after making sure that nothing changed since and
    that the period's dataset is the one `build` made and compared with the first."""
    view = view_dir(data_dir, name)
    built = _read_json(view / BUILD_FILE)
    if built is None or not built["same"]:
        raise NotAllowed(f"Run `agent holdout build` first: period {name} has no dataset "
                         "that agrees with the first one.")  # fmt: skip
    path = view / DATASET_FOLDER / FILE_NAME
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != built[
            "dataset_sha256"]:  # fmt: skip
        raise NotAllowed("The period's dataset is not the file `agent holdout build` made.")
    checked = _read_json(view / CHECK_FILE)
    if checked is None or not checked["passed"]:
        raise NotAllowed(f"Run `agent holdout check` first: period {name} has no passed "
                         "check.")  # fmt: skip
    state = frozen_state(root, data_dir, holdout_sha, period)
    changed = [k for k, v in state.items() if checked["state"].get(k) != v]
    if changed:
        raise NotAllowed(f"Changed since the check passed: {', '.join(changed)}. Nothing "
                         "may change between the check and the run.")  # fmt: skip
    return checked


# --- the two runs, once each -------------------------------------------------------------


def score(root: Path, data_dir: Path, plan: EvaluationPlan, holdout: HoldoutPlan,
          holdout_sha: str, name: str, *, progress=lambda _msg: None) -> dict:  # fmt: skip
    """The R5 metrics on the period's days with the saved models: once."""
    period = get_period(holdout, name)
    view = view_dir(data_dir, name)
    out = view / SCORE_REPORT
    if out.exists():
        raise NotAllowed(f"Period {name} was already scored: {out}. It is scored once.")
    checked = _ready(root, data_dir, holdout_sha, name, period)
    started = time.time()
    fitted = load_models(data_dir / MODELS_FOLDER / period.models)
    frame = period_frame(view, plan, period, backfill_only_signals=True)
    progress(f"{name}: {len(frame):,} rows ({frame['mint'].nunique():,} coins), launched "
             f"{frame['date'].min()} to {frame['date'].max()} UTC")  # fmt: skip
    report = {
        "set": name, "title": period.title, "days": period.days, "scored_at": time.time(),
        "rows": len(frame), "coins": int(frame["mint"].nunique()),
        "coins_by_group": {g: int(n) for g, n in
                           frame.drop_duplicates("mint")["group"].value_counts().items()},
        "state": checked["state"], "code": r6.code_version(root),
        "stages": score_frame(frame, fitted, plan),
    }  # fmt: skip
    report["seconds"] = round(time.time() - started)
    _write_json(out, report)
    return _as_saved(report)


def backtest(root: Path, data_dir: Path, plan: EvaluationPlan, holdout: HoldoutPlan,
             holdout_sha: str, name: str, *, progress=lambda _msg: None) -> dict:  # fmt: skip
    """The R6 simulation on the period's days with the frozen cuts and settings: once.
    The report has the shape of R6's own, so the same tables can be written from it."""
    period = get_period(holdout, name)
    view = view_dir(data_dir, name)
    out = view / BACKTEST_REPORT
    if out.exists():
        raise NotAllowed(f"Period {name} was already simulated: {out}. It is simulated "
                         "once.")  # fmt: skip
    checked = _ready(root, data_dir, holdout_sha, name, period)
    started = time.time()
    folder = data_dir / r6.OUT_FOLDER / period.backtest
    presets, _ = load_presets(root / "config" / "padre_presets.yaml")
    settings, _ = r6.load_settings(root / "config" / "backtest.yaml")
    fitted = load_models(data_dir / MODELS_FOLDER / period.models)
    cuts = _read_json(folder / "cuts.json")
    frame = period_frame(view, plan, period, backfill_only_signals=False)
    progress(f"{name}: {len(frame):,} rows ({frame['mint'].nunique():,} coins), launched "
             f"{frame['date'].min()} to {frame['date'].max()} UTC")  # fmt: skip
    scores = r6.raw_scores(frame, fitted, settings)
    masks = r6.signal_masks(frame, scores, cuts, settings)
    ledger = view / "ledger"
    made = r6.build_ledger(view, frame, presets, settings, ledger, progress=progress)
    progress(f"Ledger: {made['rows']:,} rows in {time.time() - started:.0f} s; summarizing")
    kinds = r6.variants(presets, settings)
    aligned = r6.align(frame, ledger, presets, kinds, made["base_fee_lamports"])
    cells = r6.summarize(frame, masks, aligned, kinds, plan)
    horizon = int(frame["offset_s"].max()) + presets.horizon_s
    report = {
        "set": name, "title": period.title, "made_at": time.time(),
        "rows": len(frame), "coins": int(frame["mint"].nunique()),
        "dates": sorted(frame["date"].unique()), "launch_range": [period.start, period.end],
        "coins_by_group": {g: int(n) for g, n in
                           frame.drop_duplicates("mint")["group"].value_counts().items()},
        "frozen": _read_json(folder / "frozen.json"), "state": checked["state"],
        "code": r6.code_version(root),
        "presets": json.loads(presets.model_dump_json()),
        "settings": json.loads(settings.model_dump_json()),
        "variants": kinds, "base_fee_lamports": made["base_fee_lamports"],
        "signals": {key: {"label": label, "rows": int(mask.sum())}
                    for key, (label, mask) in masks.items()},
        "ledger": {"rows": made["rows"], "files": made["files"]},
        "pool_check": verify_pool_trades(view, period.start, period.end + horizon),
        "cells": cells,
    }  # fmt: skip
    report["seconds"] = round(time.time() - started)
    _write_json(out, report)
    return _as_saved(report)
