"""Bounded shared compression with unchanged64-slot logical body views.

This codec installs no schema or writer. Whole frames own immutable typed
bytes; each view selects the original packet's contiguous body slots. Decoder
and shared-cache limits stay those of the existing indexed frame codec.
"""

from __future__ import annotations

import hashlib
import struct
from typing import Any

import zstandard

from . import indexed_frame
from .compact_json import unpack as typed_unpack

MAGIC = b"SB1\x00"
VIEW_MAGIC = b"SV1\x00"
MAX_RECORDS = 2048
MAX_PACKETS = 32
MAX_BYTES = indexed_frame.MAX_BYTES
MAX_COMPRESSED = indexed_frame.MAX_COMPRESSED


def pack_frames(frames: list[bytes]) -> bytes:
    """Keep every original canonical typed slice, including unknown fields."""
    if not 0 < len(frames) <= MAX_PACKETS:
        raise ValueError("Shared body packet cardinality bound")
    offsets = [0]
    parts: list[bytes] = []
    for frame in frames:
        positions, raw = indexed_frame.data(frame)
        for start, end in zip(positions, positions[1:]):
            if len(parts) == MAX_RECORDS or offsets[-1] + end - start > MAX_BYTES:
                raise indexed_frame.FrameSizeError("Shared body expanded bound")
            part = raw[start:end]
            typed_unpack(part, MAX_BYTES)
            offsets.append(offsets[-1] + len(part))
            parts.append(part)
    raw = b"".join(parts)
    compressed = zstandard.ZstdCompressor(level=3, write_checksum=True, threads=0).compress(raw)
    if len(raw) >= 4096:
        dense = zstandard.ZstdCompressor(level=19, write_checksum=True, threads=0).compress(raw)
        if len(dense) < len(compressed):
            compressed = dense
    header = struct.pack(">HI", len(parts), len(raw)) + struct.pack(">" + "I" * len(offsets), *offsets)
    checked = header + compressed
    result = MAGIC + hashlib.sha256(checked).digest() + checked
    if len(result) > MAX_COMPRESSED:
        raise indexed_frame.FrameSizeError("Shared body compressed bound")
    return result


def layout(frame: bytes) -> tuple[tuple[int, ...], bytes]:
    if (
        not isinstance(frame, bytes)
        or not 50 < len(frame) <= MAX_COMPRESSED
        or not frame.startswith(MAGIC)
        or hashlib.sha256(frame[36:]).digest() != frame[4:36]
    ):
        raise ValueError("Shared body checksum mismatch")
    count, size = struct.unpack(">HI", frame[36:42])
    if not 0 < count <= MAX_RECORDS or not 0 < size <= MAX_BYTES:
        raise ValueError("Shared body allocation bound")
    end = 42 + (count + 1) * 4
    if end >= len(frame):
        raise ValueError("Truncated shared body offsets")
    offsets = struct.unpack(">" + "I" * (count + 1), frame[42:end])
    if offsets[0] != 0 or offsets[-1] != size or any(b <= a for a, b in zip(offsets, offsets[1:])):
        raise ValueError("Invalid shared body offsets")
    compressed = frame[end:]
    try:
        parameters = zstandard.get_frame_parameters(compressed)
    except zstandard.ZstdError as error:
        raise ValueError("Invalid shared body compression") from error
    if (
        parameters.content_size != size
        or parameters.window_size > MAX_BYTES
        or parameters.dict_id
        or not parameters.has_checksum
    ):
        raise ValueError("Shared body decompression bound")
    return offsets, compressed


def data(frame: bytes, cache: Any = None) -> tuple[tuple[int, ...], bytes]:
    offsets, compressed = layout(frame)
    return offsets, _data(frame, offsets, compressed, cache)


def _data(frame: bytes, offsets: tuple[int, ...], compressed: bytes, cache: Any) -> bytes:
    def read() -> bytes:
        try:
            raw = zstandard.ZstdDecompressor(max_window_size=MAX_BYTES // 1024).decompress(
                compressed, max_output_size=offsets[-1], allow_extra_data=False
            )
        except zstandard.ZstdError as error:
            raise ValueError("Invalid shared body compression") from error
        if len(raw) != offsets[-1]:
            raise ValueError("Incomplete shared body")
        return raw

    raw = read() if cache is None else cache("shared-body-frame-v1", "", frame, read)
    if not isinstance(raw, bytes) or len(raw) != offsets[-1]:
        raise ValueError("Invalid pure shared body cache result")
    return raw


def view(frame: bytes, first: int, count: int) -> bytes:
    offsets, _ = layout(frame)
    _coordinate(offsets, first, count)
    return VIEW_MAGIC + struct.pack(">HH", first, count) + frame


def _coordinate(offsets: tuple[int, ...], first: int, count: int) -> None:
    if (
        type(first) is not int
        or type(count) is not int
        or not 0 <= first < first + count < len(offsets)
        or not 0 < count <= indexed_frame.MAX_RECORDS
    ):
        raise ValueError("Invalid shared body view coordinate")


def view_layout(value: bytes) -> tuple[int, int, bytes]:
    first, count, frame, _, _ = _view(value)
    return first, count, frame


def _view(value: bytes) -> tuple[int, int, bytes, tuple[int, ...], bytes]:
    if not isinstance(value, bytes) or len(value) <= 8 or not value.startswith(VIEW_MAGIC):
        raise ValueError("Invalid shared body view")
    first, count = struct.unpack(">HH", value[4:8])
    frame = value[8:]
    offsets, compressed = layout(frame)
    _coordinate(offsets, first, count)
    return first, count, frame, offsets, compressed


def parts(value: bytes, cache: Any = None) -> list[bytes]:
    first, count, frame, offsets, compressed = _view(value)
    raw = _data(frame, offsets, compressed, cache)
    return [raw[offsets[at] : offsets[at + 1]] for at in range(first, first + count)]


def select(value: bytes, slot: int, cache: Any = None) -> Any:
    first, count, frame, offsets, compressed = _view(value)
    if type(slot) is not int or not 0 <= slot < count:
        raise ValueError("Invalid shared body slot")
    raw = _data(frame, offsets, compressed, cache)
    at = first + slot
    return typed_unpack(raw[offsets[at] : offsets[at + 1]], MAX_BYTES)
