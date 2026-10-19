"""Immutable exact metric values shared by retained records and numeric summaries."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any

from .metric_codec import canonical, pack, restore_view, unpack


def fingerprint(raw: bytes) -> bytes:
    # This is a lookup/checksum, never an identity supplied by a consumer.
    # Hash collisions are compared using the complete immutable payload.
    return hashlib.sha256(raw).digest()[:8]


def install_functions(db: sqlite3.Connection) -> None:
    def change(raw: str, column: str, delta: int) -> int:
        values = json.loads(raw)
        if not isinstance(values, list) or any(type(v) is not int or not 0 < v < 2**63 for v in values):
            raise ValueError("Invalid numeric lease identities")
        adjust(db, set(values), column, delta, statement_atomic=True)
        return 1

    db.create_function("cw_numeric_adjust", 3, change)

    def valid(raw: str, column: str, minimum: int) -> int:
        if column not in ("detail_refs", "quality_refs") or type(minimum) is not int or minimum not in (0, 1):
            raise ValueError("Invalid numeric validation request")
        try:
            values = json.loads(raw)
            if not isinstance(values, list):
                return 0
            rows = raw_many(db, set(values))
            return int(all((v[1] if column == "detail_refs" else v[2]) >= minimum for v in rows.values()))
        except (ValueError, TypeError):
            return 0

    db.create_function("cw_numeric_valid", 3, valid)


def ownership_triggers(db: sqlite3.Connection, *, replace: bool = False) -> None:
    for event in ("insert", "delete", "update"):
        name = "market_numeric_owner_" + event
        if replace:
            old = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
            if old is None or "cw_numeric_refs" not in old[0]:
                raise ValueError("Unknown numeric owner trigger")
            db.execute(f"DROP TRIGGER {name}")
        statements = []
        for alias, delta in (("OLD", -1), ("NEW", 1)):
            if (event == "insert" and alias == "OLD") or (event == "delete" and alias == "NEW"):
                continue
            statements.append(
                f"SELECT cw_numeric_adjust(cw_numeric_refs({alias}.key,{alias}.payload_json),'detail_refs',{delta});"
            )
        suffix = " OF key,payload_json" if event == "update" else ""
        when = " WHEN OLD.key<>NEW.key OR OLD.payload_json<>NEW.payload_json" if event == "update" else ""
        db.execute(
            f"CREATE TRIGGER {name} AFTER {event.upper()}{suffix} ON runtime{when} BEGIN {' '.join(statements)} END"
        )


def adjust(
    db: sqlite3.Connection, ids: set[int], column: str, delta: int, *, statement_atomic: bool = False
) -> None:
    from contextlib import nullcontext

    from .candle_blocks import atomic
    from .numeric_blocks import adjust as cold_adjust
    from .numeric_blocks import installed

    if column not in ("detail_refs", "quality_refs") or type(delta) is not int or delta not in (-1, 1):
        raise ValueError("Invalid numeric lease operation")
    if not ids:
        return
    # SQLite already rolls back the complete invoking statement (including UDF
    # writes). Opening a SAVEPOINT inside that statement is not allowed.
    with nullcontext() if statement_atomic else atomic(db):
        values = raw_many(db, ids)
        legacy = set()
        for identity, (_raw, details, quality, cold) in values.items():
            count = details if column == "detail_refs" else quality
            if not 0 <= count + delta < 2**63:
                raise ValueError("Numeric lease underflow/overflow")
            if not cold:
                legacy.add(identity)
        if legacy:
            db.executemany(
                f"UPDATE market_numeric SET {column}={column}+? WHERE id=?", [(delta, i) for i in legacy]
            )
        if ids - legacy:
            if not installed(db):
                raise ValueError("Missing numeric block adapter")
            cold_adjust(db, ids - legacy, column, delta)


def raw_many(db: sqlite3.Connection, ids: set[int]) -> dict[int, tuple[bytes, int, int, bool]]:
    return _raw_many(db, ids, leases=True)


def _raw_many(
    db: sqlite3.Connection, ids: set[int], *, leases: bool
) -> dict[int, tuple[bytes, int, int, bool]]:
    if any(type(value) is not int or not 0 < value < 2**63 for value in ids):
        raise ValueError("Invalid numeric metric identity")
    from .numeric_blocks import installed
    from .numeric_blocks import load_many as cold_load
    from .numeric_blocks import values_many as cold_values

    result = {}
    ordered = sorted(ids)
    for offset in range(0, len(ordered), 256):
        group = ordered[offset : offset + 256]
        placeholders = ",".join("?" for _ in group)
        for row in db.execute(
            f"SELECT id,fingerprint,payload,detail_refs,quality_refs FROM market_numeric WHERE id IN({placeholders})",
            group,
        ):
            raw = bytes(row[2])
            if fingerprint(raw) != bytes(row[1]):
                raise ValueError("Invalid numeric metric checksum")
            if any(type(n) is not int or not 0 <= n < 2**63 for n in row[3:5]):
                raise ValueError("Invalid numeric metric lease")
            result[int(row[0])] = raw, int(row[3]), int(row[4]), False
        if installed(db):
            cold = (
                cold_load(db, set(group))
                if leases
                else {i: (raw, 0, 0) for i, raw in cold_values(db, set(group)).items()}
            )
            for identity, (raw, details, quality) in cold.items():
                if identity in result:
                    raise ValueError("Duplicate numeric metric coordinate")
                result[identity] = raw, details, quality, True
    if set(result) != ids:
        raise ValueError("Missing retained numeric metric")
    return result


def install(db: sqlite3.Connection) -> None:
    install_functions(db)
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='market_numeric'").fetchone():
        marker = db.execute("SELECT value FROM market_meta WHERE key='numeric_layout'").fetchone()
        if marker is None or marker[0] != "1":
            raise ValueError("Unknown numeric metric layout; database preserved")
        return
    if db.in_transaction:
        raise ValueError("Numeric pool installation requires an outer boundary")
    try:
        db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE market_numeric(
                id INTEGER PRIMARY KEY AUTOINCREMENT,fingerprint BLOB NOT NULL,payload BLOB NOT NULL,
                detail_refs INTEGER NOT NULL DEFAULT 0 CHECK(detail_refs>=0),
                quality_refs INTEGER NOT NULL DEFAULT 0 CHECK(quality_refs>=0));
            CREATE INDEX market_numeric_lookup ON market_numeric(fingerprint);
            CREATE INDEX market_numeric_unused ON market_numeric(id) WHERE detail_refs=0 AND quality_refs=0;
            CREATE TRIGGER market_numeric_immutable BEFORE UPDATE OF fingerprint,payload ON market_numeric
                WHEN NEW.fingerprint<>OLD.fingerprint OR NEW.payload<>OLD.payload BEGIN
                SELECT RAISE(ABORT,'Immutable numeric metric');
            END;
            CREATE TRIGGER market_numeric_retained BEFORE DELETE ON market_numeric
                WHEN OLD.detail_refs>0 OR OLD.quality_refs>0 BEGIN
                SELECT RAISE(ABORT,'Retained numeric metric');
            END;
            CREATE TRIGGER market_numeric_insert AFTER INSERT ON market_numeric BEGIN
                INSERT INTO market_counts VALUES('numeric_details',1,length(NEW.payload),length(NEW.payload),0)
                    ON CONFLICT(category) DO UPDATE SET records=records+1,
                    logical_bytes=logical_bytes+excluded.logical_bytes,inserted_bytes=inserted_bytes+excluded.inserted_bytes;
            END;
            CREATE TRIGGER market_numeric_delete AFTER DELETE ON market_numeric BEGIN
                UPDATE market_counts SET records=records-1,logical_bytes=logical_bytes-length(OLD.payload),
                    deleted_bytes=deleted_bytes+length(OLD.payload)
                    WHERE category=CASE WHEN OLD.quality_refs>0 THEN 'quality_numeric' ELSE 'numeric_details' END;
            END;
            CREATE TRIGGER market_numeric_archive AFTER UPDATE OF quality_refs ON market_numeric
                WHEN (OLD.quality_refs=0)<>(NEW.quality_refs=0) BEGIN
                UPDATE market_counts SET records=records-1,logical_bytes=logical_bytes-length(OLD.payload),
                    deleted_bytes=deleted_bytes+length(OLD.payload)
                    WHERE category=CASE WHEN OLD.quality_refs>0 THEN 'quality_numeric' ELSE 'numeric_details' END;
                INSERT INTO market_counts
                    VALUES(CASE WHEN NEW.quality_refs>0 THEN 'quality_numeric' ELSE 'numeric_details' END,
                        1,length(NEW.payload),length(NEW.payload),0)
                    ON CONFLICT(category) DO UPDATE SET records=records+1,
                    logical_bytes=logical_bytes+excluded.logical_bytes,inserted_bytes=inserted_bytes+excluded.inserted_bytes;
            END;
            CREATE TRIGGER market_numeric_owner_insert AFTER INSERT ON runtime BEGIN
                SELECT CASE WHEN EXISTS(SELECT 1 FROM json_each(cw_numeric_refs(NEW.key,NEW.payload_json)) r
                    LEFT JOIN market_numeric m ON m.id=r.value WHERE m.id IS NULL)
                    THEN RAISE(ABORT,'Missing numeric metric') END;
                UPDATE market_numeric SET detail_refs=detail_refs+1
                    WHERE id IN(SELECT value FROM json_each(cw_numeric_refs(NEW.key,NEW.payload_json)));
            END;
            CREATE TRIGGER market_numeric_owner_delete AFTER DELETE ON runtime BEGIN
                UPDATE market_numeric SET detail_refs=detail_refs-1
                    WHERE id IN(SELECT value FROM json_each(cw_numeric_refs(OLD.key,OLD.payload_json)));
            END;
            CREATE TRIGGER market_numeric_owner_update AFTER UPDATE OF key,payload_json ON runtime
                WHEN OLD.key<>NEW.key OR OLD.payload_json<>NEW.payload_json BEGIN
                SELECT CASE WHEN EXISTS(SELECT 1 FROM json_each(cw_numeric_refs(NEW.key,NEW.payload_json)) r
                    LEFT JOIN market_numeric m ON m.id=r.value WHERE m.id IS NULL)
                    THEN RAISE(ABORT,'Missing numeric metric') END;
                UPDATE market_numeric SET detail_refs=detail_refs-1 WHERE id IN(
                    SELECT value FROM json_each(cw_numeric_refs(OLD.key,OLD.payload_json)));
                UPDATE market_numeric SET detail_refs=detail_refs+1 WHERE id IN(
                    SELECT value FROM json_each(cw_numeric_refs(NEW.key,NEW.payload_json)));
            END;
            INSERT INTO market_meta VALUES('numeric_layout','1');
            COMMIT;
        """)
    except BaseException:
        db.rollback()
        raise


