"""The evaluation plan (spec 15.4; `config/evaluation.yaml`): which days are held out, and
intervals that resample whole hours.

The split is by whole UTC days, never random. The most recent quarter of the days the
backfill covers (at least two) is the test set, scored once at the end; the day before
is the validation set; the rest is for training. Exploration (R3) and training only see
what came before the validation days, so nothing chosen along the way has looked at the
days that judge it. While there are too few days for that split, nothing is held out.

Models (R5) use whole days only, and leave out coins launched so close before the
validation or the test days that their labels would read into them (`model_role`).

The metrics of the plan are here too, each with an interval from resampling whole launch
hours: PR-AUC, recall of bad coins when blocking at most a share of fine ones, precision
of the top coins per hour, the Brier score and a calibration curve. Tied scores are never
broken by row order: a tie counts as its expected value.

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
    # Models (R5): leave out coins launched this long or less before the validation or
    # the test days (the labels' reach, 65 min), so no label used for training or tuning
    # reads a later set's minutes. 0 keeps them.
    drop_label_overlap_s: int = 0


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

    def model_role(self, ts: int, drop_label_overlap_s: int = 0) -> str | None:
        """The set a coin launched at `ts` is modeled in (R5): 'train', 'validation' or
        'test', by whole days only. None for part-days at the edges of the backfill and
        for coins launched within `drop_label_overlap_s` before the next set starts."""
        day = ts - ts % DAY_S
        for name, days, following in (("train", self.train, self.validation),
                                      ("validation", self.validation, self.test),
                                      ("test", self.test, [])):  # fmt: skip
            if day in days:
                if following and ts + drop_label_overlap_s > following[0]:
                    return None
                return name
        return None


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


# --- metrics (spec 15.4), with intervals that resample whole hours -----------------------


@dataclass(frozen=True)
class Estimate:
    value: float
    low: float
    high: float

    def as_dict(self) -> dict:
        return {"value": _clean(self.value), "low": _clean(self.low), "high": _clean(self.high)}


def _clean(x: float) -> float | None:
    return None if x is None or math.isnan(x) else float(x)


def _draws(n_hours: int, resamples: int, seed: int) -> np.ndarray:
    """How often each hour is drawn in each resample (resamples x hours)."""
    rng = np.random.default_rng(seed)
    return rng.multinomial(n_hours, np.full(n_hours, 1 / n_hours), size=resamples)


def _interval(stats: np.ndarray, level: float) -> tuple[float, float]:
    stats = stats[~np.isnan(stats)]
    if not len(stats):
        return math.nan, math.nan
    tail = (1 - level) / 2
    low, high = np.quantile(stats, [tail, 1 - tail])
    return float(low), float(high)


class Ranking:
    """Outcomes (0/1) ranked by a score, higher first, counted per launch hour and per
    distinct score, so every resample of hours is a matrix product."""

    def __init__(self, y, score, hours) -> None:
        y = np.asarray(y, dtype=float)
        score = np.asarray(score, dtype=float)
        if np.isnan(score).any() or np.isnan(y).any():
            raise ValueError("scores and outcomes must not be missing")
        self.n = len(y)
        _, hour_index = np.unique(np.asarray(hours), return_inverse=True)
        self.n_hours = int(hour_index.max()) + 1 if self.n else 0
        order = np.argsort(-score, kind="stable")
        ranked = score[order]
        group = np.cumsum(np.r_[True, ranked[1:] != ranked[:-1]]) - 1 if self.n else order
        n_groups = int(group[-1]) + 1 if self.n else 0
        cell = hour_index[order] * n_groups + group
        size = self.n_hours * n_groups
        yo = y[order]
        self.pos = np.bincount(cell, weights=yo, minlength=size).reshape(-1, n_groups)
        self.neg = np.bincount(cell, weights=1 - yo, minlength=size).reshape(-1, n_groups)

    def _stats(self, draws: np.ndarray, fine_blocked: float | None):
        """(PR-AUC, recall at the fine-blocked limit) for each row of hour counts."""
        tp = np.cumsum(draws @ self.pos, axis=1)
        fp = np.cumsum(draws @ self.neg, axis=1)
        positives, negatives = tp[:, -1], fp[:, -1]
        with np.errstate(divide="ignore", invalid="ignore"):
            precision = tp / (tp + fp)
            gained = np.diff(tp, axis=1, prepend=0.0)
            ap = np.nansum(gained * precision, axis=1) / positives
            recall = None
            if fine_blocked is not None:
                # Block every coin scored at or above a threshold: the lowest threshold
                # that still blocks at most the allowed share of fine coins.
                allowed = (fp <= fine_blocked * negatives[:, None]).sum(axis=1)
                caught = np.where(allowed > 0,
                                  tp[np.arange(len(tp)), np.maximum(allowed - 1, 0)], 0.0)
                recall = caught / positives
        return ap, recall

    def estimates(self, *, fine_blocked: float | None = None, level: float = 0.95,
                  resamples: int = 2000, seed: int = 0) -> dict[str, Estimate]:  # fmt: skip
        if not self.n or self.pos.sum() == 0:
            nan = Estimate(math.nan, math.nan, math.nan)
            return {"pr_auc": nan} | ({"recall": nan} if fine_blocked is not None else {})
        ap, recall = self._stats(np.ones((1, self.n_hours)), fine_blocked)
        draws = _draws(self.n_hours, resamples, seed).astype(float)
        aps, recalls = [], []
        for start in range(0, resamples, 50):  # in batches, to bound memory
            a, r = self._stats(draws[start : start + 50], fine_blocked)
            aps.append(a)
            recalls.append(r)
        out = {"pr_auc": Estimate(float(ap[0]), *_interval(np.concatenate(aps), level))}
        if fine_blocked is not None:
            out["recall"] = Estimate(float(recall[0]),
                                     *_interval(np.concatenate(recalls), level))  # fmt: skip
        return out


def pr_auc(y, score) -> float:
    """Average precision (the area under the precision-recall curve)."""
    return Ranking(y, score, np.zeros(len(np.asarray(y)), dtype=int)).estimates(
        resamples=1)["pr_auc"].value  # fmt: skip


def ratio_interval(numerator, denominator, hours, *, level: float = 0.95,
                   resamples: int = 2000, seed: int = 0) -> Estimate:  # fmt: skip
    """sum(numerator) / sum(denominator) with an interval from resampling whole hours."""
    num = np.asarray(numerator, dtype=float)
    den = np.asarray(denominator, dtype=float)
    if not len(num) or den.sum() == 0:
        return Estimate(math.nan, math.nan, math.nan)
    _, index = np.unique(np.asarray(hours), return_inverse=True)
    nums, dens = np.bincount(index, weights=num), np.bincount(index, weights=den)
    draws = _draws(len(nums), resamples, seed)
    with np.errstate(divide="ignore", invalid="ignore"):
        stats = (draws @ nums) / (draws @ dens)
    return Estimate(float(num.sum() / den.sum()), *_interval(stats, level))


def top_k_per_hour(y, score, hours, k: int, **interval) -> Estimate:
    """Precision of the `k` highest-scored coins of each launch hour (all of them in an
    hour with fewer), pooled over hours. Coins tied at the edge share the places left."""
    y = np.asarray(y, dtype=float)
    score = np.asarray(score, dtype=float)
    hours = np.asarray(hours)
    order = np.lexsort((-score, hours))
    y, score, hours = y[order], score[order], hours[order]
    starts = np.flatnonzero(np.r_[True, hours[1:] != hours[:-1]]) if len(y) else []
    hits, picks = [], []
    for a, b in zip(starts, np.r_[starts[1:], len(y)], strict=False):
        ys, ss = y[a:b], score[a:b]
        take = min(k, b - a)
        edge = ss[take - 1]
        above = ss > edge
        tied = ss == edge
        hits.append(ys[above].sum() + (take - above.sum()) * ys[tied].mean())
        picks.append(take)
    return ratio_interval(hits, picks, hours[starts] if len(y) else [], **interval)


def brier(y, probability, hours, **interval) -> Estimate:
    """Mean squared difference between the predicted probability and the outcome."""
    y = np.asarray(y, dtype=float)
    p = np.asarray(probability, dtype=float)
    return ratio_interval((p - y) ** 2, np.ones(len(y)), hours, **interval)


def calibration_curve(y, probability, bins: int = 10) -> list[dict]:
    """Coins in `bins` groups of equal size by predicted probability (fewer when many
    share one value): the mean prediction and the share that happened in each."""
    y = np.asarray(y, dtype=float)
    p = np.asarray(probability, dtype=float)
    if not len(y):
        return []
    edges = np.unique(np.quantile(p, np.linspace(0, 1, bins + 1)))
    index = np.clip(np.searchsorted(edges, p, side="right") - 1, 0, max(len(edges) - 2, 0))
    out = []
    for b in range(max(len(edges) - 1, 1)):
        inside = index == b
        if inside.any():
            out.append({"n": int(inside.sum()), "predicted": float(p[inside].mean()),
                        "observed": float(y[inside].mean())})  # fmt: skip
    return out
