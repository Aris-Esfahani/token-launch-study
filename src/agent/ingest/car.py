"""Read Old Faithful's archive format: one CAR file per epoch with every block and
transaction (spec 4.1).

Layout (yellowstone-faithful's ledger schema; checked against epoch 1083 on 2026-11-12):

- A CAR v1 file is a header followed by sections: varint(length), a CID, then one node.
- Each node is a DAG-CBOR array whose first item is its kind:
    0 Transaction  [kind, data: DataFrame, metadata: DataFrame, slot, index?]
    1 Entry, 3 Subset, 4 Epoch, 5 Rewards (not needed here)
    2 Block        [kind, slot, shredding, entries, meta, rewards],
                   meta = [parent_slot, blocktime, height?]
    6 DataFrame    [kind, hash?, index?, total?, data, next?: [CID, ...]]
- A value too large for one node is split over several DataFrames. The first one names
  the others in `next`; each frame has its position in `index` and the count in `total`.
- Transaction data is the transaction as it was sent (Solana wire format). Metadata is a
  zstd-compressed protobuf `TransactionStatusMeta` (solana storage-proto), the same record
  an RPC node returns as `meta`.

Each epoch also has an index, `epoch-<E>-slot-ranges.raw`: 12 bytes per slot of the epoch
(offset u64, length u32, little-endian), the byte range of that slot's nodes in the CAR
file. Skipped slots (no block) are all zero. This lets a reader download only the slots it
wants with HTTP range requests.

Nothing here touches the network or the disk.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Iterator
from dataclasses import dataclass, field

import cbor2
import zstandard

TRANSACTION, ENTRY, BLOCK, SUBSET, EPOCH, REWARDS, DATAFRAME = range(7)

_SHA2_256 = 0x12  # multihash code
_CID_TAG = 42  # CBOR tag for a link to another node
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_MAX_META_BYTES = 64_000_000


class CarError(ValueError):
    """The bytes are not what the archive format says they should be."""


# --- the slot index ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SlotRange:
    slot: int
    offset: int
    length: int


class SlotIndex:
    """`epoch-<E>-slot-ranges.raw`: where each slot's nodes are in the epoch's CAR file."""

    RECORD = 12

    def __init__(self, epoch: int, data: bytes) -> None:
        if not data or len(data) % self.RECORD:
            raise CarError(f"slot index is {len(data)} bytes, not a multiple of {self.RECORD}")
        self.epoch = epoch
        self._data = data
        self.slots = len(data) // self.RECORD
        # The archive has one record per slot of the epoch, so the epoch length comes from
        # the file itself. The readers check every node's slot against it.
        self.first_slot = epoch * self.slots

    def get(self, slot: int) -> SlotRange | None:
        """The slot's byte range, or None if the slot was skipped (no block)."""
        i = slot - self.first_slot
        if not 0 <= i < self.slots:
            raise IndexError(f"slot {slot} is not in epoch {self.epoch}")
        offset, length = struct.unpack_from("<QI", self._data, i * self.RECORD)
        return SlotRange(slot, offset, length) if length else None

    def ranges(self, first: int, last: int) -> list[SlotRange]:
        """Byte ranges of the slots in [first, last] that have a block."""
        out = []
        for slot in range(max(first, self.first_slot), min(last, self.last_slot) + 1):
            r = self.get(slot)
            if r is not None:
                out.append(r)
        return out

    @property
    def last_slot(self) -> int:
        return self.first_slot + self.slots - 1

    def skipped(self) -> int:
        return sum(
            1 for i in range(self.slots)
            if not struct.unpack_from("<I", self._data, i * self.RECORD + 8)[0]
        )  # fmt: skip


# --- CAR sections ------------------------------------------------------------------------


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    try:
        while True:
            b = buf[pos]
            pos += 1
            result |= (b & 0x7F) << shift
            if b < 0x80:
                return result, pos
            shift += 7
            if shift > 63:
                raise CarError("varint too long")
    except IndexError:
        raise CarError("data ends inside a varint") from None


@dataclass(frozen=True, slots=True)
class Section:
    offset: int  # where the section starts in the CAR file (its length prefix)
    size: int  # the whole section: prefix, CID and node
    cid: bytes
    node: bytes

    @property
    def kind(self) -> int:
        return node_kind(self.node)


