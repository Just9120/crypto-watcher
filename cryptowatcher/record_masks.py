"""Checked per-packet owner slots; every mutation belongs to its outer transaction."""

from __future__ import annotations

import sqlite3
import uuid
from collections import defaultdict
from typing import Any

from . import owner_slots, packet_store, record_blocks


def tallies(db: sqlite3.Connection, category: str) -> dict[str, int]:
    values = dict(db.execute("SELECT name,n FROM market_catalogue_index_counts ORDER BY name LIMIT 5"))
    if set(values) != {"parents", "owners", "history", "membership"} or any(
        type(n) is not int or not 0 <= n < 2**63 for n in values.values()
    ):
        raise ValueError("Missing catalogue index counter")
    if category == "record_parents":
        count, size = values["parents"], 41 * values["parents"]
    elif category == "record_owners":
        count, size = values["owners"], 56 * values["owners"]
    elif category == "record_aliases":
        count = values["history"] + values["membership"]
        size = 24 * values["history"] + 40 * values["membership"]
    else:
        raise ValueError("Unknown catalogue index category")
    row = db.execute(
        "SELECT records,logical_bytes FROM market_counts WHERE category=?", (category,)
    ).fetchone()
    if (
        count >= 2**63
        or size >= 2**63
        or (row is None and (count or size))
        or (row is not None and (any(type(n) is not int for n in row) or tuple(row) != (count, size)))
    ):
        raise ValueError("Missing catalogue index byte counter")
    return values


def packet_masks(db: sqlite3.Connection, packet: int) -> list[tuple[int, bytes]]:
    if type(packet) is not int or not 0 < packet < 2**63:
        raise ValueError("Invalid owner packet")
    rows = db.execute(
        "SELECT parent,slots,checksum FROM market_record_owners WHERE packet=? ORDER BY parent LIMIT 65",
        (packet,),
    ).fetchall()
    if len(rows) > 64:
        raise ValueError("Too many packet owners")
    used = 0
    result = []
    for parent, slots, checksum in rows:
        if owner_slots.signature(parent, packet, slots) != checksum:
            raise ValueError("Corrupt record owner mask")
        mask = owner_slots.bits(slots)
        if not mask or used & mask:
            raise ValueError("Empty or overlapping record owner mask")
        used |= mask
        result.append((parent, slots))
    return result


def owner(db: sqlite3.Connection, packet: int, slot: int, expires: int | None) -> int | None:
    tallies(db, "record_owners")
    tallies(db, "record_parents")
    owner_slots.slot_number(slot)
    selected = next(
        (parent for parent, slots in packet_masks(db, packet) if owner_slots.has(slots, slot)), None
    )
    if (selected is None) == (expires is None) or (expires is not None and type(expires) is not int):
        raise ValueError("Missing or inconsistent record owner")
    if (
        selected is not None
        and db.execute("SELECT 1 FROM market_record_parents WHERE id=?", (selected,)).fetchone() is None
    ):
        raise ValueError("Missing retained record parent")
    return selected


def event(db: sqlite3.Connection, packet: int, slot: int, cache: Any, binary_cache: Any) -> str | None:
    from .record_ownership import block_entry

    entry = block_entry(db, packet, slot, cache)
    if entry[0] == "episode":
        coordinate = record_blocks.Coordinate(*entry[:3])
        value = record_blocks.read(packet_store.read(db, packet), slot, coordinate, binary_cache)
        return value.get("event_id")
    return entry[5]


def bind(db: sqlite3.Connection, packet: int, owners: list[int | None]) -> None:
    groups: dict[int, list[int]] = defaultdict(list)
    if not db.in_transaction or not 0 < len(owners) <= 64:
        raise ValueError("Owner publication requires a bounded outer transaction")
    for slot, parent in enumerate(owners):
        if parent is not None:
            groups[parent].append(slot)
    for parent, slots in groups.items():
        mask = owner_slots.mask(slots)
        checksum = owner_slots.signature(parent, packet, mask)
        existing = db.execute(
            "SELECT slots,checksum FROM market_record_owners WHERE parent=? AND packet=?", (parent, packet)
        ).fetchone()
        if existing is not None:
            if tuple(existing) != (mask, checksum):
                raise ValueError("Conflicting record owner publication")
            continue
        db.execute("INSERT INTO market_record_owners VALUES(?,?,?,?)", (parent, packet, mask, checksum))


