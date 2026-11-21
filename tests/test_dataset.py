"""The research dataset (spec 15.3, R2): one row per coin and cutoff, signals from the live
code replayed on the backfill only, the no-lookahead rule on the builder (spec 6), and
the same files from the same data."""

from __future__ import annotations

import ast
import json
import shutil
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from agent.dataset.build import ID_COLUMNS, LABEL_PREFIX, RULE_COLUMNS, build_dataset
from agent.features.token_state import NATIVE_SOL, SNAPSHOT_OFFSETS_S, CreatorIndex, TokenState
from agent.models.rules import judge, load_rules
from agent.sim.curve_math import (
    Curve,
    FeeRates,
    buy_with_budget,
    mayhem_agent_address,
    sell_tokens,
)

ROOT = Path(__file__).resolve().parents[1]
SUPPLY = 10**15
START = Curve(30 * 10**9, 1_073_000_000 * 10**6, 0, 793_100_000 * 10**6)
RATES = FeeRates(95, 30)
T0, SLOT0 = 1_000_000, 500_000
END = T0 + 5_000  # the backfill covers [T0 - 100, END]
AGENT = mayhem_agent_address()


class Coin:
    """A coin's create row and its trades, with reserves that follow the curve math."""

    def __init__(self, mint: str, created: int, *, creator: str = "DEV",
                 mayhem: bool = False) -> None:  # fmt: skip
        self.mint, self.created, self.slot0 = mint, created, SLOT0 + (created - T0) * 3
        self.curve, self.trades, self.n = START, [], 0
        self.token = {
            "mint": mint, "ts": created, "slot": self.slot0, "signature": f"create-{mint}",
            "event_index": 0, "creator": creator, "token_total_supply": SUPPLY,
            "quote_mint": NATIVE_SOL, "is_mayhem_mode": mayhem,
            "virtual_quote_reserves": START.virtual_quote,
            "virtual_token_reserves": START.virtual_token,
            "real_token_reserves": START.real_token,
        }  # fmt: skip

    def buy(self, wallet: str, sol: float, after_s: int) -> dict:
        return self._add(wallet, True, buy_with_budget(self.curve, int(sol * 1e9), RATES),
                         after_s)  # fmt: skip

    def sell(self, wallet: str, tokens: int, after_s: int) -> dict:
        return self._add(wallet, False, sell_tokens(self.curve, tokens, RATES), after_s)

    def _add(self, wallet, is_buy, fill, after_s) -> dict:
        self.curve, self.n = fill.after, self.n + 1
        c = fill.after
        row = {
            "mint": self.mint, "slot": self.slot0 + after_s * 3, "bf_tx_index": self.n,
            "ts": self.created + after_s, "signature": f"{self.mint}-{self.n:04d}",
            "event_index": 0, "wallet": wallet, "is_buy": is_buy,
            "quote_amount": fill.curve_quote, "token_amount": fill.tokens,
            "virtual_quote_reserves": c.virtual_quote, "virtual_token_reserves": c.virtual_token,
            "real_quote_reserves": c.real_quote, "real_token_reserves": c.real_token,
            "fee_basis_points": RATES.protocol_bps, "creator_fee_basis_points": RATES.creator_bps,
            "creator": self.token["creator"], "source": "backfill",
        }  # fmt: skip
        self.trades.append(row)
        return row


def market() -> list[Coin]:
    """Five coins: an ordinary launch, a second launch by the same creator, a mayhem coin,
    one too close to the end for labels, and one whose T+300 s is past the backfill."""
    m = Coin("M", T0)
    m.buy("DEV", 1.0, 0)
    m.buy("SNIPER", 2.0, 0)
    for i in range(6):
        m.buy(f"B{i}", 0.3 + i / 10, 5 + i * 20)
    m.sell("DEV", 10**13, 70)
    m.buy("LATE", 0.5, 2_000)
    again = Coin("M2", T0 + 200)
    again.buy("DEV", 0.5, 0)
    again.buy("X", 0.2, 40)
    may = Coin("MAY", T0 + 400, creator="DEV2", mayhem=True)
    may.buy(AGENT, 0.4, 1)
    may.buy("Y", 0.2, 3)
    may.sell(AGENT, 10**12, 50)
    late = Coin("LATE1", T0 + 4_000, creator="DEV3")
    late.buy("Z", 0.3, 10)
    edge = Coin("EDGE", T0 + 4_800, creator="DEV4")
    edge.buy("Z", 0.3, 10)
    return [m, again, may, late, edge]


def _write(data: Path, root: str, table: str, rows: list[dict], name: str = "e0") -> None:
    folder = data / root / table / "date=2026-11-12"
    folder.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), folder / f"{name}.parquet")


