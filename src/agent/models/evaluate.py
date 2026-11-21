"""The evaluation plan (spec 15.4; `config/evaluation.yaml`): which days are held out, and
intervals that resample whole hours.

The split is by whole UTC days, never random. The most recent quarter of the days the
backfill covers (at least two) is the test set, scored once at the end; the day before
is the validation set; the rest is for training. Exploration (R3) and training only see
what came before the validation days, so nothing chosen along the way has looked at the
days that judge it. While there are too few days for that split, nothing is held out.

Needs the research packages (numpy): `pip install -e ".[research]"`.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml
from pydantic import BaseModel, ConfigDict

DAY_S = 86_400
EDGE_SLACK_S = 60  # a day counts as whole if the backfill starts or ends this close to it


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SplitPlan(_Strict):
    test_share: float = 0.25
    min_test_days: int = 2
    validation_days: int = 1
    min_train_days: int = 1


class MetricsPlan(_Strict):
    stage1: list[str]
    max_fine_blocked: float
    stage2_3: list[str]


class UncertaintyPlan(_Strict):
    method: str
    level: float = 0.95
    resamples: int = 2000
    seed: int = 0


class EvaluationPlan(_Strict):
    split: SplitPlan
    baselines: list[str]
    metrics: MetricsPlan
    uncertainty: UncertaintyPlan
    report_separately: list[str]
    report_every_result: bool


def load_plan(path: Path) -> tuple[EvaluationPlan, str]:
    """The plan and a short hash of the file."""
    raw = path.read_bytes()
    plan = EvaluationPlan.model_validate(yaml.safe_load(raw) or {})
    return plan, hashlib.sha256(raw).hexdigest()[:12]


@dataclass(frozen=True)
class DaySplit:
    """Days are given by their first second (UTC midnight, Unix time)."""

    whole_days: list[int]
    train: list[int]
    validation: list[int]
    test: list[int]

    @property
    def explore_until(self) -> int | None:
        """Rows launched before this time may be explored and trained on; None when
        nothing is held out yet."""
        return self.validation[0] if self.validation else None

    def role(self, ts: int) -> str:
        """'train', 'validation' or 'test' for a launch time. Part-days count with the
        days around them: before the validation days is training, after is held out."""
        until = self.explore_until
        if until is None or ts < until:
            return "train"
        if ts < self.test[0]:
            return "validation"
        return "test"


def whole_days(spans: list[tuple[int, int]]) -> list[int]:
    """UTC days the backfill covers from midnight to midnight."""
    days = []
    for start, end in spans:
        day = math.ceil((start - EDGE_SLACK_S) / DAY_S) * DAY_S
        while day + DAY_S - EDGE_SLACK_S <= end:
            days.append(day)
            day += DAY_S
    return sorted(set(days))


def split_days(spans: list[tuple[int, int]], plan: SplitPlan) -> DaySplit:
    days = whole_days(spans)
    n_test = max(plan.min_test_days, math.ceil(len(days) * plan.test_share))
    n_val = plan.validation_days
    if len(days) < n_test + n_val + plan.min_train_days:
        return DaySplit(days, days, [], [])
    return DaySplit(days, days[: -n_test - n_val], days[-n_test - n_val : -n_test],
                    days[-n_test:])  # fmt: skip


def hour_interval(values, hours, *, level: float = 0.95, resamples: int = 2000,
                  seed: int = 0) -> tuple[float, float, float]:  # fmt: skip
    """The mean of `values` (e.g. 0/1 outcomes; NaN left out) and a `level` interval from
    resampling whole hours with replacement: coins launched in the same hour share
    market conditions, so they are not independent draws (spec 15.4)."""
    v = np.asarray(values, dtype=float)
    h = np.asarray(hours)
    keep = ~np.isnan(v)
    v, h = v[keep], h[keep]
    if not len(v):
        return math.nan, math.nan, math.nan
    _, index = np.unique(h, return_inverse=True)
    sums = np.bincount(index, weights=v)
    counts = np.bincount(index).astype(float)
    n = len(sums)
    weights = np.random.default_rng(seed).multinomial(n, np.full(n, 1 / n), size=resamples)
    stats = (weights @ sums) / (weights @ counts)  # every draw has at least one hour
    tail = (1 - level) / 2
    low, high = np.quantile(stats, [tail, 1 - tail])
    return float(v.mean()), float(low), float(high)
