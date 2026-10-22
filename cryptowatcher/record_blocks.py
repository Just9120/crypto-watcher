"""Frozen lossless immutable domain blocks, independent of runtime activation.

Projection and detail frames are separate. Metadata is bound to the actual
compressed bytes by a full SHA256, so a catalogue slot can never silently name
another public identity. Unsupported shapes remain in the legacy layout.
"""

from __future__ import annotations

import hashlib
import struct
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

import zstandard

from . import indexed_frame, shared_body_frame
from .compact_json import unpack as typed_unpack
from .record_codec import BINARY_MAGIC, TYPED_MAGIC, pack_binary, sql_payload
from .storage import encode

LEGACY_MAGIC = b"DB1\x00"
MAGIC = b"DB2\x00"
SHARED_MAGIC = b"DB3\x00"
KINDS = ("event", "outcome", "episode", "comparison", "decision")
MAX_RECORDS = 64
MAX_EXPANDED = 1024 * 1024
MAX_PACKET = 512 * 1024
HEAD_SCHEMA = "cryptowatcher.history-head.v1"


@dataclass(frozen=True)
class Coordinate:
    kind: str
    identity: str
    revision: int

    def validate(self) -> None:
        if (
            self.kind not in KINDS
            or type(self.revision) is not int
            or not 0 < self.revision <= 9999999999
            or not isinstance(self.identity, str)
            or str(uuid.UUID(self.identity)) != self.identity
        ):
            raise ValueError("Invalid domain record coordinate")

    @property
    def field(self) -> str:
        return "event_id" if self.kind == "decision" else self.kind + "_id"

    @property
    def key(self) -> str:
        prefix = "decision:telegram" if self.kind == "decision" else self.kind
        return f"{prefix}:{self.identity}:{self.revision:010d}"


def compact(coordinate: Coordinate, value: dict[str, Any]) -> dict[str, Any]:
    coordinate.validate()
    if (
        not isinstance(value, dict)
        or value.get(coordinate.field) != coordinate.identity
        or type(value.get("revision")) is not int
        or value["revision"] != coordinate.revision
    ):
        raise ValueError("Domain record does not match coordinate")
    return {k: v for k, v in value.items() if k not in (coordinate.field, "revision")}


