"""Frozen private UUID recipes with byte-exact ordinary IF1 reconstruction.

Explicit codec only; no schema, publisher or activation. Each shared frame
retains the same32packet/2048row/2MiB/512KiB bounds. Recipes have no mutable
method/config/source lookup; every unsupported identity remains literal.
"""

from __future__ import annotations

import struct
import uuid
from typing import Any

from . import indexed_frame, record_blocks, shared_body_frame
from .compact_json import pack as typed_pack
from .compact_json import unpack as typed_unpack

MAGIC = b"RP1\x00"
_NAMESPACE = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
_TIMEFRAMES = {"1m": 60000, "3m": 180000, "5m": 300000, "15m": 900000}


def _text(value: Any) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= 128:
        raise ValueError("Invalid projection recipe text")
    return value


def _integer(value: Any) -> int:
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError("Invalid projection recipe integer")
    return value


def _event(value: Any) -> str:
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("Invalid projection recipe parent")
    return value


def _identity(kind: int, head: Any, parameter: Any) -> str:
    if not isinstance(head, dict):
        raise ValueError("Missing projection recipe head")
    try:
        if kind == 0:
            timeframe = _text(head["timeframe"])
            step = _TIMEFRAMES[timeframe]
            observed = _integer(head["market_observation_ms"])
            if observed < step:
                raise ValueError("Invalid projection recipe candle")
            stream = "/".join(_text(head[key]) for key in ("market_type", "symbol", "timeframe"))
            name = (
                f"{stream}/{_text(head['detector_id'])}/{_text(head['detector_version'])}/"
                f"{_integer(parameter)}/{observed - step}"
            )
        elif kind == 1 and parameter is None:
            name = (
                f"outcome/{_event(head['event_id'])}/{_integer(head['horizon_min'])}/"
                f"{_text(head['methodology_version'])}"
            )
        elif kind == 3:
            name = f"comparison/{_event(head['event_id'])}/{_text(parameter)}"
        else:
            raise ValueError("Unknown projection recipe")
    except (KeyError, TypeError) as error:
        raise ValueError("Missing projection recipe inputs") from error
    return str(uuid.uuid5(_NAMESPACE, "cryptowatcher/" + name))


def _restore(row: Any) -> list[Any]:
    if not isinstance(row, list) or len(row) != 4 or type(row[0]) is not int:
        raise ValueError("Invalid projection recipe row")
    kind, identity, revision, head = row
    if not isinstance(identity, str):
        if kind == 1 and identity is None:
            parameter = None
        elif kind in (0, 3) and isinstance(identity, list) and len(identity) == 1:
            parameter = identity[0]
        else:
            raise ValueError("Unknown projection recipe coordinate")
        identity = _identity(kind, head, parameter)
    restored = [kind, identity, revision, head]
    record_blocks.projection(restored)
    return restored


def _fold(row: list[Any], body: Any) -> list[Any]:
    record_blocks.projection(row)
    kind, _, revision, head = row
    if kind not in (0, 1, 3) or not isinstance(body, dict):
        return row
    try:
        parameter = (
            body["settings_revision"] if kind == 0 else body["methodology_version"] if kind == 3 else None
        )
        proposed = [parameter] if kind in (0, 3) else None
        candidate = [kind, proposed, revision, head]
        if _restore(candidate) == row:
            return candidate
    except (KeyError, TypeError, ValueError):
        pass
    return row


def pack_frames(packets: list[bytes]) -> bytes:
    if not isinstance(packets, list) or not 0 < len(packets) <= shared_body_frame.MAX_PACKETS:
        raise ValueError("Projection recipe packet bound")
    frames = []
    for packet in packets:
        if not isinstance(packet, bytes) or not packet.startswith(record_blocks.MAGIC):
            raise ValueError("Projection recipes require an indexed domain packet")
        projection, _ = record_blocks.frames(packet)
        rows = indexed_frame.all_values(projection)
        bodies = record_blocks.bodies(packet)
        if len(rows) != len(bodies):
            raise ValueError("Projection recipe cardinality mismatch")
        # Check duplicate coordinates and every original typed projection,
        # including unknown fields and float bits, before publication.
        record_blocks.projections(packet)
        folded = [_fold(row, body) for row, body in zip(rows, bodies)]
        if [typed_pack(_restore(row)) for row in folded] != [typed_pack(row) for row in rows]:
            raise ValueError("Projection recipe changed immutable fields")
        frames.append(indexed_frame.pack(folded))
    result = MAGIC + shared_body_frame.pack_frames(frames)
    if len(result) > shared_body_frame.MAX_COMPRESSED:
        raise indexed_frame.FrameSizeError("Projection recipe compressed bound")
    return result


def layout(frame: bytes) -> tuple[tuple[int, ...], bytes]:
    if (
        not isinstance(frame, bytes)
        or not frame.startswith(MAGIC)
        or len(frame) > shared_body_frame.MAX_COMPRESSED
        or not frame[4:].startswith(shared_body_frame.MAGIC)
    ):
        raise ValueError("Invalid projection recipe version or bound")
    return shared_body_frame.layout(frame[4:])


def restore(frame: bytes, first: int, count: int, cache: Any = None) -> bytes:
    """Authenticate current bytes/range, then reuse only a pure IF1 derivative."""
    layout(frame)
    shared_body_frame.view(frame[4:], first, count)
    key = MAGIC + struct.pack(">HH", first, count) + frame

    def read() -> bytes:
        offsets, raw = shared_body_frame.data(frame[4:], cache)
        rows = [
            _restore(typed_unpack(raw[offsets[i] : offsets[i + 1]], indexed_frame.MAX_BYTES))
            for i in range(first, first + count)
        ]
        if len({(row[0], row[1], row[2]) for row in rows}) != count:
            raise ValueError("Duplicate projection recipe coordinate")
        # This is transient ordinary IF1, not a new stored writer format.
        # Skip expensive dense compression on the main reader; keep its exact
        # original decoder bounds and retry the old encoder only if necessary.
        try:
            return indexed_frame.pack(rows, dense=False)
        except indexed_frame.FrameSizeError:
            return indexed_frame.pack(rows)

    result = read() if cache is None else cache("projection-recipes-v1", "", key, read)
    if not isinstance(result, bytes) or len(indexed_frame.layout(result)[0]) - 1 != count:
        raise ValueError("Invalid pure projection recipe result")
    return result