def intern(db: sqlite3.Connection, value: Any) -> int | None:
    from .storage import encode

    raw = pack(value)
    if raw is None:
        return None
    if encode(unpack(raw)) != encode(value):
        raise ValueError("Numeric metric codec changed public values")
    signature = fingerprint(raw)
    for row in db.execute("SELECT id,payload FROM market_numeric WHERE fingerprint=?", (signature,)):
        if bytes(row[1]) == raw:
            return int(row[0])
    from . import numeric_blocks

    if numeric_blocks.installed(db):
        found = numeric_blocks.find(db, raw)
        if found:
            return found[0]
    if numeric_blocks.writing(db):
        return numeric_blocks.intern(db, raw)
    result = db.execute("INSERT INTO market_numeric(fingerprint,payload) VALUES(?,?)", (signature, raw))
    return int(result.lastrowid or 0)


def load_many(db: sqlite3.Connection, ids: set[int]) -> dict[int, Any]:
    return {identity: unpack(value[0]) for identity, value in _raw_many(db, ids, leases=False).items()}


def load(db: sqlite3.Connection, identity: int) -> dict[str, Any]:
    return load_many(db, {identity})[identity]


def reference_id(reference: dict[str, Any]) -> int:
    if not isinstance(reference, dict) or set(reference) != {"mode", "id"}:
        raise ValueError("Invalid numeric metric reference")
    identity = reference["id"]
    mode = reference["mode"]
    if mode not in ("numeric", "numeric2") or type(identity) is not int:
        raise ValueError("Invalid numeric metric reference")
    if mode == "numeric2":
        if not 4 <= identity < 2**65:
            raise ValueError("Invalid numeric metric view coordinate")
        identity //= 4
    if not 0 < identity < 2**63:
        raise ValueError("Invalid numeric metric identity")
    return identity


