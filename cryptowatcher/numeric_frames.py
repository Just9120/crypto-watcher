"""Transactional BF1 containers for closed, quality-only numeric ID blocks.

Explicit installation only. Old populated prototype tables keep their frozen
CHECK and NB reader; only a newly created guarded table admits NF1 pointers.
Current production's legacy numeric rows enter that table through the existing
finite ID-preserving transfer. No large table rebuild is introduced.
"""

from __future__ import annotations

import sqlite3
import struct
import time
from typing import Any

from . import numeric_block_codec, owner_slots
from . import numeric_frame_codec as codec

MAGIC = b"NF1\x00"
ZERO = bytes(8)
CATEGORY = "quality_numeric_frames"
PAYLOAD_CHECK = (
    "typeof(payload)='blob' AND ((substr(payload,1,4)=x'4e463100' AND length(payload)=13) OR "
    "(substr(payload,1,4) IN(x'4e423100',x'4e423200') AND length(payload) BETWEEN 109 AND 4096))"
)
TABLE_PREFIX = "CREATE TABLE market_numeric_blocks(id INTEGER PRIMARY KEY CHECK(id>=0 AND id<=144115188075855871),payload BLOB NOT NULL CHECK("
TABLE_SQL = TABLE_PREFIX + PAYLOAD_CHECK + "))"
LEGACY_SQL = TABLE_PREFIX + "typeof(payload)='blob' AND length(payload) BETWEEN 109 AND 4096))"


def coordinate(raw: bytes) -> tuple[int, int] | None:
    if not isinstance(raw, bytes) or not raw.startswith(MAGIC):
        return None
    if len(raw) != 13:
        raise ValueError("Invalid numeric frame coordinate")
    identity, slot = struct.unpack(">QB", raw[4:])
    if not 0 < identity < 2**63 or slot >= 64:
        raise ValueError("Invalid numeric frame coordinate")
    return identity, slot


def pointer(identity: int, slot: int) -> bytes:
    if type(identity) is not int or not 0 < identity < 2**63:
        raise ValueError("Invalid numeric frame identity")
    return MAGIC + struct.pack(">QB", identity, owner_slots.slot_number(slot))


def mode(db: sqlite3.Connection) -> str | None:
    row = db.execute("SELECT value FROM market_meta WHERE key='numeric_frames'").fetchone()
    if row is not None and row[0] not in ("1", "NB_COMPAT"):
        raise ValueError("Unknown numeric frame layout; database preserved")
    return row[0] if row is not None else None


def writing(db: sqlite3.Connection) -> bool:
    return mode(db) == "1"


def _frame(db: sqlite3.Connection, identity: int, cache: Any = None) -> tuple[bytes, bytes]:
    if not writing(db):
        raise ValueError("Missing numeric frame layout")
    row = db.execute(
        "SELECT f.payload,f.slots,f.checksum,c.records,c.logical_bytes FROM market_numeric_frames f "
        "LEFT JOIN market_counts c ON c.category='quality_numeric_frames' WHERE f.id=?",
        (identity,),
    ).fetchone()
    if row is None or owner_slots.signature(3, identity, bytes(row[1])) != bytes(row[2]):
        raise ValueError("Missing or corrupt numeric frame owner")
    raw, slots = bytes(row[0]), bytes(row[1])
    if type(row[3]) is not int or row[3] <= 0 or type(row[4]) is not int or row[4] < len(raw):
        raise ValueError("Missing numeric frame byte counter")
    if owner_slots.bits(slots) >> codec.data(raw, cache)[0]:
        raise ValueError("Numeric frame owner exceeds container")
    return raw, slots


def block_bytes(
    db: sqlite3.Connection, block: int, raw: bytes, cache: Any = None, *, admission: bool = False
) -> bytes:
    selected = coordinate(raw)
    if selected is None:
        return raw
    frame, slot = _bound(db, block, raw, cache, admission)
    value = codec.selected(frame, slot, cache)
    if value is None:
        raise ValueError("Missing numeric frame block")
    return value[1]