def expanded(coordinate: Coordinate, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or coordinate.field in value or "revision" in value:
        raise ValueError("Invalid stripped domain record")
    return {**value, coordinate.field: coordinate.identity, "revision": coordinate.revision}


def head_compact(coordinate: Coordinate, head: dict[str, Any] | None) -> Any:
    if head is None:
        return None
    if (
        coordinate.kind == "decision"
        or head.get("schema") != HEAD_SCHEMA
        or head.get("revision_key") != coordinate.key
    ):
        raise ValueError("Invalid projected domain head")
    result = compact(coordinate, head)
    return {k: v for k, v in result.items() if k not in ("schema", "revision_key")}


def head_expanded(coordinate: Coordinate, value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if (
        coordinate.kind == "decision"
        or not isinstance(value, dict)
        or any(name in value for name in ("schema", "revision_key"))
    ):
        raise ValueError("Invalid projected block head")
    return {**expanded(coordinate, value), "schema": HEAD_SCHEMA, "revision_key": coordinate.key}


def batches(
    entries: Iterable[tuple[Coordinate, dict[str, Any], dict[str, Any] | None]],
) -> Iterator[list[tuple[Coordinate, dict[str, Any], dict[str, Any] | None]]]:
    """Bound the materialized batch before compression, including projections.

    A caller encountering an oversized single record must leave it in the
    legacy layout. No data is truncated or silently removed from this iterator.
    """
    result: list[tuple[Coordinate, dict[str, Any], dict[str, Any] | None]] = []
    size = 0
    maximum = MAX_PACKET - 65536
    for entry in entries:
        coordinate, value, head = entry
        coordinate.validate()
        cost = len(encode(value).encode()) + len(encode(head).encode()) + 128
        if cost > maximum:
            raise ValueError("Domain record requires legacy storage")
        if result and (len(result) == MAX_RECORDS or size + cost > maximum):
            yield result
            result, size = [], 0
        result.append(entry)
        size += cost
    if result:
        yield result


def pack(
    entries: list[tuple[Coordinate, dict[str, Any], dict[str, Any] | None]], *, legacy: bool = False
) -> bytes:
    if not 0 < len(entries) <= MAX_RECORDS:
        raise ValueError("Domain block record bound")
    projections = []
    bodies = []
    seen = set()
    for coordinate, value, head in entries:
        if coordinate in seen:
            raise ValueError("Repeated domain block coordinate")
        seen.add(coordinate)
        bodies.append(compact(coordinate, value))
        projections.append(
            [
                KINDS.index(coordinate.kind),
                coordinate.identity,
                coordinate.revision,
                head_compact(coordinate, head),
            ]
        )
    texts = encode(projections), encode(bodies)
    if sum(len(t.encode()) for t in texts) > MAX_EXPANDED:
        raise indexed_frame.FrameSizeError("Expanded domain block bound")
    if legacy:
        projection, body = (pack_binary(t) for t in texts)
    else:
        projection, body = indexed_frame.pack(projections), indexed_frame.pack(bodies)
    frames = struct.pack(">I", len(projection)) + projection + body
    raw = (LEGACY_MAGIC if legacy else MAGIC) + hashlib.sha256(frames).digest() + frames
    if len(raw) > MAX_PACKET:
        raise indexed_frame.FrameSizeError("Compressed domain block bound")
    decoded = parse(body) if legacy else indexed_frame.all_values(body)
    if encode([expanded(c, v) for (c, _, _), v in zip(entries, decoded, strict=True)]) != encode(
        [v for _, v, _ in entries]
    ):
        raise ValueError("Domain block changed records")
    return raw


def frames(raw: bytes) -> tuple[bytes, bytes]:
    if (
        not isinstance(raw, bytes)
        or not 40 < len(raw) <= MAX_PACKET
        or not raw.startswith((LEGACY_MAGIC, MAGIC, SHARED_MAGIC))
        or hashlib.sha256(raw[36:]).digest() != raw[4:36]
    ):
        raise ValueError("Invalid domain block checksum")
    size = struct.unpack(">I", raw[36:40])[0]
    if not 0 < size < len(raw) - 40:
        raise ValueError("Invalid projected frame length")
    projection, body = raw[40 : 40 + size], raw[40 + size :]
    if raw.startswith(SHARED_MAGIC) != body.startswith(shared_body_frame.VIEW_MAGIC):
        raise ValueError("Invalid shared domain body version")
    return projection, body


def _body_count(frame: bytes) -> int:
    if frame.startswith(shared_body_frame.VIEW_MAGIC):
        return shared_body_frame.view_layout(frame)[1]
    return len(indexed_frame.layout(frame)[0]) - 1


def _body_parts(frame: bytes) -> list[bytes]:
    if frame.startswith(shared_body_frame.VIEW_MAGIC):
        return shared_body_frame.parts(frame)
    offsets, raw = indexed_frame.data(frame)
    return [raw[start:end] for start, end in zip(offsets, offsets[1:])]


def _body_select(frame: bytes, slot: int, cache: Any) -> Any:
    if frame.startswith(shared_body_frame.VIEW_MAGIC):
        return shared_body_frame.select(frame, slot, cache)
    return indexed_frame.select(frame, slot, cache)


def with_shared_body(raw: bytes, frame: bytes, first: int) -> bytes:
    """Re-encode only an exact body view; coordinates and projections stay fixed."""
    if not isinstance(raw, bytes) or not raw.startswith((MAGIC, SHARED_MAGIC)):
        raise ValueError("Shared bodies require an indexed domain packet")
    projection, body = frames(raw)
    count = len(projections(raw))
    view = shared_body_frame.view(frame, first, count)
    if _body_parts(body) != shared_body_frame.parts(view):
        raise ValueError("Shared body changed immutable domain values")
    checked = struct.pack(">I", len(projection)) + projection + view
    result = SHARED_MAGIC + hashlib.sha256(checked).digest() + checked
    if len(result) > MAX_PACKET:
        raise indexed_frame.FrameSizeError("Compressed shared domain block bound")
    return result


def parse(frame: bytes) -> Any:
    import json

    if frame.startswith(TYPED_MAGIC):
        offset = 40
        if not 0 < int.from_bytes(frame[4:8], "big") <= MAX_EXPANDED:
            raise ValueError("Expanded typed domain bound")
    elif frame.startswith(BINARY_MAGIC):
        offset = 36
    else:
        raise ValueError("Unsupported domain frame")
    parameters = zstandard.get_frame_parameters(frame[offset:])
    if not 0 < parameters.content_size <= MAX_EXPANDED or parameters.window_size > MAX_EXPANDED:
        raise ValueError("Expanded domain frame bound")
    # Bound allocation before decompressing; the underlying frozen typed codec
    # then checks canonical bytes, finite numbers/types and framing.
    text = sql_payload(frame)
    if len(text.encode()) > MAX_EXPANDED:
        raise ValueError("Expanded domain frame bound")
    return json.loads(text)


def projections(raw: bytes) -> list[tuple[Coordinate, dict[str, Any] | None]]:
    frame = frames(raw)[0]
    values = parse(frame) if raw.startswith(LEGACY_MAGIC) else indexed_frame.all_values(frame)
    if not isinstance(values, list) or not 0 < len(values) <= MAX_RECORDS:
        raise ValueError("Invalid domain projection shape")
    result = []
    seen = set()
    for row in values:
        if (
            not isinstance(row, list)
            or len(row) != 4
            or type(row[0]) is not int
            or not 0 <= row[0] < len(KINDS)
        ):
            raise ValueError("Invalid domain projection row")
        coordinate, head = projection(row)
        if coordinate in seen:
            raise ValueError("Duplicate domain projection coordinate")
        seen.add(coordinate)
        result.append((coordinate, head))
    return result


def projection(row: Any) -> tuple[Coordinate, dict[str, Any] | None]:
    if not isinstance(row, list) or len(row) != 4 or type(row[0]) is not int or not 0 <= row[0] < len(KINDS):
        raise ValueError("Invalid domain projection row")
    coordinate = Coordinate(KINDS[row[0]], row[1], row[2])
    coordinate.validate()
    return coordinate, head_expanded(coordinate, row[3])


def bodies(raw: bytes) -> list[Any]:
    frame = frames(raw)[1]
    values = (
        parse(frame)
        if raw.startswith(LEGACY_MAGIC)
        else [typed_unpack(part, indexed_frame.MAX_BYTES) for part in _body_parts(frame)]
    )
    if not isinstance(values, list) or not 0 < len(values) <= MAX_RECORDS:
        raise ValueError("Domain body cardinality mismatch")
    return values


def checked_projected(
    raw: bytes, slot: int, expected: Coordinate, cache: Any = None
) -> dict[str, Any] | None:
    """Validate every projection while caching only a pure immutable-byte result.

    The caller still reads current packet bytes and SQL bindings. This cache
    owns neither a binding nor a mutable head; selected heads are decoded anew.
    """
    frame = frames(raw)[0]

    def validate() -> bytes:
        projections(raw)
        return b"validated"

    result = validate() if cache is None else cache("domain-projections-v1", "", frame, validate)
    if result != b"validated":
        raise ValueError("Invalid pure projection validation result")
    return projected(raw, slot, expected, cache)


def projected(raw: bytes, slot: int, expected: Coordinate, cache: Any = None) -> dict[str, Any] | None:
    p, b = frames(raw)
    if raw.startswith((MAGIC, SHARED_MAGIC)):
        p_offsets, _ = indexed_frame.layout(p)
        if len(p_offsets) - 1 != _body_count(b):
            raise ValueError("Domain body cardinality mismatch")
        actual, head = projection(indexed_frame.select(p, slot, cache))
    else:
        values = projections(raw)
        if type(slot) is not int or not 0 <= slot < len(values):
            raise ValueError("Domain slot identity mismatch")
        actual, head = values[slot]
    if actual != expected:
        raise ValueError("Domain slot identity mismatch")
    return head


def read(raw: bytes, slot: int, expected: Coordinate, cache: Any = None) -> dict[str, Any]:
    p, b = frames(raw)
    if raw.startswith((MAGIC, SHARED_MAGIC)):
        p_offsets, _ = indexed_frame.layout(p)
        if len(p_offsets) - 1 != _body_count(b):
            raise ValueError("Domain body cardinality mismatch")
        actual, _ = projection(indexed_frame.select(p, slot, cache))
        if actual != expected:
            raise ValueError("Domain slot identity mismatch")
        return expanded(expected, _body_select(b, slot, cache))
    values = projections(raw)
    if type(slot) is not int or not 0 <= slot < len(values) or values[slot][0] != expected:
        raise ValueError("Domain slot identity mismatch")
    bodies = parse(frames(raw)[1])
    if not isinstance(bodies, list) or len(bodies) != len(values):
        raise ValueError("Domain body cardinality mismatch")
    return expanded(expected, bodies[slot])
