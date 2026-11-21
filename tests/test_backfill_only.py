"""Backfill-only signals (spec 4.1): values from full transactions, cut at the cutoff
(spec 6), with the mayhem agent's buys left out."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from agent.features.backfill_only import COLUMNS, backfill_only_signals
from agent.sim.curve_math import mayhem_agent_address

T0 = 1_000_000
AGENT = mayhem_agent_address()


def _token(mint: str, ts: int, *, fee: int = 5_000, signatures: int = 1, index: int = 7) -> dict:
    return {"mint": mint, "ts": ts, "signature": f"create-{mint}", "bf_fee": fee,
            "bf_signatures": signatures, "bf_tx_index": index}  # fmt: skip


def _trade(mint: str, ts: int, n: int, *, wallet: str = "W", is_buy: bool = True,
           fee: int = 5_000, signatures: int = 1, index: int = 10,
           compute: int = 100_000) -> dict:  # fmt: skip
    return {"mint": mint, "ts": ts, "signature": f"{mint}-{n}", "wallet": wallet,
            "is_buy": is_buy, "bf_fee": fee, "bf_signatures": signatures, "bf_tx_index": index,
            "bf_compute_units": compute}  # fmt: skip


def _write(data: Path, table: str, rows: list[dict], name: str = "e0") -> None:
    folder = data / "raw_backfill" / table / "date=2026-11-12"
    folder.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), folder / f"{name}.parquet")


def _signals(data: Path) -> dict[tuple[str, int], dict]:
    table = backfill_only_signals(data, T0 - 10, T0 + 10_000)
    return {(r["mint"], r["offset_s"]): r for r in table.to_pylist()}


def test_values_come_from_the_launch_and_the_buys_up_to_the_cutoff(tmp_path):
    _write(tmp_path, "tokens", [
        _token("A", T0, fee=10_000 + 2_000_000, signatures=2, index=3),  # 0.002 SOL priority
        _token("B", T0 + 50),  # no trades at all
        _token("OUT", T0 + 20_000),  # launched outside the window asked for
    ])  # fmt: skip
    _write(tmp_path, "trades", [
        _trade("A", T0, 1, fee=5_000 + 1_000_000, index=4, compute=90_000),  # 0.001 SOL
        _trade("A", T0 + 30, 2, fee=5_000 + 3_000_000, index=40, compute=110_000),
        _trade("A", T0 + 31, 3, fee=5_000 + 9_000_000_000, index=0),  # after T+30 s
        _trade("A", T0 + 10, 4, wallet=AGENT, fee=5_000 + 5_000_000_000),  # the agent's
        _trade("A", T0 + 12, 5, is_buy=False, fee=5_000 + 7_000_000_000),  # a sell
        _trade("A", T0 + 301, 6, fee=5_000 + 8_000_000_000),  # after the last cutoff
    ])  # fmt: skip
    got = _signals(tmp_path)
    assert sorted(got) == [(m, o) for m in ("A", "B") for o in (30, 60, 120, 300)]
    assert set(got["A", 30]) == {"mint", "offset_s", *COLUMNS}
    a30, a60 = got["A", 30], got["A", 60]
    assert a30["bf_create_priority_sol"] == pytest.approx(0.002) and a30["bf_create_tx_index"] == 3
    assert a30["bf_buy_priority_median_sol"] == pytest.approx(0.002)  # 0.001 and 0.003
    assert a30["bf_buy_priority_max_sol"] == pytest.approx(0.003)
    assert a30["bf_buy_tx_index_median"] == 22 and a30["bf_buy_compute_median"] == 100_000
    assert a60["bf_buy_priority_max_sol"] == pytest.approx(9.0)  # the T+31 s buy now counts
    assert got["A", 300]["bf_buy_priority_max_sol"] == pytest.approx(9.0)  # T+301 s never
    b = got["B", 60]
    assert b["bf_create_priority_sol"] == 0.0 and b["bf_buy_priority_median_sol"] is None


@pytest.mark.parametrize("after_s", (30, 60, 120, 300))
def test_no_lookahead_buys_after_a_cutoff_change_nothing_up_to_it(tmp_path, after_s):
    tokens = [_token("A", T0), _token("B", T0 + 40)]
    trades = [_trade(m, t0 + s, s, fee=5_000 + 1_000 * s, index=s, compute=50_000 + s)
              for m, t0 in (("A", T0), ("B", T0 + 40)) for s in (0, 20, 45, 100, 250)]  # fmt: skip
    base_dir, future_dir = tmp_path / "base", tmp_path / "future"
    for folder in (base_dir, future_dir):
        _write(folder, "tokens", tokens)
        _write(folder, "trades", trades)
    future = [_trade(m, t0 + after_s + 1 + i, 900 + i, wallet=f"NEW{i}",
                     fee=5_000 + 10**10, index=0, compute=1)
              for m, t0 in (("A", T0), ("B", T0 + 40)) for i in range(5)]  # fmt: skip
    _write(future_dir, "trades", future, name="e1")
    base, later = _signals(base_dir), _signals(future_dir)
    changed = 0
    for key, row in later.items():
        if key[1] <= after_s:
            assert row == base[key], key
        else:
            changed += row != base[key]
    if after_s < 300:
        assert changed  # the future did reach the later cutoffs: the test has teeth


def test_a_backfill_with_no_trades_still_gives_the_launch_columns(tmp_path):
    _write(tmp_path, "tokens", [_token("A", T0, fee=6_000)])
    got = _signals(tmp_path)
    assert got["A", 30]["bf_create_priority_sol"] == pytest.approx(1_000 / 1e9)
    assert got["A", 30]["bf_buy_compute_median"] is None
