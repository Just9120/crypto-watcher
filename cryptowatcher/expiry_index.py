"""Normalize repeated expiry keys via explicit stable IDs; online legacy drain.

The public SQL view preserves the old four-column contract. Installation is a
small atomic DDL extension, not a data copy. Existing rows remain authoritative
until individually moved in the caller's bounded transaction.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

SERVICE_FILTER = (
    "category NOT IN ('event','event-head','outcome','outcome-head','comparison','comparison-head','history')"
)


def install(db: sqlite3.Connection) -> None:
    row = db.execute("SELECT type FROM sqlite_master WHERE name='market_lifetimes'").fetchone()
    if row is None:
        return
    if row[0] == "view":
        version = db.execute("SELECT value FROM market_meta WHERE key='expiry_layout'").fetchone()
        if version is None or version[0] not in ("3", "4"):
            raise ValueError("Unknown expiry layout; database preserved")
        if version[0] == "3":
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("DROP VIEW market_lifetimes")
                db.execute("DROP TRIGGER market_expiry_orphan")
                views(db)
                byte_counter(db)
                db.execute("UPDATE market_meta SET value='4' WHERE key='expiry_layout'")
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return
    if db.in_transaction:
        raise ValueError("Expiry installation requires an outer transaction boundary")
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("ALTER TABLE market_lifetimes RENAME TO market_expiry_legacy")
        db.execute("""CREATE TABLE market_event_ids(
            id INTEGER PRIMARY KEY, external TEXT NOT NULL UNIQUE, refs INTEGER NOT NULL DEFAULT 0)""")
        db.execute("CREATE INDEX market_event_unused ON market_event_ids(refs,id)")
        db.execute("CREATE TABLE market_runtime_keys(id INTEGER PRIMARY KEY,external TEXT NOT NULL UNIQUE)")
        db.execute("""CREATE TABLE market_expiry(
            rid INTEGER PRIMARY KEY REFERENCES market_runtime_keys(id) ON DELETE CASCADE,
            expires INTEGER NOT NULL, category TEXT NOT NULL,
            event_ref INTEGER REFERENCES market_event_ids(id), key_bytes INTEGER NOT NULL)""")
        db.execute("CREATE INDEX market_expiry_event ON market_expiry(event_ref,rid)")
        db.execute("CREATE INDEX market_expiry_due ON market_expiry(expires,rid)")
        db.execute(
            f"CREATE INDEX market_expiry_service_due ON market_expiry(expires,rid) WHERE {SERVICE_FILTER}"
        )
        views(db)
        db.execute("""CREATE TRIGGER market_expiry_insert AFTER INSERT ON market_expiry BEGIN
            UPDATE market_event_ids SET refs=refs+1 WHERE id=NEW.event_ref;
            INSERT INTO market_counts VALUES ('service_indexes',1,NEW.key_bytes,NEW.key_bytes,0)
                ON CONFLICT(category) DO UPDATE SET records=records+1,
                    logical_bytes=logical_bytes+NEW.key_bytes,inserted_bytes=inserted_bytes+NEW.key_bytes;
        END""")
        db.execute("""CREATE TRIGGER market_expiry_delete AFTER DELETE ON market_expiry BEGIN
            UPDATE market_event_ids SET refs=refs-1 WHERE id=OLD.event_ref;
            UPDATE market_counts SET records=records-1,logical_bytes=logical_bytes-OLD.key_bytes,
                deleted_bytes=deleted_bytes+OLD.key_bytes WHERE category='service_indexes';
        END""")
        db.execute("""CREATE TRIGGER market_expiry_update AFTER UPDATE OF event_ref ON market_expiry BEGIN
            UPDATE market_event_ids SET refs=refs-1 WHERE id=OLD.event_ref;
            UPDATE market_event_ids SET refs=refs+1 WHERE id=NEW.event_ref;
        END""")
        byte_counter(db)
        db.execute("INSERT INTO market_meta VALUES ('expiry_layout','4')")
        db.commit()
    except BaseException:
        db.rollback()
        raise


def views(db: sqlite3.Connection) -> None:
    db.execute("""CREATE VIEW market_lifetimes AS
        SELECT key,expires,category,event_id FROM market_expiry_legacy UNION ALL
        SELECT cw_key_unpack(r.external),e.expires,e.category,i.external FROM market_expiry e
        JOIN market_runtime_keys r ON r.id=e.rid LEFT JOIN market_event_ids i ON i.id=e.event_ref""")
    put = """
        DELETE FROM market_expiry_legacy WHERE key=NEW.key;
        INSERT OR IGNORE INTO market_event_ids(external) SELECT NEW.event_id WHERE NEW.event_id IS NOT NULL;
        INSERT OR IGNORE INTO market_runtime_keys(external)
            SELECT cw_key_pack(key) FROM runtime WHERE key=NEW.key AND NOT EXISTS(
                SELECT 1 FROM market_runtime_keys WHERE external IN(NEW.key,cw_key_pack(NEW.key)));
        INSERT INTO market_expiry(rid,expires,category,event_ref,key_bytes)
            SELECT id,NEW.expires,NEW.category,(SELECT id FROM market_event_ids WHERE external=NEW.event_id),
                length(CAST(cw_key_pack(NEW.key) AS BLOB)) FROM market_runtime_keys
            WHERE external IN(NEW.key,cw_key_pack(NEW.key))
            ON CONFLICT(rid) DO UPDATE SET expires=excluded.expires,category=excluded.category,
                event_ref=excluded.event_ref,key_bytes=excluded.key_bytes;
    """
    for operation in ("INSERT", "UPDATE"):
        db.execute(
            f"CREATE TRIGGER market_expiry_put_{operation} INSTEAD OF {operation} ON market_lifetimes BEGIN {put} END"
        )
    db.execute("""CREATE TRIGGER market_expiry_remove INSTEAD OF DELETE ON market_lifetimes BEGIN
        DELETE FROM market_expiry_legacy WHERE key=OLD.key;
        DELETE FROM market_expiry WHERE rid IN(SELECT id FROM market_runtime_keys WHERE external IN(OLD.key,cw_key_pack(OLD.key)));
    END""")
    db.execute("""CREATE TRIGGER market_expiry_orphan AFTER DELETE ON runtime BEGIN
        DELETE FROM market_runtime_keys WHERE external IN(OLD.key,cw_key_pack(OLD.key));
        DELETE FROM market_expiry_legacy WHERE key=OLD.key;
    END""")


def byte_counter(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TRIGGER market_expiry_key_bytes AFTER UPDATE OF key_bytes ON market_expiry
        WHEN NEW.key_bytes<>OLD.key_bytes BEGIN
        UPDATE market_counts SET logical_bytes=logical_bytes+NEW.key_bytes-OLD.key_bytes,
            inserted_bytes=inserted_bytes+max(0,NEW.key_bytes-OLD.key_bytes),
            deleted_bytes=deleted_bytes+max(0,OLD.key_bytes-NEW.key_bytes) WHERE category='service_indexes';
    END""")


