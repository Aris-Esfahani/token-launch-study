"""The evaluation plan (spec 15.4): the file, the day split, and hour-resampled intervals."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

np = pytest.importorskip("numpy")  # a research package: pip install -e ".[research]"

from agent.models.evaluate import (  # noqa: E402
    DAY_S,
    SplitPlan,
    hour_interval,
    load_plan,
    split_days,
    whole_days,
)

ROOT = Path(__file__).resolve().parents[1]
D0 = 1_794_441_600  # 2026-11-12 00:00 UTC


def test_the_plan_file_states_spec_15_4_and_refuses_typos(tmp_path):
    plan, sha = load_plan(ROOT / "config" / "evaluation.yaml")
    assert plan.split == SplitPlan(test_share=0.25, min_test_days=2, validation_days=1,
                                   min_train_days=1)  # fmt: skip
    assert plan.metrics.max_fine_blocked == 0.30 and plan.uncertainty.level == 0.95
    assert plan.uncertainty.method == "resample_whole_hours"
    assert set(plan.baselines) == {"base_rate", "rules", "logistic_regression"}
    assert len(sha) == 12
    bad = tmp_path / "evaluation.yaml"
    text = (ROOT / "config" / "evaluation.yaml").read_text(encoding="utf-8")
    bad.write_text(text.replace("test_share:", "test_shares:"), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_plan(bad)


def test_whole_days_need_midnight_to_midnight():
    # Nov 11 10 PM to Nov 13 0:06 AM UTC (like epoch 1084): only Nov 12 is whole.
    assert whole_days([(D0 - 7_000, D0 + DAY_S + 400)]) == [D0]
    assert whole_days([(D0 + 30, D0 + DAY_S - 30)]) == [D0]  # within the edge slack
    assert whole_days([(D0 + 3_600, D0 + 3 * DAY_S)]) == [D0 + DAY_S, D0 + 2 * DAY_S]
    assert whole_days([(D0, D0 + DAY_S // 2), (D0 + DAY_S // 2 + 10, D0 + DAY_S)]) == []


def test_split_holds_out_the_most_recent_days_once_there_are_enough():
    plan = SplitPlan()
    one = split_days([(D0 - 7_000, D0 + DAY_S + 400)], plan)
    assert one.test == [] and one.validation == [] and one.explore_until is None
    assert one.role(D0 + 5 * DAY_S) == "train"  # nothing held out yet
    assert split_days([(D0, D0 + 3 * DAY_S)], plan).test == []  # 3 days: still too few

    nine = split_days([(D0 - 100, D0 + 9 * DAY_S + 100)], plan)  # 9 whole days
    days = [D0 + i * DAY_S for i in range(9)]
    assert nine.test == days[6:]  # a quarter of 9, rounded up
    assert nine.validation == [days[5]] and nine.train == days[:5]
    assert nine.explore_until == days[5]
    assert nine.role(D0 - 50) == "train"  # a part-day before: training
    assert nine.role(days[5] + 10) == "validation"
    assert nine.role(days[8] + 10) == "test"
    assert nine.role(D0 + 9 * DAY_S + 50) == "test"  # a part-day after: held out

    eight = split_days([(D0, D0 + 8 * DAY_S)], plan)
    assert len(eight.test) == 2 and len(eight.validation) == 1 and len(eight.train) == 5


def test_hour_interval_resamples_whole_hours():
    rng = np.random.default_rng(1)
    # 40 hours, 50 coins each; within an hour the outcome is shared (all 1 or all 0).
    hours = np.repeat(np.arange(40), 50)
    clustered = np.repeat(rng.integers(0, 2, 40), 50).astype(float)
    rate, low, high = hour_interval(clustered, hours)
    assert low < rate < high and rate == pytest.approx(clustered.mean())
    # The same coins treated as independent (one hour each) give a much tighter interval.
    _, low_i, high_i = hour_interval(clustered, np.arange(len(clustered)))
    assert (high - low) > 3 * (high_i - low_i)
    # NaN is left out; a constant has no spread; the same seed gives the same interval.
    with_nan = np.append(clustered, np.nan)
    assert hour_interval(with_nan, np.append(hours, 99))[0] == pytest.approx(rate)
    assert hour_interval(np.ones(10), np.arange(10)) == (1.0, 1.0, 1.0)
    assert hour_interval(clustered, hours) == (rate, low, high)
    assert all(np.isnan(x) for x in hour_interval([np.nan], [0]))
