"""Read frozen cold leases without changing retention or installing a layout."""

from __future__ import annotations

import re
import sqlite3

from . import catalogue_aliases, catalogue_reader, packet_store, record_blocks, record_catalog
from .storage import StateError


def active(db: sqlite3.Connection) -> bool:
    return catalogue_reader.reading(db) and record_catalog.layout(db) == 3


def parent_expiry(db: sqlite3.Connection, identity: str) -> int | None:
    if (
        not isinstance(identity, str)
        or re.fullmatch(catalogue_reader.UUID, identity) is None
        or not active(db)
    ):
        return None
    row = db.execute(
        "SELECT expires FROM market_record_parents WHERE public_uuid=?", (record_catalog.binary(identity),)
    ).fetchone()
    return int(row[0]) if row is not None else None


def expires(db: sqlite3.Connection, key: str) -> int | None:
    point = catalogue_reader.point(key)
    alias = catalogue_aliases.point(key)
    if (point is None and alias is None) or not active(db):
        return None
    if point is not None:
        if point.revision is None:
            head = record_catalog.head(db, point.kind, point.identity)
            if head is None:
                return None
            coordinate = head[0]
        else:
            coordinate = record_blocks.Coordinate(point.kind, point.identity, point.revision)
        saved = record_catalog.row(db, coordinate)
        if saved is None:
            return None
        # Bind the lease to authenticated bytes and the exact selected coordinate.
        record_blocks.projected(packet_store.read(db, saved[0]), saved[1], coordinate)
        if saved[2] is None:
            return int(saved[3])
        row = db.execute("SELECT expires FROM market_record_parents WHERE id=?", (saved[2],)).fetchone()
        if row is None:
            raise StateError("Missing frozen record lease")
        return int(row[0])
    assert alias is not None
    if catalogue_aliases.lookup(db, key) is catalogue_reader.MISSING:
        return None
    return parent_expiry(db, alias.event)


def member_deadline(db: sqlite3.Connection, prefix: str) -> int | None:
    selected = catalogue_aliases.selection(prefix, member=True)
    if selected is None or not active(db):
        return None
    where = selected.where
    for name in ("episode", "observed", "event_uuid"):
        where = re.sub(r"\b" + name + r"\b", "m." + name, where)
    row = db.execute(
        "SELECT MAX(p.expires),COUNT(*),COUNT(p.id) FROM market_record_members m "
        "LEFT JOIN market_record_parents p ON p.public_uuid=m.event_uuid AND p.observed=m.observed WHERE "
        + where,
        selected.parameters,
    ).fetchone()
    if row[1] != row[2]:
        raise StateError("Missing retained membership lease")
    return row[0]
