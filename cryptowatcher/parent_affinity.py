"""Bounded native-cohort hints for exact whole-parent packet regrouping.

Hints rank candidates only. Every selected parent's actual values, heads,
ownership and retention are still checked by the existing relocation path.
"""

from __future__ import annotations

import copy
import hashlib
import heapq
import sqlite3
import time
import uuid
from typing import Any

from . import packet_store, record_blocks, record_catalog
from .storage import StateError, encode

DAY = 86_400_000
TABLE = "market_parent_affinity"
INDEX = "CREATE INDEX market_parent_affinity_cohort ON market_parent_affinity(day,fingerprint,parent)"
SIZE = 32
_VALID = """EXISTS(SELECT 1 FROM market_counts WHERE category='parent_affinity'
    AND typeof(records)='integer' AND records>=0 AND records<288230376151711743
    AND logical_bytes=records*32 AND typeof(inserted_bytes)='integer' AND typeof(deleted_bytes)='integer'
    AND inserted_bytes>=0 AND deleted_bytes>=0 AND inserted_bytes-deleted_bytes=logical_bytes)"""


def native(value: dict[str, Any]) -> tuple[Any, ...] | None:
    names = ("market_type", "symbol", "timeframe", "detector_id", "direction", "settings_revision")
    fields = tuple(value.get(name) for name in names)
    if (
        any(not isinstance(v, str) or not 0 < len(v) <= 128 for v in fields[:5])
        or type(fields[5]) is not int
        or not 0 < fields[5] < 2**63
    ):
        return None
    return fields


def fingerprint(fields: tuple[Any, ...]) -> bytes:
    return hashlib.sha256(encode(fields).encode()).digest()[:8]


def source(fields: tuple[Any, ...]) -> tuple[Any, ...]:
    """Grouping only; the stored hint still authenticates the full native key."""
    return fields[:3] + fields[5:]


def installed(db: sqlite3.Connection) -> bool:
    marker = db.execute("SELECT value FROM market_meta WHERE key='parent_affinity'").fetchone()
    found = db.execute("SELECT type FROM sqlite_master WHERE name=?", (TABLE,)).fetchone()
    if marker is None:
        if found is not None:
            raise StateError("Unmarked parent affinity layout; database preserved")
        return False
    if marker[0] != "1" or found is None or found[0] != "table":
        raise StateError("Unknown parent affinity layout; database preserved")
    return True


def _actual(
    db: sqlite3.Connection, parent: int, cache: Any = None
) -> tuple[int, int, tuple[Any, ...] | None] | None:
    row = db.execute(
        "SELECT p.public_uuid,p.expires,r.revision,r.packet,r.slot FROM market_record_parents p "
        "JOIN market_records r ON r.kind=0 AND r.public_uuid=p.public_uuid WHERE p.id=? ORDER BY r.revision LIMIT 1",
        (parent,),
    ).fetchone()
    if row is None:
        return None
    coordinate = record_blocks.Coordinate("event", str(uuid.UUID(bytes=bytes(row[0]))), row[2])
    binding = record_catalog.row(db, coordinate)
    if binding is None or binding[:3] != (row[3], row[4], parent):
        raise StateError("Conflicting parent affinity source owner")
    value = record_blocks.read(packet_store.read(db, row[3]), row[4], coordinate, cache)
    return row[2], row[1] // DAY, native(value)


def install_functions(db: sqlite3.Connection, cache: Any = None) -> None:
    def admit(parent: int, revision: int, day: int, key: bytes) -> int:
        actual = _actual(db, parent, cache)
        return int(
            actual is not None
            and actual[:2] == (revision, day)
            and actual[2] is not None
            and fingerprint(actual[2]) == key
        )

    db.create_function("cw_parent_affinity", 4, admit)


def header(db: sqlite3.Connection) -> bool:
    if not installed(db):
        return False
    definition = db.execute(
        "SELECT sql FROM sqlite_master WHERE name='market_parent_affinity_cohort'"
    ).fetchone()
    if definition is None or " ".join(definition[0].split()) != INDEX:
        raise StateError("Missing parent affinity index; database preserved")
    columns = [row[1] for row in db.execute("PRAGMA table_info(market_parent_affinity)")]
    if columns != ["parent", "revision", "day", "fingerprint"]:
        raise StateError("Unknown parent affinity columns; database preserved")
    triggers = {
        row[0]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (TABLE,))
    }
    if (
        not {"market_parent_affinity_" + name for name in ("insert", "update", "delete", "added", "deleted")}
        <= triggers
    ):
        raise StateError("Missing parent affinity guards; database preserved")
    row = db.execute(
        "SELECT records,logical_bytes,inserted_bytes,deleted_bytes FROM market_counts WHERE category='parent_affinity'"
    ).fetchone()
    if (
        row is None
        or any(type(n) is not int or n < 0 for n in row)
        or row[1] != row[0] * SIZE
        or row[2] - row[3] != row[1]
    ):
        raise StateError("Invalid parent affinity counter; database preserved")
    return True


