"""The shared parser on real mainnet transactions (tests/fixtures/tx, captured 2026-11-12)."""

import base64

import pytest

from agent.ingest.logs import event_discriminator, walk_logs
from agent.ingest.parsers import Parser, RawTx
from helpers import OTHER, PUMP, data_line, fixture_names, load_fixture, program_block

PARSER = Parser()
SOL_PAIRED = "1" * 32  # an all-zero address: pump.fun's marker for "paired with SOL"


def parse(name: str):
    tx = load_fixture(name)
    return PARSER.parse(
        RawTx(tx["signature"], tx["slot"], tx["logMessages"], "recorder", recv_ts=1.5,
              block_time=tx["blockTime"])  # fmt: skip
    )


@pytest.mark.parametrize("name", fixture_names())
def test_every_captured_transaction_parses_cleanly(name):
    tx = load_fixture(name)
    out = parse(name)
    assert out.decode_errors == 0
    assert out.unknown_events == 0, "an event of our programs is missing from the IDL files"
    assert not out.truncated
    columns = PARSER.columns()
    seen = set()
    for table, rows in out.rows.items():
        allowed = {c.name for c in columns[table]}
        for row in rows:
            assert set(row) <= allowed, f"{table} row has a column the schema lacks"
            assert row["signature"] == tx["signature"] and row["slot"] == tx["slot"]
            assert row["source"] == "recorder" and row["recv_ts"] == 1.5
            # On-chain event time; it can differ from the node's block time by a second.
            assert abs(row["ts"] - tx["blockTime"]) <= 2
            assert row["event_index"] not in seen
            seen.add(row["event_index"])


def test_buy_v3():
    (trade,) = parse("pump-BuyV3").rows["trades"]
    assert trade["mint"] == "BrJS2qXZSa4SwFThFvKr9B5rm4RhzpVFEso6LEUSYwjN"
    assert trade["wallet"] == "Dxm74DtsrTmgdQXMrffzrRdaRhxpbWVhLcFVgwBbeTLN"
    assert "user" not in trade
    assert trade["is_buy"] is True and trade["is_creator"] is False
    assert trade["ts"] == trade["timestamp"] == 1794482629
    assert trade["sol_amount"] == trade["quote_amount"] == 296296296
    assert trade["token_amount"] == 5423787361875
    assert trade["quote_mint"] == SOL_PAIRED
    assert trade["virtual_quote_reserves"] == 42082987392
    assert trade["virtual_token_reserves"] == 764917179596058
    assert (trade["fee_basis_points"], trade["fee"]) == (95, 2814815)
    assert (trade["creator_fee_basis_points"], trade["creator_fee"]) == (30, 888889)
    assert trade["shareholders"] == []
    assert trade["event"] == "TradeEvent" and trade["event_index"] == 0


def test_sell():
    (trade,) = parse("pump-SellV3").rows["trades"]
    assert trade["mint"] == "H6Q8QkjGnAKe8qoGg55WsgcCSQV6kenk3kuXZKHypump"
    assert trade["is_buy"] is False
    assert trade["sol_amount"] == 18751289 and trade["token_amount"] == 40816358189792
    assert trade["ts"] == 1794482627  # one second before the node's block time


def test_create_with_the_creators_first_buy():
    out = parse("pump-BuyV2")
    (token,) = out.rows["tokens"]
    (trade,) = out.rows["trades"]
    assert token["mint"] == trade["mint"] == "DYZocfkvb85zbc3M7VcEJtva6H96HycPzmF49Cv1pump"
    assert (token["name"], token["symbol"]) == ("StonkMarket", "STM")
    assert token["wallet"] == token["creator"] == trade["wallet"]
    assert token["bonding_curve"] == "HGXCnJntgFQyF5eRsoqBKrcHwFU6hAXvheSbenFiLMmX"
    assert token["real_token_reserves"] == 793100000000000
    assert trade["is_creator"] is True
    assert (token["event_index"], trade["event_index"]) == (0, 1)


def test_coin_paired_with_another_pump_coin():
    (token,) = parse("pump-CreateV2").rows["tokens"]
    assert token["name"] == "Artcoin"
    assert token["quote_mint"] == "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn"
    assert token["creator_fee_bps"] == 300
    # A trade on a token-paired coin reports the amount in quote_amount; sol_amount is 0.
    (trade,) = parse("pump-MultiHopCurveSwap").rows["trades"]
    assert trade["sol_amount"] == 0 and trade["quote_amount"] == 14223256444
    assert trade["quote_mint"] == "5tePJcGmdKAoNVXe3fp2tmf8n7cmr5d3YBkYKj4Cpump"


