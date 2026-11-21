"""Running the backtest (spec 10; research track step R6): signals, the ledger of
simulated trades, and after-cost profit and loss per signal.

`agent backtest validation | train | test` simulates one set of days:

1. **Signals.** Each row of the R5 frame (a coin at a decision time) is scored with the
   saved R5 models, and a signal says buy or not (`config/backtest.yaml`). Model signals
   buy at or above a cut; the cuts are chosen on the validation day only, from how many
   picks per hour they gave there, never from profit or loss.
2. **Ledger.** Every coin of the set is simulated at every decision time, delay and exit
   plan (`sim/backtest.py`), whatever the signals say; "buy every coin" is one of them.
3. **Report.** Mean profit or loss per signal in SOL (a 1 SOL buy, so also a share of the
   stake), for each decision time, delay and exit plan; for ordinary and mayhem coins;
   for each day; with 95% intervals that resample whole launch hours (spec 15.4).

Keeping the test days honest:
- `validation` chooses the cuts and records what they were chosen under (`frozen.json`:
  the hashes of the two settings files, of the cuts, of the code and of the models).
- `test` runs once. It refuses when a test report exists, when anything in `frozen.json`
  has changed since the cuts were chosen, or when the model files differ from R5's
  manifest.
- `train` reports only the signals that were never fitted (every coin, the rules): the
  models learned from those days.
- `resummarize` builds a set's report again from its saved ledger, without simulating
  anything, after a change to Padre's fee: the one setting that changes no simulated
  trade, only what is taken off its result. It refuses when the ledger's files, the cuts,
  the signals, the models or any other trade setting differ from the report's, and keeps
  the report it replaces.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import yaml
from pydantic import BaseModel, ConfigDict

import agent
from agent.dataset.build import _git
from agent.ingest.recorder import write_parquet_atomic
from agent.models.evaluate import EvaluationPlan, ratio_interval
from agent.models.frame import STAGES, dataset_split, load_frame, matrix, role_ranges
from agent.models.train import OUT_FOLDER as MODELS_FOLDER
from agent.models.train import StageModels, load_models
from agent.sim.backtest import (
    ATTEMPT_FIELDS,
    FILLED,
    SLIPPAGE,
    STOP_LOSS,
    TAKE_PROFIT,
    TIME_STOP,
    Presets,
    load_presets,
    outcomes,
    simulate_signal,
)
from agent.sim.market import load_markets
from agent.sim.pool_math import verify_pool_trades

OUT_FOLDER = "backtest"
VERSION = "r6"
SETS = ("validation", "train", "test")
ALL_TIMES, FIRST_TIME = "all", "first"  # every decision time; a coin's first signal only
# Settings that change no simulated trade: only what is taken off a trade's result.
REPORTING_ONLY = ("platform_fee_pct", "platform_fee_tests_pct")
# The code that decides the results; its hash is recorded and frozen with the cuts.
SOURCES = ("sim/backtest.py", "backtest/run.py", "sim/market.py", "sim/pool_math.py",
           "sim/curve_math.py", "models")  # fmt: skip


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SignalSpec(_Strict):
    name: str
    label: str
    kind: Literal["all", "rules", "pick", "filter"]
    verdicts: list[str] = []
    stage: str | None = None
    model: str | None = None
    max_fine_blocked: float | None = None


class BacktestSettings(_Strict):
    models: str
    signals: list[SignalSpec]
    picks_per_hour: list[int]
    headline_footprint: Literal["none", "stays"]
    footprints: list[Literal["none", "stays"]]
    coins_per_batch_hours: int = 3


class NotAllowed(RuntimeError):
    """A run the plan forbids: the test days a second time, or after a change."""


def load_settings(path: Path) -> tuple[BacktestSettings, str]:
    raw = path.read_bytes()
    settings = BacktestSettings.model_validate(yaml.safe_load(raw) or {})
    stages = {s.name for s in STAGES}
    for spec in settings.signals:
        if spec.kind in ("pick", "filter") and (spec.stage not in stages or not spec.model):
            raise ValueError(f"backtest.yaml: signal {spec.name!r} needs a stage and a model")
        if spec.kind == "filter" and spec.max_fine_blocked is None:
            raise ValueError(f"backtest.yaml: signal {spec.name!r} needs max_fine_blocked")
    return settings, hashlib.sha256(raw).hexdigest()[:12]


def out_dir(data_dir: Path) -> Path:
    return data_dir / OUT_FOLDER / VERSION


def code_version(root: Path) -> dict:
    """The git commit, whether the code differs from it, and a hash of the simulator's and
    the models' source files."""
    package = Path(agent.__file__).parent
    digest = hashlib.sha256()
    for source in SOURCES:
        target = package / source
        for file in sorted(target.rglob("*.py")) if target.is_dir() else [target]:
            digest.update(file.relative_to(package).as_posix().encode())
            digest.update(file.read_bytes().replace(b"\r\n", b"\n"))
    status = _git(root, "status", "--porcelain", "--", "src", "config")
    return {"git_commit": _git(root, "rev-parse", "HEAD"),
            "uncommitted_changes": None if status is None else bool(status),
            "source_sha": digest.hexdigest()[:12]}  # fmt: skip


