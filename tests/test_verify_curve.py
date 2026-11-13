"""The Phase 2 check (`agent verify-curve`) on a small recording of real trades."""

import pytest
from typer.testing import CliRunner

from agent.cli.agent import app
from agent.ingest.pipeline import Pipeline
from agent.sim.chain_config import save_snapshot
from agent.sim.verify import kind_of, verify
from helpers import live, make_project
from test_chain_config import KINDS, fixture_snapshot

TRADES = [
    "pump-BuyV2", "pump-BuyV3", "pump-BuyExactSolIn", "pump-BuyExactQuoteInV3", "pump-Sell",
    "pump-SellV2", "pump-SellV3", "pump-Buy", "pump-MultiHopCurveSwap",
]  # fmt: skip


@pytest.fixture
def recording(tmp_path):
    data = tmp_path / "data"
    pipeline = Pipeline(data, data / "run")
    for name in TRADES:
        pipeline.handle(live(name))
    pipeline.close()
    return data


def test_every_recorded_trade_is_reproduced(recording):
    result = verify(recording, 0, 2e9, KINDS, [fixture_snapshot()])
    assert result.agent_trades == 1  # pump-Buy is the mayhem agent's
    assert result.total.trades == len(TRADES) - 1
    assert result.total.exact == result.total.trades
    assert result.passed
    assert set(result.buckets) == {
        "buy, exact tokens", "buy, set amount of SOL", "buy, multi-hop route", "sell"
    }  # fmt: skip
    # Fee schedule checked only for the coin created in the recording (pump-BuyV2): its
    # creator's first buy and a later buy (pump-BuyExactSolIn).
    assert result.schedule == {"SOL-paired": [2, 2]}
    # The first buy starts from the coin's creation. Trades between the two buys were not
    # captured as fixtures, so the later one does not start from a recorded state.
    assert result.continuity == {"other coins": [2, 1]}


def test_a_wrong_amount_fails_the_check(recording, monkeypatch):
    import agent.sim.verify as module

    real_replay = module.replay

    def off_by_a_lot(trade):
        fill = real_replay(trade)
        return type(fill)(**{**{f: getattr(fill, f) for f in fill.__slots__},
                             "curve_quote": fill.curve_quote + 1000})  # fmt: skip

    monkeypatch.setattr(module, "replay", off_by_a_lot)
    result = verify(recording, 0, 2e9, KINDS, [])
    assert not result.passed and result.total.within == 0
    assert all(b.examples for b in result.buckets.values())
    assert result.schedule == {}  # no snapshot: not checked


def test_trade_kinds():
    assert kind_of({"is_buy": False, "ix_name": "sell"}) == "sell"
    assert kind_of({"is_buy": True, "ix_name": "buy"}) == "buy, exact tokens"
    assert kind_of({"is_buy": True, "ix_name": "buy_exact_quote_in"}) == "buy, set amount of SOL"
    assert kind_of({"is_buy": True, "ix_name": "multi_hop_swap"}) == "buy, multi-hop route"


def test_cli_reports_pass(recording, tmp_path, monkeypatch):
    project = tmp_path / "project"
    make_project(project, env=f"DATA_DIR={recording}\n")
    (project / "config" / "settings.yaml").write_text(
        (project / "config" / "settings.yaml").read_text()
        + "chain:\n  sol_like_quote_mints: ['11111111111111111111111111111111']\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_HOME", str(project))
    monkeypatch.delenv("DATA_DIR", raising=False)
    save_snapshot(recording, fixture_snapshot(0.0))
    result = CliRunner().invoke(app, ["verify-curve", "--hours", "1000"])
    assert result.exit_code == 0, result.output
    assert "Result: PASS" in result.output
    assert "Not compared: 1 trades by pump.fun's mayhem agent" in result.output
    assert "SOL-paired: 2 of 2 (100.00%)" in result.output
