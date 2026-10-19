"""Transactional immutable domain catalogue; not enabled by opening a Store.

The future runtime adapter must merge legacy records, ownership, expiry and
cohort accounting before selecting this layout. This module alone is not a
data migration or an application capacity qualification.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any, Iterable

from . import packet_store, record_blocks
from .record_blocks import Coordinate
from .storage import encode


def install(db: sqlite3.Connection, cache: Any = None) -> None:
    from .catalogue_cohorts import install as install_counters
    from .record_ownership import install as install_ownership

    packet_store.install(db)
    marker = db.execute("SELECT value FROM market_meta WHERE key='record_catalog'").fetchone()
    if marker is not None:
        found = {
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN('market_record_parents','market_records','market_record_members')"
            )
        }
        if marker[0] != "2" or len(found) != 3:
            raise ValueError("Unknown domain catalogue; database preserved")
        install_ownership(db, cache)
        install_counters(db)
        return
    if db.in_transaction:
        raise ValueError("Catalogue installation requires an outer boundary")
    try:
        db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE market_record_parents(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_uuid BLOB NOT NULL UNIQUE CHECK(typeof(public_uuid)='blob' AND length(public_uuid)=16),
                observed INTEGER NOT NULL CHECK(typeof(observed)='integer'),
                expires INTEGER NOT NULL CHECK(typeof(expires)='integer' AND expires>=observed));
            CREATE INDEX market_record_history ON market_record_parents(observed,public_uuid);
            CREATE INDEX market_record_parent_due ON market_record_parents(expires,id);
            CREATE TABLE market_records(
                kind INTEGER NOT NULL CHECK(typeof(kind)='integer' AND kind BETWEEN 0 AND 4),
                public_uuid BLOB NOT NULL CHECK(typeof(public_uuid)='blob' AND length(public_uuid)=16),
                revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision BETWEEN 1 AND 9999999999),
                parent INTEGER REFERENCES market_record_parents(id),
                packet INTEGER NOT NULL REFERENCES market_packets(id),
                slot INTEGER NOT NULL CHECK(typeof(slot)='integer' AND slot BETWEEN 0 AND 63),
                current INTEGER NOT NULL CHECK(typeof(current)='integer' AND current IN(0,1)),
                expires INTEGER CHECK(expires IS NULL OR typeof(expires)='integer'),
                CHECK((parent IS NULL)<>(expires IS NULL)),
                PRIMARY KEY(kind,public_uuid,revision)) WITHOUT ROWID;
            CREATE INDEX market_record_children ON market_records(parent);
            CREATE UNIQUE INDEX market_record_heads ON market_records(kind,public_uuid) WHERE current=1;
            CREATE INDEX market_record_service_due ON market_records(expires) WHERE expires IS NOT NULL;
            CREATE TABLE market_record_members(
                episode BLOB NOT NULL CHECK(typeof(episode)='blob' AND length(episode)=16),
                observed INTEGER NOT NULL CHECK(typeof(observed)='integer'),
                parent INTEGER NOT NULL REFERENCES market_record_parents(id),
                PRIMARY KEY(episode,observed,parent)) WITHOUT ROWID;
            CREATE INDEX market_record_member_parent ON market_record_members(parent,episode,observed);
            CREATE TRIGGER market_record_parent_frozen BEFORE UPDATE ON market_record_parents BEGIN
                SELECT RAISE(ABORT,'Immutable record parent'); END;
            CREATE TRIGGER market_record_frozen BEFORE UPDATE OF kind,public_uuid,revision,parent,packet,slot,expires ON market_records
                BEGIN SELECT RAISE(ABORT,'Immutable record coordinate'); END;
            CREATE TRIGGER market_record_keep_parent BEFORE DELETE ON market_record_parents
                WHEN EXISTS(SELECT 1 FROM market_records WHERE parent=OLD.id) OR
                    EXISTS(SELECT 1 FROM market_record_members WHERE parent=OLD.id) BEGIN
                SELECT RAISE(ABORT,'Retained record parent'); END;
            CREATE TRIGGER market_record_new BEFORE INSERT ON market_records BEGIN
                SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM market_packets WHERE id=NEW.packet)
                    THEN RAISE(ABORT,'Missing record packet') END;
            END;
            CREATE TRIGGER market_record_insert AFTER INSERT ON market_records BEGIN
                UPDATE market_packets SET refs=refs+1 WHERE id=NEW.packet;
                INSERT INTO market_counts VALUES('record_catalog',1,56,56,0)
                    ON CONFLICT(category) DO UPDATE SET records=records+1,
                    logical_bytes=logical_bytes+56,inserted_bytes=inserted_bytes+56;
            END;
            CREATE TRIGGER market_record_delete_guard BEFORE DELETE ON market_records BEGIN
                SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM market_counts WHERE category='record_catalog' AND records>0 AND logical_bytes>=56)
                    OR NOT EXISTS(SELECT 1 FROM market_packets WHERE id=OLD.packet AND refs>0)
                    THEN RAISE(ABORT,'Missing record ownership counter') END;
            END;
            CREATE TRIGGER market_record_delete AFTER DELETE ON market_records BEGIN
                UPDATE market_packets SET refs=refs-1 WHERE id=OLD.packet;
                UPDATE market_counts SET records=records-1,logical_bytes=logical_bytes-56,
                    deleted_bytes=deleted_bytes+56 WHERE category='record_catalog';
            END;
            INSERT INTO market_meta VALUES('record_catalog','2');
            COMMIT;
        """)
    except BaseException:
        db.rollback()
        raise
    install_ownership(db, cache)
    install_counters(db)


