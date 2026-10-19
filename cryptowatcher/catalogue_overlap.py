"""Transactional fixed-size hot/cold overlap totals; no retained-key index."""

from __future__ import annotations

import hashlib
import sqlite3
import struct
import uuid
from typing import Any

from .record_blocks import KINDS

NAMES = tuple(kind + suffix for kind in KINDS for suffix in ("", "-head")) + ("history", "episode-event")
SIZE = sum(len(name) + 8 + 32 for name in NAMES)


def signature(name: str, value: int) -> bytes:
    if not isinstance(name, str) or name not in NAMES or type(value) is not int or not 0 <= value < 2**63:
        raise ValueError("Invalid catalogue overlap checksum inputs")
    return hashlib.sha256(b"CO1\0" + name.encode("ascii") + b"\0" + struct.pack(">Q", value)).digest()


def sql_signature(*values: Any) -> bytes:
    try:
        if len(values) != 2:
            return b""
        return signature(values[0], values[1])
    except ValueError:
        return b""


def install_check(db: sqlite3.Connection) -> None:
    db.create_function("cw_catalogue_shadow_digest", 2, sql_signature, deterministic=True)


def header(db: sqlite3.Connection) -> dict[str, int]:
    marker = db.execute("SELECT value FROM market_meta WHERE key='catalogue_overlap'").fetchone()
    if marker is None or marker[0] != "1":
        raise ValueError("Missing or unknown catalogue overlap layout")
    rows = db.execute(
        "SELECT CASE WHEN typeof(name)='text' AND length(CAST(name AS BLOB))<=16 THEN name END,"
        "CASE WHEN typeof(n)='integer' THEN n END,"
        "CASE WHEN typeof(checksum)='blob' AND length(checksum)=32 THEN checksum END "
        "FROM market_catalogue_overlap ORDER BY name LIMIT 13"
    ).fetchall()
    values = {name: value for name, value, _ in rows}
    counter = db.execute(
        "SELECT records,logical_bytes FROM market_counts WHERE category='catalogue_overlap'"
    ).fetchone()
    if (
        set(values) != set(NAMES)
        or any(type(n) is not int or not 0 <= n < 2**63 for n in values.values())
        or counter is None
        or any(type(n) is not int for n in counter)
        or tuple(counter) != (len(NAMES), SIZE)
        or any(signature(name, value) != checksum for name, value, checksum in rows)
    ):
        raise ValueError("Missing or inconsistent catalogue overlap counter")
    return values


def binding_key(episode: bytes | None, observed: int, event: bytes) -> str:
    if (
        type(observed) is not int
        or not 0 <= observed < 10**16
        or not isinstance(event, bytes)
        or len(event) != 16
        or (episode is not None and (not isinstance(episode, bytes) or len(episode) != 16))
    ):
        raise ValueError("Invalid catalogue binding time")
    value = f"{observed:016d}:" + str(uuid.UUID(bytes=bytes(event)))
    return (
        "radar:v1:"
        + ("history:" if episode is None else "episode-event:" + str(uuid.UUID(bytes=bytes(episode))) + ":")
        + value
    )


def head_key(kind: int, identity: bytes) -> str:
    if (
        type(kind) is not int
        or not 0 <= kind < len(KINDS)
        or not isinstance(identity, bytes)
        or len(identity) != 16
    ):
        raise ValueError("Invalid catalogue head kind")
    return (
        "radar:v1:"
        + ("decision:telegram:" if KINDS[kind] == "decision" else KINDS[kind] + "-head:")
        + str(uuid.UUID(bytes=bytes(identity)))
    )


def token(db: sqlite3.Connection, key: str) -> str | None:
    from . import catalogue_aliases, catalogue_reader, record_catalog

    selected = catalogue_reader.point(key)
    if selected is not None:
        sql = "SELECT 1 FROM market_records WHERE kind=? AND public_uuid=? AND "
        parameters: tuple[Any, ...] = (KINDS.index(selected.kind), record_catalog.binary(selected.identity))
        if selected.revision is None:
            sql += "current=1"
        else:
            sql += "revision=?"
            parameters += (selected.revision,)
        if db.execute(sql, parameters).fetchone() is not None:
            return selected.kind + ("-head" if selected.revision is None else "")
        return None
    alias = catalogue_aliases.point(key)
    if alias is None:
        return None
    if alias.episode is None:
        present = db.execute(
            "SELECT 1 FROM market_record_parents WHERE public_uuid=? AND observed=? AND history_published=1",
            (record_catalog.binary(alias.event), alias.observed),
        ).fetchone()
        return "history" if present is not None else None
    present = db.execute(
        "SELECT 1 FROM market_record_members WHERE episode=? AND observed=? AND event_uuid=?",
        (record_catalog.binary(alias.episode), alias.observed, record_catalog.binary(alias.event)),
    ).fetchone()
    return "episode-event" if present is not None else None