# --- signals -----------------------------------------------------------------------------


def raw_scores(frame: pd.DataFrame, fitted: dict[str, StageModels],
               settings: BacktestSettings) -> dict[str, np.ndarray]:  # fmt: skip
    """Each model signal's raw score for every row (the score R5 ranked by)."""
    out = {}
    for spec in settings.signals:
        if spec.kind not in ("pick", "filter"):
            continue
        models = fitted[spec.stage]
        kind = models.logistic if spec.model.startswith("logistic") else models.lightgbm
        chosen = kind[spec.model]
        x = matrix(frame, chosen["features"])
        if spec.model.startswith("logistic"):
            out[spec.name] = np.asarray(chosen["model"].decision_function(x), dtype=float)
        else:
            out[spec.name] = np.asarray(chosen["model"].predict(x, raw_score=True), dtype=float)
    return out


def _eligible(frame: pd.DataFrame, spec: SignalSpec) -> np.ndarray:
    """Rows a pick signal may choose from. Graduation is predicted for coins still on the
    curve (spec 7): `completed` is a signal known at the decision time."""
    if spec.stage == "grad_later":
        return (frame["completed"].to_numpy(dtype=float) == 0)
    return np.ones(len(frame), dtype=bool)


def choose_cuts(validation: pd.DataFrame, scores: dict[str, np.ndarray],
                settings: BacktestSettings, hours: float) -> dict:  # fmt: skip
    """The score cuts of every model signal, per decision time, from the validation day.

    pick: the cut is the k-th highest score there, k = picks per hour x the day's hours.
    filter: the coin is bought when its "bad" score is at or below the cut; scores above
    it are blocked, and the cut is the lowest that blocks at most the allowed share of the
    validation day's fine coins. Profit and loss are not read."""
    offsets = validation["offset_s"].to_numpy()
    cuts: dict = {}
    for spec in settings.signals:
        if spec.kind == "pick":
            eligible = _eligible(validation, spec)
            for per_hour in settings.picks_per_hour:
                k = round(per_hour * hours)
                entry = cuts.setdefault(f"{spec.name}_{per_hour}ph", {})
                for offset in sorted(np.unique(offsets)):
                    values = np.sort(scores[spec.name][eligible & (offsets == offset)])[::-1]
                    if not len(values):
                        raise ValueError(f"no validation rows for {spec.name} at T+{offset} s")
                    cut = float(values[min(k, len(values)) - 1])
                    entry[str(int(offset))] = {"cut": cut, "wanted": k,
                                               "picked": int((values >= cut).sum())}  # fmt: skip
        elif spec.kind == "filter":
            fine = validation["y_bad"].to_numpy(dtype=float) == 0
            entry = cuts.setdefault(spec.name, {})
            for offset in sorted(np.unique(offsets)):
                at = offsets == offset
                values = np.sort(scores[spec.name][at & fine])[::-1]
                allowed = int(spec.max_fine_blocked * len(values))
                cut = float(values[min(allowed, len(values) - 1)])
                entry[str(int(offset))] = {
                    "cut": cut, "fine_blocked": float((values > cut).mean()),
                    "bought": int((scores[spec.name][at] <= cut).sum())}  # fmt: skip
    return cuts