def _bound(db: sqlite3.Connection, block: int, raw: bytes, cache: Any, admission: bool) -> tuple[bytes, int]:
    selected = coordinate(raw)
    if selected is None:
        raise ValueError("Not a numeric frame binding")
    frame, slots = _frame(db, selected[0], cache)
    entry = codec.entry_bytes(frame, selected[1], cache)
    if struct.unpack(">Q", entry[:8])[0] != block:
        raise ValueError("Missing or conflicting numeric frame binding")
    owned = owner_slots.has(slots, selected[1])
    current = db.execute("SELECT payload FROM market_numeric_blocks WHERE id=?", (block,)).fetchone()
    current_binding = current is not None and bytes(current[0]) == raw
    if owned != current_binding or (not admission and not owned):
        raise ValueError("Conflicting numeric frame ownership")
    return frame, selected[1]


def signed_bytes(db: sqlite3.Connection, block: int, raw: bytes, cache: Any = None) -> bytes | None:
    if coordinate(raw) is None:
        return None
    frame, slot = _bound(db, block, raw, cache, False)
    return codec.entry_bytes(frame, slot, cache)


def _add(mask: bytes, slot: int) -> bytes:
    if owner_slots.has(mask, slot):
        raise ValueError("Duplicate numeric frame owner")
    return (owner_slots.bits(mask) | (1 << slot)).to_bytes(8, "big")


def install_functions(db: sqlite3.Connection, cache: Any = None) -> None:
    owner_slots.install_functions(db)

    def field(raw: bytes, index: int) -> int | None:
        value = coordinate(raw)
        return value[index] if value is not None else None

    def bind(block: int, raw: bytes) -> int:
        selected = coordinate(raw)
        if selected is None:
            return 1
        block_bytes(db, block, raw, cache, admission=True)
        identity, slot = selected
        return int(not owner_slots.has(_frame(db, identity, cache)[1], slot))

    def stable(identity: int, before: bytes, after: bytes, slots: bytes, checksum: bytes) -> int:
        if owner_slots.signature(3, identity, slots) != checksum:
            raise ValueError("Corrupt numeric frame owner")
        old, new = codec.all_values(before, cache), codec.all_values(after, cache)
        if len(old) != len(new) or owner_slots.bits(slots) >> len(old):
            raise ValueError("Immutable numeric frame positions")
        for slot, value in enumerate(old):
            if owner_slots.has(slots, slot):
                if value != new[slot]:
                    raise ValueError("Immutable retained numeric frame value")
            elif new[slot] is not None and value != new[slot]:
                raise ValueError("Cannot replace an unowned numeric frame slot")
        return 1

    def empty(identity: int, raw: bytes, slots: bytes, checksum: bytes) -> int:
        if slots != ZERO or owner_slots.signature(3, identity, slots) != checksum:
            raise ValueError("Retained numeric frame")
        for slot, value in enumerate(codec.all_values(raw, cache)):
            if value is not None:
                row = db.execute(
                    "SELECT payload FROM market_numeric_blocks WHERE id=?", (value[0],)
                ).fetchone()
                if row is not None and bytes(row[0]) == pointer(identity, slot):
                    raise ValueError("Missing numeric frame owner bit")
        return 1

    db.create_function("cw_nframe_id", 1, lambda raw: field(raw, 0), deterministic=True)
    db.create_function("cw_nframe_slot", 1, lambda raw: field(raw, 1), deterministic=True)
    db.create_function("cw_nframe_add", 2, _add, deterministic=True)
    db.create_function("cw_nframe_bind", 2, bind)
    db.create_function("cw_nframe_stable", 5, stable)
    db.create_function("cw_nframe_empty", 4, empty)
    db.create_function("cw_nframe_valid", 1, lambda raw: int(bool(codec.all_values(raw, cache))))


