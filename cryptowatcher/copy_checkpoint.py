"""Private copy checkpoints committed atomically with the copied SQLite rows."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from .history_rebuild import TABLES
from .storage import StateError, encode

TABLE = "_cw_copy_checkpoint"
SCHEMA = "cryptowatcher.copy-checkpoint.v1"
SQL = (
    f"CREATE TABLE {TABLE}(id INTEGER PRIMARY KEY CHECK(id=1), "
    "identity_json TEXT NOT NULL, state_json TEXT NOT NULL)"
)


def binding(source_identity: str, owner: int, maximum: int) -> dict[str, Any]:
    if not isinstance(source_identity, str) or re.fullmatch(r"[0-9a-f]{64}", source_identity) is None:
        raise StateError("Frozen source fingerprint required")
    if type(owner) is not int or owner <= 0 or type(maximum) is not int or maximum <= 0:
        raise StateError("Explicit copy owner and capacity required")
    fingerprint = hashlib.sha256()
    for name in (
        "copy_checkpoint.py",
        "framed_rebuild.py",
        "record_codec.py",
        "history_codec.py",
        "history_rebuild.py",
        "native_history.py",
        "storage.py",
    ):
        fingerprint.update(name.encode())
        # Python's loader normalizes CRLF; Git checkout line endings must not
        # invalidate otherwise identical committed conversion progress.
        fingerprint.update(Path(__file__).with_name(name).read_text(encoding="utf-8").encode())
    return {
        "schema": SCHEMA,
        "source_identity": source_identity,
        "owner": owner,
        "maximum": maximum,
        "algorithm": fingerprint.hexdigest(),
    }


def initial_state() -> dict[str, Any]:
    return {
        "table_index": 0,
        "cursor": None,
        "rows": 0,
        "counts": {},
        "runtime_counts": {},
        "runtime_payload_chars": {},
        "framed": 0,
        "heads": 0,
        "candles": 0,
    }


def validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(initial_state()):
        raise StateError("Invalid copy checkpoint")
    for field in ("table_index", "rows", "framed", "heads", "candles"):
        if type(value[field]) is not int or value[field] < 0:
            raise StateError("Invalid copy checkpoint")
    index = value["table_index"]
    cursor = value["cursor"]
    if (
        index > len(TABLES)
        or cursor is not None
        and (type(cursor) is not int or not -(2**63) <= cursor < 2**63)
    ):
        raise StateError("Invalid copy checkpoint")
    for field in ("counts", "runtime_counts", "runtime_payload_chars"):
        if not isinstance(value[field], dict) or any(
            not isinstance(k, str) or type(v) is not int or v < 0 for k, v in value[field].items()
        ):
            raise StateError("Invalid copy checkpoint")
    if list(value["counts"]) != sorted(list(TABLES)[:index]):
        # encode() sorts object keys; table ordering is fixed by TABLES, not JSON order.
        raise StateError("Invalid completed copy tables")
    if index == len(TABLES) and (value["rows"] != 0 or value["cursor"] is not None):
        raise StateError("Invalid completed copy cursor")
    return value


class Checkpoint:
    def __init__(self, destination: Path, identity: dict[str, Any], *, resume: bool):
        self.identity = encode(identity)
        self.path = destination.with_name(destination.name + ".copy.json")
        if self.path.is_symlink() or any(p.is_symlink() for p in self.path.parents):
            raise StateError("Unsafe copy checkpoint path")
        if resume:
            if not destination.is_file() or not self.path.is_file():
                raise StateError("Bound copy checkpoint and candidate required")
            if self.path.read_text(encoding="utf-8") != self.identity:
                raise StateError("Copy checkpoint identity mismatch")
        else:
            if destination.exists() or self.path.exists():
                raise StateError("Exclusive new candidate required")
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(self.identity)
                output.flush()
                os.fsync(output.fileno())
            if os.name == "posix":
                parent = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)

    def create(self, db: sqlite3.Connection) -> None:
        db.execute(SQL)
        db.execute(f"INSERT INTO {TABLE} VALUES(1,?,?)", (self.identity, encode(initial_state())))
        db.execute("PRAGMA user_version=0")  # Every application version refuses unfinished copies.
        db.commit()

    def read(self, db: sqlite3.Connection) -> dict[str, Any] | None:
        objects = {
            tuple(r) for r in db.execute("SELECT type,name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'")
        }
        expected = {("table", name) for name in TABLES}
        if objects == expected:
            return None  # Finalization committed; reverify every source/candidate row.
        if objects != expected | {("table", TABLE)}:
            raise StateError("Unknown copy checkpoint objects")
        definition = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (TABLE,)).fetchone()[0]
        if " ".join(definition.lower().split()) != " ".join(SQL.lower().split()):
            raise StateError("Copy checkpoint schema mismatch")
        if db.execute("PRAGMA user_version").fetchone()[0] != 0:
            raise StateError("Unfinished copy version mismatch")
        rows = db.execute(f"SELECT id,identity_json,state_json FROM {TABLE}").fetchall()
        if len(rows) != 1 or rows[0][0] != 1 or rows[0][1] != self.identity:
            raise StateError("Invalid copy checkpoint binding")
        return validate_state(json.loads(rows[0][2]))

    def save(self, db: sqlite3.Connection, state: dict[str, Any]) -> None:
        db.execute(f"UPDATE {TABLE} SET state_json=? WHERE id=1", (encode(state),))
        db.commit()  # Includes all rows since the preceding checkpoint.

    def begin(self, db: sqlite3.Connection, state: dict[str, Any]) -> None:
        db.execute("BEGIN IMMEDIATE")
        if self.read(db) != state:
            raise StateError("Concurrent copy checkpoint changed")

    def finish(self, db: sqlite3.Connection) -> None:
        db.execute(f"DROP TABLE {TABLE}")
        db.commit()
