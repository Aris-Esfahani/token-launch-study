"""Turn a piece of an Old Faithful archive into the same table rows the live recorder
writes (spec 4.1: same parser, same schema, `source = backfill`).

Kept: successful transactions that mention pump.fun or PumpSwap, the same rule the live
feed uses (`logsSubscribe` with `mentions`). Their logs go through the shared parser.

Backfill rows carry a few extra columns, all starting with `bf_`. They exist only in the
archive, never in the live feed, so they must stay out of model features (spec 4.1):

    bf_tx_index        position of the transaction in its block
    bf_fee             fee paid in lamports (base fee plus priority fee)
    bf_signatures      number of signatures (the base fee is charged per signature)
    bf_compute_units   compute units the transaction used
    bf_mentions_pump   the transaction mentions the pump.fun program, so the live feed
                       (which follows pump.fun only) would have delivered it
    bf_pump_via_lookup it mentions pump.fun only through an address lookup table
    bf_car_offset, bf_car_size
                       where the transaction is in the epoch's archive file, so it can be
                       read again later (for example for Jito tips) without the whole slot
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import zstandard

from agent.ingest.borsh_idl import Field
from agent.ingest.car import CarError, decompress_meta, parse_meta, read_slots, tx_header
from agent.ingest.parsers import Parser, RawTx
from agent.ingest.solana import b58decode, b58encode

SOURCE = "backfill"
TRUNCATED_TABLE = "truncated_txs"

BACKFILL_COLUMNS: list[Field] = [
    Field("bf_tx_index", "i32"),
    Field("bf_fee", "u64"),
    Field("bf_signatures", "u16"),
    Field("bf_compute_units", "u64"),
    Field("bf_mentions_pump", "bool"),
    Field("bf_pump_via_lookup", "bool"),
    Field("bf_car_offset", "u64"),
    Field("bf_car_size", "u32"),
]
TRUNCATED_COLUMNS: list[Field] = [
    Field("signature", "text"),
    Field("slot", "i64"),
    Field("recv_ts", "f64"),
    Field("source", "text"),
    *BACKFILL_COLUMNS,
]


def backfill_columns(parser: Parser) -> dict[str, list[Field]]:
    """The recorder's tables plus the backfill-only columns."""
    tables = {name: [*cols, *BACKFILL_COLUMNS] for name, cols in parser.columns().items()}
    tables[TRUNCATED_TABLE] = TRUNCATED_COLUMNS
    return tables


@dataclass
class Extracted:
    rows: dict[str, list[dict]] = field(default_factory=dict)
    stats: Counter = field(default_factory=Counter)
    problems: list[str] = field(default_factory=list)  # transactions that could not be read
    slots: set[int] = field(default_factory=set)  # slots whose block was read
    min_block_time: int | None = None
    max_block_time: int | None = None


class Extractor:
    def __init__(self, parser: Parser | None = None) -> None:
        self.parser = parser or Parser()
        ids = self.parser.program_ids
        self._pump = b58decode(ids["pump"])
        self._amm = b58decode(ids["pump_amm"])
        self._wanted = (self._pump, self._amm)
        self._names = tuple(name.encode() for name in ids.values())
        self._dctx = zstandard.ZstdDecompressor()

    def extract(self, data: bytes, base_offset: int, slots: set[int] | None = None,
                *, verify: bool = True) -> Extracted:
        """Rows from `data`, the bytes of one or more whole slots starting at `base_offset`
        in the CAR file. `slots`: the slots the caller expects; a node from any other slot
        means the index and the archive disagree, which is an error."""
        out = Extracted()
        nodes = read_slots(data, base_offset, verify=verify)
        st = out.stats
        st["sections"] += nodes.sections
        st["blocks"] += len(nodes.blocks)
        for slot, block in nodes.blocks.items():
            if slots is not None and slot not in slots:
                raise CarError(f"found block {slot}, expected slots {min(slots)}-{max(slots)}")
            out.slots.add(slot)
            bt = block.block_time
            if bt is not None:
                out.min_block_time = min(bt, out.min_block_time or bt)
                out.max_block_time = max(bt, out.max_block_time or bt)
        if slots is not None and out.slots != slots:
            missing = sorted(slots - out.slots)
            raise CarError(f"no block for slot(s) {missing[:5]} in the data")
        for tx in nodes.transactions:
            st["transactions"] += 1
            if tx.slot not in nodes.blocks:
                raise CarError(f"transaction in slot {tx.slot}, which has no block here")
            try:
                self._transaction(tx, nodes, out)
            except CarError as exc:
                st["unreadable"] += 1
                out.problems.append(f"slot {tx.slot} tx {tx.index}: {exc}")
        return out

    def _transaction(self, tx, nodes, out: Extracted) -> None:
        st = out.stats
        data = nodes.join(tx.data_frame)
        header = tx_header(data)
        static = set(header.static_keys)
        mentions_pump = self._pump in static
        mentions = mentions_pump or self._amm in static
        if not mentions and not header.uses_lookup_tables:
            return  # cannot reach either program (most transactions, including votes)
        meta_raw = nodes.join(tx.meta_frame)
        if not meta_raw:
            if mentions:
                raise CarError("transaction mentions our programs but has no metadata")
            return
        raw = decompress_meta(meta_raw, self._dctx)
        if not mentions:
            if not any(key in raw for key in self._wanted) and not any(
                name in raw for name in self._names
            ):
                return  # quick check before reading the protobuf
        meta = parse_meta(raw)
        loaded = set(meta.loaded_addresses)
        via_lookup = not mentions_pump and self._pump in loaded
        mentions_pump = mentions_pump or via_lookup
        if not (mentions or mentions_pump or self._amm in loaded):
            return
        st["mention our programs"] += 1
        if meta.failed:
            st["failed (dropped)"] += 1
            return
        if meta.logs is None:
            raise CarError("the archive kept no logs for this transaction")
        signature = b58encode(header.signature)
        block_time = nodes.blocks[tx.slot].block_time
        parsed = self.parser.parse(RawTx(signature, tx.slot, meta.logs, SOURCE, None, block_time))
        st["kept"] += 1
        st["events"] += parsed.events
        st["unknown_events"] += parsed.unknown_events
        st["stale_idl_events"] += parsed.stale_idl_events
        st["decode_errors"] += parsed.decode_errors
        extra = {
            "bf_tx_index": tx.index,
            "bf_fee": meta.fee,
            "bf_signatures": header.num_signatures,
            "bf_compute_units": meta.compute_units,
            "bf_mentions_pump": mentions_pump,
            "bf_pump_via_lookup": via_lookup,
            "bf_car_offset": tx.offset,
            "bf_car_size": tx.size,
        }
        for table, rows in parsed.rows.items():
            bucket = out.rows.setdefault(table, [])
            for row in rows:
                row.update(extra)
                bucket.append(row)
            st[f"rows: {table}"] += len(rows)
        if parsed.truncated:
            st["truncated"] += 1
            out.rows.setdefault(TRUNCATED_TABLE, []).append(
                {"signature": signature, "slot": tx.slot, "recv_ts": None, "source": SOURCE,
                 "ts": block_time, **extra}
            )  # fmt: skip
