"""Explicit shared record storage; no automatic installation or runtime work.

Physical packet IDs/refs remain the ownership authority. DS2 descriptors bind
immutable RP1/SB1 groups; only a byte-exact logical transition may resize an old
DB2 packet. All publication runs in the caller's existing writer transaction.
"""

from __future__ import annotations

import hashlib
import sqlite3
import struct
from dataclasses import dataclass
from typing import Any

from . import indexed_frame, packet_store, projection_recipes, record_blocks, shared_body_frame
from .compact_json import pack as typed_pack

MAGIC = b"DS2\0"
GROUPS = "shared_record_groups"
MEMBERS = "shared_record_members"


def signature(category: str, bucket: int, body: bytes, projection: bytes, initial: int) -> bytes:
    if (
        category != "detail_packets"
        or type(bucket) is not int
        or not -(2**63) <= bucket < 2**63
        or type(initial) is not int
        or not 0 < initial <= 32
        or not isinstance(body, bytes)
        or not isinstance(projection, bytes)
        or not 0 < len(body) <= shared_body_frame.MAX_COMPRESSED
        or not 0 < len(projection) <= shared_body_frame.MAX_COMPRESSED
    ):
        raise ValueError("Invalid shared record group bounds")
    if len(shared_body_frame.layout(body)[0]) != len(projection_recipes.layout(projection)[0]):
        raise ValueError("Shared record group cardinality mismatch")
    return hashlib.sha256(
        struct.pack(">qHII", bucket, initial, len(body), len(projection)) + body + projection
    ).digest()


def descriptor(group: int, first: int, count: int, digest: bytes) -> bytes:
    if (
        type(group) is not int
        or not 0 < group < 2**63
        or type(first) is not int
        or type(count) is not int
        or not 0 <= first < first + count <= shared_body_frame.MAX_RECORDS
        or not 0 < count <= record_blocks.MAX_RECORDS
        or not isinstance(digest, bytes)
        or len(digest) != 32
    ):
        raise ValueError("Invalid shared record descriptor")
    return MAGIC + struct.pack(">QHH", group, first, count) + digest


def _descriptor(raw: bytes) -> tuple[int, int, int, bytes]:
    if not isinstance(raw, bytes) or len(raw) != 48 or not raw.startswith(MAGIC):
        raise ValueError("Invalid shared record descriptor version or length")
    group, first, count = struct.unpack(">QHH", raw[4:16])
    if descriptor(group, first, count, raw[16:]) != raw:
        raise ValueError("Invalid shared record descriptor bytes")
    return group, first, count, raw[16:]


def sql_signature(*values: Any) -> bytes:
    if len(values) != 5:
        return b""
    try:
        return signature(*values)
    except ValueError:
        # Invalid stored CHECK operands must produce a failed constraint in
        # stdlib/APSW/native validation, rather than conceal it in a callback.
        return b""


def _group(db: sqlite3.Connection, identity: int) -> Any:
    row = db.execute(
        "SELECT category,bucket,body,projection,digest,initial,refs FROM market_shared_records WHERE id=? "
        "AND (SELECT value FROM market_meta WHERE key='shared_records') IN('2','3')",
        (identity,),
    ).fetchone()
    if row is None or signature(row[0], row[1], row[2], row[3], row[5]) != row[4]:
        raise ValueError("Missing or corrupt shared record group")
    return row


def _logical(row: Any, first: int, count: int, cache: Any) -> bytes:
    projection = projection_recipes.restore(row[3], first, count, cache)
    return record_blocks.shared_packet(projection, row[2], first, count)


def expand(db: sqlite3.Connection, identity: int, raw: bytes, cache: Any = None) -> bytes:
    group, first, count, digest = _descriptor(raw)
    binding = db.execute(
        "SELECT m.group_id,m.first_slot,m.slots,a.category,a.bucket FROM market_shared_members m "
        "JOIN market_packets p ON p.id=m.packet JOIN market_packet_arenas a ON a.id=p.arena WHERE m.packet=?",
        (identity,),
    ).fetchone()
    row = _group(db, group)
    if (
        binding is None
        or tuple(binding) != (group, first, count, row[0], row[1])
        or digest != row[4]
        or not 0 < row[6] <= row[5]
        or db.execute("SELECT COUNT(*) FROM market_shared_members WHERE group_id=?", (group,)).fetchone()[0]
        != row[6]
    ):
        raise ValueError("Shared record descriptor binding mismatch")
    return _logical(row, first, count, cache)


