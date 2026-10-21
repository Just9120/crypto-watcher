"""BF1: bounded self-contained groups of exact NB1/NB2 private metric frames.

Neither a source-price lookup nor a future calculator is required to restore a
block. IDs, slots, literal types and original PR1 operands remain unchanged.
The small offset index is a pure derivative in the existing byte cache only.
"""

from __future__ import annotations

import hashlib
import struct
from itertools import accumulate
from typing import Any

import zstandard

from . import numeric_block_codec as blocks
from . import numeric_frame_native

MAGIC = b"BF1\x00"
MAX_BLOCKS = 64
DEAD = 2**64 - 1
MAX_RAW = 5 + MAX_BLOCKS * (8 + 1 + blocks.SLOTS + blocks.MAX_RAW)
MAX_FRAME = MAX_RAW + 1024
# The binding accepts at least1024KiB for this option. The actual declared
# content/window are checked against MAX_RAW before creating the decompressor.
MAX_WINDOW_KIB = max(1024, (MAX_RAW + 1023) // 1024)
# C-backed translate/strip implements the same per-byte length predicate.
# Zero denotes an allowed length; every disallowed length maps to one.
_LITERAL_LENGTHS = bytes(0 if size in (0, 26, 34) else 1 for size in range(256))
_RECIPE_LENGTHS = bytes(0 if size == 0 or 6 <= size <= 34 else 1 for size in range(256))


def _parts(value: tuple[int, bytes] | None) -> tuple[int, int, list[bytes]]:
    if value is None:
        return DEAD, 0, [b""] * blocks.SLOTS
    if not isinstance(value, tuple) or len(value) != 2:
        raise ValueError("Invalid numeric frame entry")
    identity, raw = value
    frames = blocks.unpack(raw, identity)
    return identity, int(raw.startswith(b"NB2\x00")), frames


def pack(values: list[tuple[int, bytes] | None]) -> bytes:
    if not isinstance(values, list) or not 0 < len(values) <= MAX_BLOCKS:
        raise ValueError("Invalid numeric frame cardinality")
    entries = [_parts(value) for value in values]
    ids = [identity for identity, _, _ in entries if identity != DEAD]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("Empty or duplicate numeric frame identities")
    flat = [frame for _, _, frames in entries for frame in frames]
    lengths = bytes(map(len, flat))
    header = bytes([len(entries)])
    header += b"".join(struct.pack(">Q", identity) for identity, _, _ in entries)
    header += bytes(flag for _, flag, _ in entries) + lengths
    row = b"BR1\x00" + header + b"".join(flat)
    plane = (
        b"BP1\x00"
        + header
        + b"".join(
            bytes(frame[index] for frame in flat if len(frame) > index) for index in range(max(lengths))
        )
    )
    candidates = [
        zstandard.ZstdCompressor(level=level, write_checksum=True, threads=0).compress(raw)
        for raw in (row, plane)
        for level in (3, 19)
    ]
    compressed = min(candidates, key=len)
    raw = MAGIC + hashlib.sha256(MAGIC + compressed).digest() + compressed
    if len(raw) > MAX_FRAME:
        raise ValueError("Numeric frame exceeds bound")
    return raw


def _expanded(raw: bytes) -> bytes:
    compressed = raw[36:]
    try:
        parameters = zstandard.get_frame_parameters(compressed)
        if (
            not 5 < parameters.content_size <= MAX_RAW
            or parameters.window_size > MAX_RAW
            or parameters.dict_id
            or not parameters.has_checksum
        ):
            raise ValueError("Invalid numeric frame compression bounds")
        body = zstandard.ZstdDecompressor(max_window_size=MAX_WINDOW_KIB).decompress(
            compressed, max_output_size=MAX_RAW, allow_extra_data=False
        )
    except zstandard.ZstdError as error:
        raise ValueError("Invalid numeric frame compression") from error
    native = numeric_frame_native.flatten(body)
    if native is not None:
        return native
    if body[:4] not in (b"BR1\x00", b"BP1\x00"):
        raise ValueError("Unknown numeric frame representation")
    n = body[4]
    if not 0 < n <= MAX_BLOCKS or len(body) < 5 + n * (9 + blocks.SLOTS):
        raise ValueError("Invalid numeric frame shape")
    ids = struct.unpack(">" + "Q" * n, body[5 : 5 + n * 8])
    position = 5 + n * 8
    flags = body[position : position + n]
    position += n
    lengths = body[position : position + n * blocks.SLOTS]
    position += n * blocks.SLOTS
    sizes_by_block = [lengths[i * blocks.SLOTS : (i + 1) * blocks.SLOTS] for i in range(n)]
    spans = list(map(sum, sizes_by_block))
    if position + sum(spans) != len(body):
        raise ValueError("Invalid numeric frame expanded size")
    uniform_lengths = (
        _LITERAL_LENGTHS if not flags.strip(b"\0") else _RECIPE_LENGTHS if not flags.strip(b"\1") else None
    )
    valid_ids = []
    for i, identity in enumerate(ids):
        sizes = sizes_by_block[i]
        if identity == DEAD:
            if flags[i] or spans[i]:
                raise ValueError("Invalid empty numeric frame slot")
            continue
        valid_ids.append(identity)
        if flags[i] not in (0, 1) or not spans[i]:
            raise ValueError("Invalid numeric frame metric block")
        if uniform_lengths is None and sizes.translate(
            _RECIPE_LENGTHS if flags[i] else _LITERAL_LENGTHS
        ).strip(b"\0"):
            raise ValueError("Invalid numeric frame metric lengths")
        if identity == blocks.MAX_BLOCK and sizes[-1]:
            raise ValueError("Numeric frame exceeds ID ceiling")
    if uniform_lengths is not None and lengths.translate(uniform_lengths).strip(b"\0"):
        raise ValueError("Invalid numeric frame metric lengths")
    # struct.unpack('Q') already supplies nonnegative plain integers. Check
    # their shared ceiling in C once, retaining DEAD's separate slot rules.
    if valid_ids and max(valid_ids) > blocks.MAX_BLOCK:
        raise ValueError("Invalid numeric block identity")
    if not valid_ids or len(valid_ids) != len(set(valid_ids)):
        raise ValueError("Empty or duplicate numeric frame identities")
    row_data = plane_rows(lengths, body[position:]) if body[:4] == b"BP1\x00" else body[position:]
    entries = []
    position = 0
    for i, identity in enumerate(ids):
        sizes = sizes_by_block[i]
        span = spans[i]
        entries.append(
            struct.pack(">Q", identity) + flags[i : i + 1] + sizes + row_data[position : position + span]
        )
        position += span
    offsets = list(accumulate(map(len, entries), initial=0))
    return bytes([n]) + struct.pack(">" + "I" * (n + 1), *offsets) + b"".join(entries)


def plane_rows(lengths: bytes, planes: bytes) -> bytes:
    """Transpose already bounded/validated planes, retaining a portable path."""
    native = numeric_frame_native.transpose(lengths, planes)
    if native is not None:
        return native
    positive_sizes = [size for size in lengths if size]
    width = min(positive_sizes)
    count = len(positive_sizes)
    prefix = bytearray(count * width)
    position = 0
    for index in range(width):
        prefix[index::width] = planes[position : position + count]
        position += count
    flat = [bytearray(prefix[start : start + width]) for start in range(0, len(prefix), width)]
    active = [(frame, size) for frame, size in zip(flat, positive_sizes, strict=True) if size > width]
    for index in range(width, max(positive_sizes)):
        active = [(frame, size) for frame, size in active if size > index]
        for frame, _ in active:
            frame.append(planes[position])
            position += 1
    return b"".join(flat)


def data(raw: bytes, cache: Any = None) -> bytes:
    # Check the actual current bytes before looking up any pure derivative.
    if (
        not isinstance(raw, bytes)
        or not 36 < len(raw) <= MAX_FRAME
        or raw[:4] != MAGIC
        or hashlib.sha256(MAGIC + raw[36:]).digest() != raw[4:36]
    ):
        raise ValueError("Invalid numeric frame checksum/bound")

    def load() -> bytes:
        return _expanded(raw)

    return load() if cache is None else bytes(cache("numeric-frame-data", "", raw, load))


def selected(raw: bytes, slot: int, cache: Any = None) -> tuple[int, bytes] | None:
    return _selected(data(raw, cache), slot, cache)


def _selected(expanded: bytes, slot: int, cache: Any) -> tuple[int, bytes] | None:
    if type(slot) is not int or not 0 <= slot < expanded[0]:
        raise ValueError("Invalid numeric frame slot")
    count = expanded[0]
    begin, end = struct.unpack(">II", expanded[1 + slot * 4 : 1 + (slot + 2) * 4])
    offset = 1 + (count + 1) * 4
    entry = expanded[offset + begin : offset + end]
    identity = struct.unpack(">Q", entry[:8])[0]
    if identity == DEAD:
        return None

    def restore() -> bytes:
        frames = []
        position = 9 + blocks.SLOTS
        for size in entry[9 : 9 + blocks.SLOTS]:
            frames.append(entry[position : position + size])
            position += size
        raw = blocks.pack(identity, frames, recipes=bool(entry[8]))
        if entry[8] and not raw.startswith(b"NB2\x00"):
            # NB2 permits a valid literal-only block too. Keep its original
            # format flag even when today's chooser would have written NB1.
            raw = blocks.seal(b"NB2\x00", blocks.header(raw))
        return raw

    restored = (
        restore() if cache is None else bytes(cache("numeric-frame-block", str(identity), entry, restore))
    )
    return identity, restored


def all_values(raw: bytes, cache: Any = None) -> list[tuple[int, bytes] | None]:
    expanded = data(raw, cache)
    return [_selected(expanded, slot, cache) for slot in range(expanded[0])]


def entry_bytes(raw: bytes, slot: int, cache: Any = None) -> bytes:
    """Stable uncompressed identity/type/length/value bytes for the NS2 seal."""
    expanded = data(raw, cache)
    if type(slot) is not int or not 0 <= slot < expanded[0]:
        raise ValueError("Invalid numeric frame slot")
    begin, end = struct.unpack(">II", expanded[1 + slot * 4 : 1 + (slot + 2) * 4])
    offset = 1 + (expanded[0] + 1) * 4
    return expanded[offset + begin : offset + end]


def raw_entries(raw: bytes, cache: Any = None) -> list[bytes]:
    """One bounded decode for explicit owner/seal boundary reconciliation."""
    expanded = data(raw, cache)
    n = expanded[0]
    offsets = struct.unpack(">" + "I" * (n + 1), expanded[1 : 1 + (n + 1) * 4])
    offset = 1 + (n + 1) * 4
    return [expanded[offset + offsets[i] : offset + offsets[i + 1]] for i in range(n)]
