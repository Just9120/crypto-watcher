"""Frozen typed records sharing one compressed frame and explicit byte offsets.

Every access authenticates the actual frame. Only the selected typed record is
materialized; a caller may cache the pure decompression in its existing budget.
"""

from __future__ import annotations

import hashlib
import struct
from typing import Any

import zstandard

from .compact_json import pack as typed_pack
from .compact_json import unpack as typed_unpack

MAGIC = b"IF1\x00"
MAX_RECORDS = 64
MAX_BYTES = 2 * 1024 * 1024
MAX_COMPRESSED = 512 * 1024


class FrameSizeError(ValueError):
    """A valid set of records needs more than one bounded compressed frame."""


def pack(values: list[Any], *, dense: bool = True) -> bytes:
    if not 0 < len(values) <= MAX_RECORDS:
        raise ValueError("Indexed frame cardinality bound")
    parts = []
    offsets = [0]
    for value in values:
        part = typed_pack(value)
        offsets.append(offsets[-1] + len(part))
        if offsets[-1] > MAX_BYTES:
            raise FrameSizeError("Indexed frame expanded bound")
        parts.append(part)
    raw = b"".join(parts)
    compressed = zstandard.ZstdCompressor(level=3, write_checksum=True, threads=0).compress(raw)
    if dense and len(raw) >= 4096:
        denser = zstandard.ZstdCompressor(level=19, write_checksum=True, threads=0).compress(raw)
        if len(denser) < len(compressed):
            compressed = denser
    header = struct.pack(">HI", len(values), len(raw)) + struct.pack(">" + "I" * len(offsets), *offsets)
    checked = header + compressed
    result = MAGIC + hashlib.sha256(checked).digest() + checked
    if len(result) > MAX_COMPRESSED:
        raise FrameSizeError("Indexed compressed frame bound")
    return result


def layout(frame: bytes) -> tuple[tuple[int, ...], bytes]:
    if (
        not isinstance(frame, bytes)
        or not 50 < len(frame) <= MAX_COMPRESSED
        or not frame.startswith(MAGIC)
        or hashlib.sha256(frame[36:]).digest() != frame[4:36]
    ):
        raise ValueError("Indexed frame checksum mismatch")
    count, size = struct.unpack(">HI", frame[36:42])
    if not 0 < count <= MAX_RECORDS or not 0 < size <= MAX_BYTES:
        raise ValueError("Indexed frame allocation bound")
    end = 42 + (count + 1) * 4
    if end >= len(frame):
        raise ValueError("Truncated indexed frame offsets")
    offsets = struct.unpack(">" + "I" * (count + 1), frame[42:end])
    if offsets[0] != 0 or offsets[-1] != size or any(b <= a for a, b in zip(offsets, offsets[1:])):
        raise ValueError("Invalid indexed frame offsets")
    compressed = frame[end:]
    parameters = zstandard.get_frame_parameters(compressed)
    if (
        parameters.content_size != size
        or parameters.window_size > MAX_BYTES
        or parameters.dict_id
        or not parameters.has_checksum
    ):
        raise ValueError("Indexed frame decompression bound")
    return offsets, compressed


def data(frame: bytes, cache: Any = None) -> tuple[tuple[int, ...], bytes]:
    offsets, compressed = layout(frame)

    def read() -> bytes:
        raw = zstandard.ZstdDecompressor(max_window_size=MAX_BYTES // 1024).decompress(
            compressed, max_output_size=offsets[-1], allow_extra_data=False
        )
        if len(raw) != offsets[-1]:
            raise ValueError("Incomplete indexed frame")
        return raw

    raw = read() if cache is None else cache("indexed-frame", "", frame, read)
    if not isinstance(raw, bytes) or len(raw) != offsets[-1]:
        raise ValueError("Invalid pure indexed cache result")
    return offsets, raw


def select(frame: bytes, slot: int, cache: Any = None) -> Any:
    offsets, raw = data(frame, cache)
    if type(slot) is not int or not 0 <= slot < len(offsets) - 1:
        raise ValueError("Invalid indexed frame slot")
    return typed_unpack(raw[offsets[slot] : offsets[slot + 1]], MAX_BYTES)


def all_values(frame: bytes) -> list[Any]:
    offsets, raw = data(frame)
    return [typed_unpack(raw[a:b], MAX_BYTES) for a, b in zip(offsets, offsets[1:])]