def install_functions(db: sqlite3.Connection) -> None:
    install_check(db)

    def matched(key: str) -> str | None:
        selected = token(db, key)
        if selected is not None:
            header(db)
        return selected

    def valid(name: str, delta: int) -> int:
        values = header(db)
        return int(name in values and type(delta) is int and 0 <= values[name] + delta < 2**63)

    db.create_function("cw_catalogue_shadow", 1, matched)
    db.create_function("cw_catalogue_shadow_valid", 2, valid)
    db.create_function("cw_catalogue_head_key", 2, head_key, deterministic=True)
    db.create_function("cw_catalogue_binding_key", 3, binding_key, deterministic=True)


def install(db: sqlite3.Connection) -> None:
    install_functions(db)
    marker = db.execute("SELECT value FROM market_meta WHERE key='catalogue_overlap'").fetchone()
    if marker is not None:
        header(db)
        return
    if (
        db.in_transaction
        or db.execute("SELECT 1 FROM market_records LIMIT 1").fetchone()
        or db.execute("SELECT 1 FROM market_record_members LIMIT 1").fetchone()
        or db.execute("SELECT 1 FROM market_record_parents WHERE history_published=1 LIMIT 1").fetchone()
    ):
        raise ValueError("Initial catalogue overlap counters require an empty outer boundary")
    if db.execute("SELECT 1 FROM market_counts WHERE category='catalogue_overlap'").fetchone():
        raise ValueError("Unexpected catalogue overlap byte counter")
    kind = (
        "CASE {alias}.kind "
        + " ".join(f"WHEN {number} THEN '{name}'" for number, name in enumerate(KINDS))
        + " END"
    )

    def adjustment(name: str, delta: str) -> str:
        return f"SELECT CASE WHEN cw_catalogue_shadow_valid({name},{delta})<>1 THEN RAISE(ABORT,'Missing catalogue overlap counter') END; UPDATE market_catalogue_overlap SET n=n+({delta}),checksum=cw_catalogue_shadow_digest(name,n+({delta})) WHERE name={name};"

    def matched(alias: str) -> str:
        return f"CASE WHEN typeof({alias}.key)='text' AND length(CAST({alias}.key AS BLOB))<=128 THEN cw_catalogue_shadow({alias}.key) END"

    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "CREATE TABLE market_catalogue_overlap(name TEXT PRIMARY KEY CHECK(typeof(name)='text' AND length(CAST(name AS BLOB))<=16),n INTEGER NOT NULL CHECK(typeof(n)='integer' AND n>=0),checksum BLOB NOT NULL CHECK(typeof(checksum)='blob' AND length(checksum)=32 AND CASE WHEN typeof(name)='text' AND length(CAST(name AS BLOB))<=16 AND typeof(n)='integer' AND n>=0 THEN cw_catalogue_shadow_digest(name,n) ELSE X'' END=checksum)) WITHOUT ROWID"
        )
        db.executemany(
            "INSERT INTO market_catalogue_overlap VALUES(?,0,?)",
            [(name, signature(name, 0)) for name in NAMES],
        )
        db.execute("INSERT INTO market_counts VALUES('catalogue_overlap',?,?,?,0)", (len(NAMES), SIZE, SIZE))
        db.execute("INSERT INTO market_meta VALUES('catalogue_overlap','1')")
        for operation, alias, sign in (("INSERT", "NEW", 1), ("DELETE", "OLD", -1)):
            revision_key = f"cw_catalogue_key({alias}.kind,{alias}.public_uuid,{alias}.revision)"
            current_key = f"cw_catalogue_head_key({alias}.kind,{alias}.public_uuid)"
            name = "(" + kind.format(alias=alias) + ")"
            changes = adjustment(name, f"{sign}*EXISTS(SELECT 1 FROM runtime WHERE key={revision_key})")
            changes += adjustment(
                name + "||'-head'",
                f"{sign}*{alias}.current*EXISTS(SELECT 1 FROM runtime WHERE key={current_key})",
            )
            db.execute(
                f"CREATE TRIGGER market_catalogue_overlap_record_{operation} AFTER {operation} ON market_records BEGIN {changes} END"
            )
            member_key = f"cw_catalogue_binding_key({alias}.episode,{alias}.observed,{alias}.event_uuid)"
            changes = adjustment(
                "'episode-event'", f"{sign}*EXISTS(SELECT 1 FROM runtime WHERE key={member_key})"
            )
            db.execute(
                f"CREATE TRIGGER market_catalogue_overlap_member_{operation} AFTER {operation} ON market_record_members BEGIN {changes} END"
            )
            changes = f"UPDATE market_catalogue_overlap SET n=n+({sign}),checksum=cw_catalogue_shadow_digest(name,n+({sign})) WHERE name={matched(alias)};"
            db.execute(
                f"CREATE TRIGGER market_catalogue_overlap_hot_{operation} AFTER {operation} ON runtime BEGIN {changes} END"
            )
        name = "(" + kind.format(alias="NEW") + ")||'-head'"
        changes = adjustment(
            name,
            "(NEW.current-OLD.current)*EXISTS(SELECT 1 FROM runtime WHERE key=cw_catalogue_head_key(NEW.kind,NEW.public_uuid))",
        )
        db.execute(
            f"CREATE TRIGGER market_catalogue_overlap_head AFTER UPDATE OF current ON market_records BEGIN {changes} END"
        )
        for operation, alias, sign in (("UPDATE OF history_published", "NEW", 1), ("DELETE", "OLD", -1)):
            changes = adjustment(
                "'history'",
                f"{sign}*EXISTS(SELECT 1 FROM runtime WHERE key=cw_catalogue_binding_key(NULL,{alias}.observed,{alias}.public_uuid))",
            )
            db.execute(
                f"CREATE TRIGGER market_catalogue_overlap_history_{sign + 1} AFTER {operation} ON market_record_parents WHEN {alias}.history_published=1 BEGIN {changes} END"
            )
        db.execute(f"""CREATE TRIGGER market_catalogue_overlap_hot_key AFTER UPDATE OF key ON runtime BEGIN
            UPDATE market_catalogue_overlap SET n=n-1,checksum=cw_catalogue_shadow_digest(name,n-1) WHERE name={matched("OLD")};
            UPDATE market_catalogue_overlap SET n=n+1,checksum=cw_catalogue_shadow_digest(name,n+1) WHERE name={matched("NEW")};
        END""")
        db.commit()
    except BaseException:
        db.rollback()
        raise


