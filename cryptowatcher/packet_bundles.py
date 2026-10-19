"""Explicit bounded regrouping metadata, maintained in the packet transaction.

NULL means an old packet has not yet been visited by the finite upgrade. New
detail packets are pending immediately. A checked packet losing live slots is
pending again, retaining its first pending timestamp until it is serviced.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .storage import StateError

CATEGORY = "bundle_pending"
SPARSE = "category='detail_packets' AND used>786432 AND live_bytes<used/2 AND live_packets>0"
INDEXES = {
    "market_bundle_pending": "CREATE INDEX market_bundle_pending ON market_packets(id) WHERE bundle_checked=0 AND refs>0",
    "market_bundle_age": "CREATE INDEX market_bundle_age ON market_packets(bundle_since,id) WHERE bundle_checked=0 AND refs>0",
    "market_bundle_unknown": "CREATE INDEX market_bundle_unknown ON market_packets(id) WHERE bundle_checked IS NULL",
    "market_bundle_sparse": f"CREATE INDEX market_bundle_sparse ON market_packet_arenas(category,bucket,id) WHERE {SPARSE}",
}
_VALID = """EXISTS(SELECT 1 FROM market_counts WHERE category='bundle_pending'
    AND typeof(records)='integer' AND records>=0 AND records<9223372036854775807
    AND logical_bytes=0 AND inserted_bytes=0 AND deleted_bytes=0)"""
_FIELDS = """NEW.bundle_checked IN(0,1) AND typeof(NEW.bundle_checked)='integer'
    AND ((NEW.bundle_checked=1 AND NEW.bundle_since IS NULL) OR
        (NEW.bundle_checked=0 AND typeof(NEW.bundle_since)='integer' AND NEW.bundle_since>=0
        AND (SELECT category FROM market_packet_arenas WHERE id=NEW.arena)='detail_packets'))"""


def _pending(prefix: str) -> str:
    return f"coalesce({prefix}.bundle_checked=0 AND {prefix}.refs>0,0)"


TRIGGERS = {
    "market_bundle_admit": f"""CREATE TRIGGER market_bundle_admit BEFORE INSERT ON market_packets BEGIN
        SELECT CASE WHEN NOT ({_VALID}) OR NOT coalesce(({_FIELDS}),0)
            THEN RAISE(ABORT,'Invalid bundle admission or counter') END; END""",
    "market_bundle_change": f"""CREATE TRIGGER market_bundle_change
        BEFORE UPDATE OF refs,bundle_checked,bundle_since ON market_packets BEGIN
        SELECT CASE WHEN NOT ({_VALID}) OR
            (NEW.bundle_checked IS NOT NULL AND NOT coalesce(({_FIELDS}),0)) OR
            (NEW.bundle_checked IS NULL AND NEW.bundle_since IS NOT NULL) OR
            (OLD.bundle_checked IS NOT NULL AND NEW.bundle_checked IS NULL) OR
            (OLD.bundle_checked=0 AND NEW.bundle_checked=0 AND OLD.bundle_since IS NOT NEW.bundle_since) OR
            (SELECT records FROM market_counts WHERE category='bundle_pending')+
                {_pending("NEW")}-{_pending("OLD")}<0
            THEN RAISE(ABORT,'Invalid bundle state or counter') END; END""",
    "market_bundle_keep": f"""CREATE TRIGGER market_bundle_keep BEFORE DELETE ON market_packets BEGIN
        SELECT CASE WHEN NOT ({_VALID}) OR
            (SELECT records FROM market_counts WHERE category='bundle_pending')<{_pending("OLD")}
            THEN RAISE(ABORT,'Missing bundle counter') END; END""",
    "market_bundle_added": f"""CREATE TRIGGER market_bundle_added AFTER INSERT ON market_packets BEGIN
        UPDATE market_counts SET records=records+{_pending("NEW")} WHERE category='bundle_pending'; END""",
    "market_bundle_changed": f"""CREATE TRIGGER market_bundle_changed
        AFTER UPDATE OF refs,bundle_checked,bundle_since ON market_packets BEGIN
        UPDATE market_counts SET records=records+{_pending("NEW")}-{_pending("OLD")}
            WHERE category='bundle_pending'; END""",
    "market_bundle_removed": f"""CREATE TRIGGER market_bundle_removed AFTER DELETE ON market_packets BEGIN
        UPDATE market_counts SET records=records-{_pending("OLD")} WHERE category='bundle_pending'; END""",
    "market_bundle_dirty": """CREATE TRIGGER market_bundle_dirty AFTER UPDATE OF refs ON market_packets
        WHEN OLD.bundle_checked=1 AND NEW.bundle_checked=1 AND NEW.refs>0 AND NEW.refs<>OLD.refs
            AND (SELECT category FROM market_packet_arenas WHERE id=NEW.arena)='detail_packets' BEGIN
        UPDATE market_packets SET bundle_checked=0,bundle_since=cw_clock_ms() WHERE id=NEW.id; END""",
}


def installed(db: sqlite3.Connection) -> bool:
    row = db.execute("SELECT value FROM market_meta WHERE key='packet_bundles'").fetchone()
    if row is not None and row[0] != "1":
        raise StateError("Unknown packet bundle layout; data preserved")
    return row is not None


def validate(db: sqlite3.Connection) -> None:
    if not installed(db):
        raise StateError("Missing packet bundle checkpoint")
    columns = [r[1] for r in db.execute("PRAGMA table_info(market_packets)")]
    if columns != ["id", "arena", "offset", "length", "sha256", "refs", "bundle_checked", "bundle_since"]:
        raise StateError("Unknown packet bundle columns")
    for name, sql in {**INDEXES, **TRIGGERS}.items():
        row = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if row is None or " ".join(row[0].split()) != " ".join(sql.split()):
            raise StateError("Missing or unknown packet bundle guard")
    header(db)


def install(db: sqlite3.Connection) -> None:
    if installed(db):
        validate(db)
        return
    if db.in_transaction:
        raise StateError("Packet bundle installation requires an outer boundary")
    columns = [r[1] for r in db.execute("PRAGMA table_info(market_packets)")]
    if columns != ["id", "arena", "offset", "length", "sha256", "refs"]:
        raise StateError("Unknown packet bundle installation; data preserved")
    with db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "ALTER TABLE market_packets ADD COLUMN bundle_checked INTEGER "
            "CHECK(bundle_checked IS NULL OR (typeof(bundle_checked)='integer' AND bundle_checked IN(0,1)))"
        )
        db.execute(
            "ALTER TABLE market_packets ADD COLUMN bundle_since INTEGER "
            "CHECK(bundle_since IS NULL OR (typeof(bundle_since)='integer' AND bundle_since>=0))"
        )
        db.execute("INSERT INTO market_counts VALUES('bundle_pending',0,0,0,0)")
        for sql in (*INDEXES.values(), *TRIGGERS.values()):
            db.execute(sql)
        db.execute("INSERT INTO market_meta VALUES('packet_bundles','1')")


def header(db: sqlite3.Connection) -> dict[str, Any]:
    if not installed(db):
        raise StateError("Missing packet bundle checkpoint")
    row = db.execute(
        "SELECT records,logical_bytes,inserted_bytes,deleted_bytes FROM market_counts WHERE category=?",
        (CATEGORY,),
    ).fetchone()
    if (
        row is None
        or any(type(n) is not int for n in row)
        or not 0 <= row[0] < 2**63
        or tuple(row[1:]) != (0, 0, 0)
    ):
        raise StateError("Missing packet bundle counter")
    oldest = db.execute(
        "SELECT id,bundle_since FROM market_packets WHERE bundle_checked=0 AND refs>0 "
        "ORDER BY bundle_since,id LIMIT 1"
    ).fetchone()
    if (
        (oldest is None) != (row[0] == 0)
        or oldest is not None
        and (type(oldest[1]) is not int or oldest[1] < 0)
    ):
        raise StateError("Conflicting packet bundle counter or clock")
    unknown = db.execute(
        "SELECT id FROM market_packets WHERE bundle_checked IS NULL ORDER BY id LIMIT 1"
    ).fetchone()
    return {
        "count": row[0],
        "oldest_id": oldest[0] if oldest is not None else None,
        "oldest_ms": oldest[1] if oldest is not None else None,
        "unknown": unknown is not None,
        "sparse": sparse(db) is not None,
    }


def sparse(db: sqlite3.Connection) -> int | None:
    row = db.execute(
        f"SELECT id FROM market_packet_arenas WHERE {SPARSE} ORDER BY category,bucket,id LIMIT 1"
    ).fetchone()
    return row[0] if row is not None else None


def verify(db: sqlite3.Connection) -> dict[str, Any]:
    """Deep caller-budgeted reconciliation; never used by the ordinary worker."""
    result = header(db)
    actual = db.execute("SELECT count(*) FROM market_packets WHERE bundle_checked=0 AND refs>0").fetchone()[0]
    if actual != result["count"]:
        raise StateError("Conflicting actual packet bundle count")
    return result


def tag(db: sqlite3.Connection, upper: int, batch: int) -> int:
    if type(upper) is not int or not 0 <= upper < 2**63 or type(batch) is not int or not 0 < batch <= 256:
        raise StateError("Invalid finite packet bundle tagging boundary")
    header(db)
    rows = db.execute(
        "SELECT p.id,p.refs,a.category FROM market_packets p JOIN market_packet_arenas a ON a.id=p.arena "
        "WHERE p.bundle_checked IS NULL AND p.id<=? ORDER BY p.id LIMIT ?",
        (upper, batch),
    ).fetchall()
    for identity, refs, category in rows:
        pending = refs > 0 and category == "detail_packets"
        db.execute(
            "UPDATE market_packets SET bundle_checked=?,bundle_since=CASE WHEN ? THEN cw_clock_ms() END "
            "WHERE id=? AND bundle_checked IS NULL",
            (0 if pending else 1, pending, identity),
        )
    return len(rows)


def checked(db: sqlite3.Connection, identity: int) -> None:
    if (
        db.execute(
            "UPDATE market_packets SET bundle_checked=1,bundle_since=NULL WHERE id=?", (identity,)
        ).rowcount
        != 1
    ):
        raise StateError("Missing checked packet")
