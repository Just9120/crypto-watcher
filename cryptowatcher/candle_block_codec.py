"""Frozen SB1 source tiles; logical candle coordinates and every value stay exact.

No store selects these tiles until its mixed reader/writer and per-entry expiry
adapter is qualified. Header/checksum bind the stream, aligned tile and step.
Each of32 slots contains its complete ordered list of immutable revisions.
"""

from __future__ import annotations

import hashlib
import struct
from typing import Any, Iterator

from . import indexed_frame
from .storage import encode

MAGIC = b"SB1\x00"
SLOTS = 32
MAX_ROWS = 256
MAX_BYTES = 512 * 1024
STEPS = (60000, 180000, 300000, 900000)
# Frozen SB1 order, independent of future fields in other source codecs.
TAIL = ("open", "high", "low", "close", "volume", "turnover", "source", "received_ms", "server_ms")


class Capacity(ValueError):
    """A valid correction history needs the lossless legacy fallback."""


def number(value: Any, *, positive: bool = False) -> int:
    if type(value) is not int or not int(positive) <= value < 2**63:
        raise ValueError("Invalid source block coordinate")
    return value


def coordinates(stream: int, tile: int, step: int) -> None:
    number(stream, positive=True)
    number(tile)
    if (
        type(step) is not int
        or step not in STEPS
        or tile % (SLOTS * step)
        or tile + (SLOTS - 1) * step >= 2**63
    ):
        raise ValueError("Unsupported source tile alignment")


def restored(tile: int, step: int, slot: int, tail: Any) -> dict[str, Any]:
    if type(slot) is not int or not 0 <= slot < SLOTS or not isinstance(tail, list) or len(tail) != len(TAIL):
        raise ValueError("Invalid source candle slot")
    return {"start": tile + slot * step, "step": step, **dict(zip(TAIL, tail, strict=True))}