def install(db: sqlite3.Connection, cache: Any = None) -> None:
    if header(db):
        return
    if db.in_transaction or record_catalog.layout(db) != 3:
        raise StateError("Parent affinity requires an outer catalogue3 boundary")
    install_functions(db, cache)
    try:
        db.executescript(f"""
            BEGIN IMMEDIATE;
            CREATE TABLE market_parent_affinity(
                parent INTEGER PRIMARY KEY REFERENCES market_record_parents(id) ON DELETE CASCADE,
                revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>0),
                day INTEGER NOT NULL CHECK(typeof(day)='integer'),
                fingerprint BLOB NOT NULL CHECK(typeof(fingerprint)='blob' AND length(fingerprint)=8));
            CREATE TRIGGER market_parent_affinity_insert BEFORE INSERT ON market_parent_affinity BEGIN
                SELECT CASE WHEN NOT ({_VALID}) OR cw_parent_affinity(NEW.parent,NEW.revision,NEW.day,NEW.fingerprint)<>1
                    THEN RAISE(ABORT,'Invalid parent affinity source') END;
            END;
            CREATE TRIGGER market_parent_affinity_update BEFORE UPDATE ON market_parent_affinity BEGIN
                SELECT CASE WHEN NOT ({_VALID}) OR NEW.parent<>OLD.parent OR NEW.day<>OLD.day OR NEW.revision>OLD.revision
                    OR cw_parent_affinity(NEW.parent,NEW.revision,NEW.day,NEW.fingerprint)<>1
                    THEN RAISE(ABORT,'Invalid parent affinity change') END;
            END;
            CREATE TRIGGER market_parent_affinity_delete BEFORE DELETE ON market_parent_affinity BEGIN
                SELECT CASE WHEN NOT ({_VALID}) OR (SELECT records FROM market_counts WHERE category='parent_affinity')<1
                    THEN RAISE(ABORT,'Invalid parent affinity counter') END;
            END;
            CREATE TRIGGER market_parent_affinity_added AFTER INSERT ON market_parent_affinity BEGIN
                UPDATE market_counts SET records=records+1,logical_bytes=logical_bytes+32,
                    inserted_bytes=inserted_bytes+32 WHERE category='parent_affinity';
            END;
            CREATE TRIGGER market_parent_affinity_deleted AFTER DELETE ON market_parent_affinity BEGIN
                UPDATE market_counts SET records=records-1,logical_bytes=logical_bytes-32,
                    deleted_bytes=deleted_bytes+32 WHERE category='parent_affinity';
            END;
            INSERT INTO market_counts VALUES('parent_affinity',0,0,0,0);
            INSERT INTO market_meta VALUES('parent_affinity','1');
        """)
        db.execute(INDEX)
        db.commit()
    except BaseException:
        db.rollback()
        raise


def bind(db: sqlite3.Connection, parent: int, revision: int, cache: Any = None) -> None:
    if not installed(db):
        return
    old = db.execute("SELECT revision FROM market_parent_affinity WHERE parent=?", (parent,)).fetchone()
    if old is not None and old[0] <= revision:
        return
    actual = _actual(db, parent, cache)
    if actual is None or actual[2] is None:
        return
    db.execute(
        "INSERT INTO market_parent_affinity VALUES(?,?,?,?) ON CONFLICT(parent) DO UPDATE SET "
        "revision=excluded.revision,day=excluded.day,fingerprint=excluded.fingerprint",
        (parent, actual[0], actual[1], fingerprint(actual[2])),
    )