def project(tmp_path: Path, coins: list[Coin], *, completes=(), extra=()) -> tuple[Path, Path]:
    """A project folder (with the real config files) and a data folder holding a
    backfill of `coins` (plus `extra` trade rows, in a file of their own)."""
    root, data = tmp_path / "proj", tmp_path / "data"
    if not root.exists():
        (root / "config").mkdir(parents=True)
        for name in ("rules.yaml", "labels.yaml"):
            shutil.copy(ROOT / "config" / name, root / "config" / name)
    progress = data / "backfill" / "epoch-1084" / "progress.jsonl"
    progress.parent.mkdir(parents=True, exist_ok=True)
    progress.write_text(json.dumps({"kind": "commit", "chunks": [0], "failed": {}, "files": [],
                                    "times": {"0": [T0 - 100, END]}}) + "\n",
                        encoding="utf-8")  # fmt: skip
    _write(data, "raw_backfill", "tokens", [c.token for c in coins])
    _write(data, "raw_backfill", "trades", [t for c in coins for t in c.trades])
    if completes:
        _write(data, "raw_backfill", "curve_completes", list(completes))
    if extra:
        _write(data, "raw_backfill", "trades", list(extra), name="e1")
    return root, data


def rows_of(data: Path) -> list[dict]:
    return pq.read_table(data / "dataset" / "dataset.parquet").to_pylist()