def _equal(old: bytes, new: bytes, cache: Any = None) -> bool:
    if not old.startswith(record_blocks.MAGIC):
        return False
    record_blocks.projections(old)
    record_blocks.projections(new)
    old_p, old_b = record_blocks.frames(old)
    new_p, new_b = record_blocks.frames(new)
    return [typed_pack(r) for r in indexed_frame.all_values(old_p)] == [
        typed_pack(r) for r in indexed_frame.all_values(new_p)
    ] and record_blocks._body_parts(old_b) == shared_body_frame.parts(new_b, cache)


def install_functions(db: sqlite3.Connection, cache: Any = None) -> None:
    def admission(packet: int, group: int, first: int, count: int) -> int:
        row = _group(db, group)
        current = db.execute(
            "SELECT a.category,a.bucket,p.refs FROM market_packets p "
            "JOIN market_packet_arenas a ON a.id=p.arena WHERE p.id=?",
            (packet,),
        ).fetchone()
        if current is None or tuple(current[:2]) != tuple(row[:2]) or current[2] <= 0:
            return 0
        return int(_equal(packet_store.read(db, packet), _logical(row, first, count, cache), cache))

    def transition(
        packet: int,
        old_arena: int,
        old_offset: int,
        old_length: int,
        new_arena: int,
        new_offset: int,
        new_length: int,
    ) -> int:
        old = packet_store.segment(db, old_arena, old_offset, old_length)
        new = packet_store.segment(db, new_arena, new_offset, new_length)
        return int(_equal(old, expand(db, packet, new, cache), cache))

    db.create_function("cw_shared_digest", 5, sql_signature, deterministic=True)
    db.create_function("cw_shared_admit", 4, admission)
    db.create_function("cw_shared_transition", 7, transition)
    from .shared_record_work import install_functions as work_functions

    work_functions(db)


MOVE_GUARD = """CREATE TRIGGER market_packet_move BEFORE UPDATE OF arena,offset,length,sha256 ON market_packets
    WHEN (SELECT category FROM market_packet_arenas WHERE id=NEW.arena) IS NOT
            (SELECT category FROM market_packet_arenas WHERE id=OLD.arena) OR
        (SELECT bucket FROM market_packet_arenas WHERE id=NEW.arena) IS NOT
            (SELECT bucket FROM market_packet_arenas WHERE id=OLD.arena) OR
        cw_packet_digest(OLD.arena,OLD.offset,OLD.length)<>OLD.sha256 OR
        cw_packet_digest(NEW.arena,NEW.offset,NEW.length)<>NEW.sha256 OR
        ((NEW.length<>OLD.length OR NEW.sha256<>OLD.sha256) AND
            cw_shared_transition(OLD.id,OLD.arena,OLD.offset,OLD.length,NEW.arena,NEW.offset,NEW.length)<>1)
    BEGIN SELECT RAISE(ABORT,'Packet relocation changed logical values'); END"""