def signal_masks(frame: pd.DataFrame, scores: dict[str, np.ndarray], cuts: dict,
                 settings: BacktestSettings, *, fitted_signals: bool = True
                 ) -> dict[str, tuple[str, np.ndarray]]:  # fmt: skip
    """{signal: (label, which rows it buys)}, in the order of the settings file. With
    `fitted_signals=False`, only the signals that were never fitted."""
    offsets = frame["offset_s"].to_numpy()
    out: dict[str, tuple[str, np.ndarray]] = {}
    for spec in settings.signals:
        if spec.kind == "all":
            out[spec.name] = (spec.label, np.ones(len(frame), dtype=bool))
        elif spec.kind == "rules":
            out[spec.name] = (spec.label, frame["rules_verdict"].isin(spec.verdicts).to_numpy())
        elif not fitted_signals:
            continue
        elif spec.kind == "filter":
            cut = np.array([cuts[spec.name][str(int(o))]["cut"] for o in offsets])
            out[spec.name] = (spec.label, scores[spec.name] <= cut)
        else:
            eligible = _eligible(frame, spec)
            for per_hour in settings.picks_per_hour:
                key = f"{spec.name}_{per_hour}ph"
                cut = np.array([cuts[key][str(int(o))]["cut"] for o in offsets])
                out[key] = (f"{spec.label}, about {per_hour} per hour",
                            eligible & (scores[spec.name] >= cut))  # fmt: skip
    return out


def first_signal(frame: pd.DataFrame, mask: np.ndarray) -> np.ndarray:
    """The rows of `mask` that are their coin's earliest decision time in it: buying a
    coin once, the first time the signal says so."""
    rows = np.flatnonzero(mask)
    coin = pd.factorize(frame["mint"])[0][rows]
    offsets = frame["offset_s"].to_numpy()[rows]
    order = np.lexsort((offsets, coin))
    first = np.r_[True, coin[order][1:] != coin[order][:-1]] if len(rows) else []
    out = np.zeros(len(frame), dtype=bool)
    out[rows[order][first]] = True
    return out


# --- the ledger --------------------------------------------------------------------------


def ledger_schema(presets: Presets) -> pa.Schema:
    types = {"sell_ts": pa.int64(), "sell_none": pa.int64(), "sell_stays": pa.int64(),
             "slipped": pa.int8(), "chance": pa.float64()}  # fmt: skip
    fields = [
        ("mint", pa.string()), ("offset_s", pa.int32()), ("delay_s", pa.int32()),
        ("plan", pa.string()), ("entry_ts", pa.int64()), ("buy_status", pa.string()),
        ("venue", pa.string()), ("capped", pa.bool_()), ("tokens", pa.int64()),
        ("buy_net", pa.int64()), ("buy_spent", pa.int64()), ("entry_price", pa.float64()),
        ("buy_fee_bps", pa.int32()), ("buy_chance", pa.float64()),
        ("exit_reason", pa.string()), ("trigger_ts", pa.int64()), ("stuck", pa.bool_()),
    ]  # fmt: skip
    for k in range(1, presets.max_sell_attempts + 2):
        fields += [(f"{name}_{k}", types[name]) for name in ATTEMPT_FIELDS]
    return pa.schema(fields)


