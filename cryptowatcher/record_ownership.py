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
    bodies = record_blocks.bodies(raw)
    if not isinstance(bodies, list) or len(bodies) != len(projections):
        raise ValueError("Invalid owned block cardinality")
    result = []
    for (coordinate, _), body in zip(projections, bodies, strict=True):
        value = record_blocks.expanded(coordinate, body)
        numeric = sorted(numeric_pool.metric_ids(value))
        windows = source_windows.reference_ids(value)
        event = value.get("event_id")
        result.append([coordinate.kind, coordinate.identity, coordinate.revision, numeric, windows, event])
    return result


def block_entries(db: sqlite3.Connection, packet: int, cache: Any = None) -> list[Any]:
    # Actual bytes are always read and authenticated before a pure cache hit.
    raw = packet_store.read(db, packet)

    def read() -> str:
        return encode(block_references(raw))

    values = json.loads(read() if cache is None else cache("catalogue-refs", "", raw, read))
    if (
        not isinstance(values, list)
        or not 0 < len(values) <= 64
        or any(not isinstance(value, list) or len(value) != 6 for value in values)
    ):
        raise ValueError("Catalogue ownership coordinate mismatch")
    return values


def block_entry(db: sqlite3.Connection, packet: int, slot: int, cache: Any = None) -> list[Any]:
    if type(slot) is not int or not 0 <= slot < 64:
        raise ValueError("Invalid catalogue ownership slot")
    values = block_entries(db, packet, cache)
    if slot >= len(values):
        raise ValueError("Catalogue ownership coordinate mismatch")
    return values[slot]


def install_functions(db: sqlite3.Connection, cache: Any = None, binary_cache: Any = None) -> None:
    from .catalogue_overlap import install_functions as install_overlap
    from .record_masks import install_functions as install_masks

    packet_store.install_functions(db)
    install_masks(db, cache, binary_cache)

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
        value = block_entry(db, packet, slot, cache)
        coordinate = [record_blocks.KINDS[kind], str(uuid.UUID(bytes=identity)), revision]
        if value[:3] != coordinate:
            raise ValueError("Catalogue ownership coordinate mismatch")
        return json.dumps(value[3 if which == "numeric" else 4])

    db.create_function("cw_catalogue_refs", 6, references)
    db.create_function("cw_catalogue_key", 3, key, deterministic=True)
    db.create_function("cw_catalogue_record", 5, record)
    install_overlap(db)


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
                SELECT cw_numeric_adjust(cw_catalogue_refs(NEW.kind,NEW.public_uuid,NEW.revision,NEW.packet,NEW.slot,'numeric'),'detail_refs',1);
                SELECT CASE WHEN EXISTS(SELECT 1 FROM json_each(cw_catalogue_refs(NEW.kind,NEW.public_uuid,NEW.revision,NEW.packet,NEW.slot,'windows')) r
                    LEFT JOIN market_source_windows w ON w.id=r.value WHERE w.id IS NULL)
                    THEN RAISE(ABORT,'Missing catalogue dependency') END;
                UPDATE market_source_windows SET refs=refs+1 WHERE id IN(SELECT value FROM json_each(cw_catalogue_refs(NEW.kind,NEW.public_uuid,NEW.revision,NEW.packet,NEW.slot,'windows')));
            END;
            CREATE TRIGGER market_catalogue_release BEFORE DELETE ON market_records BEGIN
                SELECT CASE WHEN cw_numeric_valid(cw_catalogue_refs(OLD.kind,OLD.public_uuid,OLD.revision,OLD.packet,OLD.slot,'numeric'),'detail_refs',1)=0 THEN RAISE(ABORT,'Missing catalogue dependency counter') END;
                SELECT cw_numeric_adjust(cw_catalogue_refs(OLD.kind,OLD.public_uuid,OLD.revision,OLD.packet,OLD.slot,'numeric'),'detail_refs',-1);
                SELECT CASE WHEN EXISTS(SELECT 1 FROM json_each(cw_catalogue_refs(OLD.kind,OLD.public_uuid,OLD.revision,OLD.packet,OLD.slot,'windows')) r
                    LEFT JOIN market_source_windows w ON w.id=r.value WHERE w.id IS NULL OR w.refs<1)
                    THEN RAISE(ABORT,'Missing catalogue dependency counter') END;
                UPDATE market_source_windows SET refs=refs-1 WHERE id IN(SELECT value FROM json_each(cw_catalogue_refs(OLD.kind,OLD.public_uuid,OLD.revision,OLD.packet,OLD.slot,'windows')));
            END;
            INSERT INTO market_meta VALUES('record_ownership','1');
            COMMIT;
        """
        )
    except BaseException:
        db.rollback()
        raise


def numeric_trigger_upgrade(db: sqlite3.Connection) -> None:
    if db.execute("SELECT value FROM market_meta WHERE key='record_ownership'").fetchone() is None:
        return
    for name, alias, delta in (
        ("market_catalogue_retain", "NEW", 1),
        ("market_catalogue_release", "OLD", -1),
    ):
        row = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if row is None or "cw_catalogue_refs" not in row[0]:
            raise ValueError("Unknown catalogue numeric ownership")
        if "cw_numeric_adjust" in row[0]:
            continue
        counter = " OR n.detail_refs<1" if delta == -1 else ""
        old = (
            f"SELECT CASE WHEN EXISTS(SELECT 1 FROM json_each(cw_catalogue_refs({alias}.kind,{alias}.public_uuid,{alias}.revision,{alias}.packet,{alias}.slot,'numeric')) r\n"
            f"                    LEFT JOIN market_numeric n ON n.id=r.value WHERE n.id IS NULL{counter})\n"
            "                    OR EXISTS("
        )
        adjust = (
            (
                f"SELECT CASE WHEN cw_numeric_valid(cw_catalogue_refs({alias}.kind,{alias}.public_uuid,{alias}.revision,{alias}.packet,{alias}.slot,'numeric'),'detail_refs',1)=0 THEN RAISE(ABORT,'Missing catalogue dependency counter') END;\n                "
                if delta == -1
                else ""
            )
            + f"SELECT cw_numeric_adjust(cw_catalogue_refs({alias}.kind,{alias}.public_uuid,{alias}.revision,{alias}.packet,{alias}.slot,'numeric'),'detail_refs',{delta});\n"
            "                SELECT CASE WHEN EXISTS("
        )
        update = f"UPDATE market_numeric SET detail_refs=detail_refs{'-1' if delta == -1 else '+1'} WHERE id IN(SELECT value FROM json_each(cw_catalogue_refs({alias}.kind,{alias}.public_uuid,{alias}.revision,{alias}.packet,{alias}.slot,'numeric')));"
        if old not in row[0] or update not in row[0]:
            raise ValueError("Unsupported catalogue numeric trigger")
        sql = row[0].replace(old, adjust).replace(update, "")
        db.execute(f"DROP TRIGGER {name}")
        db.execute(sql)


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
