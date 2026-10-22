"""Explicit layout3: finite discovery, live inbox and one resumable staged group.

No installation on open. Compression is prepared elsewhere; this module moves
one already authenticated packet per writer transaction. Pending edges retain
their group while the old DB2 packets remain the record ownership authority.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from typing import Any

from . import indexed_frame, packet_bundles, packet_store, record_blocks
from . import shared_records as base

QUEUE = "shared_record_queue"
PENDING = "shared_record_pending"
RECLAIM = "shared_record_reclaim"
COALESCE_MS = 300_000
SERVICE_MS = 60_000

SCHEMA = """
CREATE TABLE market_shared_scan(id INTEGER PRIMARY KEY CHECK(id=1),
    ceiling INTEGER NOT NULL CHECK(typeof(ceiling)='integer' AND ceiling>=0),
    cursor INTEGER NOT NULL CHECK(typeof(cursor)='integer' AND cursor BETWEEN 0 AND ceiling),
    complete INTEGER NOT NULL CHECK(typeof(complete)='integer' AND complete=(cursor=ceiling)),
    reclaimed INTEGER NOT NULL CHECK(typeof(reclaimed)='integer' AND reclaimed IN(0,1)),
    at_ms INTEGER NOT NULL CHECK(typeof(at_ms)='integer' AND at_ms>0));
CREATE TRIGGER market_shared_scan_change BEFORE UPDATE ON market_shared_scan
    WHEN NEW.id<>OLD.id OR NEW.ceiling<>OLD.ceiling OR NEW.cursor<OLD.cursor OR NEW.reclaimed<OLD.reclaimed
    BEGIN SELECT RAISE(ABORT,'Invalid shared discovery checkpoint'); END;
CREATE TRIGGER market_shared_scan_keep BEFORE DELETE ON market_shared_scan
    BEGIN SELECT RAISE(ABORT,'Retained shared discovery checkpoint'); END;
CREATE TABLE market_shared_queue(packet INTEGER PRIMARY KEY REFERENCES market_packets(id) ON DELETE CASCADE,
    bucket INTEGER NOT NULL CHECK(typeof(bucket)='integer'),
    since INTEGER NOT NULL CHECK(typeof(since)='integer' AND since>0));
CREATE INDEX market_shared_queue_age ON market_shared_queue(since,packet);
CREATE INDEX market_shared_queue_bucket ON market_shared_queue(bucket,packet);
CREATE TRIGGER market_shared_queue_admit BEFORE INSERT ON market_shared_queue BEGIN
    SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM market_packets p JOIN market_packet_arenas a ON a.id=p.arena
        WHERE p.id=NEW.packet AND p.refs>0 AND a.category='detail_packets' AND a.bucket=NEW.bucket)
        OR EXISTS(SELECT 1 FROM market_shared_members WHERE packet=NEW.packet)
        THEN RAISE(ABORT,'Invalid shared inbox source') END; END;
CREATE TRIGGER market_shared_queue_immutable BEFORE UPDATE ON market_shared_queue
    BEGIN SELECT RAISE(ABORT,'Immutable shared inbox entry'); END;
CREATE TRIGGER market_shared_queue_keep BEFORE DELETE ON market_shared_queue
    WHEN EXISTS(SELECT 1 FROM market_packets WHERE id=OLD.packet AND refs>0)
        AND NOT EXISTS(SELECT 1 FROM market_shared_members WHERE packet=OLD.packet)
    BEGIN SELECT RAISE(ABORT,'Retained shared inbox source'); END;
CREATE TABLE market_shared_staging(singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    group_id INTEGER NOT NULL UNIQUE REFERENCES market_shared_records(id));
CREATE TRIGGER market_shared_staging_admit BEFORE INSERT ON market_shared_staging
    WHEN NOT EXISTS(SELECT 1 FROM market_shared_records WHERE id=NEW.group_id AND refs=0)
    BEGIN SELECT RAISE(ABORT,'Invalid shared staged group'); END;
CREATE TRIGGER market_shared_staging_immutable BEFORE UPDATE ON market_shared_staging
    BEGIN SELECT RAISE(ABORT,'Immutable shared staged group'); END;