def _read_cid(buf: bytes, pos: int) -> tuple[bytes, int, int, bytes]:
    """(cid bytes, end, multihash code, digest)"""
    start = pos
    version, pos = _varint(buf, pos)
    if version != 1:
        raise CarError(f"CID version {version} at byte {start}")
    _codec, pos = _varint(buf, pos)
    code, pos = _varint(buf, pos)
    length, pos = _varint(buf, pos)
    digest = buf[pos : pos + length]
    if len(digest) != length:
        raise CarError("data ends inside a CID")
    pos += length
    return buf[start:pos], pos, code, digest


def iter_sections(data: bytes, base_offset: int = 0, *, verify: bool = True) -> Iterator[Section]:
    """Every section in `data`, which must start and end on section boundaries (as a slot
    range from the index does). With `verify`, each node's bytes are checked against the
    hash in its CID, so a damaged download is caught."""
    pos = 0
    end = len(data)
    while pos < end:
        start = pos
        length, pos = _varint(data, pos)
        body_end = pos + length
        if body_end > end:
            raise CarError(f"section at byte {base_offset + start} runs past the data")
        cid, pos, code, digest = _read_cid(data, pos)
        node = data[pos:body_end]
        if verify and code == _SHA2_256 and hashlib.sha256(node).digest() != digest:
            raise CarError(f"node at byte {base_offset + start} does not match its CID")
        yield Section(base_offset + start, body_end - start, cid, node)
        pos = body_end


def node_kind(node: bytes) -> int:
    """A node's kind, read from its first bytes without decoding the rest."""
    if len(node) >= 2 and 0x81 <= node[0] <= 0x97 and node[1] <= 0x17:
        return node[1]  # a short CBOR array whose first item is a small integer
    value = cbor2.loads(node)
    if not isinstance(value, list) or not value or not isinstance(value[0], int):
        raise CarError("node is not a kind-tagged array")
    return value[0]


def _link(value: object) -> bytes:
    """The CID bytes of a CBOR link (tag 42, with a leading zero byte)."""
    if isinstance(value, cbor2.CBORTag) and value.tag == _CID_TAG:
        raw = bytes(value.value)
        return raw[1:] if raw[:1] == b"\x00" else raw
    raise CarError("expected a link to another node")


# --- decoded nodes -----------------------------------------------------------------------


@dataclass(slots=True)
class TxNode:
    slot: int
    index: int | None  # position in the block
    offset: int  # the Transaction node's section in the CAR file
    size: int
    data_frame: list
    meta_frame: list


@dataclass(slots=True)
class BlockNode:
    slot: int
    parent_slot: int | None
    block_time: int | None


@dataclass(slots=True)
class SlotData:
    """The nodes of one or more slots, read from their bytes."""

    transactions: list[TxNode] = field(default_factory=list)
    blocks: dict[int, BlockNode] = field(default_factory=dict)
    frames: dict[bytes, bytes] = field(default_factory=dict)  # DataFrame nodes by CID
    sections: int = 0

    def join(self, frame: list) -> bytes:
        """The full value of a DataFrame, following its links to the other parts."""
        return join_frames(frame, self.frames)


def read_slots(data: bytes, base_offset: int = 0, *, verify: bool = True) -> SlotData:
    """Decode the transactions and blocks in `data` (one or more slot ranges)."""
    out = SlotData()
    for section in iter_sections(data, base_offset, verify=verify):
        out.sections += 1
        kind = section.kind
        if kind == DATAFRAME:
            out.frames[section.cid] = section.node  # decoded only if a transaction needs it
        elif kind == TRANSACTION:
            node = cbor2.loads(section.node)
            if len(node) < 4:
                raise CarError(f"transaction node at byte {section.offset} is too short")
            out.transactions.append(
                TxNode(
                    slot=node[3],
                    index=node[4] if len(node) > 4 else None,
                    offset=section.offset,
                    size=section.size,
                    data_frame=node[1],
                    meta_frame=node[2],
                )
            )
        elif kind == BLOCK:
            node = cbor2.loads(section.node)
            meta = node[4] if len(node) > 4 else None
            parent = meta[0] if meta and len(meta) > 0 else None
            block_time = meta[1] if meta and len(meta) > 1 else None
            out.blocks[node[1]] = BlockNode(node[1], parent, block_time or None)
    return out