SCHEMA = """
CREATE TABLE market_shared_records(
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(id>0),category TEXT NOT NULL CHECK(category='detail_packets'),
    bucket INTEGER NOT NULL CHECK(typeof(bucket)='integer'),body BLOB NOT NULL,projection BLOB NOT NULL,
    digest BLOB NOT NULL CHECK(typeof(digest)='blob' AND length(digest)=32),
    initial INTEGER NOT NULL CHECK(typeof(initial)='integer' AND initial BETWEEN 1 AND 32),
    refs INTEGER NOT NULL DEFAULT 0 CHECK(typeof(refs)='integer' AND refs BETWEEN 0 AND initial),
    CHECK(typeof(body)='blob' AND length(body) BETWEEN 1 AND 524288),
    CHECK(typeof(projection)='blob' AND length(projection) BETWEEN 1 AND 524288),
    CHECK(digest=cw_shared_digest(category,bucket,body,projection,initial)));
CREATE INDEX market_shared_empty ON market_shared_records(id) WHERE refs=0;
CREATE INDEX market_shared_bucket ON market_shared_records(category,bucket,id);
CREATE TABLE market_shared_tails(bucket INTEGER PRIMARY KEY,arena INTEGER NOT NULL UNIQUE
    REFERENCES market_packet_arenas(id) ON DELETE CASCADE);
CREATE TRIGGER market_shared_tail_admit BEFORE INSERT ON market_shared_tails BEGIN
    SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM market_packet_arenas WHERE id=NEW.arena
        AND category='detail_packets' AND bucket=NEW.bucket AND used=0 AND live_packets=0)
        THEN RAISE(ABORT,'Invalid shared descriptor tail') END; END;
CREATE TRIGGER market_shared_tail_change BEFORE UPDATE ON market_shared_tails BEGIN
    SELECT CASE WHEN NEW.bucket<>OLD.bucket OR NOT EXISTS(SELECT 1 FROM market_packet_arenas WHERE id=OLD.arena
        AND used>1048528) OR NOT EXISTS(SELECT 1 FROM market_packet_arenas WHERE id=NEW.arena
        AND category='detail_packets' AND bucket=NEW.bucket AND used=0 AND live_packets=0)
        THEN RAISE(ABORT,'Invalid shared descriptor tail change') END; END;
CREATE TRIGGER market_shared_tail_keep BEFORE DELETE ON market_shared_tails
    WHEN EXISTS(SELECT 1 FROM market_packet_arenas WHERE id=OLD.arena)
    BEGIN SELECT RAISE(ABORT,'Retained shared descriptor tail'); END;
CREATE TABLE market_shared_members(
    packet INTEGER PRIMARY KEY REFERENCES market_packets(id) ON DELETE CASCADE,
    group_id INTEGER NOT NULL REFERENCES market_shared_records(id),
    first_slot INTEGER NOT NULL CHECK(typeof(first_slot)='integer' AND first_slot BETWEEN 0 AND 2047),
    slots INTEGER NOT NULL CHECK(typeof(slots)='integer' AND slots BETWEEN 1 AND 64),
    CHECK(first_slot+slots<=2048));
CREATE INDEX market_shared_member_group ON market_shared_members(group_id,first_slot);
CREATE TRIGGER market_shared_admit BEFORE INSERT ON market_shared_records BEGIN
    SELECT CASE WHEN NEW.refs<>0 OR cw_shared_digest(NEW.category,NEW.bucket,NEW.body,NEW.projection,NEW.initial)<>NEW.digest
        THEN RAISE(ABORT,'Invalid shared record group') END; END;
CREATE TRIGGER market_shared_immutable BEFORE UPDATE OF id,category,bucket,body,projection,digest,initial
    ON market_shared_records BEGIN SELECT RAISE(ABORT,'Immutable shared record group'); END;
CREATE TRIGGER market_shared_refs BEFORE UPDATE OF refs ON market_shared_records BEGIN
    SELECT CASE WHEN NEW.refs<>(SELECT COUNT(*) FROM market_shared_members WHERE group_id=OLD.id)
        THEN RAISE(ABORT,'Invalid shared record references') END; END;
CREATE TRIGGER market_shared_keep BEFORE DELETE ON market_shared_records BEGIN
    SELECT CASE WHEN OLD.refs<>0 OR EXISTS(SELECT 1 FROM market_shared_members WHERE group_id=OLD.id)
        OR NOT EXISTS(SELECT 1 FROM market_counts WHERE category='shared_record_groups'
            AND records>0 AND logical_bytes>=length(OLD.body)+length(OLD.projection))
        THEN RAISE(ABORT,'Retained shared record group or missing counter') END; END;
CREATE TRIGGER market_shared_added AFTER INSERT ON market_shared_records BEGIN
    INSERT INTO market_counts VALUES('shared_record_groups',1,length(NEW.body)+length(NEW.projection),length(NEW.body)+length(NEW.projection),0)
    ON CONFLICT(category) DO UPDATE SET records=records+1,logical_bytes=logical_bytes+excluded.logical_bytes,
        inserted_bytes=inserted_bytes+excluded.inserted_bytes; END;
CREATE TRIGGER market_shared_removed AFTER DELETE ON market_shared_records BEGIN
    UPDATE market_counts SET records=records-1,logical_bytes=logical_bytes-length(OLD.body)-length(OLD.projection),
        deleted_bytes=deleted_bytes+length(OLD.body)+length(OLD.projection) WHERE category='shared_record_groups'; END;
CREATE TRIGGER market_shared_member_admit BEFORE INSERT ON market_shared_members BEGIN
    SELECT CASE WHEN EXISTS(SELECT 1 FROM market_shared_members WHERE group_id=NEW.group_id
        AND first_slot<NEW.first_slot+NEW.slots AND first_slot+slots>NEW.first_slot)
        OR cw_shared_admit(NEW.packet,NEW.group_id,NEW.first_slot,NEW.slots)<>1
        THEN RAISE(ABORT,'Shared member changed immutable values') END; END;
CREATE TRIGGER market_shared_member_immutable BEFORE UPDATE ON market_shared_members
    BEGIN SELECT RAISE(ABORT,'Immutable shared member'); END;
CREATE TRIGGER market_shared_member_keep BEFORE DELETE ON market_shared_members BEGIN
    SELECT CASE WHEN EXISTS(SELECT 1 FROM market_packets WHERE id=OLD.packet)
        OR NOT EXISTS(SELECT 1 FROM market_counts WHERE category='shared_record_members' AND records>0 AND logical_bytes>=24)
        THEN RAISE(ABORT,'Retained shared member or missing counter') END; END;
CREATE TRIGGER market_shared_member_added AFTER INSERT ON market_shared_members BEGIN
    UPDATE market_shared_records SET refs=refs+1 WHERE id=NEW.group_id;
    INSERT INTO market_counts VALUES('shared_record_members',1,24,24,0) ON CONFLICT(category) DO UPDATE
        SET records=records+1,logical_bytes=logical_bytes+24,inserted_bytes=inserted_bytes+24; END;
CREATE TRIGGER market_shared_member_removed AFTER DELETE ON market_shared_members BEGIN
    UPDATE market_shared_records SET refs=refs-1 WHERE id=OLD.group_id;
    UPDATE market_counts SET records=records-1,logical_bytes=logical_bytes-24,deleted_bytes=deleted_bytes+24
        WHERE category='shared_record_members'; END;
CREATE TRIGGER market_shared_resize AFTER UPDATE OF length,sha256 ON market_packets
    WHEN NEW.length<>OLD.length OR NEW.sha256<>OLD.sha256 BEGIN
    SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM market_counts WHERE category=
        (SELECT category FROM market_packet_arenas WHERE id=OLD.arena) AND records>0 AND logical_bytes>=OLD.length)
        THEN RAISE(ABORT,'Missing resized packet counter') END;
    UPDATE market_packet_arenas SET live_bytes=live_bytes+NEW.length-OLD.length
        WHERE id=OLD.arena AND NEW.arena=OLD.arena;
    UPDATE market_counts SET logical_bytes=logical_bytes+NEW.length-OLD.length,
        inserted_bytes=inserted_bytes+NEW.length,deleted_bytes=deleted_bytes+OLD.length
        WHERE category=(SELECT category FROM market_packet_arenas WHERE id=OLD.arena); END;
"""


