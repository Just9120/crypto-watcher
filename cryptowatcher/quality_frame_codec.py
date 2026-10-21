"""QC1 column storage with the original typed quality entry as its public view.

The outer frame keeps IF1's independent bounds, offsets and checksums. Only the
six fields of each logical CQ5 row are transposed; vectors, scalar types, IDs,
row order and dead container positions are unchanged. IF1 remains readable.
"""

from __future__ import annotations

from typing import Any

from . import indexed_frame
from .compact_json import pack as typed_pack
from .compact_json import unpack as typed_unpack

MAGIC = b"QC1\x00"
TAG = "C1"


def _coordinates(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 4
        and all(type(n) is int and 0 <= n < 2**63 for n in value[:3])
    )


def columns(value: Any) -> Any:
    if value is None:
        return None
    if (
        not _coordinates(value)
        or not isinstance(value[3], list)
        or len(value[3]) != 2
        or value[3][1] is not None
        or not isinstance(value[3][0], list)
        or len(value[3][0]) > 128
        or any(not isinstance(row, list) or len(row) != 6 for row in value[3][0])
    ):
        raise ValueError("Invalid logical quality entry for columns")
    return [*value[:3], [TAG, [[row[index] for row in value[3][0]] for index in range(6)]]]


def rows(value: Any) -> Any:
    if value is None:
        return None
    if (
        not _coordinates(value)
        or not isinstance(value[3], list)
        or len(value[3]) != 2
        or value[3][0] != TAG
        or not isinstance(value[3][1], list)
        or len(value[3][1]) != 6
        or any(not isinstance(column, list) for column in value[3][1])
        or len(value[3][1][0]) > 128
        or any(len(column) != len(value[3][1][0]) for column in value[3][1])
    ):
        raise ValueError("Invalid quality column entry")
    return [*value[:3], [[list(row) for row in zip(*value[3][1], strict=True)], None]]


def pack(values: list[Any], *, use_columns: bool) -> bytes:
    original = indexed_frame.pack(values)
    if not use_columns:
        return original
    try:
        alternative = indexed_frame.pack([columns(value) for value in values])
    except indexed_frame.FrameSizeError:
        # A less suitable representation must not reject a valid bounded row
        # frame. This is a representation choice, not an input-size change.
        return original
    return MAGIC + alternative[4:] if len(alternative) < len(original) else original


def _data(frame: bytes, cache: Any) -> tuple[tuple[int, ...], bytes, bool]:
    if not isinstance(frame, bytes) or frame[:4] not in (MAGIC, indexed_frame.MAGIC):
        raise ValueError("Unknown quality container format")
    transposed = frame.startswith(MAGIC)
    checked = indexed_frame.MAGIC + frame[4:] if transposed else frame
    offsets, expanded = indexed_frame.data(checked, cache)
    return offsets, expanded, transposed


def entry_bytes(frame: bytes, slot: int, cache: Any = None) -> bytes:
    offsets, expanded, transposed = _data(frame, cache)
    if type(slot) is not int or not 0 <= slot < len(offsets) - 1:
        raise ValueError("Invalid quality container slot")
    entry = expanded[offsets[slot] : offsets[slot + 1]]
    return typed_pack(rows(typed_unpack(entry, indexed_frame.MAX_BYTES))) if transposed else entry


def all_values(frame: bytes, cache: Any = None) -> list[Any]:
    offsets, expanded, transposed = _data(frame, cache)
    values = [typed_unpack(expanded[a:b], indexed_frame.MAX_BYTES) for a, b in zip(offsets, offsets[1:])]
    return [rows(value) for value in values] if transposed else values