def schema() -> dict[str, str]:
    result = {
        "market_numeric_frames": """CREATE TABLE market_numeric_frames(
            id INTEGER PRIMARY KEY AUTOINCREMENT,payload BLOB NOT NULL,
            slots BLOB NOT NULL CHECK(typeof(slots)='blob' AND length(slots)=8),
            checksum BLOB NOT NULL CHECK(typeof(checksum)='blob' AND length(checksum)=32
                AND cw_owner_digest(3,id,slots)=checksum),
            gc_since INTEGER CHECK(gc_since IS NULL OR (typeof(gc_since)='integer' AND gc_since>=0)))""",
        "market_numeric_frame_gc": "CREATE INDEX market_numeric_frame_gc ON market_numeric_frames(gc_since,id) WHERE gc_since IS NOT NULL",
        "market_nframe_new": """CREATE TRIGGER market_nframe_new BEFORE INSERT ON market_numeric_frames BEGIN
            SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM market_counts WHERE category='quality_numeric_frames'
                AND typeof(records)='integer' AND records>=0 AND typeof(logical_bytes)='integer' AND logical_bytes>=0)
                OR NEW.slots<>zeroblob(8) OR cw_nframe_valid(NEW.payload)<>1
                THEN RAISE(ABORT,'Invalid numeric frame admission') END; END""",
        "market_nframe_identity": """CREATE TRIGGER market_nframe_identity BEFORE UPDATE OF id ON market_numeric_frames
            WHEN NEW.id<>OLD.id BEGIN SELECT RAISE(ABORT,'Immutable numeric frame identity'); END""",
        "market_nframe_payload": """CREATE TRIGGER market_nframe_payload BEFORE UPDATE OF payload ON market_numeric_frames
            WHEN NEW.payload<>OLD.payload BEGIN
            SELECT cw_nframe_stable(OLD.id,OLD.payload,NEW.payload,OLD.slots,OLD.checksum); END""",
        "market_nframe_keep": """CREATE TRIGGER market_nframe_keep BEFORE DELETE ON market_numeric_frames BEGIN
            SELECT cw_nframe_empty(OLD.id,OLD.payload,OLD.slots,OLD.checksum); END""",
    }
    for operation in ("INSERT", "UPDATE"):
        result["market_nframe_bind_" + operation] = (
            "CREATE TRIGGER market_nframe_bind_"
            + operation
            + " BEFORE "
            + operation
            + (" OF payload" if operation == "UPDATE" else "")
            + " ON market_numeric_blocks "
            + ("WHEN NEW.payload<>OLD.payload " if operation == "UPDATE" else "")
            + "BEGIN SELECT CASE WHEN cw_nframe_bind(NEW.id,NEW.payload)<>1 "
            "THEN RAISE(ABORT,'Invalid numeric frame binding') END; END"
        )
    for operation in ("INSERT", "UPDATE", "DELETE"):
        statements = []
        if operation != "INSERT":
            removed = "cw_owner_remove(slots,cw_nframe_slot(OLD.payload))"
            statements.append(
                f"UPDATE market_numeric_frames SET slots={removed},checksum=cw_owner_digest(3,id,{removed}),"
                "gc_since=COALESCE(gc_since,cw_clock_ms()) WHERE id=cw_nframe_id(OLD.payload);"
            )
        if operation != "DELETE":
            added = "cw_nframe_add(slots,cw_nframe_slot(NEW.payload))"
            statements.append(
                f"UPDATE market_numeric_frames SET slots={added},checksum=cw_owner_digest(3,id,{added}) "
                "WHERE id=cw_nframe_id(NEW.payload);"
            )
        result["market_nframe_owner_" + operation] = (
            "CREATE TRIGGER market_nframe_owner_"
            + operation
            + " AFTER "
            + operation
            + (" OF payload" if operation == "UPDATE" else "")
            + " ON market_numeric_blocks "
            + ("WHEN NEW.payload<>OLD.payload " if operation == "UPDATE" else "")
            + "BEGIN "
            + " ".join(statements)
            + " END"
        )
        body = (
            f"UPDATE market_counts SET records=records+1,logical_bytes=logical_bytes+length(NEW.payload),inserted_bytes=inserted_bytes+length(NEW.payload) WHERE category='{CATEGORY}';"
            if operation == "INSERT"
            else f"UPDATE market_counts SET records=records-1,logical_bytes=logical_bytes-length(OLD.payload),deleted_bytes=deleted_bytes+length(OLD.payload) WHERE category='{CATEGORY}';"
            if operation == "DELETE"
            else f"UPDATE market_counts SET logical_bytes=logical_bytes+length(NEW.payload)-length(OLD.payload),inserted_bytes=inserted_bytes+max(0,length(NEW.payload)-length(OLD.payload)),deleted_bytes=deleted_bytes+max(0,length(OLD.payload)-length(NEW.payload)) WHERE category='{CATEGORY}';"
        )
        result["count_market_numeric_frames_" + operation] = (
            "CREATE TRIGGER count_market_numeric_frames_"
            + operation
            + " AFTER "
            + operation
            + (" OF payload" if operation == "UPDATE" else "")
            + " ON market_numeric_frames BEGIN "
            + body
            + " END"
        )
        if operation != "INSERT":
            result["market_nframe_counter_" + operation] = (
                "CREATE TRIGGER market_nframe_counter_"
                + operation
                + " BEFORE "
                + operation
                + (" OF payload" if operation == "UPDATE" else "")
                + " ON market_numeric_frames BEGIN SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM market_counts "
                "WHERE category='quality_numeric_frames' AND typeof(records)='integer' AND records>0 "
                "AND typeof(logical_bytes)='integer' AND logical_bytes>=length(OLD.payload)) "
                "THEN RAISE(ABORT,'Missing numeric frame byte counter') END; END"
            )
    return result