def join_frames(frame: list, frames: dict[bytes, bytes]) -> bytes:
    """Reassemble a value split over DataFrames. Parts are collected through their `next`
    links and put in `index` order; the count must equal `total`."""
    first_data = _frame_data(frame)
    links = _frame_next(frame)
    if not links:
        return first_data
    parts: dict[int, bytes] = {_frame_index(frame, 0): first_data}
    seen: set[bytes] = set()
    pending = list(links)
    while pending:
        cid = pending.pop(0)
        if cid in seen:
            continue
        seen.add(cid)
        raw = frames.get(cid)
        if raw is None:
            raise CarError("a part of a split value is missing from the data")
        part = cbor2.loads(raw)
        parts[_frame_index(part, len(parts))] = _frame_data(part)
        pending.extend(_frame_next(part))
    total = frame[3] if len(frame) > 3 and frame[3] else len(parts)
    if len(parts) != total or sorted(parts) != list(range(total)):
        raise CarError(f"split value has parts {sorted(parts)} of {total}")
    return b"".join(parts[i] for i in range(total))


def _frame_data(frame: list) -> bytes:
    if not isinstance(frame, list) or not frame or frame[0] != DATAFRAME:
        raise CarError("expected a DataFrame")
    return bytes(frame[4]) if len(frame) > 4 and frame[4] is not None else b""


def _frame_next(frame: list) -> list[bytes]:
    value = frame[5] if len(frame) > 5 else None
    return [_link(v) for v in value] if value else []


def _frame_index(frame: list, default: int) -> int:
    value = frame[2] if len(frame) > 2 else None
    return value if isinstance(value, int) else default


# --- transaction bytes (Solana wire format) ----------------------------------------------


