"""Atomic append-only packet arenas inside the existing SQLite transaction.

This layer is not selected by the runtime until a domain reader/writer owns its
packet refs. Every read checks actual stored bytes. Published packet coordinates
may move only when both source and destination have the same full SHA256.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

CAPACITY = 1024 * 1024
MAXIMUM = CAPACITY // 2
CATEGORIES = frozenset({"detail_packets", "quality_packets"})


def install_functions(db: sqlite3.Connection) -> None:
    db.create_function(
        "cw_packet_digest",
        3,
        lambda arena, offset, length: hashlib.sha256(segment(db, arena, offset, length)).digest(),
    )


def install(db: sqlite3.Connection) -> None:
    install_functions(db)
    marker = db.execute("SELECT value FROM market_meta WHERE key='packet_layout'").fetchone()
    if marker is not None:
        present = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE name IN('market_packets','market_packet_arenas')"
            )
        }
        if marker[0] != "1" or present != {"market_packets", "market_packet_arenas"}:
            raise ValueError("Unknown packet arena layout; database preserved")
        return
    if db.in_transaction:
        raise ValueError("Packet installation requires an outer boundary")
    try:
        db.executescript(f"""
            BEGIN IMMEDIATE;
            CREATE TABLE market_packet_arenas(
                id INTEGER PRIMARY KEY AUTOINCREMENT,category TEXT NOT NULL,
                bucket INTEGER NOT NULL CHECK(typeof(bucket)='integer'),used INTEGER NOT NULL DEFAULT 0,
                live_packets INTEGER NOT NULL DEFAULT 0 CHECK(typeof(live_packets)='integer' AND live_packets>=0),
                live_bytes INTEGER NOT NULL DEFAULT 0 CHECK(typeof(live_bytes)='integer' AND live_bytes BETWEEN 0 AND used),
                payload BLOB NOT NULL CHECK(typeof(payload)='blob' AND length(payload)={CAPACITY}),
                CHECK(category IN('detail_packets','quality_packets')),
                CHECK(typeof(used)='integer' AND used BETWEEN 0 AND {CAPACITY}));
            CREATE INDEX market_packet_space ON market_packet_arenas(category,bucket,used);
            CREATE INDEX market_packet_empty ON market_packet_arenas(id) WHERE live_packets=0;
            CREATE TABLE market_packets(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                arena INTEGER NOT NULL REFERENCES market_packet_arenas(id),
                offset INTEGER NOT NULL CHECK(typeof(offset)='integer' AND offset>=0),
                length INTEGER NOT NULL CHECK(typeof(length)='integer' AND length BETWEEN 1 AND {MAXIMUM}),
                sha256 BLOB NOT NULL CHECK(typeof(sha256)='blob' AND length(sha256)=32),
                refs INTEGER NOT NULL DEFAULT 0 CHECK(typeof(refs)='integer' AND refs>=0));
            CREATE INDEX market_packet_arena ON market_packets(arena,id);
            CREATE INDEX market_packet_unused ON market_packets(id) WHERE refs=0;
            CREATE TRIGGER market_arena_prefix BEFORE UPDATE OF payload ON market_packet_arenas
                WHEN OLD.used>0 BEGIN SELECT RAISE(ABORT,'Published arena prefix is immutable'); END;
            CREATE TRIGGER market_arena_position BEFORE UPDATE OF used,bucket,category ON market_packet_arenas
                WHEN NEW.used<OLD.used OR NEW.bucket<>OLD.bucket OR NEW.category<>OLD.category BEGIN
                SELECT RAISE(ABORT,'Immutable arena position'); END;
            CREATE TRIGGER market_arena_keep BEFORE DELETE ON market_packet_arenas
                WHEN EXISTS(SELECT 1 FROM market_packets WHERE arena=OLD.id) BEGIN
                SELECT RAISE(ABORT,'Retained packet arena'); END;
            CREATE TRIGGER market_packet_keep BEFORE DELETE ON market_packets WHEN OLD.refs>0 BEGIN
                SELECT RAISE(ABORT,'Retained packet'); END;
            CREATE TRIGGER market_packet_counter_keep BEFORE DELETE ON market_packets BEGIN
                SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM market_counts WHERE
                    category=(SELECT category FROM market_packet_arenas WHERE id=OLD.arena)
                    AND records>0 AND logical_bytes>=OLD.length)
                    THEN RAISE(ABORT,'Missing packet counter') END;
            END;
            CREATE TRIGGER market_packet_admit BEFORE INSERT ON market_packets BEGIN
                SELECT CASE WHEN cw_packet_digest(NEW.arena,NEW.offset,NEW.length)<>NEW.sha256
                    THEN RAISE(ABORT,'Packet checksum mismatch') END;
            END;
            CREATE TRIGGER market_packet_move BEFORE UPDATE OF arena,offset,length,sha256 ON market_packets
                WHEN NEW.length<>OLD.length OR NEW.sha256<>OLD.sha256 OR
                    (SELECT category FROM market_packet_arenas WHERE id=NEW.arena) IS NOT
                        (SELECT category FROM market_packet_arenas WHERE id=OLD.arena) OR
                    (SELECT bucket FROM market_packet_arenas WHERE id=NEW.arena) IS NOT
                        (SELECT bucket FROM market_packet_arenas WHERE id=OLD.arena) OR
                    cw_packet_digest(OLD.arena,OLD.offset,OLD.length)<>OLD.sha256 OR
                    cw_packet_digest(NEW.arena,NEW.offset,NEW.length)<>OLD.sha256 BEGIN
                SELECT RAISE(ABORT,'Packet relocation changed bytes'); END;
            CREATE TRIGGER market_packet_insert AFTER INSERT ON market_packets BEGIN
                UPDATE market_packet_arenas SET live_packets=live_packets+1,live_bytes=live_bytes+NEW.length
                    WHERE id=NEW.arena;
                INSERT INTO market_counts SELECT category,1,NEW.length,NEW.length,0 FROM market_packet_arenas WHERE id=NEW.arena
                    ON CONFLICT(category) DO UPDATE SET records=records+1,
                    logical_bytes=logical_bytes+excluded.logical_bytes,inserted_bytes=inserted_bytes+excluded.inserted_bytes;
            END;
            CREATE TRIGGER market_packet_delete AFTER DELETE ON market_packets BEGIN
                UPDATE market_packet_arenas SET live_packets=live_packets-1,live_bytes=live_bytes-OLD.length WHERE id=OLD.arena;
                UPDATE market_counts SET records=records-1,logical_bytes=logical_bytes-OLD.length,
                    deleted_bytes=deleted_bytes+OLD.length WHERE category=(SELECT category FROM market_packet_arenas WHERE id=OLD.arena);
            END;
            CREATE TRIGGER market_packet_relocated AFTER UPDATE OF arena ON market_packets WHEN NEW.arena<>OLD.arena BEGIN
                UPDATE market_packet_arenas SET live_packets=live_packets-1,live_bytes=live_bytes-OLD.length WHERE id=OLD.arena;
                UPDATE market_packet_arenas SET live_packets=live_packets+1,live_bytes=live_bytes+NEW.length WHERE id=NEW.arena;
            END;
            INSERT INTO market_meta VALUES('packet_layout','1');
            COMMIT;
        """)
    except BaseException:
        db.rollback()
        raise


@contextmanager
def atomic(db: sqlite3.Connection) -> Iterator[None]:
    if not db.in_transaction:
        raise ValueError("Atomic packet operation requires an active writer transaction")
    db.execute("SAVEPOINT cw_packet_operation")
    try:
        yield
    except BaseException:
        db.execute("ROLLBACK TO cw_packet_operation")
        db.execute("RELEASE cw_packet_operation")
        raise
    else:
        db.execute("RELEASE cw_packet_operation")


def segment(db: sqlite3.Connection, arena: int, offset: int, length: int) -> bytes:
    if (
        type(arena) is not int
        or not 0 < arena < 2**63
        or type(offset) is not int
        or offset < 0
        or type(length) is not int
        or not 0 < length <= MAXIMUM
        or offset + length > CAPACITY
    ):
        raise ValueError("Invalid packet coordinate")
    row = db.execute("SELECT used,length(payload) FROM market_packet_arenas WHERE id=?", (arena,)).fetchone()
    if row is None or row[1] != CAPACITY or offset + length > row[0]:
        raise ValueError("Missing published packet segment")
    with db.blobopen("market_packet_arenas", "payload", arena, readonly=True) as blob:
        blob.seek(offset)
        result = blob.read(length)
    if len(result) != length:
        raise ValueError("Incomplete packet read")
    return result


def write_segment(
    db: sqlite3.Connection, category: str, bucket: int, raw: bytes, *, exclude: int = 0
) -> tuple[int, int]:
    if (
        category not in CATEGORIES
        or type(bucket) is not int
        or not -(2**63) <= bucket < 2**63
        or not isinstance(raw, bytes)
        or not 0 < len(raw) <= MAXIMUM
    ):
        raise ValueError("Invalid packet admission")
    row = db.execute(
        "SELECT id,used FROM market_packet_arenas WHERE category=? AND bucket=? AND used<=? AND id<>? ORDER BY used DESC LIMIT 1",
        (category, bucket, CAPACITY - len(raw), exclude),
    ).fetchone()
    if row is None:
        cursor = db.execute(
            "INSERT INTO market_packet_arenas(category,bucket,payload) VALUES(?,?,zeroblob(?))",
            (category, bucket, CAPACITY),
        )
        arena, offset = int(cursor.lastrowid or 0), 0
    else:
        arena, offset = int(row[0]), int(row[1])
    with db.blobopen("market_packet_arenas", "payload", arena, readonly=False) as blob:
        blob.seek(offset)
        blob.write(raw)
    changed = db.execute(
        "UPDATE market_packet_arenas SET used=used+? WHERE id=? AND used=?", (len(raw), arena, offset)
    ).rowcount
    if changed != 1:
        raise ValueError("Packet append position changed")
    return arena, offset


def append(db: sqlite3.Connection, category: str, bucket: int, raw: bytes) -> int:
    with atomic(db):
        arena, offset = write_segment(db, category, bucket, raw)
        row = db.execute(
            "INSERT INTO market_packets(arena,offset,length,sha256) VALUES(?,?,?,?)",
            (arena, offset, len(raw), hashlib.sha256(raw).digest()),
        )
        return int(row.lastrowid or 0)


def read(db: sqlite3.Connection, identity: int) -> bytes:
    if type(identity) is not int or not 0 < identity < 2**63:
        raise ValueError("Invalid packet identity")
    row = db.execute(
        "SELECT arena,offset,length,sha256 FROM market_packets WHERE id=?", (identity,)
    ).fetchone()
    if row is None:
        raise ValueError("Missing retained packet")
    raw = segment(db, int(row[0]), int(row[1]), int(row[2]))
    if hashlib.sha256(raw).digest() != bytes(row[3]):
        raise ValueError("Packet checksum mismatch")
    return raw


def retain(db: sqlite3.Connection, identity: int) -> None:
    valid_identity(identity)
    if db.execute("UPDATE market_packets SET refs=refs+1 WHERE id=?", (identity,)).rowcount != 1:
        raise ValueError("Missing packet owner target")


def release(db: sqlite3.Connection, identity: int) -> None:
    valid_identity(identity)
    if db.execute("UPDATE market_packets SET refs=refs-1 WHERE id=? AND refs>0", (identity,)).rowcount != 1:
        raise ValueError("Missing retained packet owner")


def relocate(db: sqlite3.Connection, identity: int) -> None:
    valid_identity(identity)
    with atomic(db):
        row = db.execute(
            "SELECT a.category,a.bucket,p.arena FROM market_packets p JOIN market_packet_arenas a ON a.id=p.arena WHERE p.id=?",
            (identity,),
        ).fetchone()
        if row is None:
            raise ValueError("Missing packet relocation source")
        raw = read(db, identity)
        arena, offset = write_segment(db, row[0], int(row[1]), raw, exclude=int(row[2]))
        db.execute("UPDATE market_packets SET arena=?,offset=? WHERE id=?", (arena, offset, identity))


def collect(db: sqlite3.Connection, batch: int = 32) -> int:
    if type(batch) is not int or not 0 < batch <= 256:
        raise ValueError("Invalid packet collection bound")
    with atomic(db):
        removed = db.execute(
            "DELETE FROM market_packets WHERE id IN(SELECT id FROM market_packets WHERE refs=0 ORDER BY id LIMIT ?)",
            (batch,),
        ).rowcount
        removed += db.execute(
            "DELETE FROM market_packet_arenas WHERE id IN(SELECT id FROM market_packet_arenas WHERE live_packets=0 ORDER BY id LIMIT ?)",
            (batch,),
        ).rowcount
        return removed


def valid_identity(identity: int) -> None:
    if type(identity) is not int or not 0 < identity < 2**63:
        raise ValueError("Invalid packet identity")


def summary(db: sqlite3.Connection) -> dict[str, Any]:
    row = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(used),0),COALESCE(SUM(live_bytes),0),COALESCE(SUM(live_packets),0) FROM market_packet_arenas"
    ).fetchone()
    return {
        "arenas": row[0],
        "reserved_payload_bytes": row[0] * CAPACITY,
        "written_bytes": row[1],
        "live_bytes": row[2],
        "packets": row[3],
    }