def header(db: sqlite3.Connection) -> str:
    current = mode(db)
    if current is None:
        raise ValueError("Missing numeric frame checkpoint")
    sql = db.execute("SELECT sql FROM sqlite_master WHERE name='market_numeric_blocks'").fetchone()
    expected = TABLE_SQL if current == "1" else LEGACY_SQL
    if sql is None or " ".join(sql[0].split()) != " ".join(expected.split()):
        raise ValueError("Numeric frame table does not match layout")
    if current == "1":
        for name, expected in schema().items():
            row = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
            if row is None or " ".join(row[0].split()) != " ".join(expected.split()):
                raise ValueError("Missing or unknown numeric frame guard")
        row = db.execute(
            "SELECT records,logical_bytes FROM market_counts WHERE category=?", (CATEGORY,)
        ).fetchone()
        if row is None or any(type(value) is not int or value < 0 for value in row):
            raise ValueError("Missing numeric frame byte counter")
    elif db.execute("SELECT 1 FROM sqlite_master WHERE name='market_numeric_frames'").fetchone():
        raise ValueError("Unexpected numeric frames in compatible layout")
    return current


def install(db: sqlite3.Connection, cache: Any = None) -> None:
    install_functions(db, cache)
    if mode(db) is not None:
        header(db)
        return
    if db.in_transaction:
        raise ValueError("Numeric frame installation requires an outer boundary")
    parent = db.execute("SELECT value FROM market_meta WHERE key='numeric_blocks'").fetchone()
    sql = db.execute("SELECT sql FROM sqlite_master WHERE name='market_numeric_blocks'").fetchone()
    if parent is None or parent[0] != "3" or sql is None:
        raise ValueError("Numeric frames require the explicit search layout")
    normalized = " ".join(sql[0].split())
    current = (
        "1"
        if normalized == " ".join(TABLE_SQL.split())
        else "NB_COMPAT"
        if normalized == " ".join(LEGACY_SQL.split())
        else None
    )
    if (
        current is None
        or db.execute("SELECT 1 FROM sqlite_master WHERE name='market_numeric_frames'").fetchone()
    ):
        raise ValueError("Unknown numeric frame installation; database preserved")
    with db:
        db.execute("BEGIN IMMEDIATE")
        if current == "1":
            for statement in schema().values():
                db.execute(statement)
            db.execute("INSERT INTO market_counts VALUES(?,0,0,0,0)", (CATEGORY,))
        db.execute("INSERT INTO market_meta VALUES('numeric_frames',?)", (current,))