def build_ledger(data_dir: Path, frame: pd.DataFrame, presets: Presets,
                 settings: BacktestSettings, folder: Path, *,
                 progress=lambda _msg: None) -> dict:  # fmt: skip
    """Simulate every coin of `frame` at every decision time, delay and exit plan, and
    write the rows to `folder`, one file per batch of launch times. Returns the files'
    hashes, the row count and Solana's base fee as read from the data."""
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    schema = ledger_schema(presets)
    offsets = sorted(int(o) for o in frame["offset_s"].unique())
    coins = frame[["mint", "created_ts"]].drop_duplicates().sort_values(["created_ts", "mint"])
    horizon = max(offsets) + presets.horizon_s
    batch_s = settings.coins_per_batch_hours * 3600
    files, rows_total, base_fees = {}, 0, []
    starts = coins["created_ts"].to_numpy() // batch_s * batch_s
    for start in np.unique(starts):
        batch = coins[starts == start]
        loaded = load_markets(data_dir, list(zip(batch["mint"], batch["created_ts"].astype(int),
                                                 strict=True)), horizon)  # fmt: skip
        if loaded.base_fee_lamports is not None:
            base_fees.append(loaded.base_fee_lamports)
        rows = []
        for mint in batch["mint"]:
            market = loaded.markets[mint]
            for offset in offsets:
                for delay in presets.delays:
                    rows += simulate_signal(market, offset, delay, presets, loaded.launch_rates)
        path = folder / f"{int(start)}.parquet"
        write_parquet_atomic(pa.Table.from_pylist(rows, schema=schema), path)
        files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        rows_total += len(rows)
        since = time.strftime("%b %d %H:%M", time.gmtime(start))
        progress(f"  simulated coins launched from {since} UTC: {len(batch):,} coins, "
                 f"{rows_total:,} rows so far")  # fmt: skip
    if not base_fees:
        raise ValueError("no trades read: Solana's base fee is unknown")
    return {"files": files, "rows": rows_total, "base_fee_lamports": min(base_fees)}


# --- the report --------------------------------------------------------------------------


def variants(presets: Presets, settings: BacktestSettings) -> list[dict]:
    """The headline setting first, then each other failed-transaction rate, each other
    view of our own buy, and each what-if for Padre's fee, one change at a time."""
    head = {"failed_rate": presets.failed_tx_rate, "footprint": settings.headline_footprint,
            "platform_fee_pct": presets.platform_fee_pct or 0.0}  # fmt: skip
    out = [head | {"name": "headline"}]
    out += [head | {"failed_rate": r, "name": f"failed_{r:g}"}
            for r in presets.failed_rates if r != head["failed_rate"]]  # fmt: skip
    out += [head | {"footprint": f, "name": f"footprint_{f}"}
            for f in settings.footprints if f != head["footprint"]]  # fmt: skip
    out += [head | {"platform_fee_pct": p, "name": f"platform_fee_{p:g}"}
            for p in presets.platform_fee_tests_pct if p != head["platform_fee_pct"]]  # fmt: skip
    return out


EXIT_CODES = {TAKE_PROFIT: 1, STOP_LOSS: 2, TIME_STOP: 3}


def align(frame: pd.DataFrame, folder: Path, presets: Presets, kinds: list[dict],
          base_fee_lamports: int) -> dict:  # fmt: skip
    """The ledger as arrays in the frame's row order, one per (delay, exit plan):
    `pnl[variant][(delay, plan)]`, and for the headline what happened to each buy."""
    n = len(frame)
    row_of = pd.Series(np.arange(n), index=pd.MultiIndex.from_arrays(
        [frame["mint"], frame["offset_s"].astype(int)]))  # fmt: skip
    keys = [(d, p.name) for d in presets.delays for p in presets.exit_plans]
    pnl = {v["name"]: {k: np.full(n, np.nan) for k in keys} for v in kinds}
    facts = {k: {"bought": np.zeros(n, dtype=bool), "slippage": np.zeros(n, dtype=bool),
                 "capped": np.zeros(n, dtype=bool), "stuck": np.zeros(n, dtype=bool),
                 "exit": np.zeros(n, dtype=np.int8)} for k in keys}  # fmt: skip
    for path in sorted(folder.glob("*.parquet")):
        table = pq.read_table(path).to_pandas()
        rows = row_of.reindex(pd.MultiIndex.from_arrays(
            [table["mint"], table["offset_s"].astype(int)])).to_numpy()  # fmt: skip
        if np.isnan(rows).any():
            raise ValueError(f"{path.name} holds coins that are not in the frame")
        rows = rows.astype(int)
        groups = table.groupby(["delay_s", "plan"], sort=False).indices
        for v in kinds:
            got = outcomes(table, presets, failed_rate=v["failed_rate"],
                           footprint=v["footprint"], base_fee_lamports=base_fee_lamports,
                           platform_fee_pct=v["platform_fee_pct"])  # fmt: skip
            for (delay, plan), at in groups.items():
                pnl[v["name"]][(int(delay), plan)][rows[at]] = got["pnl_sol"][at]
                if v["name"] == "headline":
                    f = facts[(int(delay), plan)]
                    f["bought"][rows[at]] = got["traded"][at]
                    f["slippage"][rows[at]] = (table["buy_status"].to_numpy()[at] == SLIPPAGE)
                    filled = table["buy_status"].to_numpy()[at] == FILLED
                    f["capped"][rows[at]] = table["capped"].to_numpy()[at] & filled
                    f["stuck"][rows[at]] = table["stuck"].to_numpy()[at] & got["traded"][at]
                    f["exit"][rows[at]] = np.where(
                        got["traded"][at],
                        table["exit_reason"].map(EXIT_CODES).fillna(0).to_numpy()[at], 0)
    for name, by_key in pnl.items():
        for key, values in by_key.items():
            if np.isnan(values).any():
                raise ValueError(f"the ledger misses rows for {name} {key}")
    return {"pnl": pnl, "facts": facts, "keys": keys}


