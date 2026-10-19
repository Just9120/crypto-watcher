"""Catalogue leases: per logical revision for numeric/windows, per frozen block
for shared JSON bodies. All publication/deletion remains in one transaction.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from . import numeric_pool, packet_store, record_blocks, source_windows
from .storage import encode


def block_references(raw: bytes) -> list[Any]:
    projections = record_blocks.projections(raw)
    bodies = (
        record_blocks.bodies(raw)
        if any(c.kind in ("event", "outcome", "comparison") for c, _ in projections)
        else [None] * len(projections)
    )
    if not isinstance(bodies, list) or len(bodies) != len(projections):
        raise ValueError("Invalid owned block cardinality")
    result = []
    for (coordinate, _), body in zip(projections, bodies, strict=True):
        numeric: list[int] = []
        windows: list[int] = []
        if coordinate.kind in ("event", "outcome", "comparison"):
            value = record_blocks.expanded(coordinate, body)
            numeric = sorted(numeric_pool.metric_ids(value))
            windows = source_windows.reference_ids(value)
        result.append([coordinate.kind, coordinate.identity, coordinate.revision, numeric, windows])
    return result


def install_functions(db: sqlite3.Connection, cache: Any = None, binary_cache: Any = None) -> None:
    packet_store.install_functions(db)

    def key(kind: int, identity: bytes, revision: int) -> str:
        if type(kind) is not int or not 0 <= kind < len(record_blocks.KINDS):
            raise ValueError("Invalid catalogue key kind")
        coordinate = record_blocks.Coordinate(
            record_blocks.KINDS[kind], str(uuid.UUID(bytes=identity)), revision
        )
        coordinate.validate()
        return "radar:v1:" + coordinate.key

    def record(kind: int, identity: bytes, revision: int, packet: int, slot: int) -> str:
        full_key = key(kind, identity, revision)
        coordinate = record_blocks.Coordinate(
            record_blocks.KINDS[kind], str(uuid.UUID(bytes=identity)), revision
        )
        raw = packet_store.read(db, packet)

        def read() -> str:
            return encode(record_blocks.read(raw, slot, coordinate, binary_cache))

        return read() if cache is None else str(cache("catalogue-record", f"{full_key}/{slot}", raw, read))

    def references(kind: int, identity: bytes, revision: int, packet: int, slot: int, which: str) -> str:
        if (
            type(kind) is not int
            or not 0 <= kind < len(record_blocks.KINDS)
            or which not in ("numeric", "windows")
        ):
            raise ValueError("Invalid owned record kind")
        # Actual bytes/checksum are read even on a pure-transform cache hit.
        raw = packet_store.read(db, packet)

        def read() -> str:
            return encode(block_references(raw))

        values = json.loads(read() if cache is None else cache("catalogue-refs", "", raw, read))
        coordinate = [record_blocks.KINDS[kind], str(uuid.UUID(bytes=identity)), revision]
        if type(slot) is not int or not 0 <= slot < len(values) or values[slot][:3] != coordinate:
            raise ValueError("Catalogue ownership coordinate mismatch")
        return json.dumps(values[slot][3 if which == "numeric" else 4])

    db.create_function("cw_catalogue_refs", 6, references)
    db.create_function("cw_catalogue_key", 3, key, deterministic=True)
    db.create_function("cw_catalogue_record", 5, record)


def install(db: sqlite3.Connection, cache: Any = None, binary_cache: Any = None) -> None:
    install_functions(db, cache, binary_cache)
    marker = db.execute("SELECT value FROM market_meta WHERE key='record_ownership'").fetchone()
    if marker is not None:
        if marker[0] != "1":
            raise ValueError("Unknown catalogue ownership; database preserved")
        return
    if db.in_transaction or db.execute("SELECT 1 FROM market_records LIMIT 1").fetchone():
        raise ValueError("Initial catalogue ownership requires an empty outer boundary")
    old = db.execute("SELECT sql FROM sqlite_master WHERE name='market_reference_delete'").fetchone()
    match = "DELETE FROM market_body_ids WHERE id=OLD.body AND NOT EXISTS("
    if old is None or old[0].count(match) != 1:
        raise ValueError("Unsupported shared-body owner layout")
    replacement = old[0].replace(
        match,
        "DELETE FROM market_body_ids WHERE id=OLD.body AND NOT EXISTS(SELECT 1 FROM market_record_bodies WHERE body=OLD.body) AND NOT EXISTS(",
    )
    prefix = (
        """BEGIN IMMEDIATE;
        CREATE TABLE market_record_bodies(
            packet INTEGER NOT NULL REFERENCES market_packets(id) ON DELETE CASCADE,
            body INTEGER NOT NULL REFERENCES market_body_ids(id),PRIMARY KEY(packet,body)) WITHOUT ROWID;
        CREATE INDEX market_record_body_reverse ON market_record_bodies(body,packet);
        DROP TRIGGER market_reference_delete;
    """
        + replacement
        + ";"
    )
    try:
        db.executescript(
            prefix
            + """
            CREATE TRIGGER market_catalogue_body_admit BEFORE INSERT ON market_record_bodies BEGIN
                SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM market_body_ids i JOIN market_body_counts c
                    ON c.body=i.external JOIN runtime r ON r.key='historyblob:v1:'||i.external
                    WHERE i.id=NEW.body AND c.refs>=0)
                    THEN RAISE(ABORT,'Missing retained catalogue body') END;
            END;
            CREATE TRIGGER market_catalogue_body_insert AFTER INSERT ON market_record_bodies BEGIN
                UPDATE market_body_counts SET refs=refs+1 WHERE body=(SELECT external FROM market_body_ids WHERE id=NEW.body);
                INSERT INTO market_counts VALUES('record_references',1,16,16,0)
                    ON CONFLICT(category) DO UPDATE SET records=records+1,logical_bytes=logical_bytes+16,inserted_bytes=inserted_bytes+16;
            END;
            CREATE TRIGGER market_catalogue_body_guard BEFORE DELETE ON market_record_bodies BEGIN
                SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM market_body_counts WHERE body=(SELECT external FROM market_body_ids WHERE id=OLD.body) AND refs>0)
                    OR NOT EXISTS(SELECT 1 FROM market_counts WHERE category='record_references' AND records>0 AND logical_bytes>=16)
                    THEN RAISE(ABORT,'Missing catalogue shared-body counter') END;
            END;
            CREATE TRIGGER market_catalogue_body_delete AFTER DELETE ON market_record_bodies BEGIN
                UPDATE market_body_counts SET refs=refs-1 WHERE body=(SELECT external FROM market_body_ids WHERE id=OLD.body);
                UPDATE market_counts SET records=records-1,logical_bytes=logical_bytes-16,deleted_bytes=deleted_bytes+16 WHERE category='record_references';
                DELETE FROM market_body_ids WHERE id=OLD.body AND NOT EXISTS(SELECT 1 FROM market_references WHERE body=OLD.body)
                    AND NOT EXISTS(SELECT 1 FROM market_record_bodies WHERE body=OLD.body);
            END;
            CREATE TRIGGER market_catalogue_retain AFTER INSERT ON market_records BEGIN
                SELECT CASE WHEN EXISTS(SELECT 1 FROM json_each(cw_catalogue_refs(NEW.kind,NEW.public_uuid,NEW.revision,NEW.packet,NEW.slot,'numeric')) r
                    LEFT JOIN market_numeric n ON n.id=r.value WHERE n.id IS NULL)
                    OR EXISTS(SELECT 1 FROM json_each(cw_catalogue_refs(NEW.kind,NEW.public_uuid,NEW.revision,NEW.packet,NEW.slot,'windows')) r
                    LEFT JOIN market_source_windows w ON w.id=r.value WHERE w.id IS NULL)
                    THEN RAISE(ABORT,'Missing catalogue dependency') END;
                UPDATE market_numeric SET detail_refs=detail_refs+1 WHERE id IN(SELECT value FROM json_each(cw_catalogue_refs(NEW.kind,NEW.public_uuid,NEW.revision,NEW.packet,NEW.slot,'numeric')));
                UPDATE market_source_windows SET refs=refs+1 WHERE id IN(SELECT value FROM json_each(cw_catalogue_refs(NEW.kind,NEW.public_uuid,NEW.revision,NEW.packet,NEW.slot,'windows')));
            END;
            CREATE TRIGGER market_catalogue_release BEFORE DELETE ON market_records BEGIN
                SELECT CASE WHEN EXISTS(SELECT 1 FROM json_each(cw_catalogue_refs(OLD.kind,OLD.public_uuid,OLD.revision,OLD.packet,OLD.slot,'numeric')) r
                    LEFT JOIN market_numeric n ON n.id=r.value WHERE n.id IS NULL OR n.detail_refs<1)
                    OR EXISTS(SELECT 1 FROM json_each(cw_catalogue_refs(OLD.kind,OLD.public_uuid,OLD.revision,OLD.packet,OLD.slot,'windows')) r
                    LEFT JOIN market_source_windows w ON w.id=r.value WHERE w.id IS NULL OR w.refs<1)
                    THEN RAISE(ABORT,'Missing catalogue dependency counter') END;
                UPDATE market_numeric SET detail_refs=detail_refs-1 WHERE id IN(SELECT value FROM json_each(cw_catalogue_refs(OLD.kind,OLD.public_uuid,OLD.revision,OLD.packet,OLD.slot,'numeric')));
                UPDATE market_source_windows SET refs=refs-1 WHERE id IN(SELECT value FROM json_each(cw_catalogue_refs(OLD.kind,OLD.public_uuid,OLD.revision,OLD.packet,OLD.slot,'windows')));
            END;
            INSERT INTO market_meta VALUES('record_ownership','1');
            COMMIT;
        """
        )
    except BaseException:
        db.rollback()
        raise


def shared_bodies(value: Any, depth: int = 0) -> set[str]:
    if depth > 64:
        raise ValueError("Shared catalogue body nesting bound")
    result: set[str] = set()
    if isinstance(value, list):
        for item in value:
            result.update(shared_bodies(item, depth + 1))
    elif isinstance(value, dict):
        for reference in value.get("_history_storage", {}).get("fields", {}).values():
            if reference.get("mode") == "value2":
                result.add(reference["id"])
            elif reference.get("mode") in ("value", "window"):
                result.update(reference["ids"])
        for key, child in value.items():
            if key != "_history_storage":
                result.update(shared_bodies(child, depth + 1))
    return result


def retain_bodies(db: sqlite3.Connection, packet: int, values: list[dict[str, Any]]) -> None:
    keys = set().union(*(shared_bodies(v) for v in values))
    for key in keys:
        db.execute("INSERT OR IGNORE INTO market_body_ids(external) VALUES(?)", (key,))
        db.execute(
            "INSERT INTO market_record_bodies SELECT ?,id FROM market_body_ids WHERE external=?",
            (packet, key),
        )