def install_functions(db: sqlite3.Connection, cache: Any = None, binary_cache: Any = None) -> None:
    owner_slots.install_functions(db)

    def admit(parent: int, packet: int, slots: bytes, checksum: bytes) -> int:
        try:
            tallies(db, "record_owners")
            if owner_slots.signature(parent, packet, slots) != checksum or not owner_slots.bits(slots):
                return 0
            row = db.execute("SELECT public_uuid FROM market_record_parents WHERE id=?", (parent,)).fetchone()
            if row is None:
                return 0
            identity = str(uuid.UUID(bytes=bytes(row[0])))
            for _, previous in packet_masks(db, packet):
                if owner_slots.bits(previous) & owner_slots.bits(slots):
                    return 0
            for slot in range(64):
                if owner_slots.has(slots, slot) and event(db, packet, slot, cache, binary_cache) != identity:
                    return 0
            return 1
        except ValueError:
            return 0

    def valid_record(kind: int, identity: bytes, revision: int, packet: int, slot: int, expires: Any) -> int:
        try:
            from .record_ownership import block_entry

            tallies(db, "record_owners")
            if type(kind) is not int or not 0 <= kind < len(record_blocks.KINDS):
                return 0
            expected = [record_blocks.KINDS[kind], str(uuid.UUID(bytes=identity)), revision]
            if block_entry(db, packet, slot, cache)[:3] != expected:
                return 0
            parent = owner(db, packet, slot, expires)
            if parent is None:
                return 1
            row = db.execute("SELECT public_uuid FROM market_record_parents WHERE id=?", (parent,)).fetchone()
            return int(event(db, packet, slot, cache, binary_cache) == str(uuid.UUID(bytes=bytes(row[0]))))
        except ValueError:
            return 0

    def change(
        parent: int, packet: int, before: bytes, old_hash: bytes, after: bytes, new_hash: bytes
    ) -> int:
        try:
            from .record_ownership import block_entry

            tallies(db, "record_owners")
            if (
                owner_slots.signature(parent, packet, before) != old_hash
                or owner_slots.signature(parent, packet, after) != new_hash
                or owner_slots.bits(after) & ~owner_slots.bits(before)
            ):
                return 0
            removed = owner_slots.bits(before) ^ owner_slots.bits(after)
            for slot in range(64):
                if removed & (1 << slot):
                    entry = block_entry(db, packet, slot, cache)
                    if db.execute(
                        "SELECT 1 FROM market_records WHERE kind=? AND public_uuid=? AND revision=?",
                        (record_blocks.KINDS.index(entry[0]), uuid.UUID(entry[1]).bytes, entry[2]),
                    ).fetchone():
                        return 0
            return 1
        except ValueError:
            return 0

    def release(parent: int, packet: int, slots: bytes, checksum: bytes) -> int:
        try:
            values = tallies(db, "record_owners")
            return int(
                values["owners"] > 0
                and slots == owner_slots.ZERO
                and owner_slots.signature(parent, packet, slots) == checksum
            )
        except ValueError:
            return 0

    def alias(which: str, delta: int) -> int:
        try:
            values = tallies(db, "record_aliases")
            return int(which in ("history", "membership") and delta in (-1, 1) and values[which] + delta >= 0)
        except ValueError:
            return 0

    def parent_count(delta: int) -> int:
        try:
            values = tallies(db, "record_parents")
            return int(type(delta) is int and delta in (-1, 1) and values["parents"] + delta >= 0)
        except ValueError:
            return 0

    db.create_function("cw_owner_admit", 4, admit)
    db.create_function("cw_owner_record", 6, valid_record)
    db.create_function("cw_owner_change", 6, change)
    db.create_function("cw_owner_release", 4, release)
    db.create_function("cw_catalogue_alias_valid", 2, alias)
    db.create_function("cw_catalogue_parent_valid", 1, parent_count)