def public(tile: int, step: int, slot: int, tail: Any) -> dict[str, Any]:
    from .market import MarketError
    from .sources import Candle

    value = restored(tile, step, slot, tail)
    # Match existing source admission, while retaining original int/float types,
    # timestamps and signed zero in `value` rather than coercing to parsed types.
    try:
        candle = Candle(**value)
        parsed = Candle.parse(
            candle.row(), str(step // 60000) + "m", candle.source, candle.received_ms, candle.server_ms
        )
    except (MarketError, TypeError, ValueError, OverflowError) as error:
        raise ValueError("Invalid frozen source candle") from error
    if candle != parsed:
        raise ValueError("Invalid frozen source candle")
    return value


def versions(tile: int, step: int, slot: int, rows: Any, *, validate: bool = True) -> list[Any]:
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ValueError("Invalid source revisions")
    previous = -1
    for row in rows:
        if not isinstance(row, list) or len(row) != 4:
            raise ValueError("Invalid source revision row")
        revision, needed, purge, tail = row
        if number(revision) <= previous:
            raise ValueError("Source revision order changed")
        previous = revision
        number(needed)
        number(purge)
        if validate:
            public(tile, step, slot, tail)
        else:
            restored(tile, step, slot, tail)
    return rows


def pack(stream: int, tile: int, step: int, slots: list[Any]) -> bytes:
    coordinates(stream, tile, step)
    if not isinstance(slots, list) or len(slots) != SLOTS:
        raise ValueError("Invalid source tile shape")
    rows = 0
    for slot, items in enumerate(slots):
        if isinstance(items, list) and rows + len(items) > MAX_ROWS:
            raise Capacity("Source tile row bound")
        rows += len(versions(tile, step, slot, items))
    if rows > MAX_ROWS:
        raise Capacity("Source tile row bound")
    if not rows:
        raise ValueError("Source tile row bound")
    header = struct.pack(">QQQ", stream, tile, step)
    try:
        checked = header + indexed_frame.pack(slots)
    except ValueError as error:
        if str(error) in ("Indexed frame expanded bound", "Indexed compressed frame bound"):
            raise Capacity("Source tile allocation bound") from error
        raise
    result = MAGIC + hashlib.sha256(checked).digest() + checked
    if len(result) > MAX_BYTES:
        raise Capacity("Compressed source tile bound")
    return result


def header(raw: bytes) -> tuple[int, int, int, bytes]:
    if (
        not isinstance(raw, bytes)
        or not 110 < len(raw) <= MAX_BYTES
        or not raw.startswith(MAGIC)
        or hashlib.sha256(raw[36:]).digest() != raw[4:36]
    ):
        raise ValueError("Source tile checksum mismatch")
    stream, tile, step = struct.unpack(">QQQ", raw[36:60])
    coordinates(stream, tile, step)
    frame = raw[60:]
    offsets, _ = indexed_frame.layout(frame)
    if len(offsets) != SLOTS + 1:
        raise ValueError("Source tile cardinality mismatch")
    return stream, tile, step, frame


def unpack(raw: bytes) -> tuple[int, int, int, list[Any]]:
    stream, tile, step, frame = header(raw)
    slots = indexed_frame.all_values(frame)
    count = 0
    for slot, items in enumerate(slots):
        count += len(versions(tile, step, slot, items))
    if not 0 < count <= MAX_ROWS:
        raise ValueError("Source tile row bound")
    return stream, tile, step, slots


def point(
    raw: bytes, stream: int, start: int, revision: int, cache: Any = None
) -> tuple[dict[str, Any], int, int] | None:
    sid, tile, step, frame = header(raw)
    number(start)
    number(revision)
    if type(stream) is not int or sid != stream or start % step or not tile <= start < tile + SLOTS * step:
        raise ValueError("Source tile identity mismatch")
    slot = (start - tile) // step
    # Admission/SQL update guards validate Candle semantics before freezing.
    # Authenticated readers restore typed values exactly, like the legacy
    # source decoder, without parsing/coercing every historical Candle again.
    rows = versions(tile, step, slot, indexed_frame.select(frame, slot, cache), validate=False)
    for rev, needed, purge, tail in rows:
        if rev == revision:
            return restored(tile, step, slot, tail), needed, purge
    return None


def rows(raw: bytes) -> list[tuple[int, int, int, int, int, dict[str, Any]]]:
    sid, tile, step, slots = unpack(raw)
    return [
        (sid, tile + slot * step, revision, needed, purge, restored(tile, step, slot, tail))
        for slot, items in enumerate(slots)
        for revision, needed, purge, tail in items
    ]


def latest(
    raw: bytes, start: int, end: int, *, reverse: bool = False, cache: Any = None
) -> Iterator[tuple[int, int, dict[str, Any]]]:
    """Read one bounded tile, materializing only in-range latest revisions."""
    from .compact_json import unpack as typed_unpack

    _, tile, step, frame = header(raw)
    offsets, data = indexed_frame.data(frame, cache)
    order = range(SLOTS - 1, -1, -1) if reverse else range(SLOTS)
    for slot in order:
        when = tile + slot * step
        if not start <= when < end:
            continue
        items = versions(
            tile,
            step,
            slot,
            typed_unpack(data[offsets[slot] : offsets[slot + 1]], indexed_frame.MAX_BYTES),
            validate=False,
        )
        if items:
            revision, _, _, tail = items[-1]
            yield when, revision, restored(tile, step, slot, tail)


def stats(raw: bytes) -> tuple[int, int, int, int, int]:
    sid, tile, step, slots = unpack(raw)
    entries = [row for part in slots for row in part]
    return sid, tile, step, len(entries), min(row[2] for row in entries)


def preserved(old: bytes, new: bytes) -> list[tuple[int, int, int, int, int, dict[str, Any]]]:
    """Reject changes to an existing source value; return removed coordinates.

    The store must separately qualify every removal against current policy and
    holds. This function never grants expiry or deletion by itself.
    """
    old_id = header(old)[:3]
    if header(new)[:3] != old_id:
        raise ValueError("Source block identity changed")
    before = rows(old)
    current = {(row[1], row[2]): row for row in rows(new)}
    removed = []
    for row in before:
        actual = current.get((row[1], row[2]))
        if actual is None:
            removed.append(row)
        elif encode(row[5]) != encode(actual[5]):
            raise ValueError("Immutable source candle changed")
        elif actual[3] < row[3] or actual[4] < row[4]:
            raise ValueError("Retained source lease shortened")
    return removed
