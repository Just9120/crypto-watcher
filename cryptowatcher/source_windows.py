"""Share identical frozen source coordinates, preserving their original SHA/lease."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any


def install(db: sqlite3.Connection) -> None:
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='market_source_windows'").fetchone():
        marker = db.execute("SELECT value FROM market_meta WHERE key='source_windows'").fetchone()
        if marker is None or marker[0] != "1":
            raise ValueError("Unknown source window layout; database preserved")
        return
    if db.in_transaction:
        raise ValueError("Source window installation requires an outer boundary")
    try:
        db.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE market_source_windows(
            id INTEGER PRIMARY KEY,digest BLOB NOT NULL UNIQUE,payload BLOB NOT NULL,expires INTEGER NOT NULL,
            refs INTEGER NOT NULL DEFAULT 0 CHECK(refs>=0));
        CREATE INDEX market_source_windows_expiry ON market_source_windows(expires,id) WHERE refs=0;
        CREATE TRIGGER market_window_insert AFTER INSERT ON market_source_windows BEGIN
            INSERT INTO market_counts VALUES('source_windows',1,length(NEW.payload),length(NEW.payload),0)
                ON CONFLICT(category) DO UPDATE SET records=records+1,
                logical_bytes=logical_bytes+excluded.logical_bytes,inserted_bytes=inserted_bytes+excluded.inserted_bytes;
        END;
        CREATE TRIGGER market_window_delete AFTER DELETE ON market_source_windows BEGIN
            UPDATE market_counts SET records=records-1,logical_bytes=logical_bytes-length(OLD.payload),
                deleted_bytes=deleted_bytes+length(OLD.payload) WHERE category='source_windows';
        END;
        CREATE TRIGGER market_window_retained BEFORE DELETE ON market_source_windows WHEN OLD.refs>0 BEGIN
            SELECT RAISE(ABORT,'Retained source window');
        END;
        CREATE TRIGGER market_window_immutable BEFORE UPDATE OF digest,payload ON market_source_windows
            WHEN NEW.digest<>OLD.digest OR NEW.payload<>OLD.payload BEGIN
            SELECT RAISE(ABORT,'Immutable source window');
        END;
        CREATE TRIGGER market_window_owner_insert AFTER INSERT ON runtime BEGIN
            SELECT CASE WHEN EXISTS(SELECT 1 FROM json_each(cw_window_refs(NEW.key,NEW.payload_json)) r
                LEFT JOIN market_source_windows w ON w.id=r.value WHERE w.id IS NULL)
                THEN RAISE(ABORT,'Missing source window') END;
            UPDATE market_source_windows SET refs=refs+1 WHERE id IN (SELECT value FROM json_each(cw_window_refs(NEW.key,NEW.payload_json)));
        END;
        CREATE TRIGGER market_window_owner_delete AFTER DELETE ON runtime BEGIN
            UPDATE market_source_windows SET refs=refs-1 WHERE id IN (SELECT value FROM json_each(cw_window_refs(OLD.key,OLD.payload_json)));
        END;
        CREATE TRIGGER market_window_owner_update AFTER UPDATE OF key,payload_json ON runtime
            WHEN OLD.payload_json<>NEW.payload_json OR OLD.key<>NEW.key BEGIN
            SELECT CASE WHEN EXISTS(SELECT 1 FROM json_each(cw_window_refs(NEW.key,NEW.payload_json)) r
                LEFT JOIN market_source_windows w ON w.id=r.value WHERE w.id IS NULL)
                THEN RAISE(ABORT,'Missing source window') END;
            UPDATE market_source_windows SET refs=refs-1 WHERE id IN (SELECT value FROM json_each(cw_window_refs(OLD.key,OLD.payload_json)));
            UPDATE market_source_windows SET refs=refs+1 WHERE id IN (SELECT value FROM json_each(cw_window_refs(NEW.key,NEW.payload_json)));
        END;
        INSERT INTO market_meta VALUES('source_windows','1');
        COMMIT;
    """)
    except BaseException:
        db.rollback()
        raise


