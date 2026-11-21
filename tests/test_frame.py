"""The rows models read (R5): which sets, which rows, which columns, and launch-context
features that only count launches before the coin's own (spec 6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")  # a research package: pip install -e ".[research]"
np = pytest.importorskip("numpy")

import duckdb  # noqa: E402

from agent.dataset.build import ID_COLUMNS, LABEL_PREFIX, RULE_COLUMNS  # noqa: E402
from agent.models.evaluate import DAY_S, EvaluationPlan, load_plan, split_days  # noqa: E402
from agent.models.frame import (  # noqa: E402
    LEFT_OUT,
    SIGNALS,
    STAGES,
    check_features,
    feature_columns,
    launch_context,
    load_frame,
    matrix,
    role_ranges,
)

ROOT = Path(__file__).resolve().parents[1]
D0 = 1_794_441_600  # 2026-11-12 00:00 UTC
DROP = 3905


def plan() -> EvaluationPlan:
    return load_plan(ROOT / "config" / "evaluation.yaml")[0]


def test_models_read_signals_only():
    for with_bf in (False, True):
        columns = feature_columns(with_backfill_only=with_bf)
        assert len(columns) == len(set(columns))
        assert not any(c.startswith(LABEL_PREFIX) or c.startswith("y_") for c in columns)
        assert not set(columns) & (set(ID_COLUMNS) | set(RULE_COLUMNS) | set(LEFT_OUT))
    assert not any(c.startswith("bf_") for c in feature_columns())
    assert any(c.startswith("bf_") for c in feature_columns(with_backfill_only=True))
    for forbidden in ("label_bad", "label_entry_price", "y_hit_2x", "mint", "created_ts",
                      "hour_ts", "history_s", "rules_verdict", "labels_complete",
                      "role"):  # fmt: skip
        with pytest.raises(ValueError):
            check_features(["trades", forbidden])
        with pytest.raises(ValueError):
            matrix(pd.DataFrame({"trades": [1], forbidden: [1]}), ["trades", forbidden])


def test_every_dataset_signal_is_used_or_left_out_for_a_stated_reason():
    manifest_signals = {  # the dataset's signal columns (R2)
        "token_age_s", "is_mayhem", "is_sol_paired", "completed", "trades", "buys", "sells",
        "unique_buyers", "unique_buyers_per_min", "buy_sol", "sell_sol", "net_sol",
        "buys_last_30s", "sells_last_30s", "net_sol_last_30s", "median_buy_sol",
        "max_buy_sol", "largest_sell_pct", "curve_progress_pct", "progress_per_min",
        "price_change_pct", "market_cap_sol", "holders", "top1_hold_pct", "top10_hold_pct",
        "dev_buy_pct", "dev_buy_sol", "dev_hold_pct", "dev_sold_pct", "dev_prior_launches",
        "launch_slot_buyers", "snipers", "sniper_hold_pct", "mayhem_agent_buy_share",
        "mayhem_agent_hold_pct",
    }  # fmt: skip
    assert set(SIGNALS) | set(LEFT_OUT) == manifest_signals


def _context(launches: list[tuple[str, str, int]]) -> dict[str, tuple[int, int, int]]:
    with duckdb.connect() as db:
        db.register("launches", pd.DataFrame(launches, columns=["mint", "creator", "created_ts"]))
        return {r[0]: r[1:] for r in db.execute(launch_context(db, "launches")).fetchall()}


def test_launch_context_counts_only_launches_before_the_coin():
    t = D0 + 5 * 3600
    launches = [
        ("old", "DEV", t - DAY_S - 10),  # more than 24 h before "c": not counted for it
        ("a", "DEV", t - DAY_S),  # exactly 24 h before: counted
        ("b", "DEV", t - 100),
        ("x", "OTHER", t - 50),
        ("c", "DEV", t),
        ("twin", "DEV", t),  # the same second: not "before"
    ]
    got = _context(launches)
    assert got["c"] == (2, 2, 5) == got["twin"]  # a and b; b and x in the last hour; 5 AM
    assert got["old"] == (0, 0, 4) and got["x"] == (0, 1, 4)
    # Spec 6: launches after a coin change nothing about it.
    later = [("n1", "DEV", t + 1), ("n2", "DEV", t + 500), ("n3", "OTHER", t + DAY_S)]
    again = _context(launches + later)
    assert {m: again[m] for m in got} == got
    assert again["n2"][0] == 4  # b, c, twin and n1: they do count for coins after them


def _dataset(tmp_path: Path, rows: list[dict], span: tuple[int, int]) -> Path:
    data = tmp_path / "data"
    (data / "dataset").mkdir(parents=True)
    base = dict.fromkeys(SIGNALS, 1.0) | {
        "creator": "DEV", "has_gap": False, "labels_complete": True, "is_sol_paired": True,
        "is_mayhem": False, "completed": False, "rules_verdict": "caution",
        "label_bad": True, "label_is_dead": True, "label_is_rug": False,
        "label_graduated_1h": False, "label_hit_2x": False,
    }  # fmt: skip
    full = []
    for r in rows:
        for offset in (30, 60):
            ts = r["created_ts"]
            full.append(base | {"date": pd.Timestamp(ts, unit="s").strftime("%Y-%m-%d"),
                                "hour_ts": ts - ts % 3600, "offset_s": offset,
                                "token_age_s": offset} | r)  # fmt: skip
    pd.DataFrame(full).to_parquet(data / "dataset" / "dataset.parquet")
    (data / "dataset" / "manifest.json").write_text(
        json.dumps({"backfill_spans": [list(span)]}), encoding="utf-8")
    return data


def test_load_frame_keeps_the_sets_apart_and_only_rows_to_model_on(tmp_path):
    span = (D0 - 6_000, D0 + 8 * DAY_S + 6_000)  # 8 whole days and a part-day at each end
    val, test = D0 + 5 * DAY_S, D0 + 6 * DAY_S
    rows = [
        {"mint": "part-before", "created_ts": D0 - 100},
        {"mint": "t1", "created_ts": D0 + 10, "label_hit_2x": True},
        {"mint": "t2", "created_ts": val - DROP, "is_mayhem": True},  # the last one kept
        {"mint": "overlap-t", "created_ts": val - DROP + 1},
        {"mint": "v1", "created_ts": val, "completed": True, "label_graduated_1h": True},
        {"mint": "overlap-v", "created_ts": test - 60},
        {"mint": "s1", "created_ts": test + 5, "label_is_dead": False, "label_bad": False},
        {"mint": "s2", "created_ts": D0 + 8 * DAY_S - 1},  # the test set's last second
        {"mint": "part-after", "created_ts": D0 + 8 * DAY_S + 100},
        {"mint": "gap", "created_ts": D0 + 20, "has_gap": True},
        {"mint": "no-labels", "created_ts": D0 + 30, "labels_complete": False},
        {"mint": "other-quote", "created_ts": D0 + 40, "is_sol_paired": False},
        {"mint": "no-entry", "created_ts": D0 + 50, "label_hit_2x": None},
    ]
    data = _dataset(tmp_path, rows, span)
    split = split_days([span], plan().split)
    assert role_ranges(split, DROP) == {"train": (D0, val - DROP + 1),
                                        "validation": (val, test - DROP + 1),
                                        "test": (test, D0 + 8 * DAY_S)}  # fmt: skip
    for (start, end), role in zip(role_ranges(split, DROP).values(),
                                  ("train", "validation", "test"), strict=True):  # fmt: skip
        assert split.model_role(start, DROP) == split.model_role(end - 1, DROP) == role
        assert split.model_role(end, DROP) != role

    frame = load_frame(data, plan(), ("train", "validation"), backfill_only_signals=False)
    by_role = frame.groupby("role")["mint"].agg(lambda m: sorted(set(m))).to_dict()
    assert by_role == {"train": ["no-entry", "t1", "t2"], "validation": ["v1"]}
    assert len(frame) == 8 and list(frame["mint"][:2]) == ["t1", "t1"]  # launch order
    assert set(frame.loc[frame["mint"] == "t2", "group"]) == {"mayhem"}
    test_rows = load_frame(data, plan(), ("test",), backfill_only_signals=False)
    assert sorted(set(test_rows["mint"])) == ["s1", "s2"] and set(test_rows["role"]) == {"test"}

    # Targets: missing where the stage does not predict the coin.
    row = {m: frame[frame["mint"] == m].iloc[0] for m in ("t1", "v1", "no-entry")}
    assert row["t1"]["y_bad"] == 1.0 and row["t1"]["y_hit_2x"] == 1.0
    assert row["t1"]["y_dead_or_rug"] == 1.0 and row["t1"]["y_grad_later"] == 0.0
    assert np.isnan(row["v1"]["y_grad_later"])  # already graduated: not predicted
    assert np.isnan(row["no-entry"]["y_hit_2x"]) and row["no-entry"]["y_bad"] == 1.0
    assert test_rows.loc[test_rows["mint"] == "s1", "y_bad"].iloc[0] == 0.0
    assert {s.target for s in STAGES} <= set(frame.columns)
    x = matrix(frame, feature_columns())
    assert x.shape == (8, len(feature_columns())) and x.dtype == np.float64

    with pytest.raises(ValueError):
        load_frame(data, plan(), ("holdout",))
    few_days = _dataset(tmp_path / "few", rows[:2], (D0 - 100, D0 + 2 * DAY_S + 100))
    with pytest.raises(ValueError):
        load_frame(few_days, plan(), ("test",), backfill_only_signals=False)
