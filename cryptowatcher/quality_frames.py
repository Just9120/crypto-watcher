"""QF1 pointers to bounded IF1/QC1 containers for logical CQ5 packets.

Only explicit maintenance installs/publishes this representation. Coordinates,
numeric leases and public facts stay unchanged; obsolete private column rows
can be collected once their values are inside the frame.
"""

from __future__ import annotations

import copy
import sqlite3
import struct
import time
from dataclasses import dataclass
from typing import Any

from . import indexed_frame, owner_slots, quality_frame_codec
from .compact_json import unpack as typed_unpack
from .storage import StateError, encode

MAGIC = b"QF1\x00"
MAX_EXPANDED = 1024 * 1024
ZERO = bytes(8)


def coordinate(raw: bytes) -> tuple[int, int] | None:
    if not isinstance(raw, bytes) or not raw.startswith(MAGIC):
        return None
    if len(raw) != 13:
        raise ValueError("Invalid quality frame coordinate")
    identity, slot = struct.unpack(">QB", raw[4:])
    if not 0 < identity < 2**63 or not 0 <= slot < 64:
        raise ValueError("Invalid quality frame coordinate")
    return identity, slot


def pointer(identity: int, slot: int) -> bytes:
    if type(identity) is not int or not 0 < identity < 2**63 or type(slot) is not int or not 0 <= slot < 64:
        raise ValueError("Invalid quality frame coordinate")
    raw = MAGIC + struct.pack(">QB", identity, slot)
    coordinate(raw)
    return raw


def _add(mask: bytes, slot: int) -> bytes:
    bit = 1 << owner_slots.slot_number(slot)
    original = owner_slots.bits(mask)
    if original & bit:
        raise ValueError("Duplicate quality frame owner")
    return (original | bit).to_bytes(8, "big")


def installed(db: sqlite3.Connection) -> bool:
    row = db.execute("SELECT value FROM market_meta WHERE key='quality_frames'").fetchone()
    if row is not None and row[0] != "1":
        raise StateError("Unknown quality frame layout; database preserved")
    if row is not None:
        names = {
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('market_quality_frames','market_quality_frame_pending')"
            )
        }
        if len(names) != 2:
            raise StateError("Incomplete quality frame layout; database preserved")
    return row is not None


def _frame(db: sqlite3.Connection, identity: int) -> tuple[bytes, bytes]:
    if not installed(db):
        raise StateError("Missing quality frame layout")
    found = db.execute(
        "SELECT payload,slots,checksum FROM market_quality_frames WHERE id=?", (identity,)
    ).fetchone()
    if found is None or owner_slots.signature(1, identity, bytes(found[1])) != bytes(found[2]):
        raise StateError("Missing or corrupt quality frame owner")
    if bytes(found[0]).startswith(quality_frame_codec.MAGIC) and not column_writing(db):
        raise StateError("Missing quality column writer checkpoint")
    return bytes(found[0]), bytes(found[1])


def entry_bytes(db: sqlite3.Connection, raw: bytes, cache: Any = None) -> bytes:
    """Read/authenticate the current container before any pure extraction cache."""
    selected = coordinate(raw)
    if selected is None:
        raise ValueError("Not a quality frame reference")
    identity, slot = selected
    frame, _ = _frame(db, identity)
    return quality_frame_codec.entry_bytes(frame, slot, cache)


def checked_entry(value: Any) -> list[Any]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(type(n) is not int or not 0 <= n < 2**63 for n in value[:3])
        or not isinstance(value[3], list)
        or len(value[3]) != 2
        or value[3][1] is not None
    ):
        raise StateError("Invalid retained quality frame entry")
    return value


def entry(db: sqlite3.Connection, raw: bytes) -> list[Any]:
    return checked_entry(typed_unpack(entry_bytes(db, raw), indexed_frame.MAX_BYTES))


def packet(db: sqlite3.Connection, raw: bytes) -> list[Any]:
    return entry(db, raw)[3]


