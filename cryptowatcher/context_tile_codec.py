"""Lossless context tile core, not selected by any runtime writer.

IF1 owns immutable point values/coordinates. Existing NR2 owns two64-slot
vectors for anchor/purge timestamps; OW1 binds the frozen mask. A frozen
point's old SHA256 is exactly derived from its authenticated immutable value.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any

from . import indexed_frame, owner_slots
from . import numeric_block_codec as nc
from .compact_json import unpack as typed_unpack
from .market_storage import DAY
from .storage import encode

MAX_EXPANDED = 1024 * 1024
KINDS = ("instrument", "ticker")


@dataclass(frozen=True)
class Point:
    stream: int
    kind: str
    at: int
    stable: dict[str, Any]
    timing: dict[str, Any]
    anchor: int
    purge: int
    integrity: bytes | None = None


@dataclass(frozen=True)
class Tile:
    identity: int
    stream: int
    kind: str
    day: int
    payload: bytes
    leases: bytes
    frozen: bytes
    seal: bytes


def number(value: Any) -> int:
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError("Invalid context tile integer")
    return value


def public(point: Point) -> dict[str, Any]:
    return {**point.stable, **point.timing}


def digest(point: Point) -> bytes:
    return hashlib.sha256(encode(public(point)).encode()).digest()


def _point(point: Point) -> None:
    if not isinstance(point, Point) or not number(point.stream) or point.kind not in KINDS:
        raise ValueError("Invalid context tile point")
    for value in (point.at, point.anchor, point.purge):
        number(value)
    if (
        not isinstance(point.stable, dict)
        or not isinstance(point.timing, dict)
        or set(point.stable) & {"available_at_ms", "market_observation_ms"}
        or set(point.timing) - {"available_at_ms", "market_observation_ms"}
        or type(point.timing.get("available_at_ms")) is not int
        or point.timing["available_at_ms"] != point.at
        or any(type(value) is not int for value in point.timing.values())
    ):
        raise ValueError("Invalid context tile timing/body")
    if point.integrity is not None and (
        not isinstance(point.integrity, bytes)
        or len(point.integrity) != 32
        or digest(point) != point.integrity
    ):
        raise ValueError("Frozen context tile hash mismatch")


def _header(tile: Tile) -> tuple[int, list[int], list[int]]:
    if (
        not isinstance(tile, Tile)
        or type(tile.identity) is not int
        or not 0 < tile.identity < 2**57
        or not number(tile.stream)
        or tile.kind not in KINDS
        or number(tile.day) > (2**63 - 1) // DAY
        or not isinstance(tile.seal, bytes)
        or owner_slots.signature(tile.stream, tile.identity, tile.frozen) != tile.seal
    ):
        raise ValueError("Invalid context tile header")
    offsets, _ = indexed_frame.layout(tile.payload)
    count = len(offsets) - 1
    if offsets[-1] > MAX_EXPANDED or owner_slots.bits(tile.frozen) >> count:
        raise ValueError("Invalid context tile allocation/mask")
    anchors, purges = nc.leases(tile.leases, tile.identity)
    if any(anchors[count:] + purges[count:]):
        raise ValueError("Context tile has leases outside its points")
    return count, anchors, purges


def get(tile: Tile, slot: int, cache: Any = None) -> Point:
    count, anchors, purges = _header(tile)
    if type(slot) is not int or not 0 <= slot < count:
        raise ValueError("Invalid context tile slot")
    value = indexed_frame.select(tile.payload, slot, cache)
    return _decoded_point(tile, slot, value, anchors, purges)


def _decoded_point(tile: Tile, slot: int, value: Any, anchors: list[int], purges: list[int]) -> Point:
    if (
        not isinstance(value, list)
        or len(value) != 5
        or value[:2] != [tile.stream, tile.kind]
        or type(value[2]) is not int
        or value[2] // DAY != tile.day
    ):
        raise ValueError("Context tile point coordinate mismatch")
    point = Point(value[0], value[1], value[2], value[3], value[4], anchors[slot], purges[slot])
    _point(point)
    if len(encode(value).encode()) > MAX_EXPANDED:
        raise ValueError("Context tile point exceeds bound")
    return replace(point, integrity=digest(point)) if owner_slots.has(tile.frozen, slot) else point


def _payload_values(payload: bytes, cache: Any = None) -> list[Any]:
    # Authenticate current bytes before a pure derivative can be reused. The
    # cache never contains tile rows, leases, masks or mutable Python objects.
    offsets, _ = indexed_frame.layout(payload)

    def read() -> list[Any]:
        _, raw = indexed_frame.data(payload)
        values = [typed_unpack(raw[a:b], MAX_EXPANDED) for a, b in zip(offsets, offsets[1:])]
        scope = None
        previous = -1
        for value in values:
            if not isinstance(value, list) or len(value) != 5:
                raise ValueError("Context tile point coordinate mismatch")
            point = Point(value[0], value[1], value[2], value[3], value[4], 0, 0)
            _point(point)
            current = (point.stream, point.kind, point.at // DAY)
            if (scope is not None and current != scope) or point.at <= previous:
                raise ValueError("Unordered or conflicting context tile points")
            scope, previous = current, point.at
        if len(encode(values).encode()) > MAX_EXPANDED:
            raise ValueError("Context tile expanded admission bound")
        return values

    if cache is None:
        return read()
    raw = cache("context-tile-values", "", payload, lambda: encode(read()).encode())
    if not isinstance(raw, bytes):
        raise ValueError("Invalid pure context tile cache result")
    values = json.loads(raw)
    if not isinstance(values, list) or len(values) != len(offsets) - 1:
        raise ValueError("Invalid pure context tile cache result")
    return values


def _current(tile: Tile, cache: Any = None) -> tuple[list[int], list[int], list[Any]]:
    count, anchors, purges = _header(tile)
    values = _payload_values(tile.payload, cache)
    if len(values) != count or values[0][:2] != [tile.stream, tile.kind] or values[0][2] // DAY != tile.day:
        raise ValueError("Context tile point coordinate mismatch")
    return anchors, purges, values


def times(tile: Tile, cache: Any = None) -> list[int]:
    _, _, values = _current(tile, cache)
    return [value[2] for value in values]


def monotonic(old: Tile, new: Tile, cache: Any = None) -> bool:
    """Current headers plus one immutable payload; never reuse mutable leases."""
    before, purges, _ = _current(old, cache)
    _, after, updated_purges = _header(new)
    if (old.identity, old.stream, old.kind, old.day, old.payload) != (
        new.identity,
        new.stream,
        new.kind,
        new.day,
        new.payload,
    ) or owner_slots.bits(old.frozen) & ~owner_slots.bits(new.frozen):
        return False
    return all(a >= b for a, b in zip(after, before)) and all(a >= b for a, b in zip(updated_purges, purges))


def points(tile: Tile, cache: Any = None) -> list[Point]:
    anchors, purges, values = _current(tile, cache)
    rows = [_decoded_point(tile, slot, value, anchors, purges) for slot, value in enumerate(values)]
    if any(a.at >= b.at for a, b in zip(rows, rows[1:])):
        raise ValueError("Unordered context tile points")
    if len(encode([_value(row) for row in rows]).encode()) > MAX_EXPANDED:
        raise ValueError("Context tile expanded admission bound")
    return rows


def _value(point: Point) -> list[Any]:
    return [point.stream, point.kind, point.at, point.stable, point.timing]


def signature(rows: list[Point]) -> str:
    return encode(
        [
            [*_value(row), row.anchor, row.purge, row.integrity.hex() if row.integrity is not None else None]
            for row in rows
        ]
    )


def pack(identity: int, rows: list[Point]) -> Tile:
    if type(identity) is not int or not 0 < identity < 2**57 or not 0 < len(rows) <= 64:
        raise ValueError("Context tile identity/cardinality bound")
    for row in rows:
        _point(row)
    stream, kind, day = rows[0].stream, rows[0].kind, rows[0].at // DAY
    if any((row.stream, row.kind, row.at // DAY) != (stream, kind, day) for row in rows) or any(
        a.at >= b.at for a, b in zip(rows, rows[1:])
    ):
        raise ValueError("Context tile crosses its immutable coordinate boundary")
    values = [_value(row) for row in rows]
    if len(encode(values).encode()) > MAX_EXPANDED:
        raise indexed_frame.FrameSizeError("Context tile expanded admission bound")
    payload = indexed_frame.pack(values)
    anchors = [row.anchor for row in rows] + [0] * (64 - len(rows))
    purges = [row.purge for row in rows] + [0] * (64 - len(rows))
    leases = nc.counts(identity, anchors, purges, compressed=True)
    frozen = owner_slots.mask(slot for slot, row in enumerate(rows) if row.integrity is not None)
    tile = Tile(
        identity, stream, kind, day, payload, leases, frozen, owner_slots.signature(stream, identity, frozen)
    )
    if signature(points(tile)) != signature(rows):
        raise ValueError("Context tile changed source values or leases")
    return tile


def renew(tile: Tile, slot: int, *, anchor: int, purge: int) -> Tile:
    old = get(tile, slot)
    anchor, purge = max(old.anchor, number(anchor)), max(old.purge, number(purge))
    if (anchor, purge) == (old.anchor, old.purge):
        return tile
    _, anchors, purges = _header(tile)
    anchors[slot], purges[slot] = anchor, purge
    candidate = replace(tile, leases=nc.counts(tile.identity, anchors, purges, compressed=True))
    if signature([get(candidate, slot)]) != signature([replace(old, anchor=anchor, purge=purge)]):
        raise ValueError("Context tile renewal changed frozen values")
    return candidate


def freeze(tile: Tile, slot: int, expected: bytes) -> Tile:
    point = get(tile, slot)
    if not isinstance(expected, bytes) or len(expected) != 32 or digest(point) != expected:
        raise ValueError("Context tile freeze hash mismatch")
    if point.integrity is not None:
        return tile
    frozen = (owner_slots.bits(tile.frozen) | (1 << slot)).to_bytes(8, "big")
    return replace(tile, frozen=frozen, seal=owner_slots.signature(tile.stream, tile.identity, frozen))


def expire(tile: Tile, now: int, *, candle_days: int, detail_days: int) -> Tile | None:
    number(now)
    if any(type(days) is not int or not 0 < days < 2**31 for days in (candle_days, detail_days)):
        raise ValueError("Invalid context tile retention policy")
    original = points(tile)
    retained = []
    for point in original:
        due = max(point.purge, point.at + candle_days * DAY, point.anchor + detail_days * DAY)
        number(due)
        if due > now:
            retained.append(replace(point, purge=due))
    if not retained:
        return None
    return tile if signature(retained) == signature(original) else pack(tile.identity, retained)


def stats(tile: Tile, cache: Any = None) -> dict[str, Any]:
    _, purges, values = _current(tile, cache)
    return {
        "identity": tile.identity,
        "stream": tile.stream,
        "kind": tile.kind,
        "day": tile.day,
        "count": len(values),
        "first_at": values[0][2],
        "last_at": values[-1][2],
        "first_due": min(purges[: len(values)]),
        "frozen": owner_slots.bits(tile.frozen).bit_count(),
    }
