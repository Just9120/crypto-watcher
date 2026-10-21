"""Exact archived block/fact totals after a finite, transactional initialization."""

from __future__ import annotations

import sqlite3
import time


def install(db: sqlite3.Connection) -> None:
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='market_quality_tally'").fetchone():
        row = db.execute("SELECT version FROM market_quality_tally WHERE id=1").fetchone()
        if row is None or row[0] != 1:
            raise ValueError("Unknown archived fact meter; database preserved")
        return
    if db.in_transaction:
        raise ValueError("Quality meter installation requires an outer boundary")
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE market_quality_tally(
            id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL,
            blocks INTEGER NOT NULL CHECK(blocks>=0),facts INTEGER NOT NULL CHECK(facts>=0),
            cursor INTEGER,ceiling INTEGER NOT NULL,schema_version INTEGER NOT NULL,complete INTEGER NOT NULL)""")
        for operation in ("INSERT", "DELETE", "UPDATE"):
            parts = []
            for alias, sign in (("OLD", -1), ("NEW", 1)):
                if (alias == "OLD" and operation == "INSERT") or (alias == "NEW" and operation == "DELETE"):
                    continue
                parts.append(
                    f"UPDATE market_quality_tally SET blocks=blocks+({sign}),facts=facts+({sign})*{alias}.n "
                    f"WHERE id=1 AND (complete=1 OR {alias}.rowid<=cursor OR {alias}.rowid>ceiling);"
                )
            db.execute(
                f"CREATE TRIGGER market_quality_tally_{operation} AFTER {operation} ON market_quality_blocks "
                f"BEGIN {' '.join(parts)} END"
            )
        last = db.execute("SELECT rowid FROM market_quality_blocks ORDER BY rowid DESC LIMIT 1").fetchone()
        db.execute(
            "INSERT INTO market_quality_tally VALUES(1,1,0,0,NULL,?,?,?)",
            (last[0] if last else 0, db.execute("PRAGMA schema_version").fetchone()[0], int(last is None)),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def drain(db: sqlite3.Connection, batch: int, deadline: float) -> bool:
    state = db.execute(
        "SELECT cursor,ceiling,schema_version,complete FROM market_quality_tally WHERE id=1"
    ).fetchone()
    schema = db.execute("PRAGMA schema_version").fetchone()[0]
    if state[2] != schema:
        last = db.execute("SELECT rowid FROM market_quality_blocks ORDER BY rowid DESC LIMIT 1").fetchone()
        upper = last[0] if last else 0
        db.execute(
            "UPDATE market_quality_tally SET blocks=0,facts=0,cursor=NULL,ceiling=?,schema_version=?,complete=? WHERE id=1",
            (upper, schema, int(last is None)),
        )
        state = (None, upper, schema, int(last is None))
    if not state[3]:
        rows = db.execute(
            "SELECT rowid,n FROM market_quality_blocks WHERE rowid"
            + (">=?" if state[0] is None else ">?")
            + " AND rowid<=? ORDER BY rowid LIMIT ?",
            (-(2**63) if state[0] is None else state[0], state[1], batch),
        ).fetchall()
        cursor, blocks, facts = state[0], 0, 0
        for identity, n in rows:
            if blocks and time.monotonic() >= deadline:
                break
            cursor = identity
            blocks += 1
            facts += n
        complete = blocks == len(rows) and (len(rows) < batch or cursor >= state[1])
        db.execute(
            "UPDATE market_quality_tally SET blocks=blocks+?,facts=facts+?,cursor=?,complete=? WHERE id=1",
            (blocks, facts, cursor, int(complete)),
        )
    return read(db) is not None


def read(db: sqlite3.Connection) -> tuple[int, int] | None:
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='market_quality_tally'").fetchone():
        return None
    row = db.execute(
        "SELECT blocks,facts,schema_version,complete FROM market_quality_tally WHERE id=1"
    ).fetchone()
    if (
        row is None
        or not row[3]
        or row[2] != db.execute("PRAGMA schema_version").fetchone()[0]
        or db.execute("SELECT 1 FROM market_quality_legacy LIMIT 1").fetchone()
    ):
        return None
    return row[0], row[1]
