"""Explicit pending hot work, reusing stable expiry-registry IDs.

This is an application-maintenance inbox, not a scheduler or retained-key index.
No installation/activation occurs when a Store is opened.
"""

from __future__ import annotations

import copy
import sqlite3
import time
from typing import Any

from . import (
    catalogue_aliases,
    catalogue_lifecycle,
    catalogue_reader,
    catalogue_transfer,
    domain_packets,
    packet_store,
    record_blocks,
    record_catalog,
)
from .events import DECISION_HEAD, HEAD_SCHEMA
from .expiry_index import hot_binding
from .key_registry import pack, unpack
from .storage import StateError, Store, encode, json_payload

CATEGORY = "record_pending"
BYTES = 32
VERSION = 1


def validate(state: dict[str, Any]) -> None:
    if (
        not isinstance(state, dict)
        or type(state.get("version", VERSION)) is not int
        or state.get("version", VERSION) != VERSION
    ):
        raise StateError("Unknown catalogue queue checkpoint; data preserved")
    for name in ("heads_retired", "bodies_moved", "aliases_moved", "queue_rows_examined"):
        value = state.get(name, 0)
        if type(value) is not int or value < 0:
            raise StateError("Invalid catalogue queue progress; data preserved")
    if state.get("next_lane", "body") not in ("body", "head"):
        raise StateError("Invalid catalogue queue turn; data preserved")
    visits = state.get("fallback_visits", {})
    if not isinstance(visits, dict) or any(
        not isinstance(key, str) or type(value) is not int or value < 0 for key, value in visits.items()
    ):
        raise StateError("Invalid catalogue queue fallback accounting; data preserved")