CREATE TRIGGER market_shared_staging_keep BEFORE DELETE ON market_shared_staging
    WHEN EXISTS(SELECT 1 FROM market_shared_pending WHERE group_id=OLD.group_id)
    BEGIN SELECT RAISE(ABORT,'Retained shared staged group'); END;
CREATE TABLE market_shared_pending(packet INTEGER PRIMARY KEY REFERENCES market_packets(id) ON DELETE CASCADE,
    group_id INTEGER NOT NULL REFERENCES market_shared_records(id),
    first_slot INTEGER NOT NULL CHECK(typeof(first_slot)='integer' AND first_slot BETWEEN 0 AND 2047),
    slots INTEGER NOT NULL CHECK(typeof(slots)='integer' AND slots BETWEEN 1 AND 64),
    digest BLOB NOT NULL CHECK(typeof(digest)='blob' AND length(digest)=32),CHECK(first_slot+slots<=2048));
CREATE INDEX market_shared_pending_group ON market_shared_pending(group_id,first_slot);
CREATE INDEX market_shared_sparse ON market_shared_records(id) WHERE refs>0 AND initial>=refs*2;
CREATE TABLE market_shared_reclaim(group_id INTEGER PRIMARY KEY REFERENCES market_shared_records(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN('garbage','sparse')),since INTEGER NOT NULL CHECK(typeof(since)='integer' AND since>0));
CREATE INDEX market_shared_reclaim_age ON market_shared_reclaim(since,group_id);
CREATE TRIGGER market_shared_reclaim_admit BEFORE INSERT ON market_shared_reclaim
    WHEN NOT EXISTS(SELECT 1 FROM market_shared_records g WHERE g.id=NEW.group_id AND
        ((NEW.kind='garbage' AND g.refs=0) OR (NEW.kind='sparse' AND g.refs>0 AND g.initial>=g.refs*2
            AND NOT EXISTS(SELECT 1 FROM market_shared_staging WHERE group_id=g.id))))
    BEGIN SELECT RAISE(ABORT,'Invalid shared reclaim entry'); END;
CREATE TRIGGER market_shared_reclaim_immutable BEFORE UPDATE ON market_shared_reclaim
    BEGIN SELECT RAISE(ABORT,'Immutable shared reclaim entry'); END;
CREATE TRIGGER market_shared_reclaim_keep BEFORE DELETE ON market_shared_reclaim
    WHEN EXISTS(SELECT 1 FROM market_shared_records g WHERE g.id=OLD.group_id AND
        ((OLD.kind='garbage' AND g.refs=0) OR (OLD.kind='sparse' AND g.refs>0 AND g.initial>=g.refs*2
            AND NOT EXISTS(SELECT 1 FROM market_shared_staging WHERE group_id=g.id))))
    BEGIN SELECT RAISE(ABORT,'Retained shared reclaim entry'); END;
CREATE TRIGGER market_shared_reclaim_new AFTER INSERT ON market_shared_records BEGIN
    INSERT INTO market_shared_reclaim VALUES(NEW.id,'garbage',cw_clock_ms()); END;
CREATE TRIGGER market_shared_reclaim_changed AFTER UPDATE OF refs ON market_shared_records BEGIN
    DELETE FROM market_shared_reclaim WHERE group_id=NEW.id AND
        ((kind='garbage' AND NEW.refs>0) OR (kind='sparse' AND
            (NEW.refs=0 OR NEW.initial<NEW.refs*2 OR EXISTS(SELECT 1 FROM market_shared_staging WHERE group_id=NEW.id))));
    INSERT OR IGNORE INTO market_shared_reclaim SELECT NEW.id,CASE WHEN NEW.refs=0 THEN 'garbage' ELSE 'sparse' END,cw_clock_ms()
        WHERE NEW.refs=0 OR (NEW.initial>=NEW.refs*2 AND NOT EXISTS(SELECT 1 FROM market_shared_staging WHERE group_id=NEW.id)); END;
CREATE TRIGGER market_shared_reclaim_closed AFTER DELETE ON market_shared_staging BEGIN
    INSERT OR IGNORE INTO market_shared_reclaim SELECT id,CASE WHEN refs=0 THEN 'garbage' ELSE 'sparse' END,cw_clock_ms()
        FROM market_shared_records WHERE id=OLD.group_id AND initial>=refs*2; END;
