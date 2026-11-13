"""Read a transaction's log lines and work out which program wrote each one.

Solana prints `Program <id> invoke [depth]` when a program starts and
`Program <id> success` / `failed` when it ends. Between those markers, `Program log:` and
`Program data:` lines belong to the program currently running. Tracking that stack matters:
any program can print text that looks like a pump.fun event, so a line only counts as
pump.fun's if pump.fun was the program running when it was written.

Shared by the live recorder and the backfill.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass

_ID = r"[1-9A-HJ-NP-Za-km-z]{32,44}"
_INVOKE = re.compile(rf"^Program ({_ID}) invoke \[(\d+)\]$")
_EXIT = re.compile(rf"^Program ({_ID}) (?:success|failed)")
_LOG = "Program log: "
_DATA = "Program data: "
_INSTRUCTION = "Instruction: "
_TRUNCATED = "Log truncated"


@dataclass(frozen=True, slots=True)
class ProgramLine:
    program_id: str
    depth: int
    kind: str  # "log" or "data"
    text: str


def walk_logs(logs: Iterable[str]) -> tuple[list[ProgramLine], bool]:
    """Return every log/data line with the program that wrote it, and whether the node
    cut the logs short (it stops at ~10 KB per transaction)."""
    stack: list[str] = []
    lines: list[ProgramLine] = []
    truncated = False
    for raw in logs:
        if raw.startswith(_LOG):
            if stack:
                lines.append(ProgramLine(stack[-1], len(stack), "log", raw[len(_LOG) :]))
        elif raw.startswith(_DATA):
            if stack:
                lines.append(ProgramLine(stack[-1], len(stack), "data", raw[len(_DATA) :]))
        elif raw == _TRUNCATED:
            truncated = True
        elif m := _INVOKE.match(raw):
            stack.append(m.group(1))
        elif m := _EXIT.match(raw):
            # Normally the program on top of the stack. If the logs were cut short the
            # stack can be out of step, so unwind to the program that is exiting.
            if m.group(1) in stack:
                while stack.pop() != m.group(1):
                    pass
    return lines, truncated


def instruction_names(lines: Iterable[ProgramLine], program_id: str) -> list[str]:
    """Names from the `Instruction: <Name>` lines Anchor programs print, for one program."""
    return [
        ln.text[len(_INSTRUCTION) :]
        for ln in lines
        if ln.kind == "log" and ln.program_id == program_id and ln.text.startswith(_INSTRUCTION)
    ]


def event_payloads(lines: Iterable[ProgramLine], program_id: str) -> list[bytes]:
    """Raw bytes of each event the program emitted (`Program data:` lines)."""
    out: list[bytes] = []
    for ln in lines:
        if ln.kind != "data" or ln.program_id != program_id:
            continue
        first = ln.text.split(" ", 1)[0]
        try:
            out.append(base64.b64decode(first, validate=True))
        except (binascii.Error, ValueError):
            continue
    return out


def event_discriminator(event_name: str) -> bytes:
    """The 8-byte tag Anchor puts at the start of an event: sha256("event:<Name>")[:8].
    Derived from the name, so nothing about the layout is hardcoded."""
    return hashlib.sha256(f"event:{event_name}".encode()).digest()[:8]
