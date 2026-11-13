"""Chain snapshots of pump.fun's parameters, and the fee schedule built from them."""

import base64
import json
from pathlib import Path

import pytest

from agent.config import load_config
from agent.ingest.borsh_idl import Idl
from agent.sim.chain_config import (
    QuoteKinds,
    Schedule,
    account_addresses,
    load_snapshots,
    save_snapshot,
    snapshot_at,
)
from agent.sim.curve_math import NO_CREATOR, Curve, FeeRates

ACCOUNTS = Path(__file__).parent / "fixtures" / "accounts"
REPO = Path(__file__).resolve().parents[1]
SETTINGS = load_config(REPO).settings.chain
KINDS = QuoteKinds(frozenset(SETTINGS.sol_like_quote_mints), frozenset(SETTINGS.stable_quote_mints))
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PUMP_COIN = "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn"
CREATOR = "Dt6ANEGay7pGgbKrXxC5cPCS1XafR5v3q8LnM82ceLb3"
CURVE = Curve(42_082_987_392, 764_917_179_596_058, 12_082_987_392, 485_017_179_596_058)


def fixture_snapshot(fetched_at: float = 1794480000.0) -> dict:
    """A snapshot built from the Global and FeeConfig accounts read on 2026-11-12."""
    snapshot = {"fetched_at": fetched_at, "accounts": {}}
    for label, idl in (("global", Idl.load("pump")), ("fee_config", Idl.load("pump_fees"))):
        saved = json.loads((ACCOUNTS / f"{label}.json").read_text(encoding="utf-8"))
        decoded = idl.decode_account(base64.b64decode(saved["data_base64"]))
        snapshot["accounts"][label] = {"address": saved["address"], "values": decoded.values}
    return snapshot


def test_account_addresses_come_from_the_idl():
    addresses = {label: address for label, (address, _) in account_addresses().items()}
    assert addresses == {
        "global": "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf",
        "fee_config": "8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt",
    }
    for label, (address, _) in account_addresses().items():
        saved = json.loads((ACCOUNTS / f"{label}.json").read_text(encoding="utf-8"))
        assert saved["address"] == address


def test_decoded_accounts_hold_the_curve_and_fee_parameters():
    schedule = Schedule.from_snapshot(fixture_snapshot(), KINDS)
    assert schedule.starting_curve() == Curve(
        30_000_000_000, 1_073_000_000_000_000, 0, 793_100_000_000_000
    )
    assert schedule.get("token_total_supply") == 1_000_000_000_000_000
    assert schedule.get("pool_migration_fee") == 15_000_001
    assert schedule.fee_config["fee_tiers"][0]["fees"]["protocol_fee_bps"] == 95


def test_rates_for_the_kinds_of_coin_seen_on_chain():
    s = Schedule.from_snapshot(fixture_snapshot(), KINDS)
    assert s.rates(CURVE, quote_mint="1" * 32, creator=CREATOR) == FeeRates(95, 30)
    assert s.rates(CURVE, quote_mint=WSOL, creator=CREATOR) == FeeRates(95, 30)
    # A coin's own creator rate replaces the schedule's (creator_fee_configurable is on).
    assert s.rates(CURVE, quote_mint="1" * 32, coin_creator_fee_bps=300, creator=CREATOR) == (
        FeeRates(95, 300)
    )
    assert s.rates(CURVE, quote_mint="1" * 32, creator=NO_CREATOR) == FeeRates(95, 0)
    assert s.rates(CURVE, quote_mint=USDC, creator=CREATOR) == FeeRates(95, 30)  # stable tiers
    assert s.rates(CURVE, quote_mint=PUMP_COIN, creator=CREATOR) == FeeRates(95, 30)  # exotic