def binary(identity: str) -> bytes:
    value = uuid.UUID(identity)
    if str(value) != identity:
        raise ValueError("Noncanonical public identity")
    return value.bytes


def row(db: sqlite3.Connection, coordinate: Coordinate) -> sqlite3.Row | tuple[Any, ...] | None:
    coordinate.validate()
    return db.execute(
        "SELECT packet,slot,parent,expires,current FROM market_records WHERE kind=? AND public_uuid=? AND revision=?",
        (record_blocks.KINDS.index(coordinate.kind), binary(coordinate.identity), coordinate.revision),
    ).fetchone()


def read(db: sqlite3.Connection, coordinate: Coordinate) -> dict[str, Any] | None:
    saved = row(db, coordinate)
    if saved is None:
        return None
    return record_blocks.read(packet_store.read(db, saved[0]), saved[1], coordinate)


def head(db: sqlite3.Connection, kind: str, identity: str) -> tuple[Coordinate, dict[str, Any] | None] | None:
    if kind not in record_blocks.KINDS:
        raise ValueError("Unknown record kind")
    saved = db.execute(
        "SELECT packet,slot,revision FROM market_records WHERE kind=? AND public_uuid=? AND current=1",
        (record_blocks.KINDS.index(kind), binary(identity)),
    ).fetchone()
    if saved is None:
        return None
    values = record_blocks.projections(packet_store.read(db, saved[0]))
    coordinate = Coordinate(kind, identity, saved[2])
    if not 0 <= saved[1] < len(values) or values[saved[1]][0] != coordinate:
        raise ValueError("Projected catalogue coordinate mismatch")
    return values[saved[1]]


def parent(db: sqlite3.Connection, identity: str, observed: int, expires: int) -> int:
    if type(observed) is not int or type(expires) is not int or expires < observed:
        raise ValueError("Invalid event retention anchor")
    public = binary(identity)
    saved = db.execute(
        "SELECT id,observed,expires FROM market_record_parents WHERE public_uuid=?", (public,)
    ).fetchone()
    if saved is None:
        db.execute(
            "INSERT OR IGNORE INTO market_record_parents(public_uuid,observed,expires) VALUES(?,?,?)",
            (public, observed, expires),
        )
        saved = db.execute(
            "SELECT id,observed,expires FROM market_record_parents WHERE public_uuid=?", (public,)
        ).fetchone()
    if saved is None or tuple(saved[1:]) != (observed, expires):
        raise ValueError("Conflicting retained event anchor")
    return int(saved[0])