def _statements(source: str = SCHEMA) -> list[str]:
    result, pending = [], ""
    for line in source.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            result.append(pending.strip().rstrip(";"))
            pending = ""
    if pending.strip():
        raise ValueError("Incomplete shared record schema")
    return result


def validate(db: sqlite3.Connection) -> None:
    marker = db.execute("SELECT value FROM market_meta WHERE key='shared_records'").fetchone()
    if marker is not None and marker[0] == "3":
        from .shared_record_work import validate as work_validate

        work_validate(db)
        return
    if marker is None or marker[0] != "2":
        raise ValueError("Unknown shared record layout")
    for sql in [*_statements(), MOVE_GUARD]:
        name = sql.split()[2].split("(")[0]
        row = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if row is None or " ".join(row[0].split()) != " ".join(sql.split()):
            raise ValueError("Missing or unknown shared record guard")


def install(db: sqlite3.Connection, cache: Any = None) -> None:
    install_functions(db, cache)
    marker = db.execute("SELECT value FROM market_meta WHERE key='shared_records'").fetchone()
    if marker is not None:
        validate(db)
        return
    if db.in_transaction:
        raise ValueError("Shared record installation requires an outer boundary")
    with db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT sql FROM sqlite_master WHERE name='market_packet_move'").fetchone()
        if old is None or " ".join(old[0].split()) != " ".join(packet_store.MOVE_GUARD.split()):
            raise ValueError("Unknown packet relocation guard; data preserved")
        for sql in _statements():
            db.execute(sql)
        db.execute("DROP TRIGGER market_packet_move")
        db.execute(MOVE_GUARD)
        db.execute("INSERT INTO market_meta VALUES('shared_records','2')")
        validate(db)


