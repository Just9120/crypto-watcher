"""Indexed domain point/range reads; complete runtime activation is separate.

This layer does not install a layout, migrate rows or enable catalogue writes.
Store/History routing remains unchanged until the remaining adapter is qualified.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from heapq import merge
from typing import Any

from . import packet_store, record_blocks, record_catalog, record_masks
from .record_blocks import HEAD_SCHEMA, KINDS, Coordinate
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
    if markers.get("record_catalog") not in ("2", "3") or {
        key: value for key, value in markers.items() if key != "record_catalog"
    } != {"record_ownership": "1", "catalogue_cohorts": "1"}:
        raise ValueError("Missing or unknown catalogue reader layout")
    if markers["record_catalog"] == "3":
        record_masks.tallies(db, "record_parents")
        record_masks.tallies(db, "record_owners")
        record_masks.tallies(db, "record_aliases")
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
    if selected is None:
        from .catalogue_aliases import lookup as alias_lookup

        return alias_lookup(db, key, hot)
    if not reading(db):
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


@dataclass(frozen=True)
class Namespace:
    kind: str
    head: bool

    @property
    def root(self) -> str:
        return PREFIX + (
            "decision:telegram:" if self.kind == "decision" else self.kind + ("-head:" if self.head else ":")
        )


NAMESPACES = tuple(Namespace(kind, head) for kind in KINDS for head in (False, True))


def uuid_range(prefix: str) -> tuple[bytes, bytes | None] | None:
    if len(prefix) > 36 or any(
        (character != "-" if index in (8, 13, 18, 23) else character not in "0123456789abcdef")
        for index, character in enumerate(prefix)
    ):
        return None
    digits = prefix.replace("-", "")
    shift = (32 - len(digits)) * 4
    lower = int(digits or "0", 16) << shift
    upper = lower + (1 << shift)
    return lower.to_bytes(16, "big"), upper.to_bytes(16, "big") if upper < 2**128 else None


def selected_range(namespace: Namespace, prefix: str) -> tuple[str, tuple[Any, ...], bool] | None:
    if namespace.root.startswith(prefix):
        suffix = ""
    elif prefix.startswith(namespace.root):
        suffix = prefix[len(namespace.root) :]
    else:
        return None
    identity, separator, revision_prefix = suffix.partition(":")
    if separator and (namespace.head or re.fullmatch(UUID, identity) is None):
        return None
    bounds = uuid_range(identity)
    if bounds is None:
        return None
    where = "kind=?" + (" AND current=1" if namespace.head else "")
    parameters: list[Any] = [KINDS.index(namespace.kind)]
    if len(identity) == 36:
        where += " AND public_uuid=?"
        parameters.append(bounds[0])
    elif identity:
        where += " AND public_uuid>=?"
        parameters.append(bounds[0])
        if bounds[1] is not None:
            where += " AND public_uuid<?"
            parameters.append(bounds[1])
    if separator:
        if len(revision_prefix) > 10 or any(c not in "0123456789" for c in revision_prefix):
            return None
        width = 10 ** (10 - len(revision_prefix))
        lower = max(1, int(revision_prefix or "0") * width)
        upper = min(10**10, (int(revision_prefix or "0") + 1) * width)
        if lower >= upper:
            return None
        where += " AND revision>=? AND revision<?"
        parameters.extend((lower, upper))
    return where, tuple(parameters), not suffix


def namespace_rows(
    db: sqlite3.Connection,
    namespace: Namespace,
    selected: tuple[str, tuple[Any, ...], bool],
    reverse: bool,
    limit: int | None,
    cache: Any,
) -> Generator[tuple[str, Any], None, None]:
    order = " DESC" if reverse else " ASC"
    sql = (
        "SELECT public_uuid,revision,packet,slot,expires FROM market_records WHERE "
        + selected[0]
        + " ORDER BY public_uuid"
        + order
        + ("" if namespace.head else ",revision" + order)
    )
    parameters = selected[1]
    if limit is not None:
        sql += " LIMIT ?"
        parameters += (limit + int(namespace.head and limit < 2**63 - 1),)
    cursor = db.execute(sql, parameters)
    version = record_catalog.layout(db)
    try:
        saved = cursor.fetchone()
        emitted = 0
        while saved is not None:
            identity, revision_number, packet, slot, expires = saved
            following = cursor.fetchone()
            if namespace.head and following is not None and following[0] == identity:
                raise StateError("Conflicting current record revisions")
            if version == 3:
                record_masks.owner(db, packet, slot, expires)
            coordinate = Coordinate(namespace.kind, str(uuid.UUID(bytes=bytes(identity))), revision_number)
            raw = packet_store.read(db, packet)
            if namespace.head:
                value = record_blocks.projected(raw, slot, coordinate, cache)
                if namespace.kind == "decision":
                    from .events import DECISION_HEAD

                    if value is not None:
                        raise StateError("Invalid catalogue decision projection")
                    value = {DECISION_HEAD: coordinate.revision}
                elif value is None:
                    raise StateError("Missing catalogue current projection")
                key = namespace.root + coordinate.identity
            else:
                value = record_blocks.read(raw, slot, coordinate, cache)
                key = PREFIX + coordinate.key
            yield key, value
            emitted += 1
            if limit is not None and emitted == limit:
                return
            saved = following
    finally:
        cursor.close()


def scan(
    db: sqlite3.Connection,
    prefix: str,
    *,
    reverse: bool = False,
    limit: int | None = None,
    cache: Any = None,
) -> Generator[tuple[str, Any], None, None]:
    """Stream domain revisions/heads in public-key order; caller owns snapshot.

    Every namespace uses its native UUID/time/revision index, without sorting
    computed public TEXT keys or loading the whole catalogue in Python.
    """
    if not isinstance(prefix, str) or (
        limit is not None and (type(limit) is not int or not 0 <= limit < 2**63)
    ):
        raise ValueError("Invalid catalogue scan bounds")
    if limit == 0:
        return
    selectors = [(namespace, selected_range(namespace, prefix)) for namespace in NAMESPACES]
    from . import catalogue_aliases

    aliases_selected = any(catalogue_aliases.selection(prefix, member=m) is not None for m in (False, True))
    if not (aliases_selected or any(selected is not None for _, selected in selectors)) or not reading(db):
        return
    streams = [
        namespace_rows(db, namespace, selected, reverse, limit, cache)
        for namespace, selected in selectors
        if selected is not None
    ]
    if aliases_selected:
        streams.append(catalogue_aliases.scan(db, prefix, reverse=reverse, limit=limit))
    try:
        for index, item in enumerate(merge(*streams, key=lambda row: row[0], reverse=reverse)):
            yield item
            if limit is not None and index + 1 == limit:
                break
    finally:
        for stream in streams:
            stream.close()


def count(db: sqlite3.Connection, prefix: str) -> int:
    if not isinstance(prefix, str):
        raise ValueError("Invalid catalogue count prefix")
    selectors = [(namespace, selected_range(namespace, prefix)) for namespace in NAMESPACES]
    from . import catalogue_aliases

    aliases_selected = any(catalogue_aliases.selection(prefix, member=m) is not None for m in (False, True))
    if not (aliases_selected or any(selected is not None for _, selected in selectors)) or not reading(db):
        return 0
    total = 0
    for namespace, selected in selectors:
        if selected is None:
            continue
        if selected[2]:
            row = db.execute(
                "SELECT n,heads FROM market_catalogue_counts WHERE kind=?", (KINDS.index(namespace.kind),)
            ).fetchone()
            if row is None or any(type(n) is not int or n < 0 for n in row) or row[1] > row[0]:
                raise ValueError("Missing or invalid catalogue namespace count")
            total += row[1] if namespace.head else row[0]
        else:
            total += db.execute(
                "SELECT COUNT(*) FROM market_records WHERE " + selected[0], selected[1]
            ).fetchone()[0]
    return total + (catalogue_aliases.count(db, prefix) if aliases_selected else 0)


def as_of(db: sqlite3.Connection, kind: str, identity: str, cutoff_ms: int, cache: Any = None) -> Any:
    if (
        kind not in KINDS[:4]
        or type(cutoff_ms) is not int
        or not 0 <= cutoff_ms < 2**63
        or not isinstance(identity, str)
        or re.fullmatch(UUID, identity) is None
    ):
        raise ValueError("Invalid catalogue as-of bounds")
    values = scan(db, PREFIX + kind + ":" + identity + ":", reverse=True, cache=cache)
    try:
        for _, value in values:
            available = value.get("available_at_ms")
            if type(available) is not int or not 0 <= available < 2**63:
                raise StateError("Missing or invalid frozen availability time")
            if available <= cutoff_ms:
                return value
    finally:
        values.close()
    return MISSING
