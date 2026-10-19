"""NB1/NR1: bounded exact metric frames and checked per-slot leases.

The ID is never encoded as a new public identity: (id-1)//64 and (id-1)%64
select the existing ID. No arithmetic, float conversion or source dependency.
"""

from __future__ import annotations

import hashlib
import struct

import zstandard

from .compact_json import integer
from .metric_codec import unpack as metric

SLOTS = 64
LIMIT = 2**63 - 1
MAX_RAW = SLOTS * 34
MAX_FRAME = 4096
MAX_BLOCK = (LIMIT - 1) // SLOTS


def block_number(block: int) -> int:
    if type(block) is not int or not 0 <= block <= MAX_BLOCK:
        raise ValueError("Invalid numeric block identity")
    return block


def number(value: int, *, positive: bool = False) -> int:
    if type(value) is not int or not int(positive) <= value <= LIMIT:
        raise ValueError("Invalid numeric block coordinate/count")
    return value


def coordinate(identity: int) -> tuple[int, int]:
    number(identity, positive=True)
    return divmod(identity - 1, SLOTS)


def seal(magic: bytes, body: bytes) -> bytes:
    return magic + hashlib.sha256(magic + body).digest() + body


def checked(raw: bytes, magic: bytes, maximum: int) -> bytes:
    if (
        not isinstance(raw, bytes)
        or not 44 <= len(raw) <= maximum
        or raw[:4] != magic
        or hashlib.sha256(magic + raw[36:]).digest() != raw[4:36]
    ):
        raise ValueError("Invalid numeric block checksum/bound")
    return raw[36:]


def pack(block: int, frames: list[bytes]) -> bytes:
    block_number(block)
    if len(frames) != SLOTS or not any(frames):
        raise ValueError("Invalid numeric block slots")
    for frame in frames:
        if not isinstance(frame, bytes):
            raise ValueError("Invalid numeric slot bytes")
        if frame:
            metric(frame)
    if block == MAX_BLOCK and frames[-1]:
        raise ValueError("Numeric slot exceeds ID ceiling")
    expanded = b"".join(frames)
    body = struct.pack(">Q", block) + bytes(map(len, frames))
    body += zstandard.ZstdCompressor(level=3, write_checksum=True, threads=0).compress(expanded)
    return seal(b"NB1\x00", body)


def unpack(raw: bytes, block: int, *, validate: bool = True) -> list[bytes]:
    body = checked(raw, b"NB1\x00", MAX_FRAME)
    block_number(block)
    if len(body) <= 72 or struct.unpack(">Q", body[:8])[0] != block:
        raise ValueError("Numeric block identity mismatch")
    lengths = body[8:72]
    size = sum(lengths)
    if any(n not in (0, 26, 34) for n in lengths) or not 0 < size <= MAX_RAW:
        raise ValueError("Invalid numeric block lengths")
    try:
        parameters = zstandard.get_frame_parameters(body[72:])
        if (
            parameters.content_size != size
            or parameters.window_size > MAX_RAW
            or parameters.dict_id
            or not parameters.has_checksum
        ):
            raise ValueError("Invalid numeric block compression bounds")
        expanded = zstandard.ZstdDecompressor(max_window_size=1024).decompress(
            body[72:], max_output_size=size, allow_extra_data=False
        )
    except zstandard.ZstdError as error:
        raise ValueError("Invalid numeric block compression") from error
    if len(expanded) != size:
        raise ValueError("Invalid numeric block expanded size")
    frames = []
    offset = 0
    for length in lengths:
        frame = expanded[offset : offset + length]
        if frame and validate:
            metric(frame)
        frames.append(frame)
        offset += length
    if block == MAX_BLOCK and frames[-1]:
        raise ValueError("Numeric slot exceeds ID ceiling")
    return frames


def counts(block: int, details: list[int], quality: list[int]) -> bytes:
    block_number(block)
    if len(details) != SLOTS or len(quality) != SLOTS:
        raise ValueError("Invalid numeric lease cardinality")
    return seal(
        b"NR1\x00", struct.pack(">Q", block) + b"".join(integer(number(n)) for n in details + quality)
    )


def leases(raw: bytes, block: int) -> tuple[list[int], list[int]]:
    body = checked(raw, b"NR1\x00", 44 + SLOTS * 18)
    if struct.unpack(">Q", body[:8])[0] != block_number(block):
        raise ValueError("Numeric lease identity mismatch")
    values = []
    offset = 8
    for _ in range(SLOTS * 2):
        start = offset
        value = 0
        for shift in range(0, 63, 7):
            if offset >= len(body):
                raise ValueError("Truncated numeric lease")
            byte = body[offset]
            offset += 1
            value |= (byte & 127) << shift
            if not byte & 128:
                break
        else:
            raise ValueError("Numeric lease overflow")
        number(value)
        if integer(value) != body[start:offset]:
            raise ValueError("Noncanonical numeric lease")
        values.append(value)
    if offset != len(body):
        raise ValueError("Trailing numeric lease bytes")
    return values[:SLOTS], values[SLOTS:]