def test_one_row_per_coin_and_cutoff_with_signals_rules_and_labels(tmp_path):
    coins = market()
    root, data = project(tmp_path, coins)
    # The live recording has a big buy of M that the backfill does not: never read.
    _write(data, "raw", "trades", [{**coins[0].trades[2], "signature": "live-only",
                                    "quote_amount": 50 * 10**9, "source": "recorder"}])
    run = build_dataset(root, data)
    rows = rows_of(data)
    keys = [(r["mint"], r["offset_s"]) for r in rows]
    assert len(rows) == len(coins) * len(SNAPSHOT_OFFSETS_S) == len(set(keys))
    assert keys[:4] == [("M", o) for o in SNAPSHOT_OFFSETS_S]  # launch order, then cutoff
    assert [k[0] for k in keys[::4]] == ["M", "M2", "MAY", "LATE1", "EDGE"]

    # Signals are the live code's snapshot of the backfilled trades, rules as live.
    creators = CreatorIndex()
    for c in coins:
        creators.add(c.token["creator"], c.created, c.mint)
    rules = load_rules(root / "config" / "rules.yaml")
    for coin in coins:
        state = TokenState(coin.token)
        for t in coin.trades:
            state.add_trade(t)
        for offset in SNAPSHOT_OFFSETS_S:
            row = next(r for r in rows if r["mint"] == coin.mint and r["offset_s"] == offset)
            expected = state.snapshot(coin.created + offset, creators=creators)
            assert {k: row[k] for k in expected} == expected, (coin.mint, offset)
            verdict = judge(expected, rules)
            assert row["rules_verdict"] == verdict.level
            assert row["rules_reasons"] == "; ".join(verdict.reasons)
    m60 = next(r for r in rows if r["mint"] == "M" and r["offset_s"] == 60)
    assert m60["buy_sol"] < 10 and m60["dev_prior_launches"] == 0  # no live-only buy
    m2 = next(r for r in rows if r["mint"] == "M2")
    assert m2["dev_prior_launches"] == 1 and m2["history_s"] == 300

    # Labels, all named label_..., from the coin and from the entry at the same cutoff.
    coin_labels = {r["mint"]: r for r in pq.read_table(data / "labels" / "coins.parquet")
                   .to_pylist()}  # fmt: skip
    for r in rows:
        assert r["labels_complete"] == coin_labels[r["mint"]]["complete"]
        assert r["label_is_dead"] == coin_labels[r["mint"]]["is_dead"]
    assert m60["labels_complete"] and m60["label_is_dead"] is False
    assert m60["label_entry_ts"] == T0 + 60 + 5 and m60["label_venue"] == "curve"
    assert next(r for r in rows if r["mint"] == "LATE1")["labels_complete"] is False

    # The edge of the backfill: T+300 s of EDGE is past it.
    edge = {r["offset_s"]: r["has_gap"] for r in rows if r["mint"] == "EDGE"}
    assert edge == {30: False, 60: False, 120: False, 300: True}
    assert not any(r["has_gap"] for r in rows if r["mint"] != "EDGE")

    m = run.manifest
    assert m["rows"] == len(rows) and m["coins"] == 5 and m["epochs"] == [1084]
    assert m["usable_rows"] == sum(not r["has_gap"] and r["labels_complete"] for r in rows)
    assert m["source"] == "raw_backfill" and m["backfill_spans"] == [(T0 - 100, END)]
    groups = m["columns"]
    names = list(rows[0])
    assert sorted(sum(groups.values(), [])) == sorted(names)  # every column, once
    assert groups["ids"] == list(ID_COLUMNS) and groups["rules"] == list(RULE_COLUMNS)
    assert all(c.startswith(LABEL_PREFIX) for c in groups["labels"][1:])
    assert not any(c.startswith(LABEL_PREFIX) for c in groups["features"])
    assert set(m["code"]) == {"git_commit", "uncommitted_changes", "source_sha"}
    assert len(m["rules_sha"]) == 12 and m["labels_settings_sha"]
    on_disk = json.loads((data / "dataset" / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk["file_sha256"] == m["file_sha256"]


def test_rebuilding_gives_identical_output(tmp_path):
    root, data = project(tmp_path, market())
    first = build_dataset(root, data)
    before = (data / "dataset" / "dataset.parquet").read_bytes()
    second = build_dataset(root, data)
    assert (data / "dataset" / "dataset.parquet").read_bytes() == before
    assert second.manifest["file_sha256"] == first.manifest["file_sha256"]
    assert first.labels_rebuilt and not second.labels_rebuilt  # labels reused when current


def _future(coins: list[Coin], after_s: int) -> tuple[list[dict], list[dict], list[dict]]:
    """A busy future for every coin, all of it after `after_s`: buys and sells by the
    creator, snipers, new wallets and the mayhem agent, the curve completing, and a new
    launch by each creator (after every coin's launch, so it is no coin's past)."""
    trades, completes, launches = [], [], []
    last = max(c.created for c in coins)
    for coin in coins:
        clone = Coin(coin.mint, coin.created, creator=coin.token["creator"])
        clone.curve, clone.n = coin.curve, 500
        for i in range(40):
            at = after_s + 1 + i * 7
            if i % 5 == 0:
                clone.sell(coin.token["creator"], 10**11, at)
            elif i % 5 == 1:
                clone.buy("SNIPER", 1.5, at)
            elif i % 5 == 2:
                clone.buy(AGENT, 0.7, at)
            else:
                clone.buy(f"NEW{i}", 3.0, at)
        trades += clone.trades
        completes.append({"mint": coin.mint, "ts": coin.created + after_s + 2})
        launches.append(Coin(f"{coin.mint}-next", last + after_s + 3,
                             creator=coin.token["creator"]))  # fmt: skip
    return trades, completes, launches


@pytest.mark.parametrize("after_s", SNAPSHOT_OFFSETS_S)
def test_no_lookahead_events_after_a_cutoff_change_nothing_up_to_it(tmp_path, after_s):
    """Spec 6 on the builder: add events after T+`after_s` to every coin and rebuild; every
    column that is not a label stays the same for every cutoff up to T+`after_s`."""
    coins = market()
    root, data = project(tmp_path / "base", coins)
    build_dataset(root, data)
    base = {(r["mint"], r["offset_s"]): r for r in rows_of(data)}

    trades, completes, launches = _future(coins, after_s)
    root, data = project(tmp_path / "future", [*coins, *launches], completes=completes,
                         extra=trades)  # fmt: skip
    build_dataset(root, data)
    changed = 0
    for r in rows_of(data):
        key = (r["mint"], r["offset_s"])
        if key not in base:
            continue  # the new launches
        past = {k: v for k, v in r.items()
                if not k.startswith(LABEL_PREFIX) and k != "labels_complete"}  # fmt: skip
        expected = {k: v for k, v in base[key].items() if k in past}
        if r["offset_s"] <= after_s:
            assert past == expected, key
        elif past != expected:
            changed += 1
    if after_s < max(SNAPSHOT_OFFSETS_S):
        assert changed  # the future did reach the later cutoffs: the test has teeth


def test_labels_are_rebuilt_when_their_settings_or_the_backfill_change(tmp_path):
    root, data = project(tmp_path, market())
    assert build_dataset(root, data).labels_rebuilt
    assert not build_dataset(root, data).labels_rebuilt
    labels_yaml = root / "config" / "labels.yaml"
    labels_yaml.write_text(labels_yaml.read_text(encoding="utf-8") + "\n# changed\n",
                           encoding="utf-8")  # fmt: skip
    assert build_dataset(root, data).labels_rebuilt
    coins = market()
    coins[0].buy("ANOTHER", 0.4, 900)
    project(tmp_path, coins)  # the backfill changed: same stretch, new files
    run = build_dataset(root, data)
    assert run.labels_rebuilt
    m = next(r for r in rows_of(data) if r["mint"] == "M")
    assert m["label_trades_1h"] == len(coins[0].trades)


def test_no_backfill_is_refused(tmp_path):
    root, _ = project(tmp_path, market())
    with pytest.raises(FileNotFoundError):
        build_dataset(root, tmp_path / "empty")


def _imports(folder: Path) -> set[str]:
    names = set()
    for path in folder.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
            elif isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
    return names


def test_nothing_that_makes_signals_reads_the_dataset():
    """The dataset holds labels, so code that makes signals or labels never imports it."""
    src = ROOT / "src" / "agent"
    for part in ("features", "daemon", "tui", "labels", "ingest", "sim", "scoring"):
        assert not any(n.startswith("agent.dataset") for n in _imports(src / part)), part
