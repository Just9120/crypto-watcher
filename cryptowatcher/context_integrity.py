"""Freeze one full SHA256 per referenced context row instead of per use.

Raw unreferenced rows need no conversion. Old combined-hash references remain
readable; converting one validates its old hash before freezing each exact part.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from .storage import encode


def install(db: sqlite3.Connection) -> None:
    marker = db.execute("SELECT value FROM market_meta WHERE key='context_integrity'").fetchone()
    column = "integrity" in {row[1] for row in db.execute("PRAGMA table_info(market_context)")}
    if marker is not None:
        if marker[0] != "1" or not column:
            raise ValueError("Unknown context integrity layout; database preserved")
        return
    if column or db.in_transaction:
        raise ValueError("Context integrity installation requires a known outer boundary")
    try:
        db.executescript("""
            BEGIN IMMEDIATE;
            ALTER TABLE market_context ADD COLUMN integrity BLOB
                CHECK(integrity IS NULL OR length(integrity)=32);
            CREATE TRIGGER market_context_frozen BEFORE UPDATE OF stream,kind,at,body,timing ON market_context
                WHEN OLD.integrity IS NOT NULL AND (NEW.stream<>OLD.stream OR NEW.kind<>OLD.kind OR
                    NEW.at<>OLD.at OR NEW.body<>OLD.body OR NEW.timing<>OLD.timing) BEGIN
                SELECT RAISE(ABORT,'Immutable referenced context');
            END;
            CREATE TRIGGER market_context_hash_frozen BEFORE UPDATE OF integrity ON market_context
                WHEN OLD.integrity IS NOT NULL AND NEW.integrity IS NOT OLD.integrity BEGIN
                SELECT RAISE(ABORT,'Immutable context integrity');
            END;
            CREATE TRIGGER market_context_hash_insert AFTER INSERT ON market_context
                WHEN NEW.integrity IS NOT NULL BEGIN
                INSERT INTO market_counts VALUES('context_integrity',1,32,32,0)
                    ON CONFLICT(category) DO UPDATE SET records=records+1,
                    logical_bytes=logical_bytes+32,inserted_bytes=inserted_bytes+32;
            END;
            CREATE TRIGGER market_context_hash_bind AFTER UPDATE OF integrity ON market_context
                WHEN OLD.integrity IS NULL AND NEW.integrity IS NOT NULL BEGIN
                INSERT INTO market_counts VALUES('context_integrity',1,32,32,0)
                    ON CONFLICT(category) DO UPDATE SET records=records+1,
                    logical_bytes=logical_bytes+32,inserted_bytes=inserted_bytes+32;
            END;
            CREATE TRIGGER market_context_hash_delete AFTER DELETE ON market_context
                WHEN OLD.integrity IS NOT NULL BEGIN
                UPDATE market_counts SET records=records-1,logical_bytes=logical_bytes-32,
                    deleted_bytes=deleted_bytes+32 WHERE category='context_integrity';
            END;
            INSERT INTO market_meta VALUES('context_integrity','1');
            COMMIT;
        """)
    except BaseException:
        db.rollback()
        raise


def part(db: sqlite3.Connection, kind: str, point: Any, *, bind: bool = False) -> dict[str, Any]:
    from .market_storage import context_public

    if (
        kind not in ("ticker", "instrument")
        or not isinstance(point, list)
        or len(point) != 2
        or type(point[0]) is not int
        or not 0 < point[0] < 2**63
        or type(point[1]) is not int
        or not -(2**63) <= point[1] < 2**63
    ):
        raise ValueError("Invalid frozen context coordinate")
    key = (point[0], kind, point[1])
    row = db.execute(
        "SELECT body,timing,integrity FROM market_context WHERE stream=? AND kind=? AND at=?", key
    ).fetchone()
    if row is None:
        raise ValueError("Missing retained context")
    value = context_public(db, row)
    digest = hashlib.sha256(encode(value).encode()).digest()
    if row[2] is None:
        if not bind:
            raise ValueError("Missing frozen context integrity")
        db.execute(
            "UPDATE market_context SET integrity=? WHERE stream=? AND kind=? AND at=? AND integrity IS NULL",
            (digest, *key),
        )
    elif bytes(row[2]) != digest:
        raise ValueError("Frozen context hash mismatch")
    return value


def convert(db: sqlite3.Connection, reference: dict[str, Any]) -> dict[str, Any]:
    from .market_storage import restore_context

    # Do not trust existing coordinates just because their rows are addressable.
    # Validate the original combined public bytes before admitting any digest.
    original = restore_context(db, reference)
    points = {
        kind: [point[0], point[2]] if point is not None else None
        for kind, point in reference["points"].items()
    }
    return freeze(db, points, original)


def freeze(db: sqlite3.Connection, points: dict[str, Any], expected: bytes) -> dict[str, Any]:
    if not isinstance(points, dict) or set(points) - {"ticker", "instrument"}:
        raise ValueError("Invalid frozen context fields")
    values = {
        kind: part(db, kind, point, bind=True) if point is not None else None
        for kind, point in points.items()
    }
    if encode(values).encode() != expected:
        raise ValueError("Frozen context conversion changed public bytes")
    return {"mode": "contexts2", "points": points}


def restore(db: sqlite3.Connection, reference: dict[str, Any]) -> bytes:
    if (
        set(reference) != {"mode", "points"}
        or reference["mode"] != "contexts2"
        or not isinstance(reference["points"], dict)
        or set(reference["points"]) - {"ticker", "instrument"}
    ):
        raise ValueError("Invalid frozen context reference")
    return encode(
        {
            kind: part(db, kind, point) if point is not None else None
            for kind, point in reference["points"].items()
        }
    ).encode()