def test_bonding_curve_completion_and_migration():
    out = parse("pump-event-CompleteEvent")
    (complete,) = out.rows["curve_completes"]
    trades = out.rows["trades"]
    assert len(trades) == 3 and {t["mint"] for t in trades} == {complete["mint"]}
    assert trades[-1]["real_token_reserves"] == 0  # the buy that emptied the curve
    assert complete["wallet"] == trades[-1]["wallet"]

    migrated = parse("pump-MigrateV2")
    (migration,) = migrated.rows["migrations"]
    (pool,) = migrated.rows["amm_pools"]
    assert migration["mint"] == complete["mint"] == pool["base_mint"]
    assert migration["pool"] == pool["pool"]
    assert migration["bonding_curve"] == complete["bonding_curve"]


def test_pumpswap_trades():
    (buy,) = parse("amm-Buy").rows["amm_trades"]
    assert buy["is_buy"] is True and buy["event"] == "BuyEvent"
    assert buy["pool"] == "25ERc7qcUtnTd5fMUvT2xc7pi2rJnMtsoQd5x6VH5RU6"
    assert (buy["base_amount_out"], buy["quote_amount_in"]) == (439541, 88016)
    assert buy["virtual_quote_reserves"] == 2516200564  # a 128-bit value, stored as text
    (sell,) = parse("amm-Sell").rows["amm_trades"]
    assert sell["is_buy"] is False and sell["event"] == "SellEvent"
    assert sell["base_amount_in"] > 0 and sell.get("base_amount_out") is None


def test_bytes_the_idl_does_not_describe_are_kept():
    # On 2026-11-12 the live program's trade events were 8 bytes longer than the official
    # IDL (published 2026-10-13) describes. The known fields decode; the rest is preserved.
    out = parse("pump-BuyV3")
    assert out.stale_idl_events == 1
    assert out.rows["trades"][0]["extra_hex"] == "00" * 8
    assert parse("pump-CreateV2").rows["tokens"][0]["extra_hex"] is None


def test_older_trade_layout_decodes_with_newer_fields_empty():
    # Rebuild the event as it looked before quote coins existed: cut the newest fields off.
    tx = load_fixture("pump-BuyV3")
    lines, _ = walk_logs(tx["logMessages"])
    payload = base64.b64decode(next(ln.text for ln in lines if ln.kind == "data"))
    old = payload[: -(8 + 32 + 6 * 8)]  # extra, quote_mint, then six 8-byte fields
    out = PARSER.parse(RawTx("sig", 1, program_block(PUMP, ["Buy"], [old]), "backfill"))
    (trade,) = out.rows["trades"]
    assert trade["sol_amount"] == 296296296 and trade["shareholders"] == []
    assert trade["quote_mint"] is None and trade["quote_amount"] is None
    assert trade["creator_fee_unclaimed"] is None and trade["extra_hex"] is None
    assert trade["recv_ts"] is None and trade["source"] == "backfill"
    assert out.stale_idl_events == 0 and out.decode_errors == 0


def test_event_printed_by_another_program_is_ignored():
    tx = load_fixture("pump-BuyV3")
    lines, _ = walk_logs(tx["logMessages"])
    payload = base64.b64decode(next(ln.text for ln in lines if ln.kind == "data"))
    out = PARSER.parse(RawTx("sig", 1, program_block(OTHER, ["Buy"], [payload]), "recorder"))
    assert out.rows == {} and out.events == 0


def test_damaged_and_unknown_events_are_counted_not_stored():
    # 32 bytes of mint, 8 of sol_amount, then one stray byte: it ends inside a field.
    # (A payload ending exactly between two fields would be read as an older layout.)
    cut = event_discriminator("TradeEvent") + b"\x01" * 41
    unknown = b"\x00" * 8 + b"whatever"
    logs = [f"Program {PUMP} invoke [1]", data_line(cut), data_line(unknown),
            "Program data: !!notbase64!!", f"Program {PUMP} success"]  # fmt: skip
    out = PARSER.parse(RawTx("sig", 1, logs, "recorder"))
    assert out.rows == {}
    assert (out.decode_errors, out.unknown_events) == (2, 1)


def test_truncated_logs_are_flagged():
    logs = load_fixture("pump-BuyV3")["logMessages"][:3] + ["Log truncated"]
    assert PARSER.parse(RawTx("sig", 1, logs, "recorder")).truncated


def test_tables_and_their_bookkeeping_columns():
    columns = PARSER.columns()
    assert set(columns) == {"tokens", "trades", "curve_completes", "post_complete_buys",
                            "migrations", "mayhem_params", "amm_trades", "amm_pools"}  # fmt: skip
    for table, cols in columns.items():
        names = [c.name for c in cols]
        assert names[:7] == ["signature", "event_index", "slot", "ts", "recv_ts", "source", "event"]
        assert names[-1] == "extra_hex" and len(names) == len(set(names)), table
    trade_cols = [c.name for c in columns["trades"]]
    wanted = {"mint", "wallet", "quote_mint", "quote_amount", "is_creator", "ix_name"}
    assert wanted <= set(trade_cols)
    assert "user" not in trade_cols
