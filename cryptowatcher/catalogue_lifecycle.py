"""Read frozen cold leases without changing retention or installing a layout."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Generator
from typing import Any

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


def owned_records(
    db: sqlite3.Connection, event_id: str, cache: Any = None
) -> Generator[tuple[record_blocks.Coordinate, bool], None, None]:
    """Read one native parent range and authenticate at most one packet at a time."""
    if (
        not isinstance(event_id, str)
        or re.fullmatch(catalogue_reader.UUID, event_id) is None
        or not active(db)
    ):
        return
    from . import owner_slots, record_masks
    from .record_ownership import block_entries

    root = db.execute(
        "SELECT id FROM market_record_parents WHERE public_uuid=?", (record_catalog.binary(event_id),)
    ).fetchone()
    if root is None:
        return
    record_masks.tallies(db, "record_owners")
    record_masks.tallies(db, "record_parents")
    cursor = db.execute(
        "SELECT packet,slots FROM market_record_owners WHERE parent=? ORDER BY packet", (root[0],)
    )
    try:
        for packet, slots in cursor:
            if (root[0], slots) not in record_masks.packet_masks(db, packet):
                raise StateError("Missing retained owner mask")
            entries = block_entries(db, packet, cache)
            if owner_slots.bits(slots) >> len(entries):
                raise StateError("Retained owner slot outside packet")
            for slot, entry in enumerate(entries):
                if not owner_slots.has(slots, slot):
                    continue
                coordinate = record_blocks.Coordinate(*entry[:3])
                coordinate.validate()
                if entry[5] != event_id:
                    raise StateError("Conflicting retained event ownership")
                saved = db.execute(
                    "SELECT packet,slot,current,expires FROM market_records WHERE kind=? AND public_uuid=? AND revision=?",
                    (
                        record_blocks.KINDS.index(coordinate.kind),
                        record_catalog.binary(coordinate.identity),
                        coordinate.revision,
                    ),
                ).fetchone()
                if saved is None or tuple(saved[:2]) != (packet, slot) or saved[3] is not None:
                    raise StateError("Missing retained owner coordinate")
                yield coordinate, bool(saved[2])
    finally:
        cursor.close()


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


def renewable(value: object, depth: int = 0) -> bool:
    """Only self-contained episode extensions enter renewable cold service rows.

    Source/context extensions retain their hot representation and exact leases;
    moving those uncommon shapes requires an independent lease adapter first.
    """
    if depth > 64:
        return False
    if isinstance(value, list):
        return all(renewable(item, depth + 1) for item in value)
    if isinstance(value, dict):
        storage = value.get("_history_storage", {})
        if not isinstance(storage, dict) or not isinstance(storage.get("fields", {}), dict):
            return False
        fields = storage.get("fields", {})
        if any(
            not isinstance(reference, dict)
            or reference.get("mode") not in ("numeric", "numeric2", "value", "value2", "window")
            for reference in fields.values()
        ):
            return False
        return all(renewable(child, depth + 1) for name, child in value.items() if name != "_history_storage")
    return True


def renew(db: sqlite3.Connection, key: str, deadline: int, *, shorten: bool = False) -> None:
    """Extend a service episode only to its actual latest retained dependency.

    Caller holds one writer SAVEPOINT around this and any hot duplicate update.
    Event-owned records always keep their frozen parent deadline.
    """
    point = catalogue_reader.point(key)
    if point is None or not active(db):
        return
    if point.revision is None:
        head = record_catalog.head(db, point.kind, point.identity)
        if head is None:
            return
        coordinate = head[0]
    else:
        coordinate = record_blocks.Coordinate(point.kind, point.identity, point.revision)
    saved = record_catalog.row(db, coordinate)
    if saved is None:
        return
    previous = expires(db, key)
    if previous == deadline or shorten and previous is not None and deadline >= previous:
        return
    if (
        not db.in_transaction
        or point.kind != "episode"
        or saved[2] is not None
        or type(deadline) is not int
        or deadline <= saved[3]
        or shorten
    ):
        raise StateError("Cannot change a frozen record lease")
    from .expiry_index import hot_binding
    from .expiry_index import member_deadline as all_members

    hot = hot_binding(db, key)
    if hot is not None and hot[0] != previous:
        raise StateError("Conflicting mixed episode lease")
    if all_members(db, catalogue_reader.PREFIX + "episode-event:" + point.identity + ":") != deadline:
        raise StateError("Episode lease must match retained members")
    value = record_blocks.read(packet_store.read(db, saved[0]), saved[1], coordinate)
    if not renewable(value):
        raise StateError("Unsupported renewable episode dependencies; retained data preserved")
    changed = db.execute(
        "UPDATE market_records SET expires=? WHERE kind=2 AND public_uuid=? AND revision=? AND expires=?",
        (deadline, record_catalog.binary(point.identity), coordinate.revision, previous),
    ).rowcount
    if changed != 1:
        raise StateError("Concurrent episode lease change")
