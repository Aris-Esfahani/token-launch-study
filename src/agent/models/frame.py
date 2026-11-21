"""The rows the models are trained, tuned and scored on (spec 15.3 step R5, 15.4).

`load_frame` reads the research dataset for the sets asked for, and only those: rows of
the test days are not read at all unless "test" is asked for. Rows kept: SOL-paired coins
(spec 5: models on SOL-paired coins first) with no backfill gap and every label known.

What a model may read is `feature_columns()` and nothing else. No id, no rules column and
nothing starting with `label_` can be in it (`check_features`, also a test).

Added to the dataset's signals, all known at the coin's launch (spec 6):
- `dev_launches_24h`: launches by the same creator in the 24 hours before this one. It
  replaces `dev_prior_launches`, which counts since the backfill began and so grows with
  the calendar (R5 leak review). The first training day has 2 to 24 hours of history.
- `launches_last_hour`: all launches in the hour before this one (market context, 8.8).
- `launch_hour_utc`: the hour of the day.
With `backfill_only=True` the `bf_` columns of `agent.features.backfill_only` are joined.

Targets (spec 7), 1.0 / 0.0, missing where a coin is not predicted for that stage:
- `y_bad`: rug, dead or bundled (stand-in). `y_dead_or_rug`: the same without the
  bundled stand-in, which is already decided at T+60 s.
- `y_grad_later`: graduates later in the hour; only for coins still on the curve.
- `y_hit_2x`: 2x before 0.5x after the simulated entry; only where there is an entry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from agent.dataset.build import FILE_NAME, ID_COLUMNS, LABEL_PREFIX, OUT_FOLDER, RULE_COLUMNS
from agent.features import backfill_only
from agent.models.evaluate import DAY_S, DaySplit, EvaluationPlan, split_days

ROLES = ("train", "validation", "test")
DAY_BACK_S, HOUR_BACK_S = 86_400, 3_600
SIGNALS = (
    "token_age_s", "is_mayhem", "completed", "trades", "buys", "sells", "unique_buyers",
    "unique_buyers_per_min", "buy_sol", "sell_sol", "net_sol", "buys_last_30s",
    "sells_last_30s", "net_sol_last_30s", "median_buy_sol", "max_buy_sol", "largest_sell_pct",
    "curve_progress_pct", "progress_per_min", "price_change_pct", "market_cap_sol", "holders",
    "top1_hold_pct", "top10_hold_pct", "dev_buy_pct", "dev_buy_sol", "dev_hold_pct",
    "dev_sold_pct", "launch_slot_buyers", "snipers", "sniper_hold_pct",
    "mayhem_agent_buy_share", "mayhem_agent_hold_pct",
)  # fmt: skip
LAUNCH_CONTEXT = ("dev_launches_24h", "launches_last_hour", "launch_hour_utc")
# Dataset signals the models do not read, and why.
LEFT_OUT = {
    "dev_prior_launches": "counts since the backfill began; replaced by dev_launches_24h",
    "is_sol_paired": "always true in the rows modeled",
}


@dataclass(frozen=True)
class Stage:
    name: str
    target: str
    title: str
    kind: str  # "filter": higher score = block the coin; "pick": higher score = choose it


STAGES = (
    Stage("bad", "y_bad", "Stage 1: bad (rug, dead or bundled)", "filter"),
    Stage("dead_or_rug", "y_dead_or_rug", "Stage 1 without the bundled stand-in", "filter"),
    Stage("grad_later", "y_grad_later", "Stage 2: graduates later in the hour", "pick"),
    Stage("hit_2x", "y_hit_2x", "Stage 3: 2x before 0.5x", "pick"),
)


def feature_columns(*, with_backfill_only: bool = False) -> list[str]:
    columns = [*SIGNALS, *LAUNCH_CONTEXT]
    if with_backfill_only:
        columns += backfill_only.COLUMNS
    check_features(columns)
    return columns


def check_features(columns: list[str]) -> None:
    """Refuse anything a model must not read: labels, ids, the rules' output, targets."""
    for c in columns:
        if (c.startswith(LABEL_PREFIX) or c.startswith("y_") or c in ID_COLUMNS
                or c in RULE_COLUMNS or c in ("labels_complete", "role", "group")):  # fmt: skip
            raise ValueError(f"{c!r} is not a feature: models may not read it")


def dataset_split(data_dir: Path, plan: EvaluationPlan) -> tuple[DaySplit, dict]:
    """The day split of the plan on the days the dataset covers, and the manifest."""
    manifest = json.loads((data_dir / OUT_FOLDER / "manifest.json").read_text(encoding="utf-8"))
    spans = [tuple(s) for s in manifest["backfill_spans"]]
    return split_days(spans, plan.split), manifest