CREATE TRIGGER market_shared_pending_admit BEFORE INSERT ON market_shared_pending BEGIN
    SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM market_shared_staging WHERE group_id=NEW.group_id)
        OR NOT EXISTS(SELECT 1 FROM market_shared_queue WHERE packet=NEW.packet)
        OR EXISTS(SELECT 1 FROM market_shared_pending WHERE group_id=NEW.group_id
            AND first_slot<NEW.first_slot+NEW.slots AND first_slot+slots>NEW.first_slot)
        OR EXISTS(SELECT 1 FROM market_shared_members WHERE group_id=NEW.group_id
            AND first_slot<NEW.first_slot+NEW.slots AND first_slot+slots>NEW.first_slot)
        OR (SELECT count(*) FROM market_shared_pending WHERE group_id=NEW.group_id)
            +(SELECT refs FROM market_shared_records WHERE id=NEW.group_id)>=(SELECT initial FROM market_shared_records WHERE id=NEW.group_id)
        OR cw_shared_pending(NEW.packet,NEW.group_id,NEW.first_slot,NEW.slots,NEW.digest)<>1
        THEN RAISE(ABORT,'Invalid shared pending binding') END; END;
CREATE TRIGGER market_shared_pending_immutable BEFORE UPDATE ON market_shared_pending
    BEGIN SELECT RAISE(ABORT,'Immutable shared pending binding'); END;
CREATE TRIGGER market_shared_pending_keep BEFORE DELETE ON market_shared_pending
    WHEN EXISTS(SELECT 1 FROM market_packets WHERE id=OLD.packet)
        AND NOT EXISTS(SELECT 1 FROM market_shared_members WHERE packet=OLD.packet AND group_id=OLD.group_id
            AND first_slot=OLD.first_slot AND slots=OLD.slots)
    BEGIN SELECT RAISE(ABORT,'Retained shared pending binding'); END;
CREATE TRIGGER market_shared_pending_finished AFTER DELETE ON market_shared_pending BEGIN
    DELETE FROM market_shared_staging WHERE group_id=OLD.group_id
        AND NOT EXISTS(SELECT 1 FROM market_shared_pending WHERE group_id=OLD.group_id); END;
CREATE TRIGGER market_shared_queue_linked AFTER INSERT ON market_shared_members BEGIN
    DELETE FROM market_shared_queue WHERE packet=NEW.packet;
    DELETE FROM market_shared_pending WHERE packet=NEW.packet; END;
CREATE TRIGGER market_shared_queue_new AFTER INSERT ON market_packets
    WHEN NEW.refs>0 AND NOT EXISTS(SELECT 1 FROM market_shared_members WHERE packet=NEW.id)
        AND (SELECT category FROM market_packet_arenas WHERE id=NEW.arena)='detail_packets' BEGIN
    INSERT OR IGNORE INTO market_shared_queue SELECT NEW.id,bucket,cw_clock_ms()
        FROM market_packet_arenas WHERE id=NEW.arena; END;
CREATE TRIGGER market_shared_queue_retained AFTER UPDATE OF refs,bundle_checked ON market_packets
    WHEN NEW.refs>0 AND NOT EXISTS(SELECT 1 FROM market_shared_members WHERE packet=NEW.id)
        AND (SELECT category FROM market_packet_arenas WHERE id=NEW.arena)='detail_packets' BEGIN
    INSERT OR IGNORE INTO market_shared_queue SELECT NEW.id,bucket,cw_clock_ms()
        FROM market_packet_arenas WHERE id=NEW.arena; END;
"""


def _counts(table: str, category: str, size: int) -> str:
    return f"""
CREATE TRIGGER {table}_counter_keep BEFORE DELETE ON {table} BEGIN
    SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM market_counts WHERE category='{category}'
        AND records>0 AND logical_bytes>={size}) THEN RAISE(ABORT,'Missing shared work counter') END; END;
CREATE TRIGGER {table}_counter_added AFTER INSERT ON {table} BEGIN
    UPDATE market_counts SET records=records+1,logical_bytes=logical_bytes+{size},inserted_bytes=inserted_bytes+{size}
        WHERE category='{category}';
    SELECT CASE WHEN changes()<>1 THEN RAISE(ABORT,'Missing shared work counter') END; END;