def candidates(db: sqlite3.Connection, parent: int, upper: int) -> list[int]:
    if not installed(db):
        return []
    hint = db.execute(
        "SELECT day,fingerprint FROM market_parent_affinity WHERE parent=?", (parent,)
    ).fetchone()
    if hint is None:
        return []
    actual = _actual(db, parent)
    if actual is None or actual[2] is None:
        return []
    fields = actual[2]
    from .detector_config import FAMILIES

    keys = {bytes(hint[1])}
    if fields[3] in FAMILIES and fields[4] in ("UP", "DOWN", "NEUTRAL"):
        keys.update(
            fingerprint((*fields[:3], family, direction, fields[5]))
            for family in FAMILIES
            for direction in ("UP", "DOWN", "NEUTRAL")
        )
    # At most15 bounded native prefixes/480 small metadata rows. Capture only
    # the oldest32 peers, preserving the previous whole-parent/memory bounds.
    rows = heapq.nsmallest(
        32,
        {
            row[0]
            for key in keys
            for row in db.execute(
                "SELECT parent FROM market_parent_affinity WHERE day=? AND fingerprint=? AND parent>? ORDER BY parent LIMIT 32",
                (hint[0], key, parent),
            )
        },
    )
    result = []
    for identity in rows:
        owners = db.execute(
            "SELECT p.id,p.bundle_checked FROM market_record_owners o JOIN market_packets p ON p.id=o.packet "
            "WHERE o.parent=? ORDER BY o.packet LIMIT 65",
            (identity,),
        ).fetchall()
        if len(owners) <= 64 and any(packet <= upper and checked == 0 for packet, checked in owners):
            result.append(identity)
    return result


def validate(state: dict[str, Any]) -> None:
    if (
        not isinstance(state, dict)
        or type(state.get("version", 1)) is not int
        or state.get("version", 1) != 1
    ):
        raise StateError("Unknown parent affinity checkpoint")
    fields = ("upper", "cursor", "packet_upper", "packet_cursor")
    if any(field in state for field in fields) and not all(field in state for field in fields):
        raise StateError("Incomplete parent affinity boundary")
    for field in (*fields, "examined", "retagged"):
        if field in state and (type(state[field]) is not int or not 0 <= state[field] < 2**63):
            raise StateError("Invalid parent affinity boundary")
    if "upper" in state and (
        state["cursor"] > state["upper"] or state["packet_cursor"] > state["packet_upper"]
    ):
        raise StateError("Parent affinity cursor exceeds boundary")
    if "initial_complete" in state and (type(state["initial_complete"]) is not bool or "upper" not in state):
        raise StateError("Missing parent affinity initial boundary")
    if state.get("initial_complete") and (
        state["cursor"] != state["upper"] or state["packet_cursor"] != state["packet_upper"]
    ):
        raise StateError("Incomplete parent affinity initial scan")


def advance(
    db: sqlite3.Connection, state: dict[str, Any], batch: int, deadline: float, cache: Any = None
) -> None:
    validate(state)
    if not db.in_transaction or not header(db) or type(batch) is not int or not 0 < batch <= 256:
        raise StateError("Parent affinity work requires a bounded outer writer")
    if state.get("initial_complete") or time.monotonic() >= deadline:
        return
    working = copy.deepcopy(state)
    with packet_store.atomic(db):
        if "upper" not in working:
            parent = db.execute("SELECT id FROM market_record_parents ORDER BY id DESC LIMIT 1").fetchone()
            packet = db.execute("SELECT id FROM market_packets ORDER BY id DESC LIMIT 1").fetchone()
            working.update(
                version=1,
                upper=parent[0] if parent else 0,
                cursor=0,
                packet_upper=packet[0] if packet else 0,
                packet_cursor=0,
            )
        if working["cursor"] < working["upper"]:
            rows = db.execute(
                "SELECT id FROM market_record_parents WHERE id>? AND id<=? ORDER BY id LIMIT ?",
                (working["cursor"], working["upper"], min(batch, 64)),
            ).fetchall()
            for (parent,) in rows:
                if time.monotonic() >= deadline:
                    break
                bind(db, parent, 2**63 - 1, cache)
                working["cursor"] = parent
                working["examined"] = working.get("examined", 0) + 1
            if not rows:
                working["cursor"] = working["upper"]
        elif time.monotonic() < deadline:
            rows = db.execute(
                "SELECT id,bundle_checked FROM market_packets WHERE id>? AND id<=? ORDER BY id LIMIT ?",
                (working["packet_cursor"], working["packet_upper"], min(batch, 256)),
            ).fetchall()
            for identity, checked in rows:
                if time.monotonic() >= deadline:
                    break
                if checked == 1:
                    changed = db.execute(
                        "UPDATE market_packets SET bundle_checked=0,bundle_since=cw_clock_ms() WHERE id=? AND refs>0 "
                        "AND (SELECT category FROM market_packet_arenas WHERE id=market_packets.arena)='detail_packets'",
                        (identity,),
                    ).rowcount
                    working["retagged"] = working.get("retagged", 0) + changed
                working["packet_cursor"] = identity
            if not rows:
                working["packet_cursor"] = working["packet_upper"]
        working["initial_complete"] = (
            working["cursor"] == working["upper"] and working["packet_cursor"] == working["packet_upper"]
        )
    state.clear()
    state.update(working)