def install_functions(db: sqlite3.Connection) -> None:
    def field(raw: Any, index: int) -> int | None:
        selected = coordinate(raw)
        return selected[index] if selected is not None else None

    def bind(raw: bytes, day: int, cohort: int, chunk: int) -> int:
        selected = coordinate(raw)
        if selected is None:
            return 0
        _, mask = _frame(db, selected[0])
        return int(not owner_slots.has(mask, selected[1]) and entry(db, raw)[:3] == [day, cohort, chunk])

    db.create_function("cw_qframe_id", 1, lambda raw: field(raw, 0), deterministic=True)
    db.create_function("cw_qframe_slot", 1, lambda raw: field(raw, 1), deterministic=True)
    db.create_function("cw_qframe_add", 2, _add, deterministic=True)
    db.create_function("cw_qframe_bind", 4, bind)


def install(db: sqlite3.Connection) -> None:
    if installed(db):
        install_columns(db)
        return
    if db.in_transaction:
        raise StateError("Quality frame installation requires an outer boundary")
    if db.execute("SELECT 1 FROM market_meta WHERE key='quality_frame_columns'").fetchone():
        raise StateError("Unmarked quality column layout; data preserved")
    layout = db.execute("SELECT value FROM market_meta WHERE key='quality_layout'").fetchone()
    if layout is None or layout[0] != "3":
        raise StateError("Quality frames require normalized archive rows")
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='market_quality_frames'").fetchone():
        raise StateError("Unmarked quality frame layout; database preserved")
    install_functions(db)
    try:
        db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE market_quality_frames(
                id INTEGER PRIMARY KEY AUTOINCREMENT,payload BLOB NOT NULL,
                slots BLOB NOT NULL CHECK(typeof(slots)='blob' AND length(slots)=8),
                checksum BLOB NOT NULL CHECK(typeof(checksum)='blob' AND length(checksum)=32
                    AND cw_owner_digest(1,id,slots)=checksum),
                gc_since INTEGER CHECK(gc_since IS NULL OR (typeof(gc_since)='integer' AND gc_since>=0)));
            CREATE INDEX market_quality_frame_gc ON market_quality_frames(gc_since,id) WHERE gc_since IS NOT NULL;
            CREATE TABLE market_quality_frame_pending(
                day INTEGER NOT NULL,cohort INTEGER NOT NULL,chunk INTEGER NOT NULL,
                queued_at INTEGER NOT NULL CHECK(typeof(queued_at)='integer' AND queued_at>=0),
                eligible_at INTEGER NOT NULL CHECK(typeof(eligible_at)='integer' AND eligible_at>=0),
                PRIMARY KEY(day,cohort,chunk)) WITHOUT ROWID;
            CREATE INDEX market_quality_frame_age ON market_quality_frame_pending(eligible_at,queued_at,day,cohort,chunk);
            CREATE TRIGGER market_quality_frame_bind_insert BEFORE INSERT ON market_quality_blocks
                WHEN substr(NEW.facts,1,4)=x'51463100' BEGIN
                SELECT CASE WHEN cw_qframe_bind(NEW.facts,NEW.day,NEW.cohort,NEW.chunk)<>1
                    THEN RAISE(ABORT,'Invalid quality frame binding') END;
            END;
            CREATE TRIGGER market_quality_frame_bind_update BEFORE UPDATE OF facts ON market_quality_blocks
                WHEN NEW.facts<>OLD.facts AND substr(NEW.facts,1,4)=x'51463100' BEGIN
                SELECT CASE WHEN cw_qframe_bind(NEW.facts,NEW.day,NEW.cohort,NEW.chunk)<>1
                    THEN RAISE(ABORT,'Invalid quality frame binding') END;
            END;
            CREATE TRIGGER market_quality_frame_coordinate BEFORE UPDATE OF day,cohort,chunk ON market_quality_blocks
                WHEN substr(OLD.facts,1,4)=x'51463100' AND
                    (NEW.day<>OLD.day OR NEW.cohort<>OLD.cohort OR NEW.chunk<>OLD.chunk) BEGIN
                SELECT RAISE(ABORT,'Immutable quality frame coordinate');
            END;
        """)
        for operation in ("INSERT", "DELETE", "UPDATE"):
            parts = []
            if operation != "INSERT":
                remove = "cw_owner_remove(slots,cw_qframe_slot(OLD.facts))"
                parts.append(
                    f"UPDATE market_quality_frames SET slots={remove},checksum=cw_owner_digest(1,id,{remove}),"
                    "gc_since=COALESCE(gc_since,cw_clock_ms()) WHERE id=cw_qframe_id(OLD.facts);"
                )
            if operation != "DELETE":
                add = "cw_qframe_add(slots,cw_qframe_slot(NEW.facts))"
                parts.append(
                    f"UPDATE market_quality_frames SET slots={add},checksum=cw_owner_digest(1,id,{add}) "
                    "WHERE id=cw_qframe_id(NEW.facts);"
                )
                parts.append(
                    "INSERT INTO market_quality_frame_pending SELECT NEW.day,NEW.cohort,NEW.chunk,cw_clock_ms(),"
                    "CASE WHEN NEW.n>=128 THEN cw_clock_ms() ELSE max(cw_clock_ms(),"
                    "(NEW.day+1+COALESCE((SELECT json_extract(settings_json,'$.detail_retention_days') FROM owner_settings LIMIT 1),30))*86400000) END "
                    "WHERE substr(NEW.facts,1,4)=x'43513500' ON CONFLICT(day,cohort,chunk) "
                    "DO UPDATE SET eligible_at=min(eligible_at,excluded.eligible_at);"
                )
            alias = "OLD" if operation == "DELETE" else "NEW"
            condition = "" if operation == "DELETE" else " AND substr(NEW.facts,1,4)<>x'43513500'"
            parts.append(
                f"DELETE FROM market_quality_frame_pending WHERE day={alias}.day AND cohort={alias}.cohort "
                f"AND chunk={alias}.chunk{condition};"
            )
            db.execute(
                f"CREATE TRIGGER market_quality_frame_{operation} AFTER {operation}"
                + (" OF facts" if operation == "UPDATE" else "")
                + " ON market_quality_blocks "
                + ("WHEN NEW.facts<>OLD.facts " if operation == "UPDATE" else "")
                + "BEGIN "
                + " ".join(parts)
                + " END"
            )
        for table, column, category, size in (
            ("market_quality_frames", "payload", "quality_frames", None),
            ("market_quality_frame_pending", "queued_at", "quality_frame_pending", "40"),
        ):
            for operation in ("INSERT", "DELETE", "UPDATE"):
                new = size or f"length(NEW.{column})"
                old = size or f"length(OLD.{column})"
                sql = (
                    f"INSERT INTO market_counts VALUES('{category}',1,{new},{new},0) ON CONFLICT(category) DO UPDATE SET records=records+1,logical_bytes=logical_bytes+{new},inserted_bytes=inserted_bytes+{new};"
                    if operation == "INSERT"
                    else f"UPDATE market_counts SET records=records-1,logical_bytes=logical_bytes-{old},deleted_bytes=deleted_bytes+{old} WHERE category='{category}';"
                    if operation == "DELETE"
                    else f"UPDATE market_counts SET logical_bytes=logical_bytes+{new}-{old},inserted_bytes=inserted_bytes+max(0,{new}-{old}),deleted_bytes=deleted_bytes+max(0,{old}-{new}) WHERE category='{category}';"
                )
                db.execute(
                    f"CREATE TRIGGER count_{table}_{operation} AFTER {operation} ON {table} BEGIN {sql} END"
                )
        db.execute("INSERT INTO market_meta VALUES('quality_frames','1')")
        db.execute("INSERT INTO market_meta VALUES('quality_frame_columns','1')")
        db.commit()
    except BaseException:
        db.rollback()
        raise


def column_writing(db: sqlite3.Connection) -> bool:
    row = db.execute("SELECT value FROM market_meta WHERE key='quality_frame_columns'").fetchone()
    if row is None:
        return False
    if row[0] != "1" or not installed(db):
        raise StateError("Unknown quality column writer checkpoint; data preserved")
    return True


def install_columns(db: sqlite3.Connection) -> None:
    if column_writing(db):
        return
    if db.in_transaction or not installed(db):
        raise StateError("Quality column installation requires an outer frame boundary")
    with db:
        db.execute("INSERT INTO market_meta VALUES('quality_frame_columns','1')")


@dataclass(frozen=True)
class Prepared:
    key: tuple[int, int, int]
    raw: bytes
    value: list[Any]


def prepare(db: sqlite3.Connection, row: Any) -> Prepared | None:
    from . import quality_packets

    day, cohort, chunk, raw = row
    raw = bytes(raw)
    if not raw.startswith(quality_packets.MAGIC):
        return None
    rows, columns = quality_packets.packet(raw)
    pairs = quality_packets.row(db, columns, "columns")[0] if columns is not None else [v[4:] for v in rows]
    if len(pairs) != len(rows) or any(not isinstance(pair, list) or len(pair) != 2 for pair in pairs):
        raise StateError("Invalid quality frame scalar columns")
    normalized = [[*value[:4], *pair] for value, pair in zip(rows, pairs, strict=True)]
    return Prepared((day, cohort, chunk), raw, [day, cohort, chunk, [normalized, None]])


def publish(db: sqlite3.Connection, records: list[Prepared]) -> int:
    from .packet_store import atomic

    if not db.in_transaction or not 0 < len(records) <= 64:
        raise StateError("Quality frame publication requires a bounded outer writer")
    if len({record.key for record in records}) != len(records):
        raise StateError("Duplicate quality frame source")
    for record in records:
        expected = prepare(db, (*record.key, record.raw))
        if expected is None or encode(expected.value) != encode(record.value):
            raise StateError("Changed quality frame preparation; data preserved")
    values = [record.value for record in records]
    if len(encode(values).encode()) > MAX_EXPANDED:
        raise StateError("Quality frame admission bound")
    frame = quality_frame_codec.pack(values, use_columns=column_writing(db))
    if encode(quality_frame_codec.all_values(frame)) != encode(values):
        raise StateError("Quality frame changed logical packet values")
    with atomic(db):
        row = db.execute("SELECT seq FROM sqlite_sequence WHERE name='market_quality_frames'").fetchone()
        identity = (row[0] if row else 0) + 1
        db.execute(
            "INSERT INTO market_quality_frames VALUES(?,?,?,?,NULL)",
            (identity, frame, ZERO, owner_slots.signature(1, identity, ZERO)),
        )
        for slot, record in enumerate(records):
            changed = db.execute(
                "UPDATE market_quality_blocks SET facts=? WHERE day=? AND cohort=? AND chunk=? AND facts=?",
                (pointer(identity, slot), *record.key, record.raw),
            ).rowcount
            if changed != 1:
                raise StateError("Stale quality frame source; data preserved")
    return identity


def collect(db: sqlite3.Connection, deadline: float, batch: int = 16) -> int:
    if not db.in_transaction or type(batch) is not int or not 0 < batch <= 256:
        raise StateError("Quality frame GC requires a bounded outer writer")
    removed = 0
    for (identity,) in db.execute(
        "SELECT id FROM market_quality_frames WHERE gc_since IS NOT NULL ORDER BY gc_since,id LIMIT ?",
        (min(batch, 16),),
    ).fetchall():
        if time.monotonic() >= deadline:
            break
        raw, slots = _frame(db, identity)
        values = quality_frame_codec.all_values(raw)
        if owner_slots.bits(slots) >> len(values):
            raise StateError("Quality frame owner exceeds frame")
        for slot, value in enumerate(values):
            if value is None:
                if owner_slots.has(slots, slot):
                    raise StateError("Missing retained quality frame slot")
                continue
            row = db.execute(
                "SELECT facts FROM market_quality_blocks WHERE day=? AND cohort=? AND chunk=?", value[:3]
            ).fetchone()
            retained = row is not None and bytes(row[0]) == pointer(identity, slot)
            if retained != owner_slots.has(slots, slot):
                raise StateError("Conflicting quality frame retention")
            if not retained:
                values[slot] = None
        if slots == ZERO:
            db.execute("DELETE FROM market_quality_frames WHERE id=?", (identity,))
            removed += 1
        else:
            replacement = quality_frame_codec.pack(values, use_columns=column_writing(db))
            if encode(quality_frame_codec.all_values(replacement)) != encode(values):
                raise StateError("Quality frame GC changed retained values")
            db.execute(
                "UPDATE market_quality_frames SET payload=?,gc_since=NULL WHERE id=?", (replacement, identity)
            )
    return removed


def status(db: sqlite3.Connection, now: int) -> dict[str, Any]:
    if not installed(db):
        return {"installed": False, "pending_work": False, "oldest_ms": None, "oldest_age_ms": None}
    pending = db.execute(
        "SELECT eligible_at,queued_at FROM market_quality_frame_pending ORDER BY eligible_at,queued_at,day,cohort,chunk LIMIT 1"
    ).fetchone()
    gc = db.execute(
        "SELECT gc_since FROM market_quality_frames WHERE gc_since IS NOT NULL ORDER BY gc_since,id LIMIT 1"
    ).fetchone()
    dictionary_gc = (
        db.execute("SELECT 1 FROM market_quality_dictionary WHERE refs=0 LIMIT 1").fetchone() is not None
    )
    stamps = ([pending[0]] if pending is not None and pending[0] <= now else []) + (
        [gc[0]] if gc is not None else []
    )
    oldest = min(stamps) if stamps else None
    return {
        "installed": True,
        "pending_work": bool(stamps) or dictionary_gc,
        "dictionary_gc_pending": dictionary_gc,
        "oldest_ms": oldest,
        "oldest_age_ms": max(0, now - oldest) if oldest is not None else None,
        "next_eligible_ms": pending[0] if pending is not None else None,
        "clock_unknown": bool(
            pending is not None and pending[1] > now + 30000 or gc is not None and gc[0] > now + 30000
        ),
    }


def validate(state: dict[str, Any]) -> None:
    if (
        not isinstance(state, dict)
        or type(state.get("version", 1)) is not int
        or state.get("version", 1) != 1
    ):
        raise StateError("Unknown quality frame checkpoint")
    if ("upper" in state) != ("cursor" in state):
        raise StateError("Incomplete quality frame boundary")
    for name in ("cursor", "upper"):
        if name in state and (
            not isinstance(state[name], list)
            or len(state[name]) != 3
            or any(type(n) is not int or not -1 <= n < 2**63 for n in state[name])
            or state[name] != [-1, -1, -1]
            and any(n < 0 for n in state[name])
        ):
            raise StateError("Invalid quality frame boundary")
    if "upper" in state and tuple(state["cursor"]) > tuple(state["upper"]):
        raise StateError("Quality frame cursor exceeds boundary")
    if "initial_complete" in state and (type(state["initial_complete"]) is not bool or "upper" not in state):
        raise StateError("Missing quality frame initial boundary")
    if state.get("initial_complete") and state["cursor"] != state["upper"]:
        raise StateError("Incomplete quality frame initial scan")
    for name in ("rows_examined", "packets_moved", "frames_created", "oversized_skipped"):
        if name in state and (type(state[name]) is not int or not 0 <= state[name] < 2**63):
            raise StateError("Invalid quality frame progress")


def advance(
    db: sqlite3.Connection,
    state: dict[str, Any],
    batch: int,
    deadline: float,
    *,
    now_ms: int | None = None,
) -> bool:
    from .packet_store import atomic

    validate(state)
    if not db.in_transaction or type(batch) is not int or not 0 < batch <= 256 or not installed(db):
        raise StateError("Quality frame work requires a bounded outer writer")
    now = db.execute("SELECT cw_clock_ms()").fetchone()[0] if now_ms is None else now_ms
    if type(now) is not int or now < 0:
        raise StateError("Invalid quality frame clock")
    if time.monotonic() >= deadline:
        return state.get("initial_complete") is True
    working = copy.deepcopy(state)
    with atomic(db):
        collect(db, deadline)
        _advance(db, working, min(batch, 64), deadline, now)
    state.clear()
    state.update(working)
    return state.get("initial_complete") is True


def _advance(db: sqlite3.Connection, state: dict[str, Any], batch: int, deadline: float, now: int) -> None:
    from . import quality_packets

    if "upper" not in state:
        upper = db.execute(
            "SELECT day,cohort,chunk FROM market_quality_blocks ORDER BY day DESC,cohort DESC,chunk DESC LIMIT 1"
        ).fetchone()
        state.update(version=1, upper=list(upper) if upper else [-1, -1, -1], cursor=[-1, -1, -1])
    initial = not state.get("initial_complete")
    if initial:
        metadata = db.execute(
            "SELECT day,cohort,chunk,length(facts),n FROM market_quality_blocks "
            "WHERE (day,cohort,chunk)>(?,?,?) AND (day,cohort,chunk)<=(?,?,?) ORDER BY day,cohort,chunk LIMIT ?",
            (*state["cursor"], *state["upper"], batch),
        ).fetchall()
    else:
        metadata = db.execute(
            "SELECT p.day,p.cohort,p.chunk,length(b.facts),b.n FROM market_quality_frame_pending p "
            "LEFT JOIN market_quality_blocks b USING(day,cohort,chunk) WHERE p.eligible_at<=? "
            "ORDER BY p.eligible_at,p.queued_at,p.day,p.cohort,p.chunk LIMIT ?",
            (now, batch),
        ).fetchall()
    buffered: list[Prepared] = []
    charge = 0

    def skip(record: Prepared) -> None:
        db.execute(
            "DELETE FROM market_quality_frame_pending WHERE day=? AND cohort=? AND chunk=?", record.key
        )
        state["oversized_skipped"] = state.get("oversized_skipped", 0) + 1

    def flush(records: list[Prepared]) -> None:
        if not records:
            return
        try:
            publish(db, records)
        except indexed_frame.FrameSizeError:
            if len(records) == 1:
                skip(records[0])
            else:
                middle = len(records) // 2
                flush(records[:middle])
                flush(records[middle:])
            return
        state["frames_created"] = state.get("frames_created", 0) + 1
        state["packets_moved"] = state.get("packets_moved", 0) + len(records)

    visited = 0
    detail_days = db.execute(
        "SELECT json_extract(settings_json,'$.detail_retention_days') FROM owner_settings LIMIT 1"
    ).fetchone()[0]
    if type(detail_days) is not int or detail_days < 0:
        raise StateError("Invalid archive frame retention setting")
    for day, cohort, chunk, size, n in metadata:
        if time.monotonic() >= deadline:
            break
        if size is None:
            raise StateError("Orphan quality frame queue")
        if type(size) is not int or not 0 < size <= quality_packets.MAXIMUM:
            raise StateError("Invalid quality frame source size; data preserved")
        saved = db.execute(
            "SELECT facts FROM market_quality_blocks WHERE day=? AND cohort=? AND chunk=?",
            (day, cohort, chunk),
        ).fetchone()
        if saved is None:
            raise StateError("Changed quality frame source")
        raw = bytes(saved[0])
        record = prepare(db, (day, cohort, chunk, raw))
        if not initial and record is None:
            raise StateError("Unexpected quality frame pending format")
        if raw.startswith(MAGIC) and entry(db, raw)[:3] != [day, cohort, chunk]:
            raise StateError("Conflicting quality frame source coordinate")
        if record is not None:
            # Full chunks are sealed. The current expiry frontier's tail stays
            # CQ5 until the next UTC day, avoiding repeated compression of an
            # actively appended block. Its public facts are already complete.
            eligible = now if n >= 128 else max(now, (day + 1 + detail_days) * 86400000)
            if eligible > now:
                db.execute(
                    "INSERT INTO market_quality_frame_pending VALUES(?,?,?,?,?) "
                    "ON CONFLICT(day,cohort,chunk) DO UPDATE SET eligible_at=min(eligible_at,excluded.eligible_at)",
                    (day, cohort, chunk, now, eligible),
                )
                record = None
        if record is not None:
            cost = len(encode(record.value).encode())
            if buffered and (charge + cost > MAX_EXPANDED - 1024 or buffered[0].key[0] != day):
                flush(buffered)
                buffered = []
                charge = 0
            if cost > MAX_EXPANDED - 1024:
                skip(record)
            else:
                buffered.append(record)
                charge += cost
        visited += 1
        state["rows_examined"] = state.get("rows_examined", 0) + 1
        if initial:
            state["cursor"] = [day, cohort, chunk]
    flush(buffered)
    if (
        initial
        and visited == len(metadata)
        and (len(metadata) < batch or tuple(state["cursor"]) >= tuple(state["upper"]))
    ):
        state["cursor"] = list(state["upper"])
        state["initial_complete"] = True
    if time.monotonic() < deadline:
        quality_packets.collect(db, min(batch, 64) * 2)