SCHEMA = """
    CREATE TABLE market_catalogue_index_counts(
        name TEXT PRIMARY KEY CHECK(name IN('parents','owners','history','membership')),
        n INTEGER NOT NULL CHECK(typeof(n)='integer' AND n>=0)) WITHOUT ROWID;
    INSERT INTO market_catalogue_index_counts VALUES('parents',0),('owners',0),('history',0),('membership',0);
    CREATE TABLE market_record_owners(
        parent INTEGER NOT NULL REFERENCES market_record_parents(id),
        packet INTEGER NOT NULL REFERENCES market_packets(id),
        slots BLOB NOT NULL CHECK(typeof(slots)='blob' AND length(slots)=8),
        checksum BLOB NOT NULL CHECK(typeof(checksum)='blob' AND length(checksum)=32
            AND cw_owner_digest(parent,packet,slots)=checksum),
        PRIMARY KEY(parent,packet)) WITHOUT ROWID;
    CREATE INDEX market_record_owner_packet ON market_record_owners(packet,parent);
    CREATE TRIGGER market_record_owner_new BEFORE INSERT ON market_record_owners BEGIN
        SELECT CASE WHEN cw_owner_admit(NEW.parent,NEW.packet,NEW.slots,NEW.checksum)<>1
            THEN RAISE(ABORT,'Invalid record owner publication') END;
    END;
    CREATE TRIGGER market_record_owner_frozen BEFORE UPDATE ON market_record_owners BEGIN
        SELECT CASE WHEN NEW.parent<>OLD.parent OR NEW.packet<>OLD.packet OR
            cw_owner_change(OLD.parent,OLD.packet,OLD.slots,OLD.checksum,NEW.slots,NEW.checksum)<>1
            THEN RAISE(ABORT,'Retained record owner slots') END;
    END;
    CREATE TRIGGER market_record_owner_keep BEFORE DELETE ON market_record_owners BEGIN
        SELECT CASE WHEN cw_owner_release(OLD.parent,OLD.packet,OLD.slots,OLD.checksum)<>1
            THEN RAISE(ABORT,'Retained record owner mask or missing counter') END;
    END;
    CREATE TRIGGER market_record_owner_added AFTER INSERT ON market_record_owners BEGIN
        UPDATE market_catalogue_index_counts SET n=n+1 WHERE name='owners';
        INSERT INTO market_counts VALUES('record_owners',1,56,56,0)
            ON CONFLICT(category) DO UPDATE SET records=records+1,logical_bytes=logical_bytes+56,inserted_bytes=inserted_bytes+56;
    END;
    CREATE TRIGGER market_record_owner_removed AFTER DELETE ON market_record_owners BEGIN
        UPDATE market_catalogue_index_counts SET n=n-1 WHERE name='owners';
        UPDATE market_counts SET records=records-1,logical_bytes=logical_bytes-56,deleted_bytes=deleted_bytes+56
            WHERE category='record_owners';
    END;
    CREATE TRIGGER market_record_slot_new BEFORE INSERT ON market_records BEGIN
        SELECT CASE WHEN cw_owner_record(NEW.kind,NEW.public_uuid,NEW.revision,NEW.packet,NEW.slot,NEW.expires)<>1
            THEN RAISE(ABORT,'Missing or inconsistent record owner') END;
    END;
    CREATE TRIGGER market_record_slot_keep BEFORE DELETE ON market_records BEGIN
        SELECT CASE WHEN cw_owner_record(OLD.kind,OLD.public_uuid,OLD.revision,OLD.packet,OLD.slot,OLD.expires)<>1
            THEN RAISE(ABORT,'Missing or inconsistent retained record owner') END;
    END;
    CREATE TRIGGER market_record_slot_removed AFTER DELETE ON market_records BEGIN
        UPDATE market_record_owners SET slots=cw_owner_remove(slots,OLD.slot),
            checksum=cw_owner_digest(parent,packet,cw_owner_remove(slots,OLD.slot))
            WHERE packet=OLD.packet AND cw_owner_has(slots,OLD.slot)=1;
        DELETE FROM market_record_owners WHERE packet=OLD.packet AND slots=zeroblob(8);
    END;
    CREATE TRIGGER market_record_head_new BEFORE INSERT ON market_records WHEN NEW.current=1 BEGIN
        SELECT CASE WHEN EXISTS(SELECT 1 FROM market_records WHERE kind=NEW.kind AND public_uuid=NEW.public_uuid AND current=1)
            THEN RAISE(ABORT,'Conflicting current record') END;
    END;
    CREATE TRIGGER market_record_head_changed BEFORE UPDATE OF current ON market_records WHEN NEW.current=1 BEGIN
        SELECT CASE WHEN EXISTS(SELECT 1 FROM market_records WHERE kind=NEW.kind AND public_uuid=NEW.public_uuid
            AND revision<>NEW.revision AND current=1) THEN RAISE(ABORT,'Conflicting current record') END;
    END;
    CREATE TRIGGER market_record_member_frozen BEFORE UPDATE ON market_record_members BEGIN
        SELECT RAISE(ABORT,'Immutable record membership'); END;
    CREATE TRIGGER market_record_member_new BEFORE INSERT ON market_record_members BEGIN
        SELECT CASE WHEN cw_catalogue_alias_valid('membership',1)<>1 OR NOT EXISTS(
            SELECT 1 FROM market_record_parents WHERE public_uuid=NEW.event_uuid AND observed=NEW.observed)
            OR NEW.observed<0 OR NEW.observed>=10000000000000000
            THEN RAISE(ABORT,'Invalid record membership or counter') END;
    END;
    CREATE TRIGGER market_record_member_added AFTER INSERT ON market_record_members BEGIN
        UPDATE market_catalogue_index_counts SET n=n+1 WHERE name='membership';
        INSERT INTO market_counts VALUES('record_aliases',1,40,40,0)
            ON CONFLICT(category) DO UPDATE SET records=records+1,logical_bytes=logical_bytes+40,inserted_bytes=inserted_bytes+40;
    END;
    CREATE TRIGGER market_record_member_keep BEFORE DELETE ON market_record_members BEGIN
        SELECT CASE WHEN cw_catalogue_alias_valid('membership',-1)<>1
            THEN RAISE(ABORT,'Missing record membership counter') END;
    END;
    CREATE TRIGGER market_record_member_removed AFTER DELETE ON market_record_members BEGIN
        UPDATE market_catalogue_index_counts SET n=n-1 WHERE name='membership';
        UPDATE market_counts SET records=records-1,logical_bytes=logical_bytes-40,deleted_bytes=deleted_bytes+40
            WHERE category='record_aliases';
    END;
    CREATE TRIGGER market_record_history_new BEFORE UPDATE OF history_published ON market_record_parents BEGIN
        SELECT CASE WHEN OLD.history_published<>0 OR NEW.history_published<>1
            OR NEW.observed<0 OR NEW.observed>=10000000000000000
            OR cw_catalogue_alias_valid('history',1)<>1
            OR NOT EXISTS(SELECT 1 FROM market_records WHERE kind=0 AND public_uuid=NEW.public_uuid)
            THEN RAISE(ABORT,'Invalid retained history publication') END;
    END;
    CREATE TRIGGER market_record_history_initial BEFORE INSERT ON market_record_parents BEGIN
        SELECT CASE WHEN NEW.history_published<>0 OR cw_catalogue_parent_valid(1)<>1
            THEN RAISE(ABORT,'Invalid initial history publication or parent counter') END;
    END;
    CREATE TRIGGER market_record_parent_added AFTER INSERT ON market_record_parents BEGIN
        UPDATE market_catalogue_index_counts SET n=n+1 WHERE name='parents';
        INSERT INTO market_counts VALUES('record_parents',1,41,41,0)
            ON CONFLICT(category) DO UPDATE SET records=records+1,logical_bytes=logical_bytes+41,inserted_bytes=inserted_bytes+41;
    END;
    CREATE TRIGGER market_record_parent_keep BEFORE DELETE ON market_record_parents BEGIN
        SELECT CASE WHEN cw_catalogue_parent_valid(-1)<>1
            THEN RAISE(ABORT,'Missing retained record parent counter') END;
    END;
    CREATE TRIGGER market_record_parent_removed AFTER DELETE ON market_record_parents BEGIN
        UPDATE market_catalogue_index_counts SET n=n-1 WHERE name='parents';
        UPDATE market_counts SET records=records-1,logical_bytes=logical_bytes-41,deleted_bytes=deleted_bytes+41
            WHERE category='record_parents';
    END;
    CREATE TRIGGER market_record_history_added AFTER UPDATE OF history_published ON market_record_parents BEGIN
        UPDATE market_catalogue_index_counts SET n=n+1 WHERE name='history';
        INSERT INTO market_counts VALUES('record_aliases',1,24,24,0)
            ON CONFLICT(category) DO UPDATE SET records=records+1,logical_bytes=logical_bytes+24,inserted_bytes=inserted_bytes+24;
    END;
    CREATE TRIGGER market_record_history_keep BEFORE DELETE ON market_record_parents WHEN OLD.history_published=1 BEGIN
        SELECT CASE WHEN cw_catalogue_alias_valid('history',-1)<>1
            THEN RAISE(ABORT,'Missing retained history counter') END;
    END;
    CREATE TRIGGER market_record_history_removed AFTER DELETE ON market_record_parents WHEN OLD.history_published=1 BEGIN
        UPDATE market_catalogue_index_counts SET n=n-1 WHERE name='history';
        UPDATE market_counts SET records=records-1,logical_bytes=logical_bytes-24,deleted_bytes=deleted_bytes+24
            WHERE category='record_aliases';
    END;
"""
