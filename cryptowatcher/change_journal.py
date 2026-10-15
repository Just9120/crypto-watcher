"""Explicit temporary key journal for lossless online compaction catch-up.

Never enabled by application startup. Overflow invalidates the maintenance proof,
not the application's write. Payloads stay in the original five state tables.
"""

from __future__ import annotations

import math
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .history_rebuild import TABLES
from .storage import COMPRESSED_RECORD_VERSION, SCHEMA_VERSION, StateError, create_schema

META = "_cw_catchup_state"
KEYS = "_cw_catchup_keys"
INDEX = "_cw_catchup_sequence"
PREFIX = "_cw_catchup_"


def definitions() -> dict[str, str]:
    result = {
        META: f"CREATE TABLE {META}(id INTEGER PRIMARY KEY CHECK(id=1), "
        "epoch TEXT NOT NULL,owner INTEGER NOT NULL,last_seq INTEGER NOT NULL,"
        "key_count INTEGER NOT NULL,key_bytes INTEGER NOT NULL,max_keys INTEGER NOT NULL,"
        "max_bytes INTEGER NOT NULL,expires INTEGER NOT NULL,valid INTEGER NOT NULL CHECK(valid IN(0,1)),"
        "reason TEXT NOT NULL)",
        KEYS: f"CREATE TABLE {KEYS}(table_name TEXT NOT NULL,key TEXT NOT NULL,seq INTEGER NOT NULL,"
        "PRIMARY KEY(table_name,key)) WITHOUT ROWID",
        INDEX: f"CREATE UNIQUE INDEX {INDEX} ON {KEYS}(seq)",
    }
    for table, columns in TABLES.items():
        column = columns[0]
        key_type = "integer" if table in ("owner_settings", "delivery_preferences") else "text"
        for event, aliases in (("INSERT", ("NEW",)), ("DELETE", ("OLD",)), ("UPDATE", ("OLD", "NEW"))):
            statements = [
                f"UPDATE {META} SET valid=0,reason='EXPIRED' "
                "WHERE id=1 AND valid=1 AND expires<=CAST(strftime('%s','now') AS INTEGER);"
            ]
            for alias in aliases:
                value = f"{alias}.{column}"
                key = f"CAST({value} AS TEXT)"
                present = f"EXISTS(SELECT 1 FROM {KEYS} WHERE table_name='{table}' AND key={key})"
                charge = f"length(CAST({key} AS BLOB))+128"
                statements += [
                    f"UPDATE {META} SET valid=0,reason='KEY_TYPE' "
                    f"WHERE id=1 AND valid=1 AND typeof({value})<>'{key_type}';",
                    f"UPDATE {META} SET valid=0,reason='CAPACITY' "
                    f"WHERE id=1 AND valid=1 AND NOT {present} "
                    f"AND (key_count>=max_keys OR key_bytes+{charge}>max_bytes);",
                    f"UPDATE {META} SET last_seq=last_seq+1,key_count=key_count+CASE WHEN {present} "
                    f"THEN 0 ELSE 1 END,key_bytes=key_bytes+CASE WHEN {present} THEN 0 ELSE {charge} END "
                    "WHERE id=1 AND valid=1;",
                    f"INSERT INTO {KEYS}(table_name,key,seq) SELECT '{table}',{key},last_seq FROM {META} "
                    "WHERE id=1 AND valid=1 ON CONFLICT(table_name,key) DO UPDATE SET seq=excluded.seq;",
                ]
            name = PREFIX + table + "_" + event.lower()
            result[name] = (
                f"CREATE TRIGGER {name} AFTER {event} ON {table} BEGIN " + " ".join(statements) + " END"
            )
    return result