CREATE TRIGGER {table}_counter_removed AFTER DELETE ON {table} BEGIN
    UPDATE market_counts SET records=records-1,logical_bytes=logical_bytes-{size},deleted_bytes=deleted_bytes+{size}
        WHERE category='{category}'; END;
"""


def _statements() -> list[str]:
    return base._statements(
        SCHEMA
        + _counts("market_shared_queue", QUEUE, 24)
        + _counts("market_shared_pending", PENDING, 64)
        + _counts("market_shared_reclaim", RECLAIM, 24)
    )


def _keep(sql: str) -> str:
    needle = "OR NOT EXISTS(SELECT 1 FROM market_counts WHERE category='shared_record_groups'"
    if sql.count(needle) != 1:
        raise ValueError("Unknown shared group retention guard")
    return sql.replace(
        needle,
        "OR EXISTS(SELECT 1 FROM market_shared_pending WHERE group_id=OLD.id) "
        "OR EXISTS(SELECT 1 FROM market_shared_staging WHERE group_id=OLD.id) " + needle,
    )


def _tail(sql: str) -> str:
    needle = "used>1048528"
    if sql.count(needle) != 1:
        raise ValueError("Unknown shared descriptor tail guard")
    return sql.replace(needle, "(used>1048528 OR (used>786432 AND live_bytes*4<used*3 AND live_packets>0))")


def install_functions(db: sqlite3.Connection) -> None:
    def pending(packet: int, group: int, first: int, count: int, digest: bytes) -> int:
        row = base._group(db, group)
        binding = base._binding(db, packet)
        raw = packet_store.read(db, packet)
        if (
            binding[5:] != tuple(row[:2])
            or not raw.startswith(record_blocks.MAGIC)
            or hashlib.sha256(raw).digest() != digest
        ):
            return 0
        projection, body = record_blocks.frames(raw)
        if (
            len(indexed_frame.layout(projection)[0]) - 1 != count
            or len(indexed_frame.layout(body)[0]) - 1 != count
        ):
            return 0
        base.descriptor(group, first, count, row[4])
        return int(first + count < len(base.shared_body_frame.layout(row[2])[0]))

    db.create_function("cw_shared_pending", 5, pending)


def enabled(db: sqlite3.Connection) -> bool:
    marker = db.execute("SELECT value FROM market_meta WHERE key='shared_records'").fetchone()
    if marker is not None and marker[0] not in ("2", "3"):
        raise ValueError("Unknown shared work layout")
    return marker is not None and marker[0] == "3"


def validate(db: sqlite3.Connection) -> None:
    if not enabled(db):
        raise ValueError("Missing explicit shared work layout")
    for sql in [*base._statements(), base.MOVE_GUARD, *_statements()]:
        name = sql.split()[2].split("(")[0]
        if name == "market_shared_keep":
            sql = _keep(sql)
        elif name == "market_shared_tail_change":
            sql = _tail(sql)
        row = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if row is None or " ".join(row[0].split()) != " ".join(sql.split()):
            raise ValueError("Missing or unknown shared work guard")
    header(db)


def install(db: sqlite3.Connection, now: int, cache: Any = None) -> None:
    """Explicit known2->3 extension; never invoked by Store or the worker."""
    if type(now) is not int or not 0 < now < 2**63 or db.in_transaction:
        raise ValueError("Shared work installation requires an outer boundary and clock")
    base.install_functions(db, cache)
    base.validate(db)
    packet_bundles.validate(db)
    if enabled(db):
        return
    with db:
        db.execute("BEGIN IMMEDIATE")
        base.validate(db)
        for sql in _statements():
            db.execute(sql)
        for name, transform in (("market_shared_keep", _keep), ("market_shared_tail_change", _tail)):
            old = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
            db.execute("DROP TRIGGER " + name)
            db.execute(transform(old[0]))
        for category in (QUEUE, PENDING, RECLAIM):
            db.execute("INSERT INTO market_counts VALUES(?,0,0,0,0)", (category,))
        # Layout2 may already contain partially released or unowned groups.
        # Start their cleanup clock at this explicit upgrade boundary; never
        # infer an earlier timestamp or let a missing inbox hide old work.
        db.execute(
            "INSERT INTO market_shared_reclaim SELECT id,CASE WHEN refs=0 THEN 'garbage' ELSE 'sparse' END,? "
            "FROM market_shared_records WHERE initial>=refs*2",
            (now,),
        )
        ceiling = db.execute("SELECT COALESCE(MAX(id),0) FROM market_packets").fetchone()[0]
        db.execute("INSERT INTO market_shared_scan VALUES(1,?,0,?,0,?)", (ceiling, int(ceiling == 0), now))
        db.execute("INSERT INTO market_meta VALUES('shared_record_writer','1')")
        if (
            db.execute("UPDATE market_meta SET value='3' WHERE key='shared_records' AND value='2'").rowcount
            != 1
        ):
            raise ValueError("Changed shared work installation marker")
        validate(db)


def header(db: sqlite3.Connection) -> dict[str, Any]:
    row = db.execute(
        "SELECT ceiling,cursor,complete,reclaimed,at_ms FROM market_shared_scan WHERE id=1"
    ).fetchone()
    if (
        row is None
        or any(type(n) is not int for n in row)
        or not 0 <= row[1] <= row[0] < 2**63
        or row[2] != int(row[1] == row[0])
        or row[3] not in (0, 1)
        or not 0 < row[4] < 2**63
    ):
        raise ValueError("Invalid shared work checkpoint")
    counts = {}
    for category in (QUEUE, PENDING, RECLAIM):
        saved = db.execute(
            "SELECT records,logical_bytes FROM market_counts WHERE category=?", (category,)
        ).fetchone()
        charge = 64 if category == PENDING else 24
        if saved is None or type(saved[0]) is not int or saved[0] < 0 or saved[1] != saved[0] * charge:
            raise ValueError("Invalid shared work counter")
        counts[category] = saved[0]
    oldest = db.execute("SELECT since FROM market_shared_queue ORDER BY since,packet LIMIT 1").fetchone()
    reclaim = db.execute(
        "SELECT since FROM market_shared_reclaim WHERE NOT EXISTS(SELECT 1 FROM market_shared_staging WHERE group_id=market_shared_reclaim.group_id) ORDER BY since,group_id LIMIT 1"
    ).fetchone()
    garbage = (
        db.execute(
            "SELECT 1 FROM market_shared_records WHERE refs=0 AND NOT EXISTS(SELECT 1 FROM market_shared_staging WHERE group_id=market_shared_records.id) ORDER BY id LIMIT 1"
        ).fetchone()
        is not None
    )
    writer = db.execute("SELECT value FROM market_meta WHERE key='shared_record_writer'").fetchone()
    if writer is None or writer[0] not in ("0", "1"):
        raise ValueError("Unknown shared writer switch")
    initial_pending = (
        db.execute("SELECT 1 FROM market_shared_queue WHERE packet<=? LIMIT 1", (row[0],)).fetchone()
        is not None
    )
    return dict(
        ceiling=row[0],
        cursor=row[1],
        initial_scan_complete=bool(row[2]),
        initial_complete=bool(row[2]) and not initial_pending,
        initial_reclaim_complete=bool(row[3]) and bool(row[2]) and not initial_pending,
        writer_enabled=writer[0] == "1",
        at_ms=row[4],
        queued=counts[QUEUE],
        pending=counts[PENDING],
        oldest_ms=oldest[0] if oldest else None,
        sparse_pending=sparse(db) is not None,
        garbage_pending=garbage,
        reclaim_queued=counts[RECLAIM],
        reclaim_oldest_ms=reclaim[0] if reclaim else None,
    )


def sparse(db: sqlite3.Connection) -> int | None:
    group = db.execute(
        "SELECT id FROM market_shared_records INDEXED BY market_shared_sparse WHERE refs>0 AND initial>=refs*2 "
        "AND NOT EXISTS(SELECT 1 FROM market_shared_staging WHERE group_id=market_shared_records.id) "
        "ORDER BY id LIMIT 1"
    ).fetchone()
    if group is None:
        return None
    row = db.execute(
        "SELECT packet FROM market_shared_members WHERE group_id=? ORDER BY first_slot LIMIT 1", (group[0],)
    ).fetchone()
    if row is None:
        raise ValueError("Missing sparse shared group member")
    return row[0]


def scan(db: sqlite3.Connection, now: int) -> None:
    if not db.in_transaction or type(now) is not int or not 0 < now < 2**63:
        raise ValueError("Shared discovery requires a writer transaction and clock")
    state = header(db)
    if state["initial_scan_complete"]:
        return
    rows = db.execute(
        "SELECT p.id,p.refs,a.category,a.bucket FROM market_packets p JOIN market_packet_arenas a ON a.id=p.arena "
        "WHERE p.id>? AND p.id<=? ORDER BY p.id LIMIT 64",
        (state["cursor"], state["ceiling"]),
    ).fetchall()
    for packet, refs, category, bucket in rows:
        if (
            refs > 0
            and category == "detail_packets"
            and db.execute("SELECT 1 FROM market_shared_members WHERE packet=?", (packet,)).fetchone() is None
        ):
            db.execute("INSERT OR IGNORE INTO market_shared_queue VALUES(?,?,?)", (packet, bucket, now))
    cursor = rows[-1][0] if len(rows) == 64 else state["ceiling"]
    db.execute(
        "UPDATE market_shared_scan SET cursor=?,complete=?,at_ms=? WHERE id=1",
        (cursor, int(cursor == state["ceiling"]), now),
    )


def stage(db: sqlite3.Connection, prepared: base.Prepared) -> int | None:
    """Publish immutable group and bounded pending edges; keep all old packets."""
    if not isinstance(prepared, base.Prepared) or not 0 < len(prepared.members) <= 32:
        raise ValueError("Invalid staged shared record group")
    with packet_store.atomic(db):
        if db.execute("SELECT 1 FROM market_shared_staging LIMIT 1").fetchone():
            return None
        for member in prepared.members:
            row = db.execute(
                "SELECT arena,offset,length,sha256,refs FROM market_packets WHERE id=?", (member.identity,)
            ).fetchone()
            if (
                row is None
                or tuple(row) != member.binding[:5]
                or db.execute(
                    "SELECT 1 FROM market_shared_queue WHERE packet=?", (member.identity,)
                ).fetchone()
                is None
            ):
                return None
            if packet_store.read(db, member.identity) != member.raw:
                raise ValueError("Shared stage changed immutable source bytes")
        category, bucket = prepared.members[0].binding[5:]
        digest = base.signature(category, bucket, prepared.body, prepared.projection, len(prepared.members))
        cursor = db.execute(
            "INSERT INTO market_shared_records(category,bucket,body,projection,digest,initial) VALUES(?,?,?,?,?,?)",
            (category, bucket, prepared.body, prepared.projection, digest, len(prepared.members)),
        )
        group = int(cursor.lastrowid or 0)
        db.execute("INSERT INTO market_shared_staging VALUES(1,?)", (group,))
        first = 0
        for member in prepared.members:
            db.execute(
                "INSERT INTO market_shared_pending VALUES(?,?,?,?,?)",
                (member.identity, group, first, member.count, hashlib.sha256(member.raw).digest()),
            )
            first += member.count
        if first != len(base.shared_body_frame.layout(prepared.body)[0]) - 1:
            raise ValueError("Shared staged cardinality mismatch")
        return group


def advance(db: sqlite3.Connection, cache: Any = None) -> int | None:
    """One full checked transition and its inbox/pending release are atomic."""
    with packet_store.atomic(db):
        row = db.execute(
            "SELECT q.packet,q.group_id,q.first_slot,q.slots,q.digest FROM market_shared_pending q "
            "JOIN market_packets p ON p.id=q.packet WHERE p.refs>0 ORDER BY q.packet LIMIT 1"
        ).fetchone()
        if row is None:
            db.execute(
                "DELETE FROM market_shared_staging WHERE NOT EXISTS(SELECT 1 FROM market_shared_pending WHERE group_id=market_shared_staging.group_id)"
            )
            return None
        packet, group, first, count, fingerprint = row
        binding = base._binding(db, packet)
        raw = packet_store.read(db, packet)
        if hashlib.sha256(raw).digest() != fingerprint:
            raise ValueError("Changed shared pending source bytes")
        saved = base._group(db, group)
        base.publish_member(db, base.Member(packet, binding, raw, count), group, first, saved[4], cache)
        return packet


def select(db: sqlite3.Connection, now: int) -> list[int]:
    """Inspect bounded indexed candidate prefixes, never the whole live inbox."""
    if not db.in_transaction or type(now) is not int or not 0 < now < 2**63:
        raise ValueError("Shared selection requires a read snapshot and clock")
    state = header(db)
    if not state["initial_scan_complete"]:
        return []
    if state["pending"] or db.execute("SELECT 1 FROM market_shared_staging LIMIT 1").fetchone():
        return []
    initial = not state["initial_complete"]
    condition = " WHERE packet<=?" if initial else ""
    order = "packet" if initial else "since,packet"
    rows = db.execute(
        "SELECT packet,bucket,since FROM market_shared_queue" + condition + f" ORDER BY {order} LIMIT 64",
        (state["ceiling"],) if initial else (),
    ).fetchall()

    def eligible(items: Any) -> set[int]:
        if not items:
            return set()
        return {
            r[0]
            for r in db.execute(
                "SELECT id FROM market_packets WHERE refs>0 AND bundle_checked=1 AND id IN("
                + ",".join("?" for _ in items)
                + ")",
                [r[0] for r in items],
            )
        }

    known = eligible(rows)
    seed = next((r for r in rows if r[0] in known), None)
    if seed is None:
        return []
    rows = db.execute(
        "SELECT packet,bucket,since FROM market_shared_queue WHERE bucket=?"
        + (" AND packet<=?" if initial else "")
        + " ORDER BY packet LIMIT 64",
        (seed[1], state["ceiling"]) if initial else (seed[1],),
    ).fetchall()
    known = eligible(rows)
    selected = [r[0] for r in rows if r[0] in known][:32]
    if not initial and len(selected) < 32 and now - min(r[2] for r in rows) < COALESCE_MS:
        return []
    return selected


def status(db: sqlite3.Connection, now: int) -> dict[str, Any]:
    marker = db.execute("SELECT value FROM market_meta WHERE key='shared_records'").fetchone()
    if marker is None:
        return {"installed": False, "work_installed": False, "pending_work": False}
    if marker[0] != "3":
        if marker[0] != "2":
            raise ValueError("Unknown shared record layout")
        return {"installed": True, "version": "2", "work_installed": False, "pending_work": True}
    value = header(db)
    age = now - value["oldest_ms"] if value["oldest_ms"] is not None else None
    reclaim_age = now - value["reclaim_oldest_ms"] if value["reclaim_oldest_ms"] is not None else None
    clock_unknown = age is not None and age < -30000 or now - value["at_ms"] < -30000
    checkpoint_stale = now - value["at_ms"] > 60000
    checkpoint_unknown = (value["sparse_pending"] or value["garbage_pending"]) and reclaim_age is None
    clock_unknown = clock_unknown or reclaim_age is not None and reclaim_age < -30000
    return {
        **value,
        "installed": True,
        "version": "3",
        "work_installed": True,
        "oldest_age_ms": age,
        "reclaim_oldest_age_ms": reclaim_age,
        "clock_unknown": clock_unknown,
        "checkpoint_stale": checkpoint_stale,
        "checkpoint_unknown": checkpoint_unknown,
        "live_deadline_ms": COALESCE_MS + SERVICE_MS,
        "pending_work": not value["initial_reclaim_complete"]
        or not value["writer_enabled"]
        or clock_unknown
        or checkpoint_stale
        or checkpoint_unknown
        or reclaim_age is not None
        and reclaim_age > SERVICE_MS
        or age is not None
        and age > COALESCE_MS + SERVICE_MS,
    }


def verify(db: sqlite3.Connection, deadline: float) -> dict[str, Any]:
    """Full counts/ownership and actual DS2 bytes in the caller's pinned snapshot.

    At most one existing1MiB arena is materialized. Full group CHECK/integrity
    and complete public value restoration remain separate qualification checks.
    """
    if not enabled(db):
        return {"state": "N/A_NOT_INSTALLED"}
    if not db.in_transaction:
        raise ValueError("Shared boundary requires a pinned read snapshot")
    if time.monotonic() >= deadline:
        return {"state": "PARTIAL_BUDGET", "packets_checked": 0}
    validate(db)
    for table, category, expression in (
        ("market_shared_records", base.GROUPS, "length(body)+length(projection)"),
        ("market_shared_members", base.MEMBERS, "24"),
        ("market_shared_queue", QUEUE, "24"),
        ("market_shared_pending", PENDING, "64"),
        ("market_shared_reclaim", RECLAIM, "24"),
    ):
        actual = db.execute(f"SELECT count(*),coalesce(sum({expression}),0) FROM {table}").fetchone()
        counter = db.execute(
            "SELECT records,logical_bytes FROM market_counts WHERE category=?", (category,)
        ).fetchone()
        if counter is None and tuple(actual) == (0, 0):
            continue
        if counter is None or tuple(actual) != tuple(counter):
            raise ValueError("Shared record counter mismatch")
    invalid = (
        "SELECT 1 FROM market_shared_records g WHERE refs<>(SELECT count(*) FROM market_shared_members WHERE group_id=g.id) LIMIT 1",
        "SELECT 1 FROM market_shared_members m LEFT JOIN market_packets p ON p.id=m.packet LEFT JOIN market_packet_arenas a ON a.id=p.arena "
        "LEFT JOIN market_shared_records g ON g.id=m.group_id WHERE p.id IS NULL OR g.id IS NULL OR a.id IS NULL OR a.category<>g.category OR a.bucket<>g.bucket OR p.length<>48 LIMIT 1",
        "SELECT 1 FROM (SELECT first_slot,lag(first_slot+slots) OVER(PARTITION BY group_id ORDER BY first_slot) AS previous FROM market_shared_members) WHERE first_slot<previous LIMIT 1",
        "SELECT 1 FROM market_shared_pending q LEFT JOIN market_shared_staging s ON s.group_id=q.group_id LEFT JOIN market_packets p ON p.id=q.packet "
        "WHERE s.group_id IS NULL OR p.id IS NULL OR q.digest<>p.sha256 LIMIT 1",
        "SELECT 1 FROM market_shared_reclaim q LEFT JOIN market_shared_records g ON g.id=q.group_id "
        "WHERE g.id IS NULL OR (q.kind='garbage' AND g.refs<>0) OR (q.kind='sparse' AND "
        "(g.refs=0 OR g.initial<g.refs*2 OR EXISTS(SELECT 1 FROM market_shared_staging WHERE group_id=g.id))) LIMIT 1",
    )
    if any(db.execute(sql).fetchone() is not None for sql in invalid):
        raise ValueError("Conflicting shared record ownership")
    checked = 0
    for arena, used in db.execute(
        "SELECT a.id,a.used FROM market_packet_arenas a WHERE EXISTS(SELECT 1 FROM market_packets p "
        "JOIN market_shared_members m ON m.packet=p.id WHERE p.arena=a.id) ORDER BY a.id"
    ):
        if time.monotonic() >= deadline:
            return {"state": "PARTIAL_BUDGET", "packets_checked": checked}
        with db.blobopen("market_packet_arenas", "payload", arena, readonly=True) as blob:
            raw = blob.read(packet_store.CAPACITY)
        if len(raw) != packet_store.CAPACITY or not 0 <= used <= len(raw):
            raise ValueError("Invalid shared descriptor arena")
        for offset, length, fingerprint, group, first, count, digest in db.execute(
            "SELECT p.offset,p.length,p.sha256,m.group_id,m.first_slot,m.slots,g.digest FROM market_packets p "
            "JOIN market_shared_members m ON m.packet=p.id JOIN market_shared_records g ON g.id=m.group_id "
            "WHERE p.arena=? ORDER BY p.id",
            (arena,),
        ):
            if checked % 256 == 0 and time.monotonic() >= deadline:
                return {"state": "PARTIAL_BUDGET", "packets_checked": checked}
            expected = base.descriptor(group, first, count, digest)
            if (
                type(offset) is not int
                or offset < 0
                or length != 48
                or offset + length > used
                or raw[offset : offset + length] != expected
                or hashlib.sha256(expected).digest() != fingerprint
            ):
                raise ValueError("Corrupt shared descriptor bytes or binding")
            checked += 1
    if time.monotonic() >= deadline:
        return {"state": "PARTIAL_BUDGET", "packets_checked": checked}
    return {
        "state": "MEASURED",
        "packets_checked": checked,
        "scope": "full counters, group/member ownership, pending bindings, actual DS2 bytes; full CHECK/integrity and public values are separate",
    }
