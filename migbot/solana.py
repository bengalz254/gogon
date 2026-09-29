"""Small pure-Python Solana helpers: base58, program-derived addresses and the
pump.fun bonding-curve layout. No wallet, no signing.
"""
from __future__ import annotations

import hashlib

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}

# ed25519 curve constants
_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = []
    while n:
        n, rem = divmod(n, 58)
        out.append(_B58_ALPHABET[rem])
    pad = len(data) - len(data.lstrip(b"\0"))
    return "1" * pad + "".join(reversed(out))


def b58decode(text: str) -> bytes:
    n = 0
    for ch in text:
        if ch not in _B58_INDEX:
            raise ValueError(f"invalid base58 character {ch!r}")
        n = n * 58 + _B58_INDEX[ch]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(text) - len(text.lstrip("1"))
    return b"\0" * pad + body


def is_pubkey(text: object) -> bool:
    """True for a base58 string that decodes to exactly 32 bytes."""
    if not isinstance(text, str) or not 32 <= len(text) <= 44:
        return False
    try:
        return len(b58decode(text)) == 32
    except ValueError:
        return False


def is_on_curve(point: bytes) -> bool:
    """Whether 32 bytes decode to a point on ed25519 (same rule as curve25519-dalek)."""
    y = int.from_bytes(point, "little") & ((1 << 255) - 1)
    y %= _P
    u = (y * y - 1) % _P
    v = (_D * y * y + 1) % _P
    if u == 0:
        return True
    w = (u * pow(v, _P - 2, _P)) % _P
    return pow(w, (_P - 1) // 2, _P) == 1


def find_program_address(seeds: list[bytes], program_id: str) -> tuple[str, int]:
    program = b58decode(program_id)
    for bump in range(255, -1, -1):
        digest = hashlib.sha256(b"".join(seeds) + bytes([bump]) + program + b"ProgramDerivedAddress").digest()
        if not is_on_curve(digest):
            return b58encode(digest), bump
    raise ValueError("no program address found")


def bonding_curve_address(mint: str) -> str:
    address, _ = find_program_address([b"bonding-curve", b58decode(mint)], PUMP_PROGRAM)
    return address


BONDING_CURVE_DISCRIMINATOR = hashlib.sha256(b"account:BondingCurve").digest()[:8]


def bonding_curve_creator(data: bytes) -> str | None:
    """Creator field of a pump.fun BondingCurve account, or None if absent.

    Layout: 8-byte discriminator, five u64 reserves/supply fields, a `complete`
    bool, then the 32-byte creator (added to the program in 2025).
    """
    if len(data) < 81 or data[:8] != BONDING_CURVE_DISCRIMINATOR:
        return None
    creator = data[49:81]
    if creator == b"\0" * 32:
        return None
    return b58encode(creator)