def count(db: sqlite3.Connection, prefix: str) -> int:
    from . import catalogue_aliases, catalogue_reader

    values = header(db)
    total = 0
    for namespace in catalogue_reader.NAMESPACES:
        selected = catalogue_reader.selected_range(namespace, prefix)
        if selected is None:
            continue
        if selected[2]:
            total += values[namespace.kind + ("-head" if namespace.head else "")]
        else:
            key = (
                "cw_catalogue_head_key(kind,public_uuid)"
                if namespace.head
                else "cw_catalogue_key(kind,public_uuid,revision)"
            )
            total += db.execute(
                "SELECT COUNT(*) FROM market_records CROSS JOIN runtime WHERE "
                + selected[0]
                + " AND runtime.key="
                + key,
                selected[1],
            ).fetchone()[0]
    for member in (False, True):
        alias = catalogue_aliases.selection(prefix, member=member)
        if alias is None:
            continue
        if alias.complete:
            total += values["episode-event" if member else "history"]
        else:
            key = (
                "cw_catalogue_binding_key(episode,observed,event_uuid)"
                if member
                else "cw_catalogue_binding_key(NULL,observed,public_uuid)"
            )
            total += db.execute(
                "SELECT COUNT(*) FROM "
                + alias.source
                + " CROSS JOIN runtime WHERE "
                + alias.where
                + " AND runtime.key="
                + key,
                alias.parameters,
            ).fetchone()[0]
    return total