def expires(db: sqlite3.Connection, key: str) -> Any:
    if db.execute("SELECT type FROM sqlite_master WHERE name='market_lifetimes'").fetchone()[0] != "view":
        return db.execute("SELECT expires FROM market_lifetimes WHERE key=?", (key,)).fetchone()
    return db.execute(
        "SELECT expires FROM market_expiry_legacy WHERE key=? UNION ALL SELECT expires FROM market_expiry WHERE rid IN(SELECT id FROM market_runtime_keys WHERE external IN(?,cw_key_pack(?)))",
        (key, key, key),
    ).fetchone()


def remove(db: sqlite3.Connection, key: str) -> None:
    if db.execute("SELECT type FROM sqlite_master WHERE name='market_lifetimes'").fetchone()[0] != "view":
        db.execute("DELETE FROM market_lifetimes WHERE key=?", (key,))
        return
    db.execute("DELETE FROM market_expiry_legacy WHERE key=?", (key,))
    db.execute(
        "DELETE FROM market_expiry WHERE rid IN(SELECT id FROM market_runtime_keys WHERE external IN(?,cw_key_pack(?)))",
        (key, key),
    )


def renew(db: sqlite3.Connection, key: str, value: int, *, shorten: bool = False) -> None:
    expression = "min(expires,?)" if shorten else "?"
    if db.execute("SELECT type FROM sqlite_master WHERE name='market_lifetimes'").fetchone()[0] != "view":
        db.execute(f"UPDATE market_lifetimes SET expires={expression} WHERE key=?", (value, key))
        return
    db.execute(f"UPDATE market_expiry_legacy SET expires={expression} WHERE key=?", (value, key))
    db.execute(
        f"UPDATE market_expiry SET expires={expression} WHERE rid IN(SELECT id FROM market_runtime_keys WHERE external IN(?,cw_key_pack(?)))",
        (value, key, key),
    )


