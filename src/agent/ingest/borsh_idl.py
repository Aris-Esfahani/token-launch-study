"""Decode pump.fun events using the layouts in the official Anchor IDL files.

Nothing about a layout is written here. The IDL (src/agent/ingest/idl/*.json) lists each
event's fields and types; this module turns that list into a decoder. When pump.fun
changes an event, replacing the IDL file is enough.

Events only ever grow by adding fields at the end, so:
- an older transaction has a shorter payload: the fields it does not have decode as None;
- a payload longer than the IDL knows (the program is newer than our IDL copy) decodes
  the known fields and reports the leftover bytes, so the engine can flag "IDL out of date".
"""

from __future__ import annotations

import json
import struct
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agent.ingest.solana import pubkey_text

IDL_DIR = Path(__file__).parent / "idl"


class DecodeError(Exception):
    """The payload does not fit the layout (for example it ends in the middle of a field)."""


class _EndOfData(Exception):
    pass


class _Reader:
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        end = self.pos + n
        if end > len(self.data):
            raise _EndOfData
        chunk = self.data[self.pos : end]
        self.pos = end
        return chunk

    @property
    def remaining(self) -> int:
        return len(self.data) - self.pos


_Decoder = Callable[[_Reader], object]

_INT_FORMATS = {
    "u8": "<B", "i8": "<b", "u16": "<H", "i16": "<h", "u32": "<I", "i32": "<i",
    "u64": "<Q", "i64": "<q", "f32": "<f", "f64": "<d",
}  # fmt: skip
_BIG_INTS = {"u128": False, "i128": True, "u256": False, "i256": True}


@dataclass(frozen=True, slots=True)
class Field:
    name: str
    kind: str  # storage kind: bool, u8..i64, f32/f64, big (128-bit+ integer), text, bytes, json


@dataclass(frozen=True, slots=True)
class DecodedEvent:
    name: str
    values: dict[str, object]
    missing_fields: int  # trailing fields absent from this (older) payload
    extra_bytes: int  # bytes left over: the program is newer than our IDL copy


