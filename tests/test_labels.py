"""Labels (spec 7; research track R1): each label on built coins, the backfill's coverage,
an end-to-end build from Parquet, and the wall between labels and features."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

from agent.ingest.backfill_coverage import backfill_gaps, backfill_spans, covered
from agent.labels.make_labels import (
    NATIVE_SOL,
    Coin,
    LabelRun,
    LabelSettings,
    Trade,
    build_labels,
    label_coin,
    load_label_settings,
    summarize,
)
from agent.sim.curve_math import Curve, FeeRates, buy_with_budget, sell_tokens

ROOT = Path(__file__).resolve().parents[1]
AGENT = "MayhemAgent1111111111111111111111111111111"
SUPPLY = 10**15
START = Curve(30 * 10**9, 1_073_000_000 * 10**6, 0, 793_100_000 * 10**6)
RATES = FeeRates(95, 30)
T0 = 1_000_000
S = LabelSettings()


class Book:
    """A coin's trades in time order: curve trades follow the curve math; `at` adds a
    trade at a chosen price (as a pool trade would be)."""

    def __init__(self) -> None:
        self.curve, self.trades = START, []

    def buy(self, wallet: str, sol: float, ts: int) -> Trade:
        fill = buy_with_budget(self.curve, int(sol * 1e9), RATES)
        return self._curve_trade(wallet, True, fill, ts)

    def sell(self, wallet: str, tokens: int, ts: int) -> Trade:
        return self._curve_trade(wallet, False, sell_tokens(self.curve, tokens, RATES), ts)

    def _curve_trade(self, wallet, is_buy, fill, ts) -> Trade:
        self.curve = fill.after
        return self._add(Trade(order=(len(self.trades), 0, 0), ts=ts, wallet=wallet,
                               is_buy=is_buy, quote=fill.curve_quote, tokens=fill.tokens,
                               after=fill.after, rates=RATES))  # fmt: skip

    def at(self, price: float, ts: int, wallet: str = "PERSON", tokens: int = 10**12) -> Trade:
        return self._add(Trade(order=(len(self.trades), 0, 0), ts=ts, wallet=wallet,
                               is_buy=True, quote=round(price * tokens), tokens=tokens,
                               on_curve=False))  # fmt: skip

    def _add(self, trade: Trade) -> Trade:
        self.trades.append(trade)
        return trade

    def coin(self, **changes) -> Coin:
        fields = {"mint": "M", "creator": "DEV", "created_ts": T0, "supply": SUPPLY,
                  "quote_mint": NATIVE_SOL, "is_mayhem": False, "start": START,
                  "trades": self.trades, **changes}  # fmt: skip
        return Coin(**fields)


def labels(coin: Coin, settings: LabelSettings = S, *, complete=True):
    return label_coin(coin, settings, AGENT, complete=complete)


def test_the_settings_file_loads_and_typos_are_refused(tmp_path):
    settings, sha = load_label_settings(ROOT / "config" / "labels.yaml")
    assert settings == LabelSettings() and len(sha) == 12  # the file states the defaults
    assert settings.horizon_s == 300 + 5 + 3600
    bad = tmp_path / "labels.yaml"
    bad.write_text("dead:\n  quiet_after: 1800\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_label_settings(bad)


def test_dead_means_no_trades_after_30_minutes():
    book = Book()
    book.buy("DEV", 1.0, T0)
    book.buy("A", 0.5, T0 + 1_700)
    row, _ = labels(book.coin())
    assert row["is_dead"] and row["is_dead_without_agent"] and row["trades_1h"] == 2
    book.at(3e-5, T0 + 3_000, wallet=AGENT)  # only the mayhem agent trades late
    row, _ = labels(book.coin())
    assert not row["is_dead"] and row["is_dead_without_agent"] and row["agent_trades_1h"] == 1
    book.buy("B", 0.1, T0 + 1_801)
    row, _ = labels(book.coin())
    assert not row["is_dead"] and not row["is_dead_without_agent"]
    book2 = Book()
    book2.buy("DEV", 1.0, T0)
    book2.at(3e-5, T0 + 3_601)  # after the hour: does not count
    assert labels(book2.coin())[0]["is_dead"]


def test_graduated_within_the_hour():
    book = Book()
    book.buy("DEV", 1.0, T0)
    row, _ = labels(book.coin(completed_ts=T0 + 900))
    assert row["graduated_1h"] and row["graduated_after_s"] == 900
    row, _ = labels(book.coin(completed_ts=T0 + 3_700))
    assert not row["graduated_1h"] and row["graduated_after_s"] is None


def test_a_rug_needs_the_creator_selling_and_an_80_percent_fall():
    book = Book()
    dev = book.buy("DEV", 1.0, T0)
    for i in range(5):
        book.buy(f"B{i}", 2.0, T0 + 10 + i)
    book.sell("DEV", dev.tokens, T0 + 60)
    peak = max(t.price for t in book.trades)  # the dev's sell starts at the top
    book.at(peak * 0.15, T0 + 90)
    row, _ = labels(book.coin())
    assert row["creator_sold_share"] == 1.0 and row["max_drawdown"] == pytest.approx(0.85)
    assert row["is_rug"] and row["bad"]
    assert row["peak_multiple"] == pytest.approx(peak / dev.price)

    held = Book()  # same fall, but the creator sold only 40%
    dev = held.buy("DEV", 1.0, T0)
    for i in range(5):
        held.buy(f"B{i}", 2.0, T0 + 10 + i)
    held.sell("DEV", dev.tokens * 4 // 10, T0 + 60)
    held.at(peak * 0.15, T0 + 90)
    row, _ = labels(held.coin())
    assert row["creator_sold_share"] == pytest.approx(0.4) and not row["is_rug"]


def test_bundled_stand_in_counts_first_second_buyers_still_holding_at_60_s():
    book = Book()
    book.buy("DEV", 1.0, T0)  # the creator does not count
    s1 = book.buy("S1", 6.0, T0)
    book.buy("S2", 6.0, T0 + 1)
    book.buy(AGENT, 4.0, T0)  # nor does the mayhem agent
    book.buy("LATE", 4.0, T0 + 2)  # first buy after the first second
    row, _ = labels(book.coin())
    assert row["early_buyers"] == 2
    expected = (s1.tokens + book.trades[2].tokens) * 100 / SUPPLY
    assert row["early_buyer_hold_pct"] == pytest.approx(expected) and expected >= 20
    assert row["is_bundled_proxy"] and row["bad"]
    book.sell("S1", s1.tokens, T0 + 30)  # sold before T+60 s
    row, _ = labels(book.coin())
    assert not row["is_bundled_proxy"]


def _entry_price(*trades_before: tuple[str, float, int]) -> float:
    book = Book()
    for wallet, sol, ts in trades_before:
        book.buy(wallet, sol, ts)
    fill = buy_with_budget(book.curve, 10**9, RATES)
    return fill.curve_quote / fill.tokens


def test_hit_2x_is_a_race_between_2x_and_half_after_a_1_sol_entry():
    book = Book()
    book.buy("DEV", 1.0, T0)
    entry = _entry_price(("DEV", 1.0, T0))
    book.at(entry * 2.1, T0 + 34)  # before the entry at T+35 s: does not count
    book.at(entry * 0.9, T0 + 100)
    book.at(entry * 2.05, T0 + 200)
    book.at(entry * 0.3, T0 + 300)
    _, entries = labels(book.coin())
    assert [e["offset_s"] for e in entries] == [30, 60, 120, 300]
    e30 = entries[0]
    assert e30["entry_ts"] == T0 + 35 and e30["venue"] == "curve"
    assert e30["entry_price"] == pytest.approx(entry)
    assert e30["entry_impact_pct"] > 0 and e30["entry_capped"] is False
    assert e30["first_hit"] == "up" and e30["hit_2x"] and e30["seconds_to_hit"] == 165
    assert e30["max_multiple"] == pytest.approx(2.05) and e30["path_trades"] == 3
    assert entries[1]["hit_2x"] and entries[2]["hit_2x"]  # entries at T+65 s and T+125 s
    # From T+300 s the entry is at T+305 s and nothing trades after it.
    assert entries[3]["first_hit"] is None and entries[3]["hit_2x"] is False
    assert entries[3]["path_trades"] == 0

    down_first = Book()
    down_first.buy("DEV", 1.0, T0)
    down_first.at(entry * 0.49, T0 + 50)
    down_first.at(entry * 3, T0 + 60)
    _, entries = labels(down_first.coin())
    assert entries[0]["first_hit"] == "down" and entries[0]["hit_2x"] is False


def test_the_mayhem_agents_prices_are_left_out_but_also_given():
    book = Book()
    book.buy("DEV", 1.0, T0)
    entry = _entry_price(("DEV", 1.0, T0))
    book.at(entry * 2.5, T0 + 50, wallet=AGENT)
    book.at(entry * 0.3, T0 + 60)
    _, entries = labels(book.coin(is_mayhem=True))
    assert entries[0]["first_hit"] == "down"
    assert entries[0]["first_hit_with_agent_prices"] == "up"
    included = LabelSettings.model_validate({"prices": {"mayhem_agent": "include"}})
    _, entries = labels(book.coin(is_mayhem=True), included)
    assert entries[0]["first_hit"] == "up"


def test_tiny_trades_are_not_prices():
    book = Book()
    book.buy("DEV", 1.0, T0)
    entry = _entry_price(("DEV", 1.0, T0))
    book.at(entry * 5, T0 + 50, tokens=10)  # a dust trade: quote far under 0.001 SOL
    _, entries = labels(book.coin())
    assert entries[0]["hit_2x"] is False and entries[0]["path_trades"] == 0


def test_a_coin_graduated_before_the_entry_is_entered_at_the_last_price():
    book = Book()
    book.buy("DEV", 1.0, T0)
    last = book.at(4e-5, T0 + 20)
    book.at(4e-5 * 2.2, T0 + 80)
    _, entries = labels(book.coin(completed_ts=T0 + 20))
    assert entries[0]["venue"] == "pool" and entries[0]["entry_price"] == last.price
    assert entries[0]["entry_impact_pct"] is None and entries[0]["hit_2x"]


def test_coins_not_paired_with_sol_get_no_price_labels():
    book = Book()
    book.buy("DEV", 1.0, T0)
    row, entries = labels(book.coin(quote_mint="OtherCoin111"))
    assert entries == [] and row["is_rug"] is None and row["max_drawdown"] is None
    assert row["is_dead"] is True and row["bad"] is True  # dead is known either way
    book.buy("B", 0.2, T0 + 2_000)
    row, _ = labels(book.coin(quote_mint="OtherCoin111"))
    assert row["is_dead"] is False and row["bad"] is None  # rug unknown


def test_coins_too_close_to_the_edge_of_the_data_get_empty_labels():
    book = Book()
    book.buy("DEV", 1.0, T0)
    row, entries = labels(book.coin(), complete=False)
    assert row["complete"] is False and entries == []
    assert row["is_dead"] is None and row["graduated_1h"] is None


def _progress(folder: Path, epoch: int, chunks: list[int], times: dict[int, list]) -> None:
    path = folder / "backfill" / f"epoch-{epoch}" / "progress.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {"kind": "commit", "chunks": chunks, "failed": {}, "files": [],
            "times": {str(k): v for k, v in times.items()}}  # fmt: skip
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")


def test_backfill_spans_join_chunks_and_epochs_and_break_at_missing_chunks(tmp_path):
    _progress(tmp_path, 1, [0, 1, 2], {0: [100, 200], 1: [201, 300], 2: [301, 400]})
    _progress(tmp_path, 2, [0, 1, 3, 4], {0: [420, 500], 1: [501, 600], 3: [700, 800],
                                          4: [None, None]})  # fmt: skip
    spans = backfill_spans(tmp_path)
    assert spans == [(100, 600), (700, 800)]
    assert covered(spans, 150, 550) and not covered(spans, 550, 650)
    assert backfill_spans(tmp_path / "nothing") == []
    gaps = backfill_gaps(spans)  # the same coverage, as gap rows
    assert [(g["start_ts"], g["end_ts"]) for g in gaps] == [
        (float("-inf"), 100), (600, 700), (800, float("inf"))]


def _write(root: Path, table: str, rows: list[dict]) -> None:
    folder = root / "raw_backfill" / table / "date=2026-11-12"
    folder.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), folder / "part-0.parquet")


def test_build_labels_end_to_end_from_backfilled_parquet(tmp_path):
    """Two coins: one with a full hour of data that graduates and keeps trading in its
    PumpSwap pool; one launched too close to the end of the data."""
    _progress(tmp_path, 1084, [0], {0: [T0 - 100, T0 + 5_000]})
    token = {"signature": "c", "creator": "DEV", "token_total_supply": SUPPLY,
             "quote_mint": NATIVE_SOL, "is_mayhem_mode": False,
             "virtual_quote_reserves": START.virtual_quote,
             "virtual_token_reserves": START.virtual_token,
             "real_token_reserves": START.real_token}  # fmt: skip
    _write(tmp_path, "tokens", [token | {"mint": "M", "ts": T0},
                                token | {"mint": "LATE", "ts": T0 + 4_000}])  # fmt: skip
    live = tmp_path / "raw" / "tokens" / "date=2026-11-12"
    live.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist([token | {"mint": "LIVE", "ts": T0 + 50}]),
                   live / "part-0.parquet")  # fmt: skip
    book = Book()
    trades = []
    for i, (wallet, sol, ts) in enumerate([("DEV", 1.0, T0), ("A", 2.0, T0 + 10)]):
        t = book.buy(wallet, sol, ts)
        c = t.after
        trades.append({
            "mint": "M", "slot": 10 + i, "bf_tx_index": 0, "event_index": 0, "ts": ts,
            "wallet": wallet, "is_buy": True, "quote_amount": t.quote,
            "token_amount": t.tokens, "virtual_quote_reserves": c.virtual_quote,
            "virtual_token_reserves": c.virtual_token, "real_quote_reserves": c.real_quote,
            "real_token_reserves": c.real_token, "fee_basis_points": 95,
            "creator_fee_basis_points": 30, "creator": "DEV",
        })  # fmt: skip
    _write(tmp_path, "trades", trades)
    _write(tmp_path, "curve_completes", [{"mint": "M", "ts": T0 + 600}])
    _write(tmp_path, "migrations", [{"mint": "M", "pool": "POOL", "ts": T0 + 600}])
    pool = {"pool": "POOL", "bf_tx_index": 0, "event_index": 0, "wallet": "P",
            "quote_amount_out": None, "base_amount_in": None}  # fmt: skip
    _write(tmp_path, "amm_trades", [
        pool | {"slot": 900, "ts": T0 + 2_000, "is_buy": True, "quote_amount_in": 10**8,
                "base_amount_out": 10**12},
        pool | {"slot": 950, "ts": T0 + 9_999, "is_buy": True, "quote_amount_in": 10**8,
                "base_amount_out": 10**12},  # past the horizon: not read
    ])  # fmt: skip

    run = build_labels(tmp_path, S, "sha")
    first = (tmp_path / "labels" / "coins.parquet").read_bytes()
    rows = pq.read_table(tmp_path / "labels" / "coins.parquet").to_pylist()
    assert [r["mint"] for r in rows] == ["M", "LATE"]  # in launch order
    build_labels(tmp_path, S, "sha")
    assert (tmp_path / "labels" / "coins.parquet").read_bytes() == first  # same data, same file
    coins = {r["mint"]: r for r in rows}
    assert coins["M"]["complete"] and coins["M"]["graduated_1h"]
    assert coins["M"]["is_dead"] is False and coins["M"]["trades_1h"] == 3  # the pool trade
    assert coins["LATE"]["complete"] is False and coins["LATE"]["is_dead"] is None
    entries = pq.read_table(tmp_path / "labels" / "entries.parquet").to_pylist()
    assert [e["offset_s"] for e in entries] == [30, 60, 120, 300]
    assert {e["mint"] for e in entries} == {"M"} and entries[0]["venue"] == "curve"
    manifest = json.loads((tmp_path / "labels" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["coins"] == 2 and manifest["complete_coins"] == 1
    assert manifest["settings_sha"] == "sha" and run.spans == [(T0 - 100, T0 + 5_000)]
    assert "LIVE" not in coins  # labels never read the live recording (raw/)
    summary = summarize(run)
    ordinary = summary["groups"]["ordinary"]
    assert summary["complete"] == 1 and ordinary["coins"] == 1
    assert ordinary["rates"]["graduated_1h"] == 1.0 and ordinary["rates"]["is_dead"] == 0.0
    assert set(ordinary["hit_2x"]) == {30, 60, 120, 300}
    assert summary["groups"]["mayhem"]["coins"] == 0
    assert summary["groups"]["mayhem"]["rates"]["is_dead"] is None


def test_the_bad_rate_leaves_out_coins_whose_rug_label_is_unknown():
    dead_other = {"mint": "D", "complete": True, "is_sol_paired": False, "is_mayhem": False,
                  "is_dead": True, "is_dead_without_agent": True, "is_rug": None,
                  "is_bundled_proxy": False, "bad": True, "graduated_1h": False}  # fmt: skip
    alive_other = dead_other | {"mint": "A", "is_dead": False, "is_dead_without_agent": False,
                                "bad": None}  # fmt: skip
    run = LabelRun([dead_other, alive_other], [], [(0, 1)], "sha", 0.0)
    rates = summarize(run)["groups"]["other quote"]["rates"]
    assert rates["bad"] is None and rates["is_dead"] == 0.5


def _imports(folder: Path) -> set[str]:
    names = set()
    for path in folder.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
            elif isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
    return names


def test_labels_and_features_never_import_each_other():
    """Labels look into the future, so nothing that makes signals may read them."""
    src = ROOT / "src" / "agent"
    for part in ("features", "daemon", "models", "tui", "scoring"):
        assert not any(n.startswith("agent.labels") for n in _imports(src / part)), part
    assert not any(n.startswith(("agent.features", "agent.daemon"))
                   for n in _imports(src / "labels"))  # fmt: skip
