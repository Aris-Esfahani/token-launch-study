"""The IDL-driven decoder, on a small made-up IDL that uses every type shape pump.fun uses."""

import struct

import pytest

from agent.ingest.borsh_idl import DecodeError, Idl
from agent.ingest.logs import event_discriminator
from agent.ingest.solana import b58decode
from helpers import OTHER, PUMP

TAG = [1, 2, 3, 4, 5, 6, 7, 8]
IDL = {
    "address": PUMP,
    "metadata": {"name": "demo"},
    "instructions": [],
    "events": [{"name": "Demo", "discriminator": TAG}],
    "types": [
        {
            "name": "Demo",
            "type": {
                "kind": "struct",
                "fields": [
                    {"name": "amount", "type": "u64"},
                    {"name": "flag", "type": "bool"},
                    {"name": "who", "type": "pubkey"},
                    {"name": "label", "type": "string"},
                    {"name": "maybe", "type": {"option": "u16"}},
                    {"name": "wrapped", "type": {"defined": {"name": "OptionBool"}}},
                    {"name": "pairs", "type": {"vec": {"defined": {"name": "Pair"}}}},
                    {"name": "big", "type": "i128"},
                    {"name": "status", "type": {"defined": {"name": "Status"}}},
                    {"name": "triple", "type": {"array": ["u8", 3]}},
                    {"name": "added_later", "type": "u64"},
                ],
            },
        },
        {"name": "OptionBool", "type": {"kind": "struct", "fields": ["bool"]}},
        {
            "name": "Pair",
            "type": {
                "kind": "struct",
                "fields": [{"name": "key", "type": "pubkey"}, {"name": "bps", "type": "u16"}],
            },
        },
        {
            "name": "Status",
            "type": {"kind": "enum", "variants": [{"name": "Paused"}, {"name": "Active"}]},
        },
    ],
}

OLD_LAYOUT = (
    bytes(TAG)
    + struct.pack("<Q", 2**63 + 5)  # amount: larger than a signed 64-bit number
    + b"\x01"  # flag
    + b58decode(PUMP)  # who
    + struct.pack("<I", 5) + "héllo".encode()[:5]  # label (length is in bytes)
    + b"\x01" + struct.pack("<H", 300)  # maybe = Some(300)
    + b"\x00"  # wrapped = false
    + struct.pack("<I", 1) + b58decode(OTHER) + struct.pack("<H", 250)  # pairs
    + (-7).to_bytes(16, "little", signed=True)  # big
    + b"\x01"  # status = Active
    + bytes([9, 8, 7])  # triple
)  # fmt: skip
CURRENT_LAYOUT = OLD_LAYOUT + struct.pack("<Q", 42)


def test_decodes_every_type_shape():
    event = Idl(IDL).decode_event(CURRENT_LAYOUT)
    assert event.name == "Demo" and event.missing_fields == 0 and event.extra_bytes == 0
    v = event.values
    assert v["amount"] == 2**63 + 5
    assert v["flag"] is True
    assert v["who"] == PUMP
    assert v["label"] == "héll"
    assert v["maybe"] == 300
    assert v["wrapped"] is False
    assert v["pairs"] == [{"key": OTHER, "bps": 250}]
    assert v["big"] == -7
    assert v["status"] == "Active"
    assert v["triple"] == [9, 8, 7]
    assert v["added_later"] == 42


def test_older_payload_without_the_newest_field():
    event = Idl(IDL).decode_event(OLD_LAYOUT)
    assert event.values["added_later"] is None
    assert event.values["triple"] == [9, 8, 7]
    assert event.missing_fields == 1


def test_newer_payload_than_the_idl_reports_leftover_bytes():
    event = Idl(IDL).decode_event(CURRENT_LAYOUT + b"\xaa\xbb")
    assert event.values["added_later"] == 42
    assert event.extra_bytes == 2


def test_payload_cut_inside_a_field_is_an_error():
    with pytest.raises(DecodeError, match="ends inside field"):
        Idl(IDL).decode_event(CURRENT_LAYOUT[:-3])


def test_unknown_event_tag_is_not_ours():
    assert Idl(IDL).decode_event(bytes(8) + b"anything") is None


def test_column_kinds():
    kinds = {f.name: f.kind for f in Idl(IDL).event_fields("Demo")}
    assert kinds == {
        "amount": "u64", "flag": "bool", "who": "text", "label": "text", "maybe": "u16",
        "wrapped": "bool", "pairs": "json", "big": "big", "status": "text", "triple": "json",
        "added_later": "u64",
    }  # fmt: skip


def test_official_idl_files_load_and_follow_the_anchor_tag_rule():
    pump, amm = Idl.load("pump"), Idl.load("pump_amm")
    assert pump.address == PUMP and pump.name == "pump" and amm.name == "pump_amm"
    for idl, names in ((pump, ["TradeEvent", "CreateEvent", "CompleteEvent"]),
                       (amm, ["BuyEvent", "SellEvent", "CreatePoolEvent"])):  # fmt: skip
        for name in names:
            assert idl.events[event_discriminator(name)] == name
            assert idl.event_fields(name)