def append(
    db: sqlite3.Connection,
    entries: list[tuple[Coordinate, dict[str, Any], dict[str, Any] | None]],
    *,
    bucket: int,
    owners: dict[Coordinate, int | None],
    deadlines: dict[Coordinate, int | None],
    current: Iterable[Coordinate] = (),
) -> int:
    """Publish an entire qualified batch or nothing; caller owns the outer tx.

    Parent anchors must already exist in the same transaction. The block bucket
    is the common expiry UTC day, so partial expiry cannot pin unrelated months.
    Existing coordinates are compared in full; a retry never creates new bodies.
    """
    heads = set(current)
    if not 0 < len(entries) <= record_blocks.MAX_RECORDS or not heads <= {c for c, _, _ in entries}:
        raise ValueError("Invalid catalogue batch")
    if len({(c.kind, c.identity) for c in heads}) != len(heads):
        raise ValueError("Conflicting current catalogue revisions")
    with packet_store.atomic(db):
        fresh = []
        for coordinate, value, projected in entries:
            if coordinate in heads and coordinate.kind != "decision" and projected is None:
                raise ValueError("Missing current record projection")
            old = row(db, coordinate)
            owner, expiry = owners[coordinate], deadlines[coordinate]
            if (owner is None) == (expiry is None):
                raise ValueError("Record must have exactly one retention anchor")
            if owner is not None:
                if type(owner) is not int or owner < 1:
                    raise ValueError("Invalid record parent")
                anchor = db.execute(
                    "SELECT public_uuid,expires FROM market_record_parents WHERE id=?", (owner,)
                ).fetchone()
                if anchor is None or value.get("event_id") != str(uuid.UUID(bytes=bytes(anchor[0]))):
                    raise ValueError("Conflicting record event parent")
                expected_expiry = anchor[1]
            else:
                if type(expiry) is not int:
                    raise ValueError("Invalid service retention deadline")
                expected_expiry = expiry
            if expected_expiry // 86400000 != bucket:
                raise ValueError("Record bucket crosses retention day")
            if old is not None:
                raw = packet_store.read(db, old[0])
                if (
                    encode(record_blocks.read(raw, old[1], coordinate)) != encode(value)
                    or encode(record_blocks.projections(raw)[old[1]][1]) != encode(projected)
                    or tuple(old[2:4]) != (owner, expiry)
                ):
                    raise ValueError("Conflicting immutable catalogue retry")
            else:
                fresh.append((coordinate, value, projected))
        packet = packet_store.append(db, "detail_packets", bucket, record_blocks.pack(fresh)) if fresh else 0
        if fresh:
            from .record_ownership import retain_bodies

            retain_bodies(db, packet, [value for _, value, _ in fresh])
        for slot, (coordinate, _, _) in enumerate(fresh):
            db.execute(
                "INSERT INTO market_records(kind,public_uuid,revision,parent,packet,slot,current,expires) VALUES(?,?,?,?,?,?,0,?)",
                (
                    record_blocks.KINDS.index(coordinate.kind),
                    binary(coordinate.identity),
                    coordinate.revision,
                    owners[coordinate],
                    packet,
                    slot,
                    deadlines[coordinate],
                ),
            )
        for coordinate in heads:
            previous = head(db, coordinate.kind, coordinate.identity)
            if previous is not None and previous[0].revision > coordinate.revision:
                raise ValueError("Stale catalogue head revision")
            if previous is not None and previous[0] == coordinate:
                continue
            key = record_blocks.KINDS.index(coordinate.kind), binary(coordinate.identity)
            db.execute(
                "UPDATE market_records SET current=0 WHERE kind=? AND public_uuid=? AND current=1", key
            )
            if (
                db.execute(
                    "UPDATE market_records SET current=1 WHERE kind=? AND public_uuid=? AND revision=?",
                    (*key, coordinate.revision),
                ).rowcount
                != 1
            ):
                raise ValueError("Missing new catalogue head")
        return len(fresh)


def member(db: sqlite3.Connection, episode: str, observed: int, owner: int) -> None:
    if type(observed) is not int or type(owner) is not int or owner < 1:
        raise ValueError("Invalid episode membership")
    db.execute(
        "INSERT OR IGNORE INTO market_record_members VALUES(?,?,?)", (binary(episode), observed, owner)
    )
