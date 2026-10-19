"""Frozen JS1 flat source slots; SB1/typed frames remain readable.

Only the already supported scalar candle tails enter this optional codec. Its
offsets, complete SHA256, compressed size/content/window bounds and cache budget
match the indexed source reader. Unsupported shapes keep the typed encoding.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
from typing import Any

import zstandard

from . import indexed_frame
from .storage import encode

MAGIC = b"JS1\x00"
SLOTS = 32
MAX_BYTES = 512 * 1024
MAX_ROWS = 256


def supported(slot: Any) -> bool:
    if not isinstance(slot, list) or len(slot) > MAX_ROWS:
        return False
    for row in slot:
        if not isinstance(row, list) or len(row) != 4:
            return False
        if any(type(n) is not int or not 0 <= n < 2**63 for n in row[:3]):
            return False
        tail = row[3]
        if not isinstance(tail, list) or len(tail) != 9 or not isinstance(tail[6], str):
            return False
        if any(type(n) is not int or not 0 <= n < 2**63 for n in tail[7:]):
            return False
        try:
            if any(type(n) not in (int, float) or not math.isfinite(n) for n in tail[:6]):
                return False
            tail[6].encode("utf-8")
        except (OverflowError, UnicodeError):
            return False
    return True


def pack(slots: list[Any]) -> bytes | None:
    if not isinstance(slots, list) or len(slots) != SLOTS or not all(supported(slot) for slot in slots):
        return None
    parts = []
    offsets = [0]
    for slot in slots:
        part = encode(slot).encode()
        offsets.append(offsets[-1] + len(part))
        if offsets[-1] > MAX_BYTES:
            return None
        parts.append(part)
    raw = b"".join(parts)
    compressed = zstandard.ZstdCompressor(level=3, write_checksum=True, threads=0).compress(raw)
    header = struct.pack(">HI", SLOTS, len(raw)) + struct.pack(">" + "I" * len(offsets), *offsets)
    checked = header + compressed
    result = MAGIC + hashlib.sha256(checked).digest() + checked
    return result if len(result) <= indexed_frame.MAX_COMPRESSED else None


def envelope(raw: bytes) -> bytes:
    if not isinstance(raw, bytes) or not raw.startswith(MAGIC):
        raise ValueError("Unknown source JSON slot frame")
    # IF1's checksum covers its header/offsets/compressed bytes. Reuse that exact
    # bounded byte-container validator, not its typed-record materializer.
    return indexed_frame.MAGIC + raw[4:]


def layout(raw: bytes) -> tuple[tuple[int, ...], bytes]:
    offsets, compressed = indexed_frame.layout(envelope(raw))
    if len(offsets) != SLOTS + 1 or offsets[-1] > MAX_BYTES:
        raise ValueError("Source JSON allocation bound")
    if zstandard.get_frame_parameters(compressed).window_size > MAX_BYTES:
        raise ValueError("Source JSON window bound")
    return offsets, compressed


def data(raw: bytes, cache: Any = None) -> tuple[tuple[int, ...], bytes]:
    layout(raw)
    return indexed_frame.data(envelope(raw), cache)


def invalid(value: Any) -> Any:
    raise ValueError("Non-scalar or non-finite source JSON")


def slot(raw: bytes) -> list[Any]:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError("Source JSON slot allocation bound")
    try:
        value = json.loads(raw, parse_constant=invalid, object_pairs_hook=invalid)
    except (RecursionError, UnicodeError) as error:
        raise ValueError("Invalid source JSON slot") from error
    if not supported(value):
        raise ValueError("Invalid source JSON slot")
    return value


def all_values(raw: bytes) -> list[Any]:
    offsets, expanded = data(raw)
    return [slot(expanded[a:b]) for a, b in zip(offsets, offsets[1:])]