def install(db: sqlite3.Connection) -> None:
    marker = db.execute("SELECT value FROM market_meta WHERE key='catalogue_queue'").fetchone()
    if marker is not None:
        header(db)
        return
    if db.in_transaction:
        raise StateError("Queue installation requires an outer boundary")
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='market_record_pending'").fetchone():
        raise StateError("Unknown catalogue queue; data preserved")
    try:
        db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE market_record_pending(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rid INTEGER NOT NULL UNIQUE REFERENCES market_runtime_keys(id) ON DELETE CASCADE,
                at_ms INTEGER NOT NULL CHECK(typeof(at_ms)='integer'));
            INSERT INTO market_counts VALUES('record_pending',0,0,0,0);
            CREATE TRIGGER market_record_pending_new BEFORE INSERT ON market_record_pending BEGIN
                SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM market_counts WHERE category='record_pending'
                    AND typeof(records)='integer' AND records>=0 AND logical_bytes=records*32)
                    THEN RAISE(ABORT,'Missing catalogue queue counter') END;
            END;
            CREATE TRIGGER market_record_pending_added AFTER INSERT ON market_record_pending BEGIN
                UPDATE market_counts SET records=records+1,logical_bytes=logical_bytes+32,
                    inserted_bytes=inserted_bytes+32 WHERE category='record_pending';
            END;
            CREATE TRIGGER market_record_pending_keep BEFORE DELETE ON market_record_pending BEGIN
                SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM market_counts WHERE category='record_pending'
                    AND typeof(records)='integer' AND records>0 AND logical_bytes=records*32)
                    THEN RAISE(ABORT,'Missing catalogue queue counter') END;
            END;
            CREATE TRIGGER market_record_pending_removed AFTER DELETE ON market_record_pending BEGIN
                UPDATE market_counts SET records=records-1,logical_bytes=logical_bytes-32,
                    deleted_bytes=deleted_bytes+32 WHERE category='record_pending';
            END;
            CREATE TRIGGER market_record_pending_frozen BEFORE UPDATE ON market_record_pending BEGIN
                SELECT RAISE(ABORT,'Immutable pending queue identity'); END;
            CREATE TRIGGER market_record_pending_admit AFTER INSERT ON market_expiry
                WHEN NEW.category IN ('event','event-head','outcome','outcome-head','episode','episode-head',
                    'comparison','comparison-head','decision','history','episode-event')
                AND (SELECT value FROM market_meta WHERE key='catalogue_queue_runtime')='1' BEGIN
                INSERT OR IGNORE INTO market_record_pending(rid,at_ms) VALUES(NEW.rid,cw_clock_ms());
            END;
            INSERT INTO market_meta VALUES('catalogue_queue','1');
            COMMIT;
        """)
    except BaseException:
        db.rollback()
        raise


def header(db: sqlite3.Connection) -> dict[str, int | None]:
    marker = db.execute("SELECT value FROM market_meta WHERE key='catalogue_queue'").fetchone()
    if marker is None or marker[0] != "1":
        raise StateError("Missing or unknown catalogue queue")
    row = db.execute(
        "SELECT records,logical_bytes FROM market_counts WHERE category=?", (CATEGORY,)
    ).fetchone()
    if row is None or any(type(value) is not int for value in row) or row[0] < 0 or row[1] != row[0] * BYTES:
        raise StateError("Missing catalogue queue counter")
    oldest = db.execute("SELECT id,at_ms FROM market_record_pending ORDER BY id LIMIT 1").fetchone()
    if (oldest is None) != (row[0] == 0):
        raise StateError("Conflicting catalogue queue counter")
    return {
        "count": row[0],
        "oldest_id": oldest[0] if oldest is not None else None,
        "oldest_ms": oldest[1] if oldest is not None else None,
    }


def activate(db: sqlite3.Connection) -> None:
    header(db)
    row = db.execute("SELECT value FROM market_meta WHERE key='catalogue_queue_runtime'").fetchone()
    if row is not None and row[0] != "1":
        raise StateError("Unknown catalogue queue writer")
    db.execute("INSERT INTO market_meta VALUES('catalogue_queue_runtime','1') ON CONFLICT(key) DO NOTHING")


def ceiling(db: sqlite3.Connection) -> int:
    header(db)
    row = db.execute("SELECT id FROM market_record_pending ORDER BY id DESC LIMIT 1").fetchone()
    return row[0] if row is not None else 0


def verify(db: sqlite3.Connection) -> dict[str, int | None]:
    """Explicit deep boundary check; callers supply the existing scan deadline."""
    result = header(db)
    actual = db.execute("SELECT count(*) FROM market_record_pending").fetchone()[0]
    if (
        actual != result["count"]
        or db.execute(
            "SELECT 1 FROM market_record_pending p LEFT JOIN market_runtime_keys k ON k.id=p.rid "
            "LEFT JOIN market_expiry e ON e.rid=p.rid WHERE k.id IS NULL OR e.rid IS NULL LIMIT 1"
        ).fetchone()
    ):
        raise StateError("Conflicting catalogue queue ownership or count")
    return result


def through(db: sqlite3.Connection, upper: int) -> bool:
    if type(upper) is not int or not 0 <= upper < 2**63:
        raise StateError("Invalid initial queue boundary")
    header(db)
    return db.execute("SELECT 1 FROM market_record_pending WHERE id<=? LIMIT 1", (upper,)).fetchone() is None


def _head(store: Store, key: str, raw: str | bytes, state: dict[str, Any]) -> bool | None:
    selected = catalogue_reader.point(key)
    assert selected is not None and selected.revision is None
    value = json_payload(raw)
    revision = catalogue_reader.revision(value, selected)
    coordinate = record_blocks.Coordinate(selected.kind, selected.identity, revision)
    saved = record_catalog.row(store.db, coordinate)
    if saved is None:
        return None
    body = record_catalog.read(store.db, coordinate, store.cached_bytes)
    if body is None:
        raise StateError("Missing queued immutable body")
    projected = record_blocks.projected(
        domain_packets.read(store.db, saved[0], store.cached_bytes), saved[1], coordinate, store.cached_bytes
    )
    if selected.kind == "decision":
        matched = value == {DECISION_HEAD: revision} or catalogue_reader.equal_public(
            store.db, value, body, store.cached_bytes
        )
    else:
        matched = projected is not None and (
            encode(value) == encode(projected)
            if value.get("schema") == HEAD_SCHEMA
            else catalogue_reader.equal_public(store.db, value, body, store.cached_bytes)
        )
    if not matched:
        return False
    lease = hot_binding(store.db, key)
    if lease is None:
        return False
    deadline = saved[3]
    if saved[2] is not None:
        deadline = store.db.execute(
            "SELECT expires FROM market_record_parents WHERE id=?", (saved[2],)
        ).fetchone()[0]
    if lease[0] != deadline:
        return False
    record_catalog.append(
        store.db,
        [(coordinate, body, projected)],
        bucket=deadline // 86400000,
        owners={coordinate: saved[2]},
        deadlines={coordinate: saved[3]},
        current=[coordinate],
    )
    catalogue_transfer._drop(store, key, raw)
    state["heads_retired"] = state.get("heads_retired", 0) + 1
    return True


def advance(store: Store, state: dict[str, Any], batch: int, deadline: float) -> None:
    validate(state)
    if not store.db.in_transaction or type(batch) is not int or not 1 <= batch <= 256:
        raise StateError("Queue work requires a bounded outer writer checkpoint")
    header(store.db)
    if not catalogue_lifecycle.active(store.db):
        raise StateError("Queued transfer requires the complete runtime adapter")
    working = copy.deepcopy(state)
    working["version"] = VERSION
    with packet_store.atomic(store.db):
        _advance(store, working, batch, deadline)
    state.clear()
    state.update(working)


def _advance(store: Store, state: dict[str, Any], batch: int, deadline: float) -> None:
    if time.monotonic() >= deadline:
        return
    rows = store.db.execute(
        "SELECT p.id,p.rid,CASE WHEN length(CAST(k.external AS BLOB))<=8192 THEN k.external END FROM market_record_pending p JOIN market_runtime_keys k ON k.id=p.rid ORDER BY p.id LIMIT ?",
        (batch,),
    ).fetchall()
    # A body flush can consume the whole quantum. Persist which lane starts
    # next so that continuous arrivals cannot keep old heads/aliases waiting.
    first = state.get("next_lane", "body")
    state["next_lane"] = "head" if first == "body" else "body"
    consumed = visited = 0
    for index, lane in enumerate((first, state["next_lane"])):
        if index and time.monotonic() >= deadline:
            break
        if lane == "body":
            charge, count = _bodies(store, state, rows, consumed, deadline)
        else:
            charge, count = _deferred(store, state, rows, consumed, deadline)
        consumed += charge
        visited += count
    state["queue_rows_examined"] = state.get("queue_rows_examined", 0) + visited


def _bodies(
    store: Store, state: dict[str, Any], rows: list[Any], consumed: int, deadline: float
) -> tuple[int, int]:
    entries: list[catalogue_transfer.Prepared] = []
    buffered = charged = visited = 0
    for index, (sequence, rid, token) in enumerate(rows):
        if index and time.monotonic() >= deadline:
            break
        key = unpack(token) if token is not None else None
        point = catalogue_reader.point(key) if key is not None else None
        if point is None or point.revision is None:
            continue
        assert key is not None
        saved = store.db.execute(
            "SELECT length(CAST(payload_json AS BLOB)) FROM runtime WHERE key=?", (key,)
        ).fetchone()
        if (
            saved is not None
            and consumed + charged + saved[0] > catalogue_transfer.MAX_BUFFER
            and (visited or consumed)
        ):
            break
        raw = store.db.execute(
            "SELECT payload_json FROM runtime WHERE key=? AND length(CAST(payload_json AS BLOB))<=?",
            (key, catalogue_transfer.MAX_BUFFER),
        ).fetchone()
        if raw is not None:
            charged += len(raw[0].encode() if isinstance(raw[0], str) else raw[0])
        entry = catalogue_transfer.prepare(store, key, raw[0], state) if raw is not None else None
        if entry is not None:
            cost = len(encode(entry.body).encode()) + len(encode(entry.projection).encode()) + 128
            if entries and (
                len(entries) == 64
                or buffered + cost > catalogue_transfer.MAX_GROUP
                or entries[0].expires // 86400000 != entry.expires // 86400000
            ):
                catalogue_transfer._flush(store, entries, state)
                entries, buffered = [], 0
            entries.append(entry)
            buffered += cost
        else:
            if raw is None and saved is not None:
                catalogue_transfer._fallback(state, "oversized_body_envelope")
            store.db.execute("DELETE FROM market_record_pending WHERE id=?", (sequence,))
        visited += 1
    catalogue_transfer._flush(store, entries, state)
    return charged, visited


def _deferred(
    store: Store, state: dict[str, Any], rows: list[Any], consumed: int, deadline: float
) -> tuple[int, int]:
    charged = visited = 0
    selected = {token for _, _, token in rows if token is not None}
    for index, (sequence, rid, token) in enumerate(rows):
        if index and time.monotonic() >= deadline:
            break
        key = unpack(token) if token is not None else None
        point = catalogue_reader.point(key) if key is not None else None
        if point is not None and point.revision is not None:
            continue
        if not store.db.execute("SELECT 1 FROM market_record_pending WHERE id=?", (sequence,)).fetchone():
            continue
        size = (
            store.db.execute(
                "SELECT length(CAST(payload_json AS BLOB)) FROM runtime WHERE key=?", (key,)
            ).fetchone()
            if key is not None
            else None
        )
        if size is not None and consumed + charged + size[0] > catalogue_transfer.MAX_BUFFER:
            if consumed or charged:
                break
            catalogue_transfer._fallback(state, "oversized_head_envelope")
            store.db.execute("DELETE FROM market_record_pending WHERE id=?", (sequence,))
            visited += 1
            continue
        row = (
            store.db.execute("SELECT payload_json FROM runtime WHERE key=?", (key,)).fetchone()
            if key is not None
            else None
        )
        visited += 1
        if row is not None and key is not None:
            assert size is not None
            charged += size[0]
            if catalogue_transfer._large_envelope(row[0]):
                catalogue_transfer._fallback(state, "oversized_head_expansion")
                store.db.execute("DELETE FROM market_record_pending WHERE id=?", (sequence,))
                continue
            point = catalogue_reader.point(key)
            if point is not None:
                matched = _head(store, key, row[0], state)
                if matched is None:
                    revision = catalogue_reader.revision(json_payload(row[0]), point)
                    coordinate = record_blocks.Coordinate(point.kind, point.identity, revision)
                    body_key = catalogue_reader.PREFIX + coordinate.key
                    if body_key in selected or pack(body_key) in selected:
                        continue  # Its selected body still has this or the next turn.
                if not matched:
                    catalogue_transfer._fallback(state, "retained_hot_head")
            else:
                alias = catalogue_aliases.point(key)
                if alias is not None:
                    if catalogue_aliases.publish(store.db, key, json_payload(row[0]), store.cached_bytes):
                        catalogue_transfer._drop(store, key, row[0])
                        state["aliases_moved"] = state.get("aliases_moved", 0) + 1
                    else:
                        body_key = (
                            catalogue_reader.PREFIX + record_blocks.Coordinate("event", alias.event, 1).key
                        )
                        if body_key in selected or pack(body_key) in selected:
                            continue  # Preserve the original body-before-alias dependency.
        store.db.execute("DELETE FROM market_record_pending WHERE id=?", (sequence,))
    return charged, visited