def test_market_cap_tiers_and_fallbacks():
    snapshot = fixture_snapshot()
    values = snapshot["accounts"]["fee_config"]["values"]
    tier = lambda threshold, p, c: {  # noqa: E731
        "market_cap_lamports_threshold": threshold,
        "fees": {"lp_fee_bps": 0, "protocol_fee_bps": p, "creator_fee_bps": c},
    }
    values["fee_tiers"] = [tier(50_000_000_000, 120, 40), tier(100_000_000_000, 90, 20)]
    values["stable_fee_tiers"] = []
    values["exotic_flat_fees"] = {"lp_fee_bps": 0, "protocol_fee_bps": 0, "creator_fee_bps": 0}
    values["flat_fees"] = {"lp_fee_bps": 0, "protocol_fee_bps": 70, "creator_fee_bps": 10}
    s = Schedule.from_snapshot(snapshot, KINDS)
    supply = 1_000_000_000_000_000

    def at(market_cap_sol: float) -> Curve:
        return Curve(int(market_cap_sol * 1e9), supply, 0, 1)

    assert s.rates(at(30), quote_mint=WSOL, creator=CREATOR) == FeeRates(120, 40)  # below all
    assert s.rates(at(60), quote_mint=WSOL, creator=CREATOR) == FeeRates(120, 40)
    assert s.rates(at(150), quote_mint=WSOL, creator=CREATOR) == FeeRates(90, 20)
    assert s.rates(at(150), quote_mint=USDC, creator=CREATOR) == FeeRates(90, 20)  # empty -> SOL
    assert s.rates(at(150), quote_mint=PUMP_COIN, creator=CREATOR) == FeeRates(70, 10)  # unset

    no_fee_config = fixture_snapshot()
    del no_fee_config["accounts"]["fee_config"]
    legacy = Schedule.from_snapshot(no_fee_config, KINDS)
    assert legacy.rates(CURVE, quote_mint=WSOL, creator=CREATOR) == FeeRates(95, 5)  # Global


def test_snapshots_are_saved_and_picked_by_time(tmp_path):
    for fetched_at in (1000.0, 2000.0, 3000.0):
        path = save_snapshot(tmp_path, {**fixture_snapshot(fetched_at)})
        assert path.suffix == ".json" and not path.with_name(path.name + ".tmp").exists()
    snapshots = load_snapshots(tmp_path)
    assert [s["fetched_at"] for s in snapshots] == [1000.0, 2000.0, 3000.0]
    assert snapshot_at(snapshots, 2500)["fetched_at"] == 2000.0
    assert snapshot_at(snapshots, 9999)["fetched_at"] == 3000.0
    assert snapshot_at(snapshots, 10)["fetched_at"] == 1000.0  # before all: the oldest
    assert snapshot_at([], 10) is None
    assert load_snapshots(tmp_path / "none") == []


def test_quote_kinds_in_settings():
    assert "1" * 32 in KINDS.sol_like and WSOL in KINDS.sol_like
    assert KINDS.stable == {USDC}


@pytest.mark.parametrize("label", ["global", "fee_config"])
def test_unknown_account_data_is_not_decoded(label):
    idl = Idl.load("pump" if label == "global" else "pump_fees")
    assert idl.decode_account(bytes(8) + b"something else") is None


def test_engine_saves_a_snapshot_at_start_and_on_schedule(tmp_path, monkeypatch):
    import asyncio

    from agent.daemon import main as engine
    from helpers import make_project

    make_project(tmp_path)
    settings = tmp_path / "config" / "settings.yaml"
    settings.write_text(settings.read_text() + "chain:\n  snapshot_every_hours: 0.0005\n")
    monkeypatch.delenv("DATA_DIR", raising=False)
    cfg = load_config(tmp_path)
    calls = []

    async def fake_fetch(rpc):
        calls.append(time_now())
        return fixture_snapshot(1794480000.0 + len(calls))

    def time_now():
        import time

        return time.monotonic()

    monkeypatch.setattr(engine, "fetch_snapshot", fake_fetch)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(engine._snapshot_loop(cfg, stop, []))
        # Every 0.0005 h = 1.8 s: a snapshot at start, then one more. Wait for the second
        # rather than a fixed time (a busy PC can take a while to start the first).
        for _ in range(100):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())
    assert len(calls) == 2
    assert calls[1] - calls[0] >= 1.7  # on the schedule, not straight away
    assert len(load_snapshots(cfg.data_dir)) == 2


def test_engine_snapshots_are_off_unless_configured(tmp_path, monkeypatch):
    import asyncio

    from agent.daemon import main as engine
    from helpers import make_project

    make_project(tmp_path)
    monkeypatch.delenv("DATA_DIR", raising=False)
    cfg = load_config(tmp_path)
    assert cfg.settings.chain.snapshot_every_hours == 0
    asyncio.run(asyncio.wait_for(engine._snapshot_loop(cfg, asyncio.Event(), []), timeout=1))
    assert load_snapshots(cfg.data_dir) == []
