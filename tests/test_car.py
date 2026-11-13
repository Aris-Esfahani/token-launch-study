"""The Old Faithful archive reader, checked against a real slot and against the RPC's
copy of the same transactions."""

from __future__ import annotations

import hashlib
import struct

import cbor2
import pytest
import zstandard

from agent.ingest.car import (
    DATAFRAME,
    CarError,
    SlotIndex,
    decompress_meta,
    iter_sections,
    join_frames,
    parse_meta,
    read_slots,
    tx_header,
)
from agent.ingest.solana import b58encode
from helpers import car_fixture


@pytest.fixture(scope="module")
def slot():
    data, info = car_fixture()
    return data, info, read_slots(data, info["offset"])


def test_fixture_is_intact(slot):
    data, info, _ = slot
    assert len(data) == info["length"]
    assert hashlib.sha256(data).hexdigest() == info["sha256"]


def test_block_and_transactions(slot):
    _, info, nodes = slot
    assert set(nodes.blocks) == {info["slot"]}
    block = nodes.blocks[info["slot"]]
    assert block.block_time == next(iter(info["rpc"].values()))["blockTime"]
    assert block.parent_slot < info["slot"]
    assert len(nodes.transactions) == 301
    assert {tx.slot for tx in nodes.transactions} == {info["slot"]}
    assert sorted(tx.index for tx in nodes.transactions) == list(range(301))


def test_every_transaction_format_is_read_exactly(slot):
    """tx_header walks every field and checks the sizes add up, so this fails if any
    layout (legacy, v0, v1) is misread."""
    _, _, nodes = slot
    kinds = set()
    for tx in nodes.transactions:
        data = nodes.join(tx.data_frame)
        header = tx_header(data)
        kinds.add("v1" if data[0] == 0x81 else "v0" if header.versioned else "legacy")
        if header.uses_lookup_tables:
            kinds.add("lookup tables")
    assert kinds == {"legacy", "v0", "v1", "lookup tables"}


def test_matches_the_rpc(slot):
    """Signature, accounts, fee, compute units, logs and errors as the RPC reports them."""
    _, info, nodes = slot
    dctx = zstandard.ZstdDecompressor()
    by_signature = {}
    for tx in nodes.transactions:
        header = tx_header(nodes.join(tx.data_frame))
        by_signature[b58encode(header.signature)] = (tx, header)
    assert len(info["rpc"]) == 128
    versions = set()
    for signature, rpc in info["rpc"].items():
        tx, header = by_signature[signature]
        meta = parse_meta(decompress_meta(nodes.join(tx.meta_frame), dctx))
        versions.add(rpc["version"])
        assert tx.slot == rpc["slot"]
        assert [b58encode(k) for k in header.static_keys] == rpc["accountKeys"]
        loaded = rpc["loadedAddresses"] or {}
        assert [b58encode(a) for a in meta.loaded_addresses] == (
            loaded.get("writable", []) + loaded.get("readonly", [])
        )
        assert meta.fee == rpc["fee"]
        assert meta.compute_units == rpc["computeUnitsConsumed"]
        assert meta.logs == rpc["logMessages"]
        assert meta.failed == (rpc["err"] is not None)
    assert versions == {"legacy", 0, 1}


def test_damaged_bytes_are_caught(slot):
    data, info, _ = slot
    damaged = bytearray(data)
    damaged[len(data) // 2] ^= 0xFF
    with pytest.raises(CarError):
        list(iter_sections(bytes(damaged), info["offset"]))
    with pytest.raises(CarError):
        list(iter_sections(data[:-10], info["offset"]))
    # Without the hash check the flipped byte may go unnoticed, so the check is on by default.
    assert len(list(iter_sections(data, info["offset"]))) == read_slots(data).sections


def test_a_wrong_layout_does_not_add_up(slot):
    _, _, nodes = slot
    data = nodes.join(nodes.transactions[0].data_frame)
    with pytest.raises(CarError):
        tx_header(data + b"\x00")
    with pytest.raises(CarError):
        tx_header(data[:-1])


def _cid(payload: bytes) -> bytes:
    return b"\x01\x71\x12\x20" + hashlib.sha256(payload).digest()


def _link(cid: bytes) -> cbor2.CBORTag:
    return cbor2.CBORTag(42, b"\x00" + cid)


def test_split_values_are_joined_in_order():
    parts = [b"aaa", b"bbb", b"ccc"]
    second = cbor2.dumps([DATAFRAME, None, 1, 3, parts[1]])
    third = cbor2.dumps([DATAFRAME, None, 2, 3, parts[2]])
    frames = {_cid(second): second, _cid(third): third}
    first = [DATAFRAME, None, 0, 3, parts[0], [_link(_cid(third)), _link(_cid(second))]]
    assert join_frames(first, frames) == b"aaabbbccc"
    # Linked as a chain (each part names the next) works too.
    chained_second = cbor2.dumps([DATAFRAME, None, 1, 3, parts[1], [_link(_cid(third))]])
    frames2 = {_cid(chained_second): chained_second, _cid(third): third}
    first2 = [DATAFRAME, None, 0, 3, parts[0], [_link(_cid(chained_second))]]
    assert join_frames(first2, frames2) == b"aaabbbccc"
    with pytest.raises(CarError):
        join_frames(first, {_cid(second): second})  # a part is missing
    with pytest.raises(CarError):
        join_frames([DATAFRAME, None, 0, 4, parts[0], first[5]], frames)  # total is wrong


def test_slot_index():
    epoch, slots = 7, 10
    data = bytearray(12 * slots)
    struct.pack_into("<QI", data, 12 * 2, 1000, 50)
    struct.pack_into("<QI", data, 12 * 3, 1050, 70)
    index = SlotIndex(epoch, bytes(data))
    assert index.first_slot == 70 and index.last_slot == 79
    assert index.get(72).offset == 1000 and index.get(73).length == 70
    assert index.get(70) is None
    assert [r.slot for r in index.ranges(70, 79)] == [72, 73]
    assert index.skipped() == 8
    with pytest.raises(IndexError):
        index.get(80)
    with pytest.raises(CarError):
        SlotIndex(epoch, b"\x00" * 13)


def _pb_bytes(field: int, value: bytes) -> bytes:
    return bytes([(field << 3) | 2, len(value)]) + value


def test_metadata_fields():
    log = _pb_bytes(6, b"Program log: hi")
    meta = parse_meta(log + bytes([(2 << 3), 0x88, 0x27]))  # fee 5000
    assert meta.logs == ["Program log: hi"] and meta.fee == 5000 and not meta.failed
    assert parse_meta(_pb_bytes(1, b"\x01\x02")).failed
    assert parse_meta(bytes([(11 << 3), 1])).logs is None  # the node kept no logs
    with pytest.raises(CarError):
        parse_meta(_pb_bytes(6, b"cut")[:-1])
    with pytest.raises(CarError):
        decompress_meta(b"not zstd")
