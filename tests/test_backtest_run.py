"""Running the backtest (research track R6) on made-up coins: the cuts come from the
validation day only, signals buy the right rows, the report's numbers match the ledger
worked out by hand, and the test days can be simulated once."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")  # research packages: pip install -e ".[research]"
np = pytest.importorskip("numpy")
pytest.importorskip("lightgbm")

from agent.backtest import run as R  # noqa: E402
from agent.backtest.report import write_results  # noqa: E402
from agent.models.evaluate import load_plan  # noqa: E402
from agent.sim import backtest as B  # noqa: E402
from agent.sim.market import Loaded  # noqa: E402
from test_backtest import LAUNCH, T0, Book  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SETTINGS, _ = R.load_settings(ROOT / "config" / "backtest.yaml")
PRESETS, _ = B.load_presets(ROOT / "config" / "padre_presets.yaml")
DAY = 86_400


def small_plan():
    plan, _ = load_plan(ROOT / "config" / "evaluation.yaml")
    return plan.model_copy(update={
        "uncertainty": plan.uncertainty.model_copy(update={"resamples": 50})})  # fmt: skip


def frame_of(n: int, seed: int, day: int = 17, offsets=(30, 60, 120, 300)) -> pd.DataFrame:
    """Made-up rows: one score per model signal, the rules' verdict and what stage 1 and
    stage 2 need."""
    rng = np.random.default_rng(seed)
    created = day * DAY + np.sort(rng.integers(0, DAY, n))
    group = np.where(rng.random(n) < 0.4, "mayhem", "ordinary")
    parts = []
    for offset in offsets:
        f = pd.DataFrame({"mint": [f"d{day}c{i}" for i in range(n)]})
        f["created_ts"] = created
        f["offset_s"] = offset
        f["hour_ts"] = f["created_ts"] // 3600 * 3600
        f["date"] = f"2026-11-{day:02d}"
        f["group"] = group
        f["rules_verdict"] = rng.choice(["avoid", "caution", "pass"], n, p=[0.5, 0.4, 0.1])
        f["completed"] = (rng.random(n) < 0.1).astype(float)
        f["y_bad"] = (rng.random(n) < 0.9).astype(float)
        for spec in SETTINGS.signals:
            if spec.kind in ("pick", "filter"):
                f[f"score_{spec.name}"] = rng.normal(size=n) + (f["y_bad"] if spec.kind
                                                                == "filter" else 0)  # fmt: skip
        parts.append(f)
    return pd.concat(parts, ignore_index=True)


def scores_of(frame: pd.DataFrame) -> dict:
    return {c.removeprefix("score_"): frame[c].to_numpy() for c in frame.columns
            if c.startswith("score_")}  # fmt: skip


# --- settings, cuts and signals --------------------------------------------------------------


def test_the_settings_file_loads_and_a_signal_without_its_model_is_refused(tmp_path):
    settings, sha = R.load_settings(ROOT / "config" / "backtest.yaml")
    assert [s.name for s in settings.signals] == [
        "everything", "rules_pass", "rules_not_avoid", "lightgbm_not_bad", "logistic_2x",
        "lightgbm_2x", "lightgbm_grad"]  # fmt: skip
    assert settings.headline_footprint == "none" and settings.picks_per_hour == [5, 20]
    assert len(sha) == 12
    bad = tmp_path / "backtest.yaml"
    text = (ROOT / "config" / "backtest.yaml").read_text(encoding="utf-8")
    bad.write_text(text.replace("stage: hit_2x, model: lightgbm}", "stage: hit_2x}"),
                   encoding="utf-8")  # fmt: skip
    with pytest.raises(ValueError):
        R.load_settings(bad)


def test_cuts_give_the_wanted_picks_per_hour_on_the_validation_day():
    validation = frame_of(4_000, 1)
    scores = scores_of(validation)
    cuts = R.choose_cuts(validation, scores, SETTINGS, hours=20.0)
    masks = R.signal_masks(validation, scores, cuts, SETTINGS)
    assert list(masks) == [
        "everything", "rules_pass", "rules_not_avoid", "lightgbm_not_bad", "logistic_2x_5ph",
        "logistic_2x_20ph", "lightgbm_2x_5ph", "lightgbm_2x_20ph", "lightgbm_grad_5ph",
        "lightgbm_grad_20ph"]  # fmt: skip
    offsets = validation["offset_s"].to_numpy()
    for offset in (30, 60, 120, 300):
        at = offsets == offset
        assert masks["lightgbm_2x_5ph"][1][at].sum() == 100  # 5 per hour x 20 hours
        assert masks["lightgbm_2x_20ph"][1][at].sum() == 400
        # The cut is the 100th highest score at that decision time.
        ranked = np.sort(scores["lightgbm_2x"][at])[::-1]
        assert cuts["lightgbm_2x_5ph"][str(offset)]["cut"] == ranked[99]
        # Graduation is only picked among coins still on the curve.
        grad = masks["lightgbm_grad_5ph"][1] & at
        assert grad.sum() == 100 and (validation["completed"][grad] == 0).all()
        # The filter blocks at most 30% of the fine coins, and close to it.
        fine = at & (validation["y_bad"] == 0).to_numpy()
        blocked = ~masks["lightgbm_not_bad"][1]
        assert 0.29 < blocked[fine].mean() <= 0.30
        assert blocked[at & ~fine].mean() > 0.5  # bad coins score higher in the made-up rows
    assert masks["everything"][1].all()
    assert (masks["rules_pass"][1] == (validation["rules_verdict"] == "pass")).all()
    assert (masks["rules_not_avoid"][1] == (validation["rules_verdict"] != "avoid")).all()
    assert masks["lightgbm_2x_5ph"][0] == "LightGBM 2x, about 5 per hour"


def test_cuts_are_fixed_numbers_applied_to_other_days_and_unfitted_signals_stand_alone():
    validation, test = frame_of(4_000, 1), frame_of(3_000, 2, day=18)
    cuts = R.choose_cuts(validation, scores_of(validation), SETTINGS, hours=20.0)
    masks = R.signal_masks(test, scores_of(test), cuts, SETTINGS)
    at = (test["offset_s"] == 60).to_numpy()
    cut = cuts["lightgbm_2x_5ph"]["60"]["cut"]
    assert (masks["lightgbm_2x_5ph"][1][at] == (test["score_lightgbm_2x"][at] >= cut)).all()
    assert masks["lightgbm_2x_5ph"][1][at].sum() != 100  # not re-ranked on the new day
    plain = R.signal_masks(test, {}, {}, SETTINGS, fitted_signals=False)
    assert list(plain) == ["everything", "rules_pass", "rules_not_avoid"]


def test_first_signal_keeps_each_coins_earliest_decision_time():
    frame = pd.DataFrame({"mint": ["a", "b", "c", "a", "b", "c", "a", "b", "c"],
                          "offset_s": [30, 30, 30, 60, 60, 60, 120, 120, 120]})  # fmt: skip
    mask = np.array([0, 1, 0, 1, 1, 0, 1, 0, 0], dtype=bool)
    assert R.first_signal(frame, mask).tolist() == [0, 1, 0, 1, 0, 0, 0, 0, 0]
    assert not R.first_signal(frame, np.zeros(9, dtype=bool)).any()


def test_variants_change_one_thing_at_a_time():
    kinds = R.variants(PRESETS, SETTINGS)
    assert kinds[0] == {"failed_rate": 0.05, "footprint": "none", "platform_fee_pct": 1.0,
                        "name": "headline"}  # fmt: skip
    assert [k["name"] for k in kinds[1:]] == ["failed_0", "failed_0.1", "footprint_stays",
                                              "platform_fee_0"]  # fmt: skip
    for kind in kinds[1:]:
        assert sum(kind[key] != kinds[0][key] for key in kinds[0] if key != "name") == 1


# --- a whole run on made-up coins --------------------------------------------------------------


def book_of(i: int) -> Book:
    """A few kinds of coin: quiet, pumped, dumped, graduated."""
    book = Book(mayhem=i % 3 == 0)
    first = book.buy(1.0 + i % 4, T0 + 5)
    if i % 4 == 1:
        for k in range(6):
            book.buy(3.0, T0 + 100 + 20 * k)
    elif i % 4 == 2:
        book.sell(first.tokens, T0 + 90 + i)
    elif i % 4 == 3:
        book.buy(2.0, T0 + 50)
        book.buy(0.5, T0 + 400)
    return book


class Fake:
    """Stands in for the dataset, the models and the backfill."""

    def __init__(self) -> None:
        self.frames, self.books = {}, {}
        for role, days in (("train", (12, 13)), ("validation", (17,)), ("test", (18, 19))):
            parts = [frame_of(40, day, day=day, offsets=(30, 60)) for day in days]
            self.frames[role] = pd.concat(parts, ignore_index=True)
        for frame in self.frames.values():
            for i, (mint, ts) in enumerate(frame.drop_duplicates("mint")[["mint", "created_ts"]]
                                           .itertuples(index=False)):  # fmt: skip
                self.books[mint] = (book_of(i), int(ts))

    def market(self, mint: str):
        book, created = self.books[mint]
        shift = created - T0
        market = book.market()
        market.mint, market.created_ts = mint, created
        for name in ("c_ts", "p_ts", "x_ts"):
            setattr(market, name, getattr(market, name) + shift)
        if market.completed_ts is not None:
            market.completed_ts += shift
        return market

    def install(self, monkeypatch) -> None:
        launch = type(LAUNCH)([(0, False, 95, 30), (0, True, 95, 30)])
        monkeypatch.setattr(R, "load_frame", lambda _d, _p, roles, **_k: self.frames[roles[0]])
        monkeypatch.setattr(R, "dataset_split", lambda _d, _p: (None, {}))
        monkeypatch.setattr(R, "role_ranges", lambda _s, _o: {
            "train": (12 * DAY, 14 * DAY), "validation": (17 * DAY, 18 * DAY),
            "test": (18 * DAY, 20 * DAY)})  # fmt: skip
        monkeypatch.setattr(R, "_checked_models", lambda _d, _s: ({}, "models-sha"))
        monkeypatch.setattr(R, "raw_scores", lambda frame, _f, _s: scores_of(frame))
        monkeypatch.setattr(R, "load_markets", lambda _d, coins, _h: Loaded(
            {mint: self.market(mint) for mint, _ in coins}, launch, 5_000))  # fmt: skip
        monkeypatch.setattr(R, "verify_pool_trades", lambda _d, _a, _b: {
            "sell": {"trades": 2, "amounts_exact": 2, "fees_exact": 2, "with_previous": 1,
                     "continues": 1}})  # fmt: skip


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    shutil.copytree(ROOT / "config", root / "config")
    (root / "docs").mkdir()
    fake = Fake()
    fake.install(monkeypatch)
    return root, tmp_path / "data", fake


def test_the_test_days_run_once_and_only_on_what_was_frozen(project):
    root, data, _ = project
    plan = small_plan()
    with pytest.raises(R.NotAllowed, match="validation"):
        R.run(root, data, plan, "test")  # no cuts yet
    first = R.run(root, data, plan, "validation")
    validation = R.run(root, data, plan, "validation")
    assert first["cells"] == validation["cells"]  # the same again
    folder = R.out_dir(data)
    cuts = json.loads((folder / "cuts.json").read_text(encoding="utf-8"))
    frozen = json.loads((folder / "frozen.json").read_text(encoding="utf-8"))
    assert validation["frozen"] == frozen and set(cuts) == {
        "lightgbm_not_bad", "logistic_2x_5ph", "logistic_2x_20ph", "lightgbm_2x_5ph",
        "lightgbm_2x_20ph", "lightgbm_grad_5ph", "lightgbm_grad_20ph"}  # fmt: skip
    assert {k: v for k, v in first["frozen"].items() if k != "frozen_at"} == {
        k: v for k, v in frozen.items() if k != "frozen_at"}  # fmt: skip

    # A settings change after the cuts were chosen blocks the test days.
    presets = root / "config" / "padre_presets.yaml"
    original = presets.read_bytes()
    presets.write_bytes(original.replace(b"slippage_pct: 20", b"slippage_pct: 30"))
    with pytest.raises(R.NotAllowed, match="presets_sha"):
        R.run(root, data, plan, "test")
    presets.write_bytes(original)
    # So does a cuts file edited by hand.
    edited = json.loads(json.dumps(cuts))
    edited["lightgbm_2x_5ph"]["30"]["cut"] -= 1.0
    (folder / "cuts.json").write_text(json.dumps(edited), encoding="utf-8")
    with pytest.raises(R.NotAllowed, match="cuts"):
        R.run(root, data, plan, "test")
    (folder / "cuts.json").write_text(json.dumps(cuts, indent=1), encoding="utf-8")

    test = R.run(root, data, plan, "test")
    assert test["dates"] == ["2026-11-18", "2026-11-19"] and test["frozen"] == frozen
    assert json.loads((folder / "cuts.json").read_text(encoding="utf-8")) == cuts
    with pytest.raises(R.NotAllowed, match="once"):
        R.run(root, data, plan, "test")
    with pytest.raises(R.NotAllowed, match="again is refused"):
        R.run(root, data, plan, "validation")  # no new cuts after the test days
    train = R.run(root, data, plan, "train")  # the unfitted signals only
    assert list(train["signals"]) == ["everything", "rules_pass", "rules_not_avoid"]
    with pytest.raises(ValueError):
        R.run(root, data, plan, "tomorrow")

    path = write_results(root, data)
    text = path.read_text(encoding="utf-8")
    for part in ("Test days 2026-11-18, 2026-11-19, simulated once", "Validation day 2026-11-17",
                 "Training days 2026-11-12 to 2026-11-13", "Exit plan `double_or_half`",
                 "**Delay sensitivity**", "**Per day**", "**Ordinary vs. mayhem coins**",
                 "LightGBM 2x, about 5 per hour", "Our SOL stays in the reserves",
                 "10% of orders fail", "Padre fee 0%", "| `lightgbm_2x_5ph` |"):  # fmt: skip
        assert part in text, part
    assert "built again" not in text


def test_a_report_is_built_again_from_the_same_trades_after_a_fee_change_only(project):
    root, data, _ = project
    plan = small_plan()
    R.run(root, data, plan, "validation")
    test = R.run(root, data, plan, "test")
    folder = R.out_dir(data)
    cells = {(c["signal"], c["time"], c["delay_s"], c["plan"], c["slice"], c["variant"]): c
             for c in test["cells"]}  # fmt: skip
    with pytest.raises(R.NotAllowed, match="Nothing to do"):
        R.resummarize(root, data, plan, "test")
    with pytest.raises(R.NotAllowed, match="no train report"):
        R.resummarize(root, data, plan, "train")

    presets = root / "config" / "padre_presets.yaml"
    original = presets.read_bytes()
    swapped = original.replace(b"platform_fee_pct: 1.0", b"platform_fee_pct: 0.0").replace(
        b"platform_fee_tests_pct: [0.0]", b"platform_fee_tests_pct: [1.0]")  # fmt: skip
    presets.write_bytes(swapped)
    with pytest.raises(R.NotAllowed):  # the fee changed after the cuts were frozen
        R.run(root, data, plan, "train")
    files = {p.name: p.read_bytes() for p in (folder / "ledger" / "test").glob("*.parquet")}
    again = R.resummarize(root, data, plan, "test")
    assert {p.name: p.read_bytes() for p in (folder / "ledger" / "test").glob("*.parquet")} == files
    assert again["ledger"] == test["ledger"] and again["made_at"] == test["made_at"]
    assert again["variants"][0]["platform_fee_pct"] == 0.0
    assert again["summarized_again"]["was"]["platform_fee_pct"] == 1.0
    kept = json.loads((folder / again["summarized_again"]["kept"]).read_text(encoding="utf-8"))
    assert kept["cells"] == test["cells"]  # the report it replaced is still there
    # The two fee settings swap places, cell for cell.
    new = {(c["signal"], c["time"], c["delay_s"], c["plan"], c["slice"], c["variant"]): c
           for c in again["cells"]}  # fmt: skip
    for key, cell in new.items():
        other = {"headline": "platform_fee_1", "platform_fee_1": "headline"}.get(key[5])
        if other == "headline":
            was = cells[key[:5] + ("headline",)]
        elif other == "platform_fee_1" and key[:5] + ("platform_fee_0",) in cells:
            was = cells[key[:5] + ("platform_fee_0",)]
        else:
            continue
        assert cell.get("mean_pnl_sol") == was.get("mean_pnl_sol"), key
    assert ("everything", "all", 5, "hold_5_min", "2026-11-18", "headline") in new
    text = write_results(root, data).read_text(encoding="utf-8")
    assert "built again from the same saved trades" in text and "from 1% to 0%" in text

    # Any other change, or a ledger that is not the one simulated, is refused.
    presets.write_bytes(swapped.replace(b"slippage_pct: 20", b"slippage_pct: 30"))
    with pytest.raises(R.NotAllowed, match="trade setting"):
        R.resummarize(root, data, plan, "test")
    presets.write_bytes(original)
    first = sorted((folder / "ledger" / "test").glob("*.parquet"))[0]
    first.write_bytes(first.read_bytes() + b" ")
    with pytest.raises(R.NotAllowed, match="ledger"):
        R.resummarize(root, data, plan, "test")


def test_the_reports_numbers_are_the_ledgers(project):
    root, data, fake = project
    plan = small_plan()
    report = R.run(root, data, plan, "validation")
    frame = fake.frames["validation"]
    presets, _ = B.load_presets(root / "config" / "padre_presets.yaml")
    assert report["rows"] == len(frame) == 80 and report["coins"] == 40
    assert report["ledger"]["rows"] == 80 * 3 * 3 and report["base_fee_lamports"] == 5_000
    cells = {(c["signal"], c["time"], c["delay_s"], c["plan"], c["slice"], c["variant"]): c
             for c in report["cells"]}  # fmt: skip
    assert len(cells) == len(report["cells"])
    launch = type(LAUNCH)([(0, False, 95, 30), (0, True, 95, 30)])

    def by_hand(rows: pd.DataFrame, delay: int, plan_name: str, **how) -> list[float]:
        out = []
        for mint, offset in rows[["mint", "offset_s"]].itertuples(index=False):
            made = B.simulate_signal(fake.market(mint), int(offset), delay, presets, launch)
            row = next(r for r in made if r["plan"] == plan_name)
            out.append(float(B.outcomes({k: [v] for k, v in row.items()}, presets,
                                        base_fee_lamports=5_000, **how)["pnl_sol"][0]))  # fmt: skip
        return out

    head = {"failed_rate": 0.05, "footprint": "none", "platform_fee_pct": 1.0}
    everything = cells[("everything", "all", 5, "hold_5_min", "all", "headline")]
    want = by_hand(frame, 5, "hold_5_min", **head)
    assert everything["n"] == 80
    assert everything["mean_pnl_sol"]["value"] == pytest.approx(np.mean(want))
    assert everything["total_pnl_sol"] == pytest.approx(np.sum(want))
    assert everything["median_pnl_sol"] == pytest.approx(np.median(want))
    e = everything["mean_pnl_sol"]
    assert e["low"] <= e["value"] <= e["high"]
    assert sum(everything["exits"].values()) == pytest.approx(everything["bought"])

    # A rules signal at one decision time, another delay and plan, mayhem coins only.
    rows = frame[(frame["rules_verdict"] != "avoid") & (frame["offset_s"] == 60)
                 & (frame["group"] == "mayhem")]  # fmt: skip
    cell = cells[("rules_not_avoid", 60, 10, "double_or_half", "mayhem", "headline")]
    assert cell["n"] == len(rows) > 0
    assert cell["mean_pnl_sol"]["value"] == pytest.approx(
        np.mean(by_hand(rows, 10, "double_or_half", **head)))  # fmt: skip
    # The generous view of our own buy, and no failed orders.
    stays = cells[("everything", "all", 5, "hold_5_min", "all", "footprint_stays")]
    assert stays["mean_pnl_sol"]["value"] == pytest.approx(
        np.mean(by_hand(frame, 5, "hold_5_min", **(head | {"footprint": "stays"}))))
    assert stays["mean_pnl_sol"]["value"] > everything["mean_pnl_sol"]["value"]
    none_fail = cells[("everything", "all", 5, "hold_5_min", "all", "failed_0")]
    assert none_fail["mean_pnl_sol"]["value"] == pytest.approx(
        np.mean(by_hand(frame, 5, "hold_5_min", **(head | {"failed_rate": 0.0}))))
    no_fee = cells[("everything", "all", 5, "hold_5_min", "all", "platform_fee_0")]
    assert no_fee["mean_pnl_sol"]["value"] > everything["mean_pnl_sol"]["value"] + 0.01
    assert no_fee["mean_pnl_sol"]["value"] == pytest.approx(
        np.mean(by_hand(frame, 5, "hold_5_min", **(head | {"platform_fee_pct": 0.0}))))
    # A coin's first signal: every coin once, at the first decision time.
    first = cells[("everything", "first", 5, "hold_5_min", "all", "headline")]
    assert first["n"] == 40
    assert first["mean_pnl_sol"]["value"] == pytest.approx(
        np.mean(by_hand(frame[frame["offset_s"] == 30], 5, "hold_5_min", **head)))
    # Ordinary and mayhem coins add up to all coins.
    parts = [cells[("everything", "all", 5, "hold_5_min", s, "headline")]
             for s in ("ordinary", "mayhem")]  # fmt: skip
    assert sum(p["n"] for p in parts) == 80
    assert sum(p["total_pnl_sol"] for p in parts) == pytest.approx(everything["total_pnl_sol"])
    # The ledger's files are recorded with their hashes, and building again gives the same.
    again = R.run(root, data, plan, "validation")
    assert again["ledger"]["files"] == report["ledger"]["files"]
    assert len(next(iter(report["ledger"]["files"].values()))) == 64