def _estimate(values: np.ndarray, hours: np.ndarray, interval: dict) -> dict:
    return ratio_interval(values, np.ones(len(values)), hours, **interval).as_dict()


def summarize(frame: pd.DataFrame, masks: dict[str, tuple[str, np.ndarray]], aligned: dict,
              kinds: list[dict], plan: EvaluationPlan) -> list[dict]:  # fmt: skip
    """One cell per signal, decision time (each, all, and a coin's first signal), delay,
    exit plan, slice (all coins, ordinary, mayhem, each day) and variant. Slices by day
    are for the headline variant only."""
    u = plan.uncertainty
    interval = {"level": u.level, "resamples": u.resamples, "seed": u.seed}
    hours = frame["hour_ts"].to_numpy()
    offsets = frame["offset_s"].to_numpy()
    dates = sorted(frame["date"].unique())
    slices = {"all": np.ones(len(frame), dtype=bool)}
    slices |= {g: (frame["group"] == g).to_numpy() for g in ("ordinary", "mayhem")}
    day_slices = {d: (frame["date"] == d).to_numpy() for d in dates} if len(dates) > 1 else {}
    cells = []
    for signal, (_, mask) in masks.items():
        times = {int(o): mask & (offsets == o) for o in sorted(np.unique(offsets))}
        times |= {ALL_TIMES: mask, FIRST_TIME: first_signal(frame, mask)}
        for when, at_time in times.items():
            for v in kinds:
                head = v["name"] == "headline"
                for name, inside in (slices | day_slices if head else slices).items():
                    pick = at_time & inside
                    n = int(pick.sum())
                    for delay, plan_name in aligned["keys"]:
                        cell = {"signal": signal, "time": when, "delay_s": delay,
                                "plan": plan_name, "slice": name, "variant": v["name"],
                                "n": n}  # fmt: skip
                        if n:
                            values = aligned["pnl"][v["name"]][(delay, plan_name)][pick]
                            cell["mean_pnl_sol"] = _estimate(values, hours[pick], interval)
                            cell["total_pnl_sol"] = float(values.sum())
                            cell["median_pnl_sol"] = float(np.median(values))
                        if n and head:
                            f = aligned["facts"][(delay, plan_name)]
                            bought = f["bought"][pick]
                            values_bought = values[bought]
                            cell |= {
                                "bought": float(bought.mean()),
                                "won": (float((values_bought > 0).mean())
                                        if len(values_bought) else None),
                                "slippage": float(f["slippage"][pick].mean()),
                                "capped": float(f["capped"][pick].mean()),
                                "stuck": float(f["stuck"][pick].mean()),
                                "exits": {reason: float((f["exit"][pick] == code).mean())
                                          for reason, code in EXIT_CODES.items()},
                            }  # fmt: skip
                        cells.append(cell)
    return cells


# --- a run -------------------------------------------------------------------------------


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def _checked_models(data_dir: Path, settings: BacktestSettings) -> tuple[dict, str]:
    """The saved models, after checking their files against R5's manifest."""
    folder = data_dir / MODELS_FOLDER / settings.models
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    changed = [f for f, sha in manifest["files"].items()
               if hashlib.sha256((folder / f).read_bytes()).hexdigest() != sha]  # fmt: skip
    if changed:
        raise ValueError(f"Model files differ from the manifest: {', '.join(changed)}")
    listed = json.dumps(manifest["files"], sort_keys=True).encode()
    return load_models(folder), hashlib.sha256(listed).hexdigest()[:12]