def intern(db: sqlite3.Connection, reference: dict[str, Any], expires: int) -> dict[str, Any]:
    from .market_storage import compact_reference, encoded, reference_points
    from .storage import encode

    if reference.get("mode") == "source":
        reference = compact_reference(reference["stream"], reference_points(reference), reference["sha256"])
    raw = encode(reference).encode()
    digest = hashlib.sha256(raw).digest()
    row = db.execute(
        "SELECT id,payload,expires FROM market_source_windows WHERE digest=?", (digest,)
    ).fetchone()
    if row is None:
        result = db.execute(
            "INSERT INTO market_source_windows(digest,payload,expires) VALUES(?,?,?)",
            (digest, encoded(reference), expires),
        )
        return {"mode": "source-ref", "id": result.lastrowid}
    if encode(load(db, int(row[0]))).encode() != raw:
        raise ValueError("Conflicting source window identity")
    if expires > row[2]:
        db.execute("UPDATE market_source_windows SET expires=? WHERE id=?", (expires, row[0]))
    return {"mode": "source-ref", "id": row[0]}


def load(db: sqlite3.Connection, row_id: int) -> dict[str, Any]:
    from .market_storage import decoded
    from .storage import encode

    if type(row_id) is not int or row_id < 1:
        raise ValueError("Invalid source window identity")
    row = db.execute("SELECT digest,payload FROM market_source_windows WHERE id=?", (row_id,)).fetchone()
    if row is None:
        raise ValueError("Missing retained source window")
    value = decoded(bytes(row[1]), 4 * 1024 * 1024)
    if (
        not isinstance(value, dict)
        or value.get("mode") not in ("source", "source-runs")
        or hashlib.sha256(encode(value).encode()).digest() != bytes(row[0])
    ):
        raise ValueError("Invalid shared source window")
    return value


def lease_deadline(db: sqlite3.Connection, anchor: int, latest_end: int) -> int:
    settings = json.loads(db.execute("SELECT settings_json FROM owner_settings LIMIT 1").fetchone()[0])
    return (
        anchor + settings.get("detail_retention_days", 30) * 86400000
        if anchor > 0
        else latest_end + settings.get("candle_retention_days", 35) * 86400000
    )


def collect(db: sqlite3.Connection, now: int, batch: int) -> int:
    return db.execute(
        "DELETE FROM market_source_windows WHERE id IN (SELECT id FROM market_source_windows WHERE expires<=? AND refs=0 ORDER BY expires,id LIMIT ?)",
        (now, batch),
    ).rowcount


def references(key: str, payload: Any) -> str:
    from .storage import json_payload

    # Only immutable revisions own these leases. Arbitrary runtime values and
    # shared opaque blobs must remain writable/diagnosable without decoding.
    if not re.fullmatch(r"radar:v1:(?:event|outcome|comparison):[^:]+:[0-9]{10}", key):
        return "[]"
    if isinstance(payload, str) and "source-ref" not in payload:
        return "[]"
    value = json_payload(payload)
    return json.dumps(reference_ids(value))


def reference_ids(value: Any) -> list[int]:
    """Pure extraction shared by legacy records and frozen domain blocks."""
    ids: set[int] = set()

    def visit(item: Any, depth: int = 0) -> None:
        if depth > 32:
            raise ValueError("Source reference nesting exceeds bound")
        if isinstance(item, dict):
            storage = item.get("_history_storage", {})
            if not isinstance(storage, dict) or not isinstance(storage.get("fields", {}), dict):
                raise ValueError("Invalid source storage fields")
            for reference in storage.get("fields", {}).values():
                if not isinstance(reference, dict):
                    raise ValueError("Invalid source storage reference")
                if reference.get("mode") == "source-ref":
                    if (
                        set(reference) != {"mode", "id"}
                        or type(reference["id"]) is not int
                        or reference["id"] < 1
                    ):
                        raise ValueError("Invalid source window reference")
                    ids.add(reference["id"])
            for key, child in item.items():
                if key != "_history_storage":
                    visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)

    visit(value)
    return sorted(ids)
