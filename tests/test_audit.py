"""The coverage audit against a fake RPC node (no network)."""

import asyncio
import json

import httpx
import pytest

from agent.audit import _signatures_in_window, recorded_span, run_audit
from agent.ingest import rpc as rpc_module
from agent.ingest.gaps import GapLog
from agent.ingest.parsers import Parser
from agent.ingest.pipeline import Pipeline
from agent.ingest.rpc import Rpc, RpcError
from helpers import live, load_fixture

MARKER = "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"
RECORDED = ["pump-BuyV2", "pump-ExtendAccount"]  # two creates the recorder saw
MISSED = "pump-CreateV2"  # a create it did not see
START, END = 1794482620, 1794482640


def fake_rpc(signatures: dict[str, list[dict]], transactions: dict[str, dict]) -> Rpc:
    """An Rpc whose node answers from the given tables."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        if method == "getSignaturesForAddress":
            items = signatures.get(params[0], [])
            before = params[1].get("before")
            if before:
                index = next(i for i, item in enumerate(items) if item["signature"] == before)
                items = items[index + 1 :]
            result = items[: params[1]["limit"]]
        else:
            result = transactions.get(params[0])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    return Rpc("https://rpc.invalid", max_rps=10_000, transport=httpx.MockTransport(handler))


def entry(name: str, *, signature: str | None = None, err=None, block_time=None) -> dict:
    tx = load_fixture(name)
    return {"signature": signature or tx["signature"], "slot": tx["slot"], "err": err,
            "blockTime": block_time or tx["blockTime"]}  # fmt: skip


def chain_tx(name: str) -> dict:
    tx = load_fixture(name)
    return {"slot": tx["slot"], "blockTime": tx["blockTime"],
            "meta": {"err": None, "logMessages": tx["logMessages"]}}  # fmt: skip


@pytest.fixture
def recording(tmp_path):
    """A data folder holding what the recorder saw, and the matching fake chain."""
    data = tmp_path / "data"
    pipeline = Pipeline(data, data / "run")
    for name in RECORDED:
        pipeline.handle(live(name))
    pipeline.close()
    signatures = {
        MARKER: [
            entry("pump-BuyV2", signature="FailedCreate", err={"InstructionError": [0, "x"]}),
            entry("pump-ExtendAccount"),
            entry("pump-BuyV2"),
            entry(MISSED),
            entry("pump-Sell"),  # mentions the account but holds no create event
            entry("pump-BuyV2", signature="TooOld", block_time=START - 500),
        ]
    }
    transactions = {
        load_fixture(MISSED)["signature"]: chain_tx(MISSED),
        load_fixture("pump-Sell")["signature"]: chain_tx("pump-Sell"),
    }
    return data, signatures, transactions


def audit(data, signatures, transactions, **kwargs):
    async def go():
        rpc = fake_rpc(signatures, transactions)
        try:
            return await run_audit(data, rpc, Parser(), MARKER, start=START, end=END, **kwargs)
        finally:
            await rpc.aclose()

    return asyncio.run(go())


def test_a_missed_create_is_found(recording):
    result = audit(*recording)
    assert result.onchain_creates == 3
    assert result.recorded_creates == 2
    assert result.missed_outside_gaps == [load_fixture(MISSED)["signature"]]
    assert result.missed_inside_gaps == []
    assert result.not_creates == 1  # the sell
    assert result.recorded_not_onchain == [] and result.unfetched == []
    assert result.missed_share == pytest.approx(1 / 3)
    assert result.to_json()["missed_share"] == pytest.approx(1 / 3)


def test_a_miss_during_a_logged_gap_is_counted_separately(recording):
    data, signatures, transactions = recording
    missed_at = load_fixture(MISSED)["blockTime"]
    GapLog(data / "gaps").append(
        {"start_ts": missed_at - 1.0, "end_ts": missed_at + 1.0, "start_slot": 1, "end_slot": 2,
         "reason": "disconnect", "detail": ""}  # fmt: skip
    )
    result = audit(data, signatures, transactions)
    assert result.missed_outside_gaps == []
    assert result.missed_inside_gaps == [load_fixture(MISSED)["signature"]]
    assert result.missed_share == 0 and len(result.gaps) == 1


def test_nothing_missed_when_the_recording_is_complete(recording):
    data, signatures, transactions = recording
    signatures[MARKER] = [e for e in signatures[MARKER]
                          if e["signature"] != load_fixture(MISSED)["signature"]]  # fmt: skip
    result = audit(data, signatures, transactions)
    assert (result.onchain_creates, result.recorded_creates) == (2, 2)
    assert result.missed_share == 0


def test_a_transaction_the_node_cannot_return_is_reported_not_guessed(recording):
    data, signatures, transactions = recording
    del transactions[load_fixture(MISSED)["signature"]]
    result = audit(data, signatures, transactions)
    assert result.unfetched == [load_fixture(MISSED)["signature"]]
    assert result.missed_outside_gaps == []


def test_trade_sample_finds_a_missed_trade(recording):
    data, signatures, transactions = recording
    create_a, create_b = load_fixture("pump-BuyV2"), load_fixture("pump-ExtendAccount")
    curve_a = "HGXCnJntgFQyF5eRsoqBKrcHwFU6hAXvheSbenFiLMmX"
    curve_b = "9bQVnpzuBN165T4gUkMsEGWQ7vCQ2wohsDdiE2Zjmqnd"
    signatures[curve_a] = [
        entry("pump-BuyV2", signature="MissedTrade"),  # a trade on token A we never saw
        entry("pump-Sell", signature="OtherTokenTrade"),  # touches the account, other mint
        entry("pump-BuyV2"),
    ]
    signatures[curve_b] = [entry("pump-ExtendAccount")]
    transactions["MissedTrade"] = chain_tx("pump-BuyV2")
    transactions["OtherTokenTrade"] = chain_tx("pump-Sell")

    result = audit(data, signatures, transactions, trades_sample=5)
    assert result.trade_sample_tokens == 2
    assert result.onchain_trades == 3  # two recorded transactions and the missed one
    assert result.missed_trades == ["MissedTrade"]
    assert result.missed_trade_share == pytest.approx(1 / 3)
    assert result.recorded_trades_not_onchain == []
    assert {create_a["signature"], create_b["signature"]}.isdisjoint(result.missed_trades)


def test_trade_sample_finds_a_recorded_trade_that_is_not_on_chain(recording):
    data, signatures, transactions = recording
    signatures["HGXCnJntgFQyF5eRsoqBKrcHwFU6hAXvheSbenFiLMmX"] = []  # chain has nothing
    signatures["9bQVnpzuBN165T4gUkMsEGWQ7vCQ2wohsDdiE2Zjmqnd"] = [entry("pump-ExtendAccount")]
    result = audit(data, signatures, transactions, trades_sample=5)
    assert result.recorded_trades_not_onchain == [load_fixture("pump-BuyV2")["signature"]]
    assert result.missed_trades == []


def test_recorded_span(recording, tmp_path):
    data, _, _ = recording
    assert recorded_span(data / "raw") == (1794482633.0, 1794482635.0)
    assert recorded_span(tmp_path / "empty") is None


def test_signature_listing_pages_until_the_window_start():
    items = [{"signature": f"s{i}", "slot": i, "err": None, "blockTime": 10_000 - i}
             for i in range(2300)]  # newest first, one per second  # fmt: skip
    items[5]["err"] = {"failed": True}

    async def go():
        rpc = fake_rpc({"addr": items}, {})
        try:
            found, complete = await _signatures_in_window(rpc, "addr", start=8_500, end=9_990)
            cut, cut_complete = await _signatures_in_window(
                rpc, "addr", start=0, end=9_990, max_pages=1
            )
            assert complete and not cut_complete and len(cut) == 990
            return found, rpc.requests
        finally:
            await rpc.aclose()

    found, requests = asyncio.run(go())
    assert requests == 3  # two pages for the full listing, one for the capped listing
    assert found[0]["signature"] == "s10" and found[-1]["signature"] == "s1500"
    assert len(found) == 1491 and all(item["err"] is None for item in found)


def test_rpc_retries_when_told_to_slow_down_and_reports_real_errors(monkeypatch):
    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(rpc_module.asyncio, "sleep", no_wait)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["method"])
        if calls[-1] == "bad":
            return httpx.Response(200, json={"error": {"code": -32602, "message": "nope"}})
        if calls[-1] == "down":
            return httpx.Response(503)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"result": 7})

    async def go():
        rpc = Rpc("https://rpc.invalid/?api-key=SECRETKEY", max_rps=10_000,
                  max_attempts=4, transport=httpx.MockTransport(handler))  # fmt: skip
        try:
            assert await rpc.call("getSlot", []) == 7
            with pytest.raises(RpcError, match="nope"):
                await rpc.call("bad", [])
            with pytest.raises(RpcError, match="gave up after 4 attempts") as failure:
                await rpc.call("down", [])
            assert "SECRETKEY" not in str(failure.value)
        finally:
            await rpc.aclose()

    asyncio.run(go())
    assert calls.count("getSlot") == 3 and calls.count("bad") == 1 and calls.count("down") == 4