@dataclass(frozen=True)
class Member:
    identity: int
    binding: tuple[Any, ...]
    raw: bytes
    count: int


@dataclass(frozen=True)
class Prepared:
    members: tuple[Member, ...]
    body: bytes
    projection: bytes


def _append_descriptor(
    db: sqlite3.Connection, bucket: int, raw: bytes, *, exclude: int = 0
) -> tuple[int, int]:
    _descriptor(raw)
    row = db.execute(
        "SELECT t.arena,a.category,a.bucket,a.used FROM market_shared_tails t "
        "LEFT JOIN market_packet_arenas a ON a.id=t.arena WHERE t.bucket=?",
        (bucket,),
    ).fetchone()
    if row is not None and (row[1] != "detail_packets" or row[2] != bucket or type(row[3]) is not int):
        raise ValueError("Missing shared descriptor tail arena")
    if row is None or row[0] == exclude or row[3] + len(raw) > packet_store.CAPACITY:
        cursor = db.execute(
            "INSERT INTO market_packet_arenas(category,bucket,payload) VALUES('detail_packets',?,zeroblob(?))",
            (bucket, packet_store.CAPACITY),
        )
        arena = int(cursor.lastrowid or 0)
        if row is None:
            db.execute("INSERT INTO market_shared_tails VALUES(?,?)", (bucket, arena))
        elif (
            db.execute(
                "UPDATE market_shared_tails SET arena=? WHERE bucket=? AND arena=?", (arena, bucket, row[0])
            ).rowcount
            != 1
        ):
            raise ValueError("Changed shared descriptor tail")
    else:
        arena = row[0]
    return packet_store.write_segment(db, "detail_packets", bucket, raw, target=arena)


def relocate(db: sqlite3.Connection, identity: int, cache: Any = None) -> None:
    """Evacuate one exact descriptor into its reserved pool, preserving owners."""
    with packet_store.atomic(db):
        binding = _binding(db, identity)
        raw = packet_store.read(db, identity)
        expand(db, identity, raw, cache)
        arena, offset = _append_descriptor(db, binding[6], raw, exclude=binding[0])
        changed = db.execute(
            "UPDATE market_packets SET arena=?,offset=? WHERE id=? AND arena=? AND offset=? AND length=? AND sha256=? AND refs=?",
            (arena, offset, identity, *binding[:5]),
        ).rowcount
        if changed != 1 or packet_store.read(db, identity) != raw:
            raise ValueError("Changed shared descriptor relocation source")


def _binding(db: sqlite3.Connection, identity: int) -> tuple[Any, ...]:
    packet_store.valid_identity(identity)
    row = db.execute(
        "SELECT p.arena,p.offset,p.length,p.sha256,p.refs,a.category,a.bucket FROM market_packets p "
        "JOIN market_packet_arenas a ON a.id=p.arena WHERE p.id=?",
        (identity,),
    ).fetchone()
    if row is None or row[4] <= 0 or row[5] != "detail_packets":
        raise ValueError("Missing retained shared record source")
    return tuple(row)