def reference_value(reference: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    reference_id(reference)
    return restore_view(body, reference["id"] % 4) if reference["mode"] == "numeric2" else dict(body)


def encode_reference(db: sqlite3.Connection, value: Any) -> dict[str, Any] | None:
    candidate = canonical(value)
    if candidate is None:
        return None
    body, view = candidate
    identity = intern(db, body)
    if identity is None:
        raise ValueError("Canonical metric has no exact frame")
    return {"mode": "numeric2", "id": identity * 4 + view}


def existing_reference(db: sqlite3.Connection, value: Any) -> tuple[dict[str, Any], int, int] | None:
    """Find an exact view without creating a body for a unique archived value."""
    candidate = canonical(value)
    if candidate is None:
        return None
    body, view = candidate
    raw = pack(body)
    if raw is None:
        raise ValueError("Canonical metric has no exact frame")
    for identity, payload, details, quality in db.execute(
        "SELECT id,payload,detail_refs,quality_refs FROM market_numeric WHERE fingerprint=?",
        (fingerprint(raw),),
    ):
        if bytes(payload) == raw:
            return {"mode": "numeric2", "id": int(identity) * 4 + view}, int(details), int(quality)
    from .numeric_blocks import find, installed

    if installed(db):
        found = find(db, raw)
        if found:
            identity, details, quality = found
            return {"mode": "numeric2", "id": identity * 4 + view}, details, quality
    return None


def resolve(db: sqlite3.Connection, reference: dict[str, Any]) -> dict[str, Any]:
    return reference_value(reference, load(db, reference_id(reference)))


def collect(db: sqlite3.Connection, batch: int = 128) -> int:
    from .candle_blocks import atomic
    from .numeric_blocks import collect as cold_collect
    from .numeric_blocks import installed

    if type(batch) is not int or not 0 < batch <= 256:
        raise ValueError("Invalid numeric collection batch")
    with atomic(db):
        count = db.execute(
            "DELETE FROM market_numeric WHERE id IN(SELECT id FROM market_numeric WHERE detail_refs=0 AND quality_refs=0 ORDER BY id LIMIT ?)",
            (batch,),
        ).rowcount
        if count < batch and installed(db):
            count += cold_collect(db, batch - count)
        return count


def metric_ids(value: Any, depth: int = 0, *, omit_fields: frozenset[str] = frozenset()) -> set[int]:
    result: set[int] = set()

    def visit(item: Any, level: int) -> None:
        if level > 64:
            raise ValueError("Numeric metric reference nesting exceeds bound")
        if isinstance(item, dict):
            storage = item.get("_history_storage", {})
            if not isinstance(storage, dict) or not isinstance(storage.get("fields", {}), dict):
                raise ValueError("Invalid numeric storage fields")
            for name, ref in storage.get("fields", {}).items():
                if name in omit_fields:
                    continue
                if isinstance(ref, dict) and ref.get("mode") in ("numeric", "numeric2"):
                    if (
                        set(storage) != {"schema", "fields"}
                        or storage.get("schema") != "cryptowatcher.shared-fields.v1"
                    ):
                        raise ValueError("Invalid numeric storage schema")
                    if name != "metrics":
                        raise ValueError("Invalid numeric metric reference")
                    result.add(reference_id(ref))
            for key, child in item.items():
                if key != "_history_storage" and key not in omit_fields and isinstance(child, (dict, list)):
                    visit(child, level + 1)
        elif isinstance(item, list):
            for child in item:
                if isinstance(child, (dict, list)):
                    visit(child, level + 1)

    visit(value, depth)
    return result


def references(key: str, payload: Any) -> str:
    from .storage import json_payload

    if not re.fullmatch(r"radar:v1:(?:event|outcome|comparison):[^:]+:[0-9]{10}", key):
        return "[]"
    if isinstance(payload, str) and '"numeric"' not in payload and '"numeric2"' not in payload:
        return "[]"
    return json.dumps(sorted(metric_ids(json_payload(payload))))


def projection(db: sqlite3.Connection, value: Any) -> Any:
    """SQL metrics only: other frozen/shared source fields remain unexpanded."""
    if isinstance(value, dict) and value.get("schema") == "cryptowatcher.history-head.v1":
        raise ValueError("Numeric query requires the retained immutable revision")
    values = load_many(db, metric_ids(value))
    if not values:
        return value

    def visit(item: Any) -> Any:
        if isinstance(item, list):
            return [visit(v) for v in item]
        if not isinstance(item, dict):
            return item
        result = {
            k: visit(v) if isinstance(v, (dict, list)) else v
            for k, v in item.items()
            if k != "_history_storage"
        }
        if "_history_storage" in item:
            storage = item["_history_storage"]
            fields = dict(storage["fields"])
            for name, ref in list(fields.items()):
                if isinstance(ref, dict) and ref.get("mode") in ("numeric", "numeric2"):
                    if name in result:
                        raise ValueError("Conflicting numeric metric projection")
                    result[name] = reference_value(ref, values[reference_id(ref)])
                    del fields[name]
            if fields:
                result["_history_storage"] = {**storage, "fields": fields}
        return result

    return visit(value)
