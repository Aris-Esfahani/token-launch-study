"""Small Solana helpers: base58 text encoding and program-derived addresses (PDAs).

Pure Python with no extra dependencies. Used to print addresses the way explorers and
Padre show them, and to derive well-known pump.fun accounts from the seeds in the IDL
instead of writing their addresses into the code.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache

_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {char: i for i, char in enumerate(_ALPHABET)}


def b58encode(data: bytes) -> str:
    number = int.from_bytes(data, "big")
    digits = bytearray()
    while number:
        number, remainder = divmod(number, 58)
        digits.append(_ALPHABET[remainder])
    leading_zeros = len(data) - len(data.lstrip(b"\0"))
    return (b"1" * leading_zeros + bytes(reversed(digits))).decode()


def b58decode(text: str) -> bytes:
    number = 0
    try:
        for char in text.encode():
            number = number * 58 + _INDEX[char]
    except KeyError:
        raise ValueError(f"not base58: {text!r}") from None
    body = number.to_bytes((number.bit_length() + 7) // 8, "big")
    leading_ones = len(text) - len(text.lstrip("1"))
    return b"\0" * leading_ones + body


def is_address(text: object) -> bool:
    """A Solana address (wallet, mint or program): base58 text of exactly 32 bytes."""
    if not isinstance(text, str) or not 32 <= len(text) <= 44:
        return False
    try:
        raw = b58decode(text)
    except ValueError:
        return False
    return len(raw) == 32 and b58encode(raw) == text


@lru_cache(maxsize=200_000)
def pubkey_text(raw: bytes) -> str:
    """A 32-byte address as base58 text. Cached: the same wallets and mints repeat a lot."""
    return b58encode(raw)


# --- program-derived addresses -------------------------------------------------------

_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def _is_on_curve(point: bytes) -> bool:
    """Whether 32 bytes are a valid ed25519 point. A PDA is, by definition, one that is not."""
    y = (int.from_bytes(point, "little") & ((1 << 255) - 1)) % _P
    y2 = y * y % _P
    x2 = (y2 - 1) * pow((_D * y2 + 1) % _P, _P - 2, _P) % _P
    return x2 == 0 or pow(x2, (_P - 1) // 2, _P) == 1


def find_program_address(seeds: list[bytes], program_id: str) -> tuple[str, int]:
    """Solana's `findProgramAddress`: the PDA for these seeds under `program_id`."""
    program = b58decode(program_id)
    for bump in range(255, -1, -1):
        digest = hashlib.sha256(
            b"".join(seeds) + bytes([bump]) + program + b"ProgramDerivedAddress"
        ).digest()
        if not _is_on_curve(digest):
            return b58encode(digest), bump
    raise ValueError("no valid program address for these seeds")
