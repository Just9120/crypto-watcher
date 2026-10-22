"""SB2 exact typed slices with bounded per-slot unit restoration.

The authenticated header gives both stored offsets and every restoration size.
Their complete sum is checked before data decompression or a pure cache hit.
The small compressed delta vector is bounded to2048unsigned32bit entries.
"""

from __future__ import annotations

import hashlib
import struct
from typing import Any

import zstandard

from . import scalar_units
from .shared_body_frame import MAX_BYTES, MAX_COMPRESSED, MAX_RECORDS

MAGIC = b"SB2\0"


def _compress(raw: bytes) -> bytes:
    selected = zstandard.ZstdCompressor(level=3, write_checksum=True, threads=0).compress(raw)
    if len(raw) >= 4096:
        dense = zstandard.ZstdCompressor(level=19, write_checksum=True, threads=0).compress(raw)
        if len(dense) < len(selected):
            selected = dense
    return selected


def _decompress(compressed: bytes, size: int) -> bytes:
    try:
        raw = zstandard.ZstdDecompressor(max_window_size=MAX_BYTES // 1024).decompress(
            compressed, max_output_size=size, allow_extra_data=False
        )
    except zstandard.ZstdError as error:
        raise ValueError("Invalid scalar body compression") from error
    if len(raw) != size:
        raise ValueError("Incomplete scalar body data")
    return raw


def _parameters(compressed: bytes, size: int, window: int) -> None:
    try:
        value = zstandard.get_frame_parameters(compressed)
    except zstandard.ZstdError as error:
        raise ValueError("Invalid scalar body compression") from error
    if value.content_size != size or value.window_size > window or value.dict_id or not value.has_checksum:
        raise ValueError("Scalar body decompression bound")


def pack(parts: list[bytes]) -> bytes:
    """Caller has already validated each original typed slice."""
    from .indexed_frame import FrameSizeError

    if not isinstance(parts, list) or not 0 < len(parts) <= MAX_RECORDS:
        raise ValueError("Scalar body cardinality bound")
    encoded_offsets = [0]
    encoded_parts: list[bytes] = []
    deltas: list[int] = []
    original_size = 0
    for part in parts:
        if not isinstance(part, bytes) or not part:
            raise ValueError("Invalid scalar body slice")
        original_size += len(part)
        if original_size > MAX_BYTES:
            raise FrameSizeError("Scalar body expanded bound")
        encoded = scalar_units.transform(part, MAX_BYTES)
        if len(encoded) > len(part):
            raise ValueError("Scalar body transform increased its input")
        encoded_offsets.append(encoded_offsets[-1] + len(encoded))
        encoded_parts.append(encoded)
        deltas.append(len(part) - len(encoded))
    delta_data = struct.pack(">" + "I" * len(parts), *deltas)
    delta_frame = zstandard.ZstdCompressor(level=3, write_checksum=True, threads=0).compress(delta_data)
    header = struct.pack(">HIIH", len(parts), original_size, encoded_offsets[-1], len(delta_frame))
    header += struct.pack(">" + "I" * len(encoded_offsets), *encoded_offsets)
    checked = header + delta_frame + _compress(b"".join(encoded_parts))
    result = MAGIC + hashlib.sha256(checked).digest() + checked
    if len(result) > MAX_COMPRESSED:
        raise FrameSizeError("Scalar body compressed bound")
    return result


def _layout(frame: bytes) -> tuple[tuple[int, ...], tuple[int, ...], bytes]:
    if (
        not isinstance(frame, bytes)
        or not 66 < len(frame) <= MAX_COMPRESSED
        or not frame.startswith(MAGIC)
        or hashlib.sha256(frame[36:]).digest() != frame[4:36]
    ):
        raise ValueError("Scalar body checksum mismatch")
    count, original_size, stored_size, delta_size = struct.unpack(">HIIH", frame[36:48])
    if not 0 < count <= MAX_RECORDS or not 0 < stored_size <= original_size <= MAX_BYTES:
        raise ValueError("Scalar body allocation bound")
    offset_end = 48 + (count + 1) * 4
    delta_end = offset_end + delta_size
    if not offset_end < delta_end < len(frame):
        raise ValueError("Truncated scalar body metadata")
    stored = struct.unpack(">" + "I" * (count + 1), frame[48:offset_end])
    if stored[0] != 0 or stored[-1] != stored_size or any(b <= a for a, b in zip(stored, stored[1:])):
        raise ValueError("Invalid scalar body offsets")
    delta_frame = frame[offset_end:delta_end]
    _parameters(delta_frame, count * 4, MAX_RECORDS * 4)
    deltas = struct.unpack(">" + "I" * count, _decompress(delta_frame, count * 4))
    original = [0]
    increase = 0
    for end, delta in zip(stored[1:], deltas, strict=True):
        increase += delta
        if end + increase > original_size:
            raise ValueError("Scalar body restoration allocation bound")
        original.append(end + increase)
    if original[-1] != original_size:
        raise ValueError("Scalar body restoration size mismatch")
    compressed = frame[delta_end:]
    _parameters(compressed, stored_size, MAX_BYTES)
    return tuple(original), stored, compressed


def layout(frame: bytes) -> tuple[tuple[int, ...], bytes]:
    original, _, compressed = _layout(frame)
    return original, compressed


def _restore(raw: bytes, expected: int) -> bytes:
    value = scalar_units.transform(raw, expected, inverse=True)
    if len(value) != expected:
        raise ValueError("Scalar body restored slice size mismatch")
    return value


def data(frame: bytes, cache: Any = None) -> tuple[tuple[int, ...], bytes]:
    original, stored, compressed = _layout(frame)

    def read() -> bytes:
        raw = _decompress(compressed, stored[-1])
        return b"".join(
            _restore(raw[start:end], original[i + 1] - original[i])
            for i, (start, end) in enumerate(zip(stored, stored[1:]))
        )

    value = read() if cache is None else cache("scalar-body-data-v1", "", frame, read)
    if not isinstance(value, bytes) or len(value) != original[-1]:
        raise ValueError("Invalid pure scalar body cache result")
    return original, value


def parts(frame: bytes, first: int, count: int, cache: Any = None) -> list[bytes]:
    from .shared_body_frame import _coordinate

    original, stored, compressed = _layout(frame)
    _coordinate(original, first, count)

    def read() -> bytes:
        raw = _decompress(compressed, stored[-1])
        return b"".join(
            _restore(raw[stored[i] : stored[i + 1]], original[i + 1] - original[i])
            for i in range(first, first + count)
        )

    value = read() if cache is None else cache("scalar-body-parts-v1", f"{first}/{count}", frame, read)
    start = original[first]
    if not isinstance(value, bytes) or len(value) != original[first + count] - start:
        raise ValueError("Invalid pure scalar body parts cache result")
    return [
        value[a - start : b - start]
        for a, b in zip(original[first : first + count], original[first + 1 : first + count + 1])
    ]


def select(frame: bytes, slot: int, cache: Any = None) -> bytes:
    original, stored, compressed = _layout(frame)
    if type(slot) is not int or not 0 <= slot < len(original) - 1:
        raise ValueError("Invalid scalar body slot")
    expected = original[slot + 1] - original[slot]

    def read() -> bytes:
        raw = _decompress(compressed, stored[-1])
        return _restore(raw[stored[slot] : stored[slot + 1]], expected)

    value = read() if cache is None else cache("scalar-body-slot-v1", str(slot), frame, read)
    if not isinstance(value, bytes) or len(value) != expected:
        raise ValueError("Invalid pure scalar body slot cache result")
    return value