class Idl:
    def __init__(self, source: dict | Path) -> None:
        idl = source if isinstance(source, dict) else json.loads(source.read_text("utf-8"))
        self.address: str = idl["address"]
        self.name: str = idl["metadata"]["name"]
        self._types = {t["name"]: t["type"] for t in idl.get("types", [])}
        self._compiled: dict[str, _Decoder] = {}
        self._event_decoders: dict[str, list[tuple[str, _Decoder]]] = {}
        self.events: dict[bytes, str] = {
            bytes(e["discriminator"]): e["name"] for e in idl.get("events", [])
        }
        self.instructions: dict[str, dict] = {i["name"]: i for i in idl.get("instructions", [])}
        self.accounts: dict[bytes, str] = {
            bytes(a["discriminator"]): a["name"] for a in idl.get("accounts", [])
        }

    @classmethod
    def load(cls, name: str) -> Idl:
        return cls(IDL_DIR / f"{name}.json")

    # --- public ------------------------------------------------------------------------

    def event_fields(self, event_name: str) -> list[Field]:
        """The event's top-level fields, in order, with how each is stored."""
        struct_def = self._types[event_name]
        if struct_def["kind"] != "struct":
            raise ValueError(f"{event_name} is not a struct")
        return [Field(f["name"], self._kind(f["type"])) for f in struct_def.get("fields", [])]

    def decode_event(self, payload: bytes) -> DecodedEvent | None:
        """Decode one event payload (8-byte tag + fields). None if the tag is not ours."""
        name = self.events.get(payload[:8])
        if name is None:
            return None
        decoders = self._event_decoders.get(name)
        if decoders is None:
            decoders = self._event_decoders[name] = [
                (f["name"], self._decoder(f["type"])) for f in self._types[name].get("fields", [])
            ]
        reader = _Reader(payload[8:])
        values: dict[str, object] = {}
        missing = 0
        for field_name, decode in decoders:
            if reader.remaining == 0:
                values[field_name] = None
                missing += 1
                continue
            try:
                values[field_name] = decode(reader)
            except _EndOfData:
                raise DecodeError(f"{name}: payload ends inside field {field_name}") from None
        return DecodedEvent(name, values, missing, reader.remaining)

    def account_address(self, instruction: str, account: str) -> str:
        """Address of a fixed account of an instruction: one the IDL names outright, or a
        program-derived address (PDA) whose seeds and program are all fixed."""
        from agent.ingest.solana import b58encode, find_program_address

        accounts = self.instructions[instruction]["accounts"]
        spec = next(a for a in accounts if a["name"] == account)
        if "address" in spec:
            return spec["address"]
        pda = spec["pda"]
        if any(seed["kind"] != "const" for seed in pda["seeds"]):
            raise ValueError(f"{instruction}.{account} depends on other accounts")
        program = self.address
        if "program" in pda:
            owner = pda["program"]
            if owner["kind"] == "const":
                program = b58encode(bytes(owner["value"]))
            elif owner["kind"] == "account":
                program = self.account_address(instruction, owner["path"])
            else:
                raise ValueError(f"{instruction}.{account}: unsupported program kind")
        return find_program_address([bytes(seed["value"]) for seed in pda["seeds"]], program)[0]

    def decode_account(self, data: bytes) -> DecodedEvent | None:
        """Decode an account's data (8-byte tag + fields) with the layout in the IDL. None if
        the tag is not one of this program's account types. Accounts are often allocated
        larger than their fields; the unused tail is reported as extra bytes."""
        name = self.accounts.get(data[:8])
        if name is None:
            return None
        reader = _Reader(data[8:])
        values: dict[str, object] = {}
        for field in self._types[name].get("fields", []):
            try:
                values[field["name"]] = self._decoder(field["type"])(reader)
            except _EndOfData:
                raise DecodeError(f"{name}: data ends inside field {field['name']}") from None
        return DecodedEvent(name, values, 0, reader.remaining)

    # --- building decoders from IDL types ----------------------------------------------

    def _decoder(self, idl_type: object) -> _Decoder:
        if isinstance(idl_type, str):
            return _primitive(idl_type)
        if "option" in idl_type or "coption" in idl_type:
            inner = self._decoder(idl_type.get("option", idl_type.get("coption")))
            width = 1 if "option" in idl_type else 4

            def decode_option(r: _Reader) -> object:
                return inner(r) if int.from_bytes(r.take(width), "little") else None

            return decode_option
        if "vec" in idl_type:
            item = self._decoder(idl_type["vec"])
            return lambda r: [item(r) for _ in range(struct.unpack("<I", r.take(4))[0])]
        if "array" in idl_type:
            item_type, length = idl_type["array"]
            item = self._decoder(item_type)
            return lambda r: [item(r) for _ in range(length)]
        if "defined" in idl_type:
            defined = idl_type["defined"]
            return self._defined(defined["name"] if isinstance(defined, dict) else defined)
        raise DecodeError(f"unsupported IDL type: {idl_type!r}")

    def _defined(self, name: str) -> _Decoder:
        if name in self._compiled:
            return self._compiled[name]
        # Register a forwarder first so a type that refers to itself does not loop forever.
        self._compiled[name] = lambda r: self._compiled[name](r)
        definition = self._types[name]
        if definition["kind"] == "struct":
            fields = definition.get("fields", [])
            if fields and not isinstance(fields[0], dict):  # tuple struct, e.g. OptionBool(bool)
                parts = [self._decoder(t) for t in fields]
                if len(parts) == 1:
                    decoder: _Decoder = parts[0]
                else:
                    decoder = lambda r: [p(r) for p in parts]  # noqa: E731
            else:
                named = [(f["name"], self._decoder(f["type"])) for f in fields]
                decoder = lambda r: {n: d(r) for n, d in named}  # noqa: E731
        elif definition["kind"] == "enum":
            variants = []
            for variant in definition["variants"]:
                parts = [
                    self._decoder(f["type"] if isinstance(f, dict) else f)
                    for f in variant.get("fields") or []
                ]
                variants.append((variant["name"], parts))

            def decoder(r: _Reader) -> object:
                index = r.take(1)[0]
                if index >= len(variants):
                    raise DecodeError(f"{name}: unknown variant {index}")
                variant_name, parts = variants[index]
                if not parts:
                    return variant_name
                return {"variant": variant_name, "fields": [p(r) for p in parts]}

        else:
            raise DecodeError(f"unsupported IDL type kind: {definition['kind']}")
        self._compiled[name] = decoder
        return decoder

    def _kind(self, idl_type: object) -> str:
        """How a top-level field is stored in a table column."""
        if isinstance(idl_type, str):
            if idl_type in _INT_FORMATS or idl_type == "bool":
                return idl_type
            if idl_type in _BIG_INTS:
                return "big"
            if idl_type in ("pubkey", "string"):
                return "text"
            if idl_type == "bytes":
                return "bytes"
            raise DecodeError(f"unsupported IDL type: {idl_type}")
        if "option" in idl_type:
            return self._kind(idl_type["option"])
        if "defined" in idl_type:
            defined = idl_type["defined"]
            definition = self._types[defined["name"] if isinstance(defined, dict) else defined]
            fields = definition.get("fields", [])
            is_struct = definition["kind"] == "struct"
            if is_struct and len(fields) == 1 and not isinstance(fields[0], dict):
                return self._kind(fields[0])  # single-value wrapper such as OptionBool
            if definition["kind"] == "enum" and not any(
                v.get("fields") for v in definition["variants"]
            ):
                return "text"
        return "json"  # lists and nested structs are stored as JSON text


def _primitive(name: str) -> _Decoder:
    if name in _INT_FORMATS:
        fmt = struct.Struct(_INT_FORMATS[name])
        return lambda r: fmt.unpack(r.take(fmt.size))[0]
    if name == "bool":
        return lambda r: r.take(1)[0] != 0
    if name in _BIG_INTS:
        size, signed = int(name[1:]) // 8, _BIG_INTS[name]
        return lambda r: int.from_bytes(r.take(size), "little", signed=signed)
    if name == "pubkey":
        return lambda r: pubkey_text(r.take(32))
    if name == "string":
        return lambda r: r.take(struct.unpack("<I", r.take(4))[0]).decode("utf-8", "replace")
    if name == "bytes":
        return lambda r: r.take(struct.unpack("<I", r.take(4))[0])
    raise DecodeError(f"unsupported IDL type: {name}")