def prepare(db: sqlite3.Connection, identities: list[int]) -> Prepared:
    """Prepare on a bounded read snapshot, before entering the main writer."""
    if not db.in_transaction or not isinstance(identities, list) or not 0 < len(identities) <= 32:
        raise ValueError("Shared preparation requires a snapshot and at most32packets")
    if len(set(identities)) != len(identities):
        raise ValueError("Duplicate shared source packet")
    members: list[Member] = []
    for identity in identities:
        binding = _binding(db, identity)
        if members and binding[5:] != members[0].binding[5:]:
            raise ValueError("Shared group crosses category or retention day")
        raw = packet_store.read(db, identity)
        if not raw.startswith(record_blocks.MAGIC):
            raise ValueError("Shared preparation requires ordinary DB2 packets")
        members.append(Member(identity, binding, raw, len(record_blocks.projections(raw))))
    body = shared_body_frame.pack_frames([record_blocks.frames(m.raw)[1] for m in members])
    projection = projection_recipes.pack_frames([m.raw for m in members])
    return Prepared(tuple(members), body, projection)


def publish(db: sqlite3.Connection, prepared: Prepared, cache: Any = None) -> int:
    """Fresh CAS and full equivalence under the existing packet savepoint."""
    if not isinstance(prepared, Prepared) or not 0 < len(prepared.members) <= 32:
        raise ValueError("Invalid prepared shared record group")
    with packet_store.atomic(db):
        first_member = prepared.members[0]
        category, bucket = first_member.binding[5:]
        for member in prepared.members:
            if (
                _binding(db, member.identity) != member.binding
                or packet_store.read(db, member.identity) != member.raw
            ):
                raise ValueError("Stale shared source capture")
        digest = signature(category, bucket, prepared.body, prepared.projection, len(prepared.members))
        cursor = db.execute(
            "INSERT INTO market_shared_records(category,bucket,body,projection,digest,initial) VALUES(?,?,?,?,?,?)",
            (category, bucket, prepared.body, prepared.projection, digest, len(prepared.members)),
        )
        group = int(cursor.lastrowid or 0)
        first = 0
        for member in prepared.members:
            publish_member(db, member, group, first, digest, cache)
            first += member.count
        if first != len(shared_body_frame.layout(prepared.body)[0]) - 1:
            raise ValueError("Shared publication cardinality mismatch")
        return group


def publish_member(
    db: sqlite3.Connection, member: Member, group: int, first: int, digest: bytes, cache: Any = None
) -> None:
    """One checked transition; caller owns its transaction/savepoint."""
    if (
        not db.in_transaction
        or _binding(db, member.identity) != member.binding
        or packet_store.read(db, member.identity) != member.raw
    ):
        raise ValueError("Stale shared source capture")
    db.execute(
        "INSERT INTO market_shared_members VALUES(?,?,?,?)", (member.identity, group, first, member.count)
    )
    raw = descriptor(group, first, member.count, digest)
    arena, offset = _append_descriptor(db, member.binding[6], raw)
    changed = db.execute(
        "UPDATE market_packets SET arena=?,offset=?,length=?,sha256=? WHERE id=? "
        "AND arena=? AND offset=? AND length=? AND sha256=? AND refs=?",
        (arena, offset, len(raw), hashlib.sha256(raw).digest(), member.identity, *member.binding[:5]),
    ).rowcount
    if changed != 1 or packet_store.read(db, member.identity) != raw:
        raise ValueError("Shared packet publication CAS changed")
    if not _equal(member.raw, expand(db, member.identity, raw, cache), cache):
        raise ValueError("Shared packet publication changed values")


def collect(db: sqlite3.Connection, batch: int = 32) -> int:
    if type(batch) is not int or not 0 < batch <= 256:
        raise ValueError("Invalid shared collection bound")
    with packet_store.atomic(db):
        marker = db.execute("SELECT value FROM market_meta WHERE key='shared_records'").fetchone()
        pending = (
            " AND NOT EXISTS(SELECT 1 FROM market_shared_pending WHERE group_id=market_shared_records.id)"
            " AND NOT EXISTS(SELECT 1 FROM market_shared_staging WHERE group_id=market_shared_records.id)"
            if marker is not None and marker[0] == "3"
            else ""
        )
        return db.execute(
            "DELETE FROM market_shared_records WHERE id IN(SELECT id FROM market_shared_records "
            "WHERE refs=0" + pending + " ORDER BY id LIMIT ?)",
            (batch,),
        ).rowcount