def run(root: Path, data_dir: Path, plan: EvaluationPlan, which: str, *,
        progress=lambda _msg: None) -> dict:  # fmt: skip
    """Simulate one set of days and write `<data>/backtest/r6/<set>_report.json`."""
    if which not in SETS:
        raise ValueError(f"unknown set {which!r}: one of {', '.join(SETS)}")
    started = time.time()
    folder = out_dir(data_dir)
    report_path = folder / f"{which}_report.json"
    if which == "test" and report_path.exists():
        raise NotAllowed(f"The test days were already simulated: {report_path}. They are "
                         "simulated once.")  # fmt: skip
    presets, presets_sha = load_presets(root / "config" / "padre_presets.yaml")
    settings, settings_sha = load_settings(root / "config" / "backtest.yaml")
    fitted, models_sha = _checked_models(data_dir, settings)
    frozen_now = {"presets_sha": presets_sha, "settings_sha": settings_sha,
                  "models_sha": models_sha,
                  "source_sha": code_version(root)["source_sha"]}  # fmt: skip
    frozen = _read_json(folder / "frozen.json")
    cuts = _read_json(folder / "cuts.json")
    if which != "validation":
        if frozen is None or cuts is None:
            raise NotAllowed("Run `agent backtest validation` first: it chooses the cuts.")
        changed = [k for k, v in frozen_now.items() if frozen.get(k) != v]
        if changed:
            raise NotAllowed(
                "Changed since the cuts were chosen on the validation day: "
                f"{', '.join(changed)}. Run `agent backtest validation` again first.")
        if frozen["cuts_sha"] != hashlib.sha256(
                json.dumps(cuts, sort_keys=True).encode()).hexdigest()[:12]:  # fmt: skip
            raise NotAllowed("cuts.json differs from the cuts that were frozen.")
    elif (folder / "test_report.json").exists():
        raise NotAllowed("The test days were already simulated with the frozen cuts; "
                         "choosing cuts again is refused.")  # fmt: skip

    frame = load_frame(data_dir, plan, (which,), backfill_only_signals=False)
    split, _ = dataset_split(data_dir, plan)
    start, end = role_ranges(split, plan.split.drop_label_overlap_s)[which]
    progress(f"{which}: {len(frame):,} rows ({frame['mint'].nunique():,} coins), launched "
             f"{frame['date'].min()} to {frame['date'].max()} UTC")  # fmt: skip
    # The training days are reported for the unfitted signals only: no model scores them.
    scores = {} if which == "train" else raw_scores(frame, fitted, settings)
    if which == "validation":
        cuts = choose_cuts(frame, scores, settings, (end - start) / 3600)
        folder.mkdir(parents=True, exist_ok=True)
        _write_json(folder / "cuts.json", cuts)
        frozen = frozen_now | {"cuts_sha": hashlib.sha256(
            json.dumps(cuts, sort_keys=True).encode()).hexdigest()[:12],
            "frozen_at": time.time(), "validation_hours": (end - start) / 3600}  # fmt: skip
        _write_json(folder / "frozen.json", frozen)
        progress("Cuts chosen on the validation day and frozen.")
    masks = signal_masks(frame, scores, cuts, settings, fitted_signals=which != "train")

    made = build_ledger(data_dir, frame, presets, settings, folder / "ledger" / which,
                        progress=progress)  # fmt: skip
    progress(f"Ledger: {made['rows']:,} rows in {time.time() - started:.0f} s; summarizing")
    kinds = variants(presets, settings)
    aligned = align(frame, folder / "ledger" / which, presets, kinds, made["base_fee_lamports"])
    cells = summarize(frame, masks, aligned, kinds, plan)
    horizon = int(frame["offset_s"].max()) + presets.horizon_s
    report = {
        "set": which, "made_at": time.time(), "seconds": round(time.time() - started),
        "rows": len(frame), "coins": int(frame["mint"].nunique()),
        "dates": sorted(frame["date"].unique()), "launch_range": [int(start), int(end)],
        "coins_by_group": {g: int(n) for g, n in
                           frame.drop_duplicates("mint")["group"].value_counts().items()},
        "frozen": frozen, "code": code_version(root),
        "presets": presets.model_dump(), "settings": settings.model_dump(),
        "variants": kinds, "base_fee_lamports": made["base_fee_lamports"],
        "signals": {name: {"label": label, "rows": int(mask.sum())}
                    for name, (label, mask) in masks.items()},
        "ledger": {"rows": made["rows"], "files": made["files"]},
        "pool_check": verify_pool_trades(data_dir, int(start), int(end) + horizon),
        "cells": cells,
    }  # fmt: skip
    _write_json(report_path, report)
    return report


