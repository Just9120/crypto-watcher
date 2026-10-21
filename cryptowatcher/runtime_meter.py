"""Exact namespace accounting, with finite online initialization and no probe scan."""

from __future__ import annotations

import sqlite3
import time
from collections import defaultdict
from typing import Any


def namespace(key: str) -> str:
    return (key[9:] if key.startswith("radar:v1:") else key).split(":", 1)[0]


def install(db: sqlite3.Connection) -> None:
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='market_namespace_scan'").fetchone():
        row = db.execute("SELECT version FROM market_namespace_scan WHERE id=1").fetchone()
        if row is None or row[0] != 1:
            raise ValueError("Unknown namespace meter; database preserved")
        return
    if db.in_transaction:
        raise ValueError("Namespace meter installation requires an outer boundary")
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE market_namespaces(
            namespace TEXT PRIMARY KEY,records INTEGER NOT NULL CHECK(records>=0),
            key_bytes INTEGER NOT NULL CHECK(key_bytes>=0),payload_bytes INTEGER NOT NULL CHECK(payload_bytes>=0)) WITHOUT ROWID""")
        db.execute("""CREATE TABLE market_namespace_scan(
            id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL,cursor INTEGER,
            ceiling INTEGER NOT NULL,schema_version INTEGER NOT NULL,complete INTEGER NOT NULL)""")
        for operation in ("INSERT", "DELETE", "UPDATE"):
            parts = []
            for alias, sign in (("OLD", -1), ("NEW", 1)):
                if (alias == "OLD" and operation == "INSERT") or (alias == "NEW" and operation == "DELETE"):
                    continue
                # Rows beyond the initial ceiling are counted immediately. Rows
                # inside it become counted only after their committed scan slice.
                condition = f"complete=1 OR {alias}.rowid<=cursor OR {alias}.rowid>ceiling"
                if alias == "OLD":
                    parts.append(f"""SELECT CASE WHEN EXISTS(
                        SELECT 1 FROM market_namespace_scan WHERE id=1 AND ({condition}))
                        AND NOT EXISTS(SELECT 1 FROM market_namespaces WHERE namespace=cw_namespace(OLD.key) AND records>0)
                        THEN RAISE(ABORT,'Missing namespace counter') END;""")
                    parts.append(f"""UPDATE market_namespaces SET records=records-1,
                        key_bytes=key_bytes-length(CAST(OLD.key AS BLOB)),
                        payload_bytes=payload_bytes-length(CAST(OLD.payload_json AS BLOB))
                        WHERE namespace=cw_namespace(OLD.key) AND EXISTS(
                            SELECT 1 FROM market_namespace_scan WHERE id=1 AND ({condition}));""")
                    continue
                parts.append(f"""INSERT INTO market_namespaces
                    SELECT cw_namespace({alias}.key),{sign},{sign}*length(CAST({alias}.key AS BLOB)),
                        {sign}*length(CAST({alias}.payload_json AS BLOB)) FROM market_namespace_scan
                    WHERE id=1 AND ({condition})
                    ON CONFLICT(namespace) DO UPDATE SET records=records+excluded.records,
                        key_bytes=key_bytes+excluded.key_bytes,payload_bytes=payload_bytes+excluded.payload_bytes;""")
            db.execute(
                f"CREATE TRIGGER market_namespace_{operation} AFTER {operation} ON runtime BEGIN {' '.join(parts)} END"
            )
        last = db.execute("SELECT rowid FROM runtime ORDER BY rowid DESC LIMIT 1").fetchone()
        upper = last[0] if last else 0
        schema = db.execute("PRAGMA schema_version").fetchone()[0]
        db.execute(
            "INSERT INTO market_namespace_scan VALUES(1,1,NULL,?,?,?)", (upper, schema, int(last is None))
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def drain(db: sqlite3.Connection, *, batch: int = 256, deadline: float) -> bool:
    """Caller owns the transaction. Concurrent writes are covered by triggers."""
    state = db.execute(
        "SELECT cursor,ceiling,schema_version,complete FROM market_namespace_scan WHERE id=1"
    ).fetchone()
    schema = db.execute("PRAGMA schema_version").fetchone()[0]
    if state[2] != schema:
        # A rebuild/VACUUM can relocate transient rowids. Restart finite physical
        # accounting; public identities and source data remain untouched.
        last = db.execute("SELECT rowid FROM runtime ORDER BY rowid DESC LIMIT 1").fetchone()
        upper = last[0] if last else 0
        db.execute("DELETE FROM market_namespaces")
        db.execute(
            "UPDATE market_namespace_scan SET cursor=NULL,ceiling=?,schema_version=?,complete=? WHERE id=1",
            (upper, schema, int(last is None)),
        )
        state = (None, upper, schema, int(last is None))
    if state[3]:
        return True
    rows = db.execute(
        "SELECT rowid,key,length(CAST(payload_json AS BLOB)) FROM runtime WHERE rowid"
        + (">=?" if state[0] is None else ">?")
        + " AND rowid<=? ORDER BY rowid LIMIT ?",
        (-(2**63) if state[0] is None else state[0], state[1], batch),
    ).fetchall()
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    cursor = state[0]
    processed = 0
    for rid, key, size in rows:
        if processed and time.monotonic() >= deadline:
            break
        totals = counts[namespace(key)]
        totals[0] += 1
        totals[1] += len(key.encode())
        totals[2] += size
        cursor = rid
        processed += 1
    db.executemany(
        """INSERT INTO market_namespaces VALUES(?,?,?,?) ON CONFLICT(namespace)
        DO UPDATE SET records=records+excluded.records,key_bytes=key_bytes+excluded.key_bytes,
            payload_bytes=payload_bytes+excluded.payload_bytes""",
        [(key, *value) for key, value in counts.items()],
    )
    complete = processed == len(rows) and (len(rows) < batch or (cursor is not None and cursor >= state[1]))
    db.execute("UPDATE market_namespace_scan SET cursor=?,complete=? WHERE id=1", (cursor, int(complete)))
    return complete


def read(db: sqlite3.Connection) -> dict[str, Any] | None:
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='market_namespace_scan'").fetchone():
        return None
    row = db.execute("SELECT complete,schema_version FROM market_namespace_scan WHERE id=1").fetchone()
    if row is None or not row[0] or row[1] != db.execute("PRAGMA schema_version").fetchone()[0]:
        return None
    return {
        row[0]: {"records": row[1], "key_bytes": row[2], "payload_bytes": row[3]}
        for row in db.execute(
            "SELECT namespace,records,key_bytes,payload_bytes FROM market_namespaces WHERE records>0"
        )
    }
