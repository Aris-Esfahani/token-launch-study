"""The one parser shared by the live recorder and the backfill.

Input: a transaction's log lines plus where it came from. Output: table rows. Both paths
must call `Parser.parse` on the same kind of input, so that recorded and backfilled data
are identical (spec 4.1, parity test).

Each stored table mirrors one on-chain event: columns keep the names pump.fun uses in its
IDL (one rename: `user` becomes `wallet`), with a few bookkeeping columns in front.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field

from agent.ingest.borsh_idl import DecodeError, Field, Idl
from agent.ingest.logs import walk_logs

# (program, event name) -> table. Events not listed here are counted but not stored.
EVENT_TABLES: dict[tuple[str, str], str] = {
    ("pump", "CreateEvent"): "tokens",
    ("pump", "TradeEvent"): "trades",
    ("pump", "CompleteEvent"): "curve_completes",
    ("pump", "PostCompleteBuyEvent"): "post_complete_buys",
    ("pump", "CompletePumpAmmMigrationEvent"): "migrations",
    # pump.fun's mayhem program resets a mayhem coin's virtual reserves between trades;
    # without these, its price history has jumps no trade explains.
    ("pump", "UpdateMayhemVirtualParamsEvent"): "mayhem_params",
    ("pump_amm", "BuyEvent"): "amm_trades",
    ("pump_amm", "SellEvent"): "amm_trades",
    ("pump_amm", "CreatePoolEvent"): "amm_pools",
}
RENAMES = {"user": "wallet"}  # `user` is a reserved word in SQL

# Bookkeeping columns at the front of every table.
ENVELOPE: list[Field] = [
    Field("signature", "text"),
    Field("event_index", "i32"),  # position among this transaction's pump.fun events
    Field("slot", "i64"),
    Field("ts", "i64"),  # on-chain time of the event (epoch seconds): use this for cutoffs
    Field("recv_ts", "f64"),  # when the recorder saw it (None for backfill)
    Field("source", "text"),  # recorder | backfill | external
    Field("event", "text"),
]
# Bytes at the end of an event that our IDL copy does not describe (hex). pump.fun can
# deploy a new field before publishing its layout; keeping the bytes means the field can
# still be decoded from recorded data once the IDL is updated.
EXTRA_BYTES = Field("extra_hex", "text")
# Extra columns worked out from the event, per table.
DERIVED: dict[str, list[Field]] = {
    "trades": [Field("is_creator", "bool")],
    "amm_trades": [Field("is_buy", "bool")],
}


@dataclass(slots=True)
class RawTx:
    signature: str
    slot: int
    logs: list[str]
    source: str
    recv_ts: float | None = None
    block_time: int | None = None


@dataclass(slots=True)
class ParsedTx:
    rows: dict[str, list[dict]] = field(default_factory=dict)
    truncated: bool = False  # the node cut the logs short; events may be missing
    events: int = 0  # events decoded (stored or not)
    unknown_events: int = 0  # data lines from our programs that match no event in the IDL
    stale_idl_events: int = 0  # events longer than the IDL knows: the IDL copy is out of date
    decode_errors: int = 0


class Parser:
    def __init__(self, idls: list[Idl] | None = None) -> None:
        idls = idls or [Idl.load("pump"), Idl.load("pump_amm")]
        self._by_address = {idl.address: idl for idl in idls}
        self._idls = {idl.name: idl for idl in idls}

    @property
    def program_ids(self) -> dict[str, str]:
        """{short name: address} of the programs this parser understands."""
        return {idl.name: idl.address for idl in self._by_address.values()}

    def columns(self) -> dict[str, list[Field]]:
        """Every table's columns, in order, built from the IDL."""
        tables: dict[str, list[Field]] = {}
        for (program, event), table in EVENT_TABLES.items():
            columns = tables.setdefault(table, list(ENVELOPE))
            names = {c.name for c in columns}
            for f in self._idls[program].event_fields(event):
                name = RENAMES.get(f.name, f.name)
                if name not in names:
                    columns.append(Field(name, f.kind))
                    names.add(name)
        for table, extra in DERIVED.items():
            tables[table].extend(extra)
        for columns in tables.values():
            columns.append(EXTRA_BYTES)
        return tables

    def parse(self, tx: RawTx) -> ParsedTx:
        out = ParsedTx()
        lines, out.truncated = walk_logs(tx.logs)
        index = -1
        for line in lines:
            idl = self._by_address.get(line.program_id)
            if idl is None or line.kind != "data":
                continue
            index += 1  # counts every data line of our programs, so it is stable
            payload = _decode_base64(line.text)
            if payload is None:
                out.decode_errors += 1
                continue
            try:
                event = idl.decode_event(payload)
            except DecodeError:
                out.decode_errors += 1
                continue
            if event is None:
                out.unknown_events += 1
                continue
            out.events += 1
            if event.extra_bytes:
                out.stale_idl_events += 1
            table = EVENT_TABLES.get((idl.name, event.name))
            if table is None:
                continue
            values = event.values
            row = {
                "signature": tx.signature,
                "event_index": index,
                "slot": tx.slot,
                "ts": _first_set(values.get("timestamp"), tx.block_time, tx.recv_ts),
                "recv_ts": tx.recv_ts,
                "source": tx.source,
                "event": event.name,
            }
            for name, value in values.items():
                row[RENAMES.get(name, name)] = value
            row["extra_hex"] = payload[-event.extra_bytes :].hex() if event.extra_bytes else None
            if table == "trades":
                creator = values.get("creator")
                row["is_creator"] = None if creator is None else values.get("user") == creator
            elif table == "amm_trades":
                row["is_buy"] = event.name == "BuyEvent"
            out.rows.setdefault(table, []).append(row)
        return out


def to_storage(value: object, kind: str) -> object:
    """Convert a decoded value to what its table column holds."""
    if value is None:
        return None
    if kind == "big":
        return str(value)  # 128-bit integers do not fit a 64-bit column; keep them exact
    if kind == "json":
        return json.dumps(value, separators=(",", ":"), default=_json_default)
    return value


def _json_default(value: object) -> object:
    if isinstance(value, bytes | bytearray):
        return value.hex()
    raise TypeError(type(value).__name__)


def _first_set(*values: object) -> int | None:
    for value in values:
        if value is not None:
            return int(value)
    return None


def _decode_base64(text: str) -> bytes | None:
    try:
        return base64.b64decode(text.split(" ", 1)[0], validate=True)
    except (binascii.Error, ValueError):
        return None