def member_deadline(db: sqlite3.Connection, prefix: str) -> int | None:
    if db.execute("SELECT type FROM sqlite_master WHERE name='market_lifetimes'").fetchone()[0] != "view":
        return db.execute(
            "SELECT MIN(expires) FROM market_lifetimes WHERE key>=? AND key<?", (prefix, prefix + "~")
        ).fetchone()[0]
    return db.execute(
        "SELECT MIN(expires) FROM (SELECT expires FROM market_expiry_legacy WHERE key>=? AND key<? UNION ALL SELECT e.expires FROM runtime r JOIN market_runtime_keys k ON k.external IN(r.key,cw_key_pack(r.key)) JOIN market_expiry e ON e.rid=k.id WHERE r.key>=? AND r.key<?)",
        (prefix, prefix + "~", prefix, prefix + "~"),
    ).fetchone()[0]


def drain(db: sqlite3.Connection, batch: int = 128, seconds: float = 0.025) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    rows = db.execute(
        "SELECT key,expires,category,event_id FROM market_expiry_legacy ORDER BY key LIMIT ?", (batch,)
    ).fetchall()
    processed = 0
    for row in rows:
        if time.monotonic() >= deadline:
            break
        # The view trigger already removes the legacy row and admits only an
        # existing runtime key. Avoid a redundant lookup on every migrated row.
        db.execute("INSERT INTO market_lifetimes VALUES (?,?,?,?)", tuple(row))
        processed += 1
    collect_garbage(db, batch)
    return {"moved": processed, "legacy_empty": not rows or (len(rows) < batch and processed == len(rows))}


def collect_garbage(db: sqlite3.Connection, batch: int = 128) -> None:
    db.execute(
        "DELETE FROM market_event_ids WHERE id IN (SELECT id FROM market_event_ids WHERE refs=0 LIMIT ?)",
        (batch,),
    )


def due_services(db: sqlite3.Connection, now_ms: int, batch: int) -> list[Any]:
    """Limit each indexed branch before merging; never sort all expired rows."""
    if db.execute("SELECT type FROM sqlite_master WHERE name='market_lifetimes'").fetchone()[0] != "view":
        return db.execute(
            f"SELECT key,category FROM market_lifetimes WHERE expires<=? AND {SERVICE_FILTER} ORDER BY expires,key LIMIT ?",
            (now_ms, batch),
        ).fetchall()
    legacy = db.execute(
        f"SELECT key,category,expires FROM market_expiry_legacy WHERE expires<=? AND {SERVICE_FILTER} ORDER BY expires,key LIMIT ?",
        (now_ms, batch),
    ).fetchall()
    compact = db.execute(
        f"SELECT cw_key_unpack(r.external),e.category,e.expires FROM market_expiry e JOIN market_runtime_keys r ON r.id=e.rid WHERE expires<=? AND {SERVICE_FILTER} ORDER BY e.expires,e.rid LIMIT ?",
        (now_ms, batch),
    ).fetchall()
    return sorted([*legacy, *compact], key=lambda row: (row[2], row[0]))[:batch]
