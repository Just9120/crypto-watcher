"""Indexed mixed point reads; activation belongs to the complete runtime adapter.

This layer does not install a layout, migrate rows or enable catalogue writes.
Store/History routing remains unchanged until the remaining adapter is qualified.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from . import record_catalog
from .record_blocks import HEAD_SCHEMA, Coordinate
from .storage import StateError, encode

MISSING = object()
PREFIX = "radar:v1:"
UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
POINT = re.compile(
    rf"(?:(event|outcome|episode|comparison):({UUID}):([0-9]{{10}})|"
    rf"(event|outcome|episode|comparison)-head:({UUID})|"
    rf"decision:telegram:({UUID})(?::([0-9]{{10}}))?)"
)


@dataclass(frozen=True)
class Point:
    kind: str
    identity: str
    revision: int | None


def point(key: str) -> Point | None:
    if not isinstance(key, str) or not key.startswith(PREFIX):
        return None
    matched = POINT.fullmatch(key[len(PREFIX) :])
    if matched is None:
        return None
    kind, identity, revision, head_kind, head_id, decision_id, decision_revision = matched.groups()
    if decision_id is not None:
        kind, identity, revision = "decision", decision_id, decision_revision
    elif head_id is not None:
        kind, identity = head_kind, head_id
    if revision is not None and int(revision) == 0:
        return None
    return Point(kind, identity, int(revision) if revision is not None else None)


def reading(db: sqlite3.Connection) -> bool:
    flag = db.execute("SELECT value FROM market_meta WHERE key='record_catalogue_runtime'").fetchone()
    if flag is None:
        return False
    if flag[0] != "1":
        raise ValueError("Unknown catalogue runtime adapter")
    markers = dict(
        db.execute(
            "SELECT key,value FROM market_meta WHERE key IN('record_catalog','record_ownership','catalogue_cohorts')"
        )
    )
    if markers != {"record_catalog": "2", "record_ownership": "1", "catalogue_cohorts": "1"}:
        raise ValueError("Missing or unknown catalogue reader layout")
    return True


def cold(db: sqlite3.Connection, selected: Point, cache: Any = None) -> Any:
    if selected.revision is not None:
        value = record_catalog.read(
            db, Coordinate(selected.kind, selected.identity, selected.revision), cache
        )
        return MISSING if value is None else value
    current = record_catalog.head(db, selected.kind, selected.identity, cache)
    if current is None:
        return MISSING
    coordinate, projection = current
    if selected.kind == "decision":
        from .events import DECISION_HEAD

        if projection is not None:
            raise StateError("Invalid catalogue decision projection")
        return {DECISION_HEAD: coordinate.revision}
    if projection is None:
        raise StateError("Missing catalogue current projection")
    return projection


def revision(value: Any, selected: Point) -> int:
    from .events import DECISION_HEAD

    if not isinstance(value, dict):
        raise StateError("Invalid mixed history head")
    if DECISION_HEAD in value:
        if selected.kind != "decision" or set(value) != {DECISION_HEAD}:
            raise StateError("Invalid mixed decision head")
        number = value[DECISION_HEAD]
    else:
        field = "event_id" if selected.kind == "decision" else selected.kind + "_id"
        if value.get(field) != selected.identity:
            raise StateError("Conflicting mixed history head identity")
        number = value.get("revision")
    if type(number) is not int or not 0 < number <= 9999999999:
        raise StateError("Invalid mixed history head revision")
    return number


def equal_public(db: sqlite3.Connection, first: Any, second: Any, cache: Any = None) -> bool:
    if encode(first) == encode(second):
        return True
    from .history_codec import Codec

    codec = Codec(db, binary_cache=cache)
    return codec.canonical_json(first) == codec.canonical_json(second)


def lookup(db: sqlite3.Connection, key: str, hot: Any = MISSING, cache: Any = None) -> Any:
    selected = point(key)
    if selected is None or not reading(db):
        return hot
    stored = cold(db, selected, cache)
    if stored is MISSING:
        return hot
    if hot is MISSING:
        return stored
    if selected.revision is not None:
        if not equal_public(db, hot, stored, cache):
            raise StateError("Conflicting mixed immutable history revision")
        return hot
    hot_revision, cold_revision = revision(hot, selected), revision(stored, selected)
    if hot_revision != cold_revision:
        return hot if hot_revision > cold_revision else stored
    if encode(hot) == encode(stored):
        return hot
    # Legacy full heads are compared to the actual immutable record. Comparing
    # just a projection would conceal changes in other frozen public fields.
    if selected.kind == "decision" or hot.get("schema") != HEAD_SCHEMA:
        body = record_catalog.read(db, Coordinate(selected.kind, selected.identity, cold_revision), cache)
        if body is not None and equal_public(db, hot, body, cache):
            return hot
    raise StateError("Conflicting mixed history heads at the same revision")
