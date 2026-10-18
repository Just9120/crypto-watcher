"""Replace repeated owner keys/body hashes in links with indexed integer pairs.

The compatibility view preserves both the public link coordinates and legacy
rows during a finite online drain. Body counts remain the shared authority.
"""

from __future__ import annotations

import sqlite3
import time


def install(db: sqlite3.Connection) -> None:
    existing = db.execute("SELECT type FROM sqlite_master WHERE name='market_body_links'").fetchone()
    if existing is None:
        return
    if existing[0] == "view":
        marker = db.execute("SELECT value FROM market_meta WHERE key='reference_layout'").fetchone()
        if marker is None or marker[0] != "1":
            raise ValueError("Unknown body reference layout; database preserved")
        return
    if db.in_transaction:
        raise ValueError("Reference installation requires an outer boundary")
    try:
        db.executescript("""
            BEGIN IMMEDIATE;
            ALTER TABLE market_body_links RENAME TO market_body_links_legacy;
            CREATE TABLE market_body_ids(id INTEGER PRIMARY KEY,external TEXT NOT NULL UNIQUE);
            CREATE TABLE market_references(
                owner INTEGER NOT NULL REFERENCES market_runtime_keys(id) ON DELETE CASCADE,
                body INTEGER NOT NULL REFERENCES market_body_ids(id),PRIMARY KEY(owner,body)) WITHOUT ROWID;
            CREATE INDEX market_reference_reverse ON market_references(body,owner);
            CREATE VIEW market_body_links AS
                SELECT owner,body FROM market_body_links_legacy UNION ALL
                SELECT k.external,b.external FROM market_references r
                JOIN market_runtime_keys k ON k.id=r.owner JOIN market_body_ids b ON b.id=r.body;
            CREATE TRIGGER market_reference_put INSTEAD OF INSERT ON market_body_links BEGIN
                SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM market_body_counts WHERE body=NEW.body)
                    THEN RAISE(ABORT,'Missing shared body counter') END;
                INSERT OR IGNORE INTO market_runtime_keys(external) VALUES(NEW.owner);
                INSERT OR IGNORE INTO market_body_ids(external) VALUES(NEW.body);
                DELETE FROM market_body_links_legacy WHERE owner=NEW.owner AND body=NEW.body;
                INSERT OR IGNORE INTO market_references VALUES(
                    (SELECT id FROM market_runtime_keys WHERE external=NEW.owner),
                    (SELECT id FROM market_body_ids WHERE external=NEW.body));
            END;
            CREATE TRIGGER market_reference_remove INSTEAD OF DELETE ON market_body_links BEGIN
                DELETE FROM market_body_links_legacy WHERE owner=OLD.owner AND body=OLD.body;
                DELETE FROM market_references WHERE owner=(SELECT id FROM market_runtime_keys WHERE external=OLD.owner)
                    AND body=(SELECT id FROM market_body_ids WHERE external=OLD.body);
            END;
            CREATE TRIGGER market_reference_insert AFTER INSERT ON market_references BEGIN
                UPDATE market_body_counts SET refs=refs+1 WHERE body=(SELECT external FROM market_body_ids WHERE id=NEW.body);
                INSERT INTO market_counts VALUES('reference_indexes',1,16,16,0) ON CONFLICT(category) DO UPDATE SET
                    records=records+1,logical_bytes=logical_bytes+16,inserted_bytes=inserted_bytes+16;
            END;
            CREATE TRIGGER market_reference_delete AFTER DELETE ON market_references BEGIN
                UPDATE market_body_counts SET refs=refs-1 WHERE body=(SELECT external FROM market_body_ids WHERE id=OLD.body);
                UPDATE market_counts SET records=records-1,logical_bytes=logical_bytes-16,
                    deleted_bytes=deleted_bytes+16 WHERE category='reference_indexes';
                DELETE FROM market_body_ids WHERE id=OLD.body AND NOT EXISTS(
                    SELECT 1 FROM market_references WHERE body=OLD.body);
                DELETE FROM market_runtime_keys WHERE id=OLD.owner AND NOT EXISTS(
                    SELECT 1 FROM market_references WHERE owner=OLD.owner) AND NOT EXISTS(
                    SELECT 1 FROM market_expiry WHERE rid=OLD.owner) AND NOT EXISTS(
                    SELECT 1 FROM runtime WHERE key=market_runtime_keys.external);
            END;
            INSERT INTO market_meta VALUES('reference_layout','1');
            COMMIT;
        """)
    except BaseException:
        db.rollback()
        raise


def drain(db: sqlite3.Connection, batch: int, deadline: float) -> bool:
    rows = db.execute(
        "SELECT owner,body FROM market_body_links_legacy ORDER BY owner,body LIMIT ?", (batch,)
    ).fetchall()
    processed = 0
    for value in rows:
        if processed and time.monotonic() >= deadline:
            break
        db.execute("INSERT INTO market_body_links VALUES(?,?)", tuple(value))
        processed += 1
    return processed == len(rows) and len(rows) < batch