def _objects(db: sqlite3.Connection) -> dict[str, str]:
    return dict(db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"))


def _canonical(db: sqlite3.Connection, *, journal: bool) -> None:
    with sqlite3.connect(":memory:") as reference:
        create_schema(reference)
        expected = _objects(reference)
    if journal:
        expected.update(definitions())
    actual = _objects(db)

    def normalized(value: str) -> str:
        return " ".join(value.lower().split())

    if set(actual) != set(expected) or any(
        normalized(actual[k]) != normalized(v) for k, v in expected.items()
    ):
        raise StateError("Catch-up schema identity refused")


def state(db: sqlite3.Connection, epoch: str, owner: int) -> dict[str, Any]:
    _canonical(db, journal=True)
    names = (
        "epoch",
        "owner",
        "last_seq",
        "key_count",
        "key_bytes",
        "max_keys",
        "max_bytes",
        "expires",
        "valid",
        "reason",
    )
    row = db.execute(f"SELECT {','.join(names)} FROM {META} WHERE id=1").fetchone()
    if row is None or tuple(row[0:2]) != (epoch, owner):
        raise StateError("Catch-up epoch or owner refused")
    result = dict(zip(names, row, strict=True))
    if result["valid"] != 1 or result["expires"] <= time.time():
        raise StateError("Catch-up completeness unavailable")
    return result


def install(
    db: sqlite3.Connection,
    *,
    epoch: str,
    owner: int,
    max_keys: int,
    max_bytes: int,
    expires: int,
) -> dict[str, Any]:
    """Install under one writer lock; never reset an existing epoch or touch payloads."""
    if (
        not isinstance(epoch, str)
        or re.fullmatch(r"[0-9a-f]{32}", epoch) is None
        or any(type(v) is not int or v <= 0 for v in (owner, max_keys, max_bytes, expires))
        or expires <= time.time()
        or db.in_transaction
    ):
        raise StateError("Explicit catch-up identity and bounds required")
    db.execute("BEGIN IMMEDIATE")
    try:
        if db.execute("PRAGMA user_version").fetchone()[0] not in (SCHEMA_VERSION, COMPRESSED_RECORD_VERSION):
            raise StateError("Catch-up source schema version refused")
        if META in _objects(db):
            result = state(db, epoch, owner)
            if (result["max_keys"], result["max_bytes"], result["expires"]) != (max_keys, max_bytes, expires):
                raise StateError("Catch-up bounds changed")
        else:
            _canonical(db, journal=False)
            owners = [tuple(row) for row in db.execute("SELECT owner_id FROM owner_settings")]
            if owners != [(owner,)]:
                raise StateError("Catch-up source owner refused")
            for sql in definitions().values():
                db.execute(sql)
            db.execute(
                f"INSERT INTO {META} VALUES(1,?,?,0,0,0,?,?,?,1,'OK')",
                (epoch, owner, max_keys, max_bytes, expires),
            )
            result = state(db, epoch, owner)
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise


@contextmanager
def changes(
    db: sqlite3.Connection,
    *,
    epoch: str,
    owner: int,
    after: int,
    deadline: float,
) -> Iterator[tuple[dict[str, Any], Iterator[dict[str, Any]]]]:
    """Payloads, deletions and upper watermark belong to the same WAL snapshot."""
    if db.in_transaction or type(after) is not int or after < 0 or not math.isfinite(deadline):
        raise StateError("Invalid catch-up snapshot")
    db.execute("BEGIN")
    try:
        snapshot = state(db, epoch, owner)
        if after > snapshot["last_seq"]:
            raise StateError("Catch-up cursor beyond snapshot")

        def rows() -> Iterator[dict[str, Any]]:
            for table, columns in TABLES.items():
                names = ",".join("s." + c for c in columns)
                query = (
                    f"SELECT j.key,{names},s.rowid FROM {KEYS} j LEFT JOIN {table} s ON s.{columns[0]}=j.key "
                    "WHERE j.table_name=? AND j.seq>? ORDER BY j.key"
                )
                for row in db.execute(query, (table, after)):
                    if time.monotonic() >= deadline:
                        raise StateError("Catch-up time budget exhausted")
                    yield {
                        "table": table,
                        "key": row[0],
                        "values": tuple(row[1:-1]) if row[-1] is not None else None,
                    }

        yield snapshot, rows()
    finally:
        db.rollback()
