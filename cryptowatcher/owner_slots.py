"""OW1: exact64-slot ownership masks bound to stable parent and packet IDs.

The checksum UDF is bounded and pure. Validation connections install the same
function before the ordinary full SQLite CHECK; no schema or data is changed.
"""

from __future__ import annotations

import hashlib
import sqlite3
import struct
from collections.abc import Iterable
from typing import Any

ZERO = bytes(8)


def bits(value: bytes) -> int:
    if not isinstance(value, bytes) or len(value) != 8:
        raise ValueError("Invalid record owner mask")
    return int.from_bytes(value, "big")


def slot_number(slot: int) -> int:
    if type(slot) is not int or not 0 <= slot < 64:
        raise ValueError("Invalid record owner slot")
    return slot


def mask(slots: Iterable[int]) -> bytes:
    result = 0
    for slot in slots:
        flag = 1 << slot_number(slot)
        if result & flag:
            raise ValueError("Duplicate record owner slot")
        result |= flag
    return result.to_bytes(8, "big")


def has(value: bytes, slot: int) -> bool:
    return bool(bits(value) & (1 << slot_number(slot)))


def remove(value: bytes, slot: int) -> bytes:
    flag = 1 << slot_number(slot)
    original = bits(value)
    if not original & flag:
        raise ValueError("Missing record owner slot")
    return (original ^ flag).to_bytes(8, "big")


def signature(parent: int, packet: int, value: bytes) -> bytes:
    if any(type(identity) is not int or not 0 < identity < 2**63 for identity in (parent, packet)):
        raise ValueError("Invalid record owner coordinate")
    bits(value)
    return hashlib.sha256(b"OW1\x00" + struct.pack(">QQ", parent, packet) + value).digest()


def sql_signature(*values: Any) -> bytes:
    if len(values) != 3:
        return b""
    try:
        return signature(values[0], values[1], values[2])
    except ValueError:
        # This cannot equal a required32byte checksum. Invalid CHECK inputs
        # produce a constraint failure during full validation, not a fabricated
        # valid value or a callback exception that conceals corrupt row data.
        return b""


def install_functions(db: sqlite3.Connection) -> None:
    db.create_function("cw_owner_digest", 3, sql_signature, deterministic=True)
    db.create_function("cw_owner_has", 2, has, deterministic=True)
    db.create_function("cw_owner_remove", 2, remove, deterministic=True)
