"""Bounded native cold expiry, called inside the existing retention transaction."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from itertools import islice
from typing import Any

from . import catalogue_aliases, catalogue_lifecycle, catalogue_reader, record_blocks, record_catalog
from .events import PREFIX
from .storage import StateError, Store, json_payload


@dataclass(frozen=True)
class EventBatch:
    rows: list[tuple[str, dict[str, str]]]
    cursor: str


def due_events(store: Store, now: int, cutoff: int, batch: int) -> EventBatch:
    from .catalogue_store import hot_rows
    from .expiry_index import expires

    if type(batch) is not int or not 0 <= batch < 2**63:
        raise ValueError("Invalid mixed runtime scan bounds")
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
    hot = hot_rows(store.db, prefix, False, batch, cursor or None, ceiling)
    try:
        # Cold parents have their own native expiry range below. Do not read
        # every cold history alias again merely to discard its future lease.
        scanned = [(key, catalogue_aliases.lookup(store.db, key, value)) for key, value, _ in hot]
    finally:
        hot.close()
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
    selected = sorted(found.items())[:batch]
    next_cursor = scanned[-1][0] if scanned and len(scanned) == batch else ""
    if next_cursor and selected:
        # The merged admission limit may leave later hot rows unselected.
        next_cursor = min(next_cursor, selected[-1][0])
    return EventBatch(selected, next_cursor)


def archive_candidates(
    store: Store, seed: dict[str, Any], now: int, cutoff: int, deadline: float
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Bounded hints only; the oldest selected seed always expires first."""
    from . import parent_affinity
    from .events import History

    fields = parent_affinity.native(seed)
    if fields is None or not parent_affinity.installed(store.db):
        return
    try:
        identity = record_catalog.binary(seed["event_id"])
    except (ValueError, TypeError, AttributeError):
        return
    hint = store.db.execute(
        "SELECT a.day,a.fingerprint,p.observed,p.expires FROM market_record_parents p "
        "JOIN market_parent_affinity a ON a.parent=p.id WHERE p.public_uuid=?",
        (identity,),
    ).fetchone()
    if (
        hint is None
        or bytes(hint[1]) != parent_affinity.fingerprint(fields)
        or hint[2] != seed.get("market_observation_ms")
        or hint[2] >= cutoff
        or hint[3] > now
    ):
        return
    # LIMIT precedes eligibility filtering, so a future-heavy day cannot turn
    # this optional lookup into an unbounded scan of rejected parent rows.
    rows = store.db.execute(
        "SELECT p.public_uuid,p.observed,p.expires FROM market_parent_affinity a "
        "INDEXED BY market_parent_affinity_cohort "
        "JOIN market_record_parents p ON p.id=a.parent WHERE a.day=? AND a.fingerprint=? "
        "ORDER BY a.parent LIMIT 64",
        tuple(hint[:2]),
    ).fetchall()
    history = History(store)
    for public, observed, expires in sorted(rows, key=lambda row: (row[1], bytes(row[0]))):
        if time.monotonic() >= deadline:
            return
        if bytes(public) == identity or expires > now or observed >= cutoff:
            continue
        event_id = str(uuid.UUID(bytes=bytes(public)))
        event = history.metadata("event-head:" + event_id)
        if (
            event is not None
            and parent_affinity.native(event) == fields
            and event.get("market_observation_ms") == observed
        ):
            yield PREFIX + f"history:{observed:016d}:" + event_id, event


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
        reader = catalogue_lifecycle.owned_records(store.db, event_id, store.cached_text, store.cached_bytes)
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
    try:
        identity = record_catalog.binary(event_id)
    except (ValueError, TypeError, AttributeError):
        # Unsupported legacy IDs never have a compact parent. Their hot
        # results were still archived and removed through native owner keys.
        return removed
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
    from .expiry_index import hot_binding, renew
    from .storage_lifecycle import forget, remove

    saved = record_catalog.row(store.db, coordinate)
    if saved is None or saved[2] is not None or saved[3] > now:
        return 0
    key = PREFIX + coordinate.key
    if coordinate.kind == "episode":
        keep = catalogue_lifecycle.episode_deadline(store.db, coordinate.identity, coordinate.revision)
        if keep is not None and keep > now:
            renew(store.db, key, keep)
            return 0
    # Authenticate bytes before deleting, even when there are no private refs.
    record_catalog.read(store.db, coordinate, store.cached_bytes)
    head = (
        PREFIX
        + ("decision:telegram:" if coordinate.kind == "decision" else coordinate.kind + "-head:")
        + coordinate.identity
    )
    hot = store.db.execute("SELECT payload_json FROM runtime WHERE key=?", (head,)).fetchone()
    same_hot = (
        hot is not None
        and catalogue_reader.revision(
            json_payload(hot[0]), catalogue_reader.Point(coordinate.kind, coordinate.identity, None)
        )
        == coordinate.revision
    )
    if same_hot:
        lease = hot_binding(store.db, head)
        if lease is None:
            raise StateError("Missing retained service head lease")
        if lease[0] > now:
            return 0
    if saved[4]:
        store.db.execute(
            "UPDATE market_records SET current=0 WHERE kind=? AND public_uuid=? AND revision=?",
            (
                record_blocks.KINDS.index(coordinate.kind),
                record_catalog.binary(coordinate.identity),
                coordinate.revision,
            ),
        )
    if same_hot:
        forget(store, head)
        store.db.execute("DELETE FROM runtime WHERE key=?", (head,))
    remove(store, key)
    store.db.execute(
        "DELETE FROM market_records WHERE kind=? AND public_uuid=? AND revision=?",
        (
            record_blocks.KINDS.index(coordinate.kind),
            record_catalog.binary(coordinate.identity),
            coordinate.revision,
        ),
    )
    return 1 + int(saved[4] or same_hot)
