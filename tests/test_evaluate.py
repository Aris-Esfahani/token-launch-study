"""The evaluation plan (spec 15.4): the file, the day split, and hour-resampled intervals."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

np = pytest.importorskip("numpy")  # a research package: pip install -e ".[research]"

from agent.models.evaluate import (  # noqa: E402
    DAY_S,
    Ranking,
    SplitPlan,
    brier,
    calibration_curve,
    hour_interval,
    load_plan,
    pr_auc,
    split_days,
    top_k_per_hour,
    whole_days,
)

ROOT = Path(__file__).resolve().parents[1]
D0 = 1_794_441_600  # 2026-11-12 00:00 UTC


def test_the_plan_file_states_spec_15_4_and_refuses_typos(tmp_path):
    plan, sha = load_plan(ROOT / "config" / "evaluation.yaml")
    assert plan.split == SplitPlan(test_share=0.25, min_test_days=2, validation_days=1,
                                   min_train_days=1, drop_label_overlap_s=3905)  # fmt: skip
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


def test_the_overlap_models_drop_is_as_far_as_the_labels_reach():
    from agent.labels.make_labels import load_label_settings

    plan, _ = load_plan(ROOT / "config" / "evaluation.yaml")
    labels, _ = load_label_settings(ROOT / "config" / "labels.yaml")
    assert plan.split.drop_label_overlap_s == labels.horizon_s


def test_models_use_whole_days_and_drop_coins_whose_labels_reach_the_next_set():
    split = split_days([(D0 - 7_000, D0 + 8 * DAY_S + 9_000)], SplitPlan())  # 8 whole days
    val, test = D0 + 5 * DAY_S, D0 + 6 * DAY_S
    assert split.model_role(D0 - 50) is None  # the part-day before: in no set
    assert split.model_role(D0 + 8 * DAY_S + 50) is None  # and the one after
    assert split.model_role(D0) == "train" and split.model_role(val - 1) == "train"
    assert split.model_role(val) == "validation" and split.model_role(test) == "test"
    assert split.model_role(D0 + 8 * DAY_S - 1, 3905) == "test"  # nothing follows the test
    # A label reaches 3905 s past launch: launched later than that before the next set
    # starts, it would read into it.
    assert split.model_role(val - 3905, 3905) == "train"
    assert split.model_role(val - 3904, 3905) is None
    assert split.model_role(test - 3905, 3905) == "validation"
    assert split.model_role(test - 1, 3905) is None


def test_pr_auc_matches_scikit_learn_with_and_without_ties():
    metrics = pytest.importorskip("sklearn.metrics")
    rng = np.random.default_rng(3)
    y = (rng.random(400) < 0.2).astype(float)
    smooth = rng.random(400) + y * 0.3
    coarse = np.round(smooth * 4)  # many ties
    for score in (smooth, coarse, np.zeros(400)):
        assert pr_auc(y, score) == pytest.approx(metrics.average_precision_score(y, score))


def test_recall_when_blocking_at_most_a_share_of_fine_coins():
    # Scores high to low: bad, bad, fine, bad, fine, fine, fine, bad (1 = bad).
    y = np.array([1, 1, 0, 1, 0, 0, 0, 1.0])
    score = np.arange(8, 0, -1.0)
    hours = np.zeros(8)
    got = Ranking(y, score, hours).estimates(fine_blocked=0.25, resamples=10)
    assert got["recall"].value == pytest.approx(3 / 4)  # one of four fine coins blocked
    assert Ranking(y, score, hours).estimates(fine_blocked=0.0, resamples=10)[
        "recall"].value == pytest.approx(2 / 4)
    # A constant score blocks everything or nothing: nothing is caught within the limit.
    flat = Ranking(y, np.ones(8), hours).estimates(fine_blocked=0.3, resamples=10)
    assert flat["recall"].value == 0.0
    # Ties block together: the two tied coins hold a fine one, over a limit of zero.
    tied = Ranking(y, np.array([9, 5, 5, 4, 3, 2, 1, 0.0]), hours)
    assert tied.estimates(fine_blocked=0.0, resamples=10)["recall"].value == pytest.approx(1 / 4)


def test_ranking_intervals_resample_whole_hours():
    rng = np.random.default_rng(5)
    hours = np.repeat(np.arange(30), 40)
    quality = np.repeat(rng.random(30), 40)  # how well the score works differs by hour
    y = (rng.random(1200) < 0.3).astype(float)
    score = y * quality + rng.random(1200)
    by_hour = Ranking(y, score, hours).estimates()["pr_auc"]
    alone = Ranking(y, score, np.arange(1200)).estimates()["pr_auc"]
    assert by_hour.low < by_hour.value < by_hour.high
    assert by_hour.value == pytest.approx(alone.value) == pytest.approx(pr_auc(y, score))
    assert (by_hour.high - by_hour.low) > (alone.high - alone.low)
    assert Ranking(y, score, hours).estimates()["pr_auc"] == by_hour  # same seed, same draw
    assert np.isnan(Ranking(np.zeros(5), np.arange(5.0), np.zeros(5)).estimates()[
        "pr_auc"].value)  # no positive coin: undefined


def test_top_k_per_hour_pools_hours_and_shares_ties():
    # Hour 0: top 2 of 4 are (1, 0). Hour 1: only one coin, taken. Hour 2: all tied.
    y = np.array([1, 0, 1, 1, 1, 1, 0, 0, 0.0])
    score = np.array([9, 8, 7, 6, 5, 1, 1, 1, 1.0])
    hours = np.array([0, 0, 0, 0, 1, 2, 2, 2, 2])
    got = top_k_per_hour(y, score, hours, 2, resamples=50)
    assert got.value == pytest.approx((1 + 1 + 2 * 0.25) / (2 + 1 + 2))
    assert got.low <= got.value <= got.high
    shuffled = np.random.default_rng(0).permutation(9)  # row order does not matter
    again = top_k_per_hour(y[shuffled], score[shuffled], hours[shuffled], 2, resamples=50)
    assert again.value == pytest.approx(got.value)


def test_brier_and_calibration_curve():
    y = np.array([0, 0, 1, 1.0])
    p = np.array([0.1, 0.1, 0.9, 0.9])
    assert brier(y, p, np.arange(4), resamples=20).value == pytest.approx(0.01)
    curve = calibration_curve(y, p, bins=2)
    assert [(b["n"], b["predicted"], b["observed"]) for b in curve] == [
        (2, pytest.approx(0.1), 0.0), (2, pytest.approx(0.9), 1.0)]
    assert len(calibration_curve(y, np.full(4, 0.5))) == 1  # one value: one group