def publish(db: sqlite3.Connection, sources: list[tuple[int, bytes]], cache: Any = None) -> int | None:
    from . import numeric_blocks as blocks
    from . import numeric_search
    from .packet_store import atomic

    if not writing(db) or not db.in_transaction or not 0 < len(sources) <= 64:
        raise ValueError("Numeric frame publication requires a bounded outer writer")
    if len({identity for identity, _ in sources}) != len(sources):
        raise ValueError("Duplicate numeric frame publication source")
    for block, raw in sources:
        candidate = blocks.read(db, block)
        refs = db.execute("SELECT unused FROM market_numeric_block_refs WHERE id=?", (block,)).fetchone()
        if (
            candidate is None
            or candidate[3] != raw
            or coordinate(raw) is not None
            or any(candidate[1])
            or refs is None
            or refs[0]
            or (block + 1) * 64 > blocks.state(db)[0]
            or not numeric_search.sealed(db, block)
        ):
            raise ValueError("Changed or ineligible numeric frame source")
    values: list[tuple[int, bytes] | None] = list(sources)
    payload = codec.pack(values)
    restored = codec.all_values(payload, cache)
    for source_entry, restored_entry in zip(sources, restored, strict=True):
        if (
            restored_entry is None
            or restored_entry[0] != source_entry[0]
            or restored_entry[1][:4] != source_entry[1][:4]
            or numeric_block_codec.unpack(restored_entry[1], restored_entry[0])
            != numeric_block_codec.unpack(source_entry[1], source_entry[0])
        ):
            raise ValueError("Numeric frame changed private metric frames")
    if len(payload) + len(sources) * 13 + 104 >= sum(len(raw) for _, raw in sources):
        return None
    with atomic(db):
        row = db.execute("SELECT seq FROM sqlite_sequence WHERE name='market_numeric_frames'").fetchone()
        identity = (row[0] if row else 0) + 1
        db.execute(
            "INSERT INTO market_numeric_frames VALUES(?,?,?,?,NULL)",
            (identity, payload, ZERO, owner_slots.signature(3, identity, ZERO)),
        )
        for slot, (block, expected) in enumerate(sources):
            old = blocks.read(db, block)
            counts = bytes(
                db.execute("SELECT counts FROM market_numeric_block_refs WHERE id=?", (block,)).fetchone()[0]
            )
            if old is None or old[3] != expected:
                raise ValueError("Stale numeric frame source")
            old_allocation = blocks.allocation(
                block, *old[:3], len(expected), index_bytes=40, counts_bytes=len(counts)
            )
            raw = pointer(identity, slot)
            if (
                db.execute(
                    "UPDATE market_numeric_blocks SET payload=? WHERE id=? AND payload=?",
                    (raw, block, expected),
                ).rowcount
                != 1
            ):
                raise ValueError("Numeric frame publication CAS failed")
            db.execute(
                "UPDATE market_numeric_block_refs SET search_seal=? WHERE id=?",
                (numeric_search.bound_signature(db, block, raw, counts), block),
            )
            new_allocation = blocks.allocation(
                block, *old[:3], len(raw), index_bytes=40, counts_bytes=len(counts)
            )
            numeric_search.reaccount(db, old_allocation, new_allocation)
            actual = blocks.read(db, block)
            if actual is None or actual[:3] != old[:3]:
                raise ValueError("Numeric frame publication changed metrics or leases")
    return identity


def collect(db: sqlite3.Connection, deadline: float, batch: int = 16, cache: Any = None) -> int:
    if not db.in_transaction or type(batch) is not int or not 0 < batch <= 256 or not writing(db):
        raise ValueError("Numeric frame GC requires a bounded outer writer")
    removed = 0
    for (identity,) in db.execute(
        "SELECT id FROM market_numeric_frames WHERE gc_since IS NOT NULL ORDER BY gc_since,id LIMIT ?",
        (min(batch, 16),),
    ).fetchall():
        if time.monotonic() >= deadline:
            break
        raw, slots = _frame(db, identity, cache)
        values = codec.all_values(raw, cache)
        for slot, value in enumerate(values):
            retained = False
            if value is not None:
                row = db.execute(
                    "SELECT payload FROM market_numeric_blocks WHERE id=?", (value[0],)
                ).fetchone()
                retained = row is not None and bytes(row[0]) == pointer(identity, slot)
            if retained != owner_slots.has(slots, slot):
                raise ValueError("Conflicting numeric frame retained slot")
            if not retained:
                values[slot] = None
        if slots == ZERO:
            db.execute("DELETE FROM market_numeric_frames WHERE id=?", (identity,))
            removed += 1
        else:
            replacement = codec.pack(values)
            if codec.all_values(replacement, cache) != values:
                raise ValueError("Numeric frame GC changed retained block bytes")
            db.execute(
                "UPDATE market_numeric_frames SET payload=?,gc_since=NULL WHERE id=?", (replacement, identity)
            )
    return removed
