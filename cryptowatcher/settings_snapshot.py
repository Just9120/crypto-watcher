"""Export private settings to a small SQLite file and restore them to a new database."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .reset_history import MAX_PRIVATE, private_state, restore_private, signature
from .storage import StateError, Store, check_integrity, encode

APPLICATION_ID = 0x43575331


def safe_path(path: Path) -> Path:
    path = path.absolute()
    if path.resolve() != path or any(p.is_symlink() for p in (path, *path.parents)):
        raise StateError("Unsafe settings snapshot path")
    return path


def read_record(db: sqlite3.Connection, owner: int) -> dict[str, Any]:
    if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
        raise StateError("Not a CryptoWatcher settings snapshot")
    row = db.execute(
        "SELECT payload, sha256 FROM settings_snapshot WHERE owner_id=? AND length(payload)<=?",
        (owner, MAX_PRIVATE),
    ).fetchone()
    if row is None:
        raise StateError("Settings snapshot owner or size mismatch")
    payload = json.loads(row[0])
    if signature(payload) != row[1]:
        raise StateError("Settings snapshot checksum mismatch")
    return payload


def export_settings(source: Path, snapshot: Path, owner: int) -> dict[str, Any]:
    source, snapshot = safe_path(source), safe_path(snapshot)
    if source == snapshot or (snapshot.exists() and source.samefile(snapshot)):
        raise StateError("Settings snapshot must be separate from the working database")
    if snapshot.exists() and (not snapshot.is_file() or snapshot.stat().st_nlink != 1):
        raise StateError("Settings snapshot must be a regular file with a single link")
    with tempfile.TemporaryDirectory(prefix=".cw-settings-", dir=snapshot.parent) as work:
        exists = snapshot.exists()
        destination = snapshot if exists else Path(work) / "new.sqlite3"
        mode = "rw" if exists else "rwc"
        with closing(sqlite3.connect(destination.as_uri() + "?mode=" + mode, uri=True)) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                if exists:
                    read_record(db, owner)
                else:
                    destination.chmod(0o600)
                    db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                    db.execute(
                        "CREATE TABLE settings_snapshot (owner_id INTEGER PRIMARY KEY, "
                        "payload BLOB NOT NULL, sha256 TEXT NOT NULL)"
                    )
                with closing(Store(source, owner, readonly=True)) as store:
                    store.db.execute("BEGIN")
                    settings = private_state(store)
                digest = signature(settings)
                db.execute(
                    "INSERT INTO settings_snapshot VALUES (?,?,?) "
                    "ON CONFLICT(owner_id) DO UPDATE SET payload=excluded.payload,sha256=excluded.sha256",
                    (owner, encode(settings).encode(), digest),
                )
                if read_record(db, owner) != settings:
                    raise StateError("Settings snapshot readback mismatch")
                check_integrity(db, deadline=time.monotonic() + 10)
        if not exists:
            os.link(destination, snapshot)
    return {"state": "PASS", "sha256": digest, "snapshot_bytes": snapshot.stat().st_size}


def restore_settings(snapshot: Path, destination: Path, owner: int) -> dict[str, Any]:
    snapshot, destination = safe_path(snapshot), safe_path(destination)
    if destination.exists():
        raise StateError("Settings restore destination already exists")
    with closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True)) as db:
        check_integrity(db, deadline=time.monotonic() + 10)
        settings = read_record(db, owner)
    with tempfile.TemporaryDirectory(prefix=".cw-settings-restore-", dir=destination.parent) as work:
        stage = Path(work) / "restored.sqlite3"
        with closing(Store(stage, owner)) as store:
            stage.chmod(0o600)
            restore_private(store, settings)
            check_integrity(store.db, deadline=time.monotonic() + 10)
        with closing(Store(stage, owner, readonly=True)) as store:
            if private_state(store) != settings:
                raise StateError("Restored settings readback mismatch")
        os.link(stage, destination)
    return {"state": "PASS", "sha256": signature(settings)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("export", "restore"))
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--owner-id", type=int, required=True)
    args = parser.parse_args()
    if args.command == "export":
        result = export_settings(args.database, args.snapshot, args.owner_id)
    else:
        result = restore_settings(args.snapshot, args.database, args.owner_id)
    print(encode(result))


if __name__ == "__main__":
    main()
