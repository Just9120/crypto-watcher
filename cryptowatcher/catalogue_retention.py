"""Bounded native cold expiry, called inside the existing retention transaction."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from itertools import islice

from . import catalogue_lifecycle, record_blocks, record_catalog
from .events import PREFIX, History
from .storage import StateError, Store


@dataclass(frozen=True)
class EventBatch:
    rows: list[tuple[str, dict[str, str]]]
    cursor: str


def due_events(store: Store, now: int, cutoff: int, batch: int) -> EventBatch:
    from .expiry_index import expires

    prefix = PREFIX + "history:"
    ceiling = prefix + f"{max(0, cutoff):016d}"
    checkpoint = store.db.execute(
        "SELECT value FROM market_meta WHERE key='retention_history_cursor'"
    ).fetchone()
    cursor = checkpoint[0] if checkpoint is not None else ""
    if not isinstance(cursor, str) or cursor and (not cursor.startswith(prefix) or len(cursor) > 8192):
        raise StateError("Invalid bounded retention cursor; data preserved")
    if cursor >= ceiling:
        cursor = ""
    scanned = list(store.scan_runtime(prefix, after=cursor or None, before=ceiling, limit=batch))
    found = {}
    for key, value in scanned:
        lease = expires(store.db, key)
        if lease is None or lease[0] <= now:
            found[key] = value
    if catalogue_lifecycle.active(store.db):
        for identity, observed in store.db.execute(
            "SELECT public_uuid,observed FROM market_record_parents WHERE expires<=? AND observed<? "
            "ORDER BY expires,id LIMIT ?",
            (now, cutoff, batch),
        ):
            event = str(uuid.UUID(bytes=bytes(identity)))
            found[prefix + f"{observed:016d}:" + event] = {"event_id": event}
    return EventBatch(sorted(found.items())[:batch], scanned[-1][0] if len(scanned) == batch else "")


def remove_event(store: Store, event_id: str, terminal: Callable[[str, str], None]) -> int:
    """The caller has already archived this event in the same writer transaction."""
    from .expiry_index import owned_hot_rows
    from .storage_lifecycle import remove

    if not store.db.in_transaction:
        raise StateError("Event expiry requires an outer writer transaction")
    removed = 0
    while rows := list(owned_hot_rows(store.db, event_id, limit=128)):
        for key, category in rows:
            raw = store.get_runtime(key, {})
            state = raw.get("state") if isinstance(raw, dict) else None
            if state in ("PENDING", "RETRY", "IN_FLIGHT", "BLOCKED", "INVALID_SOURCE", "INCOMPLETE"):
                terminal(category, state)
            remove(store, key)
            removed += 1
    if not catalogue_lifecycle.active(store.db):
        return removed
    while True:
        reader = catalogue_lifecycle.owned_records(store.db, event_id, store.cached_text)
        try:
            # Close the cursor before mutating its owner range.
            records = list(islice(reader, 64))
        finally:
            reader.close()
        if not records:
            break
        for coordinate, current in records:
            store.db.execute(
                "DELETE FROM market_records WHERE kind=? AND public_uuid=? AND revision=?",
                (
                    record_blocks.KINDS.index(coordinate.kind),
                    record_catalog.binary(coordinate.identity),
                    coordinate.revision,
                ),
            )
            removed += 1 + int(current)
    identity = record_catalog.binary(event_id)
    store.db.execute("DELETE FROM market_record_members WHERE event_uuid=?", (identity,))
    store.db.execute("DELETE FROM market_record_parents WHERE public_uuid=?", (identity,))
    return removed


def due_services(store: Store, now: int, batch: int) -> list[tuple[record_blocks.Coordinate, int]]:
    if not catalogue_lifecycle.active(store.db):
        return []
    return [
        (
            record_blocks.Coordinate(
                record_blocks.KINDS[kind], str(uuid.UUID(bytes=bytes(identity))), revision
            ),
            expires,
        )
        for kind, identity, revision, expires in store.db.execute(
            "SELECT kind,public_uuid,revision,expires FROM market_records WHERE expires IS NOT NULL AND expires<=? ORDER BY expires LIMIT ?",
            (now, batch),
        )
    ]


def service(store: Store, coordinate: record_blocks.Coordinate, now: int) -> int:
    """A retained member renews episodes; deleting a current body removes its head."""
    from .expiry_index import member_deadline, renew
    from .storage_lifecycle import remove

    saved = record_catalog.row(store.db, coordinate)
    if saved is None or saved[2] is not None or saved[3] > now:
        return 0
    key = PREFIX + coordinate.key
    if coordinate.kind == "episode":
        keep = member_deadline(store.db, PREFIX + "episode-event:" + coordinate.identity + ":")
        if keep is not None and keep > now:
            renew(store.db, key, keep)
            return 0
    # Authenticate bytes before deleting, even when there are no private refs.
    record_catalog.read(store.db, coordinate, store.cached_bytes)
    if saved[4]:
        head = (
            "decision:telegram:" if coordinate.kind == "decision" else coordinate.kind + "-head:"
        ) + coordinate.identity
        History(store).delete(head)
    remove(store, key)
    store.db.execute(
        "DELETE FROM market_records WHERE kind=? AND public_uuid=? AND revision=?",
        (
            record_blocks.KINDS.index(coordinate.kind),
            record_catalog.binary(coordinate.identity),
            coordinate.revision,
        ),
    )
    return 1 + int(saved[4])