def _compact_u16(buf: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    for _ in range(3):
        if pos >= len(buf):
            raise CarError("transaction ends inside a length")
        b = buf[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if b < 0x80:
            return value, pos
        shift += 7
    raise CarError("length longer than 3 bytes")


@dataclass(frozen=True, slots=True)
class TxHeader:
    signature: bytes  # the first signature: the transaction's id
    num_signatures: int
    versioned: bool
    static_keys: tuple[bytes, ...]  # accounts listed in the message itself
    uses_lookup_tables: bool  # more accounts are loaded from address lookup tables


_V1 = 0x81  # first byte of a v1 transaction (SIMD-0385, active on mainnet since 2026-10)


def tx_header(data: bytes) -> TxHeader:
    """The parts of a transaction needed to identify it and to tell which programs it can
    reach. A transaction without lookup tables can only call programs whose address is
    among its own accounts. Every field is walked to the end, and the lengths must add up
    to exactly the transaction's size, so a misread layout is caught rather than guessed.

    Three formats are in use: legacy and v0 (signatures first, then the message; v0 adds
    address lookup tables) and v1 (SIMD-0385: version byte 0x81 first, fixed-width
    counts, signatures last, no lookup tables)."""
    if data[:1] == bytes([_V1]):
        return _v1_header(data)
    n, pos = _compact_u16(data, 0)
    if n < 1 or pos + 64 * n > len(data):
        raise CarError("transaction has no signature")
    signature = data[pos : pos + 64]
    pos += 64 * n
    if pos >= len(data):
        raise CarError("transaction has no message")
    versioned = bool(data[pos] & 0x80)
    if versioned:
        if data[pos] != 0x80:
            raise CarError(f"unknown message version byte {data[pos]:#x}")
        pos += 1
    pos += 3  # message header: required signatures, read-only signed, read-only unsigned
    count, pos = _compact_u16(data, pos)
    keys_end = pos + 32 * count
    if keys_end > len(data):
        raise CarError("transaction ends inside its account list")
    keys = tuple(data[i : i + 32] for i in range(pos, keys_end, 32))
    pos = keys_end + 32  # recent blockhash
    instructions, pos = _compact_u16(data, pos)
    for _ in range(instructions):
        pos += 1  # program index
        accounts, pos = _compact_u16(data, pos)
        pos += accounts
        length, pos = _compact_u16(data, pos)
        pos += length
    tables = 0
    if versioned:
        tables, pos = _compact_u16(data, pos)
        for _ in range(tables):
            pos += 32  # the lookup table's address
            writable, pos = _compact_u16(data, pos)
            pos += writable
            readonly, pos = _compact_u16(data, pos)
            pos += readonly
    if pos != len(data):
        raise CarError(f"transaction layout adds up to {pos} bytes, not {len(data)}")
    return TxHeader(signature, n, versioned, keys, tables > 0)


def _v1_header(data: bytes) -> TxHeader:
    # SIMD-0385 layout: version u8, header (3 x u8), config mask u32, lifetime [32],
    # instruction count u8, address count u8, addresses [32 each], config values (4 bytes
    # per mask bit), instruction headers (u8 program, u8 accounts, u16 data length),
    # instruction payloads (account indexes, then data), signatures [64 each] last.
    # Byte order is little-endian (Solana's convention; confirmed by the size check below
    # on real transactions).
    if len(data) < 42:
        raise CarError("v1 transaction is too short")
    signatures = data[1]
    mask = int.from_bytes(data[4:8], "little")
    instructions, addresses = data[40], data[41]
    pos = 42
    keys = tuple(data[pos + 32 * i : pos + 32 * i + 32] for i in range(addresses))
    pos += 32 * addresses + 4 * mask.bit_count()
    headers = data[pos : pos + 4 * instructions]
    pos += 4 * instructions
    for i in range(instructions):
        accounts = headers[4 * i + 1]
        length = int.from_bytes(headers[4 * i + 2 : 4 * i + 4], "little")
        pos += accounts + length
    if signatures < 1 or pos + 64 * signatures != len(data):
        raise CarError(f"v1 transaction layout adds up to {pos + 64 * signatures} bytes, "
                       f"not {len(data)}")  # fmt: skip
    return TxHeader(data[pos : pos + 64], signatures, True, keys, False)


# --- transaction metadata (protobuf) -----------------------------------------------------


@dataclass(slots=True)
class TxMeta:
    failed: bool
    fee: int | None
    logs: list[str] | None  # None when the node did not keep the logs
    loaded_addresses: list[bytes]  # accounts loaded from lookup tables
    compute_units: int | None


# TransactionStatusMeta field numbers (solana storage-proto, confirmed_block.proto).
_ERR, _FEE, _LOGS, _LOGS_NONE = 1, 2, 6, 11
_LOADED_WRITABLE, _LOADED_READONLY, _COMPUTE_UNITS = 12, 13, 16


def decompress_meta(raw: bytes, dctx: zstandard.ZstdDecompressor | None = None) -> bytes:
    if raw[:4] != _ZSTD_MAGIC:
        raise CarError("transaction metadata is not zstd-compressed")
    dctx = dctx or zstandard.ZstdDecompressor()
    try:
        return dctx.decompress(raw, max_output_size=_MAX_META_BYTES)
    except zstandard.ZstdError as exc:
        raise CarError(f"transaction metadata does not decompress: {exc}") from None


def parse_meta(buf: bytes) -> TxMeta:
    """Read the fields this project uses from a TransactionStatusMeta protobuf."""
    failed = False
    fee = compute_units = None
    logs: list[str] = []
    logs_none = False
    loaded: list[bytes] = []
    pos, end = 0, len(buf)
    try:
        while pos < end:
            key, pos = _varint(buf, pos)
            number, wire = key >> 3, key & 7
            if wire == 0:
                value, pos = _varint(buf, pos)
                if number == _FEE:
                    fee = value
                elif number == _COMPUTE_UNITS:
                    compute_units = value
                elif number == _LOGS_NONE:
                    logs_none = bool(value)
            elif wire == 2:
                length, pos = _varint(buf, pos)
                value_end = pos + length
                if value_end > end:
                    raise CarError("metadata ends inside a field")
                if number == _LOGS:
                    logs.append(buf[pos:value_end].decode("utf-8", "replace"))
                elif number == _ERR:
                    failed = True
                elif number in (_LOADED_WRITABLE, _LOADED_READONLY):
                    loaded.append(bytes(buf[pos:value_end]))
                pos = value_end
            elif wire == 1:
                pos += 8
            elif wire == 5:
                pos += 4
            else:
                raise CarError(f"unexpected protobuf wire type {wire}")
    except IndexError:
        raise CarError("metadata ends early") from None
    if pos != end:
        raise CarError("metadata ends inside a field")
    return TxMeta(failed, fee, None if logs_none and not logs else logs, loaded, compute_units)