def _cuts_sha(cuts: dict) -> str:
    return hashlib.sha256(json.dumps(cuts, sort_keys=True).encode()).hexdigest()[:12]


def resummarize(root: Path, data_dir: Path, plan: EvaluationPlan, which: str, *,
                progress=lambda _msg: None) -> dict:  # fmt: skip
    """Build the report of a set again from its saved ledger, with the Padre fee now in
    `config/padre_presets.yaml`. Nothing is simulated: the ledger's files must be the ones
    the report lists, and everything that decides a simulated trade or a signal must be as
    it was. The report it replaces is kept as `<set>_report_before_<time>.json`."""
    if which not in SETS:
        raise ValueError(f"unknown set {which!r}: one of {', '.join(SETS)}")
    folder = out_dir(data_dir)
    report_path = folder / f"{which}_report.json"
    old = _read_json(report_path)
    if old is None:
        raise NotAllowed(f"There is no {which} report to build again: run "
                         f"`agent backtest {which}` first.")  # fmt: skip
    presets, _ = load_presets(root / "config" / "padre_presets.yaml")
    settings, settings_sha = load_settings(root / "config" / "backtest.yaml")
    fitted, models_sha = _checked_models(data_dir, settings)
    cuts = _read_json(folder / "cuts.json")
    frozen = old["frozen"]

    def trade_settings(values: dict) -> dict:
        return {k: v for k, v in values.items() if k not in REPORTING_ONLY}

    changed = []
    if trade_settings(json.loads(presets.model_dump_json())) != trade_settings(old["presets"]):
        changed.append("a trade setting in padre_presets.yaml other than Padre's fee")
    if settings_sha != frozen["settings_sha"]:
        changed.append("backtest.yaml")
    if models_sha != frozen["models_sha"]:
        changed.append("the models")
    if cuts is None or _cuts_sha(cuts) != frozen["cuts_sha"]:
        changed.append("the cuts")
    ledger = folder / "ledger" / which
    on_disk = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
               for p in sorted(ledger.glob("*.parquet"))}  # fmt: skip
    if on_disk != old["ledger"]["files"]:
        changed.append("the ledger's files")
    if changed:
        raise NotAllowed("The report can only be built again from the same simulated "
                         f"trades. Changed: {'; '.join(changed)}.")  # fmt: skip
    kinds = variants(presets, settings)
    if kinds == old["variants"]:
        raise NotAllowed("Nothing to do: the report already uses these settings.")

    frame = load_frame(data_dir, plan, (which,), backfill_only_signals=False)
    progress(f"{which}: {len(frame):,} rows; building the report again from "
             f"{old['ledger']['rows']:,} saved trades (nothing is simulated)")  # fmt: skip
    scores = {} if which == "train" else raw_scores(frame, fitted, settings)
    masks = signal_masks(frame, scores, cuts, settings, fitted_signals=which != "train")
    aligned = align(frame, ledger, presets, kinds, old["base_fee_lamports"])
    kept = folder / f"{which}_report_before_{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}.json"
    report_path.replace(kept)
    report = old | {
        "presets": json.loads(presets.model_dump_json()), "variants": kinds,
        "cells": summarize(frame, masks, aligned, kinds, plan),
        "summarized_again": {
            "at": time.time(), "code": code_version(root), "kept": kept.name,
            "simulated_at": old["made_at"],
            "was": {k: old["variants"][0][k] for k in ("failed_rate", "footprint",
                                                       "platform_fee_pct")},
        },
    }  # fmt: skip
    _write_json(report_path, report)
    return report