def role_ranges(split: DaySplit, drop_label_overlap_s: int) -> dict[str, tuple[int, int]]:
    """[start, end) of launch times for each set: whole days, less the stretch before the
    next set whose labels would read into it. The same rule as `DaySplit.model_role`."""
    out = {}
    for name, days, following in (("train", split.train, split.validation),
                                  ("validation", split.validation, split.test),
                                  ("test", split.test, [])):  # fmt: skip
        if not days:
            continue
        end = days[-1] + DAY_S
        if following:
            # model_role leaves out ts + drop > next start, so the last one kept is
            # next start - drop.
            end = min(end, following[0] - drop_label_overlap_s + 1)
        out[name] = (days[0], end)
    return out


def launch_context(db: duckdb.DuckDBPyConnection, launches: str) -> str:
    """SQL for `mint` and the launch-context columns from a relation of every launch
    (`mint`, `creator`, `created_ts`). Each coin's values count launches strictly before
    its own, so later launches change nothing (spec 6)."""
    return (
        f"SELECT mint, "
        f"count(*) OVER (PARTITION BY creator ORDER BY created_ts "
        f"  RANGE BETWEEN {DAY_BACK_S} PRECEDING AND 1 PRECEDING) AS dev_launches_24h, "
        f"count(*) OVER (ORDER BY created_ts "
        f"  RANGE BETWEEN {HOUR_BACK_S} PRECEDING AND 1 PRECEDING) AS launches_last_hour, "
        f"(created_ts % {DAY_S}) // 3600 AS launch_hour_utc "
        f"FROM (SELECT DISTINCT mint, creator, created_ts FROM {launches})"
    )


def load_frame(data_dir: Path, plan: EvaluationPlan, roles: tuple[str, ...], *,
               backfill_only_signals: bool = True) -> pd.DataFrame:  # fmt: skip
    """The rows of `roles`, with `role`, `group` (ordinary / mayhem), the features and the
    targets. Sorted by launch time, mint and cutoff."""
    unknown = set(roles) - set(ROLES)
    if unknown:
        raise ValueError(f"unknown set(s): {sorted(unknown)}")
    split, _ = dataset_split(data_dir, plan)
    ranges = role_ranges(split, plan.split.drop_label_overlap_s)
    missing = [r for r in roles if r not in ranges]
    if missing:
        raise ValueError(f"the dataset has too few whole days for: {', '.join(missing)}")
    path = (data_dir / OUT_FOLDER / FILE_NAME).as_posix()
    signals = ", ".join(f"d.{c}" for c in SIGNALS)
    frames = []
    with duckdb.connect() as db:
        db.execute(f"SET temp_directory = '{(data_dir / 'duckdb_tmp').as_posix()}'")
        db.execute(f"CREATE TEMP VIEW ds AS SELECT * FROM read_parquet('{path}')")
        # Launch times of every coin (not outcomes): each coin's context counts only
        # launches before its own.
        db.execute(f"CREATE TEMP TABLE context AS {launch_context(db, 'ds')}")
        for role in roles:
            start, end = ranges[role]
            frame = db.execute(
                f"SELECT d.mint, d.created_ts, d.date, d.hour_ts, d.offset_s, "
                f"'{role}' AS role, "
                f"CASE WHEN d.is_mayhem THEN 'mayhem' ELSE 'ordinary' END AS \"group\", "
                f"d.rules_verdict, {signals}, c.dev_launches_24h, c.launches_last_hour, "
                f"c.launch_hour_utc, "
                f"d.label_bad::DOUBLE AS y_bad, "
                f"(d.label_is_dead OR d.label_is_rug)::DOUBLE AS y_dead_or_rug, "
                f"CASE WHEN d.completed THEN NULL ELSE d.label_graduated_1h::DOUBLE END "
                f"  AS y_grad_later, "
                f"d.label_hit_2x::DOUBLE AS y_hit_2x "
                f"FROM ds d JOIN context c USING (mint) "
                f"WHERE d.created_ts >= ? AND d.created_ts < ? AND d.is_sol_paired "
                f"AND NOT d.has_gap AND d.labels_complete "
                f"ORDER BY d.created_ts, d.mint, d.offset_s",
                [start, end],
            ).df()
            if backfill_only_signals:
                extra = backfill_only.backfill_only_signals(data_dir, start, end).to_pandas()
                frame = frame.merge(extra, on=["mint", "offset_s"], how="left", validate="1:1")
            frames.append(frame)
    out = pd.concat(frames, ignore_index=True)
    for c in out.columns:
        if out[c].dtype == bool or str(out[c].dtype) == "boolean":
            out[c] = out[c].astype(float)
    return out


def matrix(frame: pd.DataFrame, columns: list[str]) -> np.ndarray:
    """The feature values as numbers, missing as NaN."""
    check_features(columns)
    return frame[columns].to_numpy(dtype=np.float64, na_value=np.nan)
