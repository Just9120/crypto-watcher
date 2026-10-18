"""Share immutable quality dimensions/provenance across days and chunks."""

from __future__ import annotations

import sqlite3
from typing import Any


def install(db: sqlite3.Connection) -> None:
    row = db.execute("SELECT type FROM sqlite_master WHERE name='market_quality'").fetchone()
    if row is None:
        return
    meta = db.execute("SELECT 1 FROM sqlite_master WHERE name='market_meta'").fetchone()
    if row[0] == "view":
        version = (
            db.execute("SELECT value FROM market_meta WHERE key='quality_layout'").fetchone()
            if meta
            else None
        )
        if meta and (version is None or version[0] != "3"):
            raise ValueError("Unknown quality layout; database preserved")
        return
    if db.in_transaction:
        raise ValueError("Quality installation requires an outer transaction boundary")
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("ALTER TABLE market_quality RENAME TO market_quality_legacy")
        db.execute("""CREATE TABLE market_quality_cohorts(
            id INTEGER PRIMARY KEY,external TEXT NOT NULL UNIQUE,dimensions TEXT NOT NULL,
            refs INTEGER NOT NULL DEFAULT 0)""")
        db.execute("""CREATE TABLE market_quality_provenance(
            id INTEGER PRIMARY KEY,payload TEXT NOT NULL UNIQUE,refs INTEGER NOT NULL DEFAULT 0)""")
        for table in ("market_quality_cohorts", "market_quality_provenance"):
            db.execute(f"CREATE INDEX {table}_unused ON {table}(refs,id)")
        db.execute("""CREATE TABLE market_quality_blocks(
            day INTEGER NOT NULL,cohort INTEGER NOT NULL,chunk INTEGER NOT NULL,
            expires_day INTEGER NOT NULL,provenance INTEGER NOT NULL,facts BLOB NOT NULL,n INTEGER NOT NULL,
            PRIMARY KEY(day,cohort,chunk))""")
        db.execute("CREATE INDEX market_quality_blocks_expiry ON market_quality_blocks(expires_day)")
        db.execute("""CREATE VIEW market_quality AS
            SELECT day,cohort,chunk,expires_day,dimensions,provenance,facts,n FROM market_quality_legacy
            UNION ALL SELECT b.day,c.external,b.chunk,b.expires_day,c.dimensions,p.payload,b.facts,b.n
            FROM market_quality_blocks b JOIN market_quality_cohorts c ON c.id=b.cohort
            JOIN market_quality_provenance p ON p.id=b.provenance""")
        put_sql = """
            INSERT OR IGNORE INTO market_quality_cohorts(external,dimensions) VALUES(NEW.cohort,NEW.dimensions);
            SELECT CASE WHEN (SELECT dimensions FROM market_quality_cohorts WHERE external=NEW.cohort)<>NEW.dimensions
                THEN RAISE(ABORT,'Conflicting quality cohort') END;
            INSERT OR IGNORE INTO market_quality_provenance(payload) VALUES(NEW.provenance);
            DELETE FROM market_quality_legacy WHERE day=NEW.day AND cohort=NEW.cohort AND chunk=NEW.chunk;
            INSERT INTO market_quality_blocks VALUES(NEW.day,
                (SELECT id FROM market_quality_cohorts WHERE external=NEW.cohort),NEW.chunk,NEW.expires_day,
                (SELECT id FROM market_quality_provenance WHERE payload=NEW.provenance),NEW.facts,NEW.n)
                ON CONFLICT(day,cohort,chunk) DO UPDATE SET facts=excluded.facts,n=excluded.n;
        """
        for op in ("INSERT", "UPDATE"):
            db.execute(
                f"CREATE TRIGGER market_quality_put_{op} INSTEAD OF {op} ON market_quality BEGIN {put_sql} END"
            )
        db.execute("""CREATE TRIGGER market_quality_remove INSTEAD OF DELETE ON market_quality BEGIN
            DELETE FROM market_quality_legacy WHERE day=OLD.day AND cohort=OLD.cohort AND chunk=OLD.chunk;
            DELETE FROM market_quality_blocks WHERE day=OLD.day AND chunk=OLD.chunk
                AND cohort=(SELECT id FROM market_quality_cohorts WHERE external=OLD.cohort);
        END""")
        db.execute("""CREATE TRIGGER market_quality_insert AFTER INSERT ON market_quality_blocks BEGIN
            UPDATE market_quality_cohorts SET refs=refs+1 WHERE id=NEW.cohort;
            UPDATE market_quality_provenance SET refs=refs+1 WHERE id=NEW.provenance;
            INSERT INTO market_counts VALUES ('quality',1,length(NEW.facts),length(NEW.facts),0)
                ON CONFLICT(category) DO UPDATE SET records=records+1,
                    logical_bytes=logical_bytes+length(NEW.facts),inserted_bytes=inserted_bytes+length(NEW.facts);
        END""")
        db.execute("""CREATE TRIGGER market_quality_delete AFTER DELETE ON market_quality_blocks BEGIN
            UPDATE market_quality_cohorts SET refs=refs-1 WHERE id=OLD.cohort;
            UPDATE market_quality_provenance SET refs=refs-1 WHERE id=OLD.provenance;
            UPDATE market_counts SET records=records-1,logical_bytes=logical_bytes-length(OLD.facts),
                deleted_bytes=deleted_bytes+length(OLD.facts) WHERE category='quality';
        END""")
        db.execute("""CREATE TRIGGER market_quality_update AFTER UPDATE OF facts ON market_quality_blocks BEGIN
            UPDATE market_counts SET logical_bytes=logical_bytes+length(NEW.facts)-length(OLD.facts),
                inserted_bytes=inserted_bytes+max(0,length(NEW.facts)-length(OLD.facts)),
                deleted_bytes=deleted_bytes+max(0,length(OLD.facts)-length(NEW.facts)) WHERE category='quality';
        END""")
        if meta:
            db.execute("INSERT INTO market_meta VALUES ('quality_layout','3')")
        db.commit()
    except BaseException:
        db.rollback()
        raise


def put(db: sqlite3.Connection, values: tuple[Any, ...]) -> None:
    kind = db.execute("SELECT type FROM sqlite_master WHERE name='market_quality'").fetchone()[0]
    db.execute(
        "INSERT INTO market_quality VALUES (?,?,?,?,?,?,?,?)"
        + (
            " ON CONFLICT(day,cohort,chunk) DO UPDATE SET facts=excluded.facts,n=excluded.n"
            if kind == "table"
            else ""
        ),
        values,
    )


def drain(db: sqlite3.Connection, batch: int = 32) -> int:
    rows = db.execute(
        "SELECT * FROM market_quality_legacy ORDER BY day,cohort,chunk LIMIT ?", (batch,)
    ).fetchall()
    for row in rows:
        put(db, tuple(row))
    collect_garbage(db, batch)
    return len(rows)


def collect_garbage(db: sqlite3.Connection, batch: int = 128) -> None:
    for table in ("market_quality_cohorts", "market_quality_provenance"):
        if (
            table == "market_quality_provenance"
            and db.execute("SELECT 1 FROM sqlite_master WHERE name='market_body_links'").fetchone()
        ):
            for (identity,) in db.execute(
                "SELECT id FROM market_quality_provenance WHERE refs=0 LIMIT ?", (batch,)
            ).fetchall():
                db.execute("DELETE FROM market_body_links WHERE owner=?", (f"quality-provenance:{identity}",))
        db.execute(f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} WHERE refs=0 LIMIT ?)", (batch,))
