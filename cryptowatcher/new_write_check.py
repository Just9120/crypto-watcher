"""Bounded compatibility check for the single owner-approved additive rollout.

Reads production through a read-only WAL snapshot. Writes only a small private
configuration snapshot and disposable fixture. Never asserts full DB integrity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .events import HEAD_SCHEMA, History
from .storage import StateError, Store, create_schema, encode

PRIVATE_TABLES = ("owner_settings", "delivery_preferences", "mutes", "legacy_import")
MAX_SNAPSHOT = 8 * 1024 * 1024


def probe(
    database: Path, scratch: Path, *, destination: Path | None = None, after_ms: int = 0
) -> dict[str, Any]:
    deadline = time.monotonic() + 110
    if database.is_symlink() or not database.is_file() or database.resolve() != database:
        raise StateError("Unexpected source database")
    if scratch.is_symlink() or not scratch.is_dir():
        raise StateError("Private scratch missing")
    with closing(Store(database, 233382575, readonly=True)) as source:
        source.db.execute("PRAGMA query_only=ON")
        source.db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        source.db.execute("BEGIN")
        settings = source.snapshot()
        tables = {r[0] for r in source.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables != {*PRIVATE_TABLES, "runtime"}:
            raise StateError("Unexpected schema for additive delivery")
        private: dict[str, list[list[Any]]] = {}
        for name in PRIVATE_TABLES:
            rows = [list(row) for row in source.db.execute(f"SELECT * FROM {name} LIMIT 10001")]
            if len(rows) > 10000:
                raise StateError("Private snapshot row bound")
            private[name] = rows
        raw = encode({"schema": "cryptowatcher.config-recovery.v1", "tables": private}).encode()
        if len(raw) > MAX_SNAPSHOT:
            raise StateError("Private snapshot byte bound")
        legacy = private["legacy_import"]
        if len(legacy) != 1:
            raise StateError("Migration identity missing")
        history = History(source)
        records: dict[str, dict[str, Any]] = {}
        sampled = 0
        for kind in ("event", "outcome", "episode", "comparison"):
            for reverse in (False, True):
                for _, value in history.items(kind + "-head:", 4, reverse=reverse):
                    if not isinstance(value, dict) or value.get("schema") == HEAD_SCHEMA:
                        raise StateError("Incomplete sampled public record")
                    records[kind] = value
                    sampled += 1
        # The timestamp index avoids an unbounded JSON scan of old heads.
        recent = []
        for _, item in history.items("history:", 16, reverse=True):
            key = "event-head:" + item["event_id"]
            value = history.get(key)
            stored = source.get_runtime("radar:v1:" + key)
            if value["available_at_ms"] >= after_ms:
                recent.append(stored.get("schema") == HEAD_SCHEMA)
        if not records.get("event"):
            raise StateError("Real event history sample unavailable")
        if after_ms and recent and not all(recent):
            raise StateError("New event head still duplicates immutable revision")
        config_hash = hashlib.sha256(raw).hexdigest()
        if destination is not None:
            if (
                destination.is_symlink()
                or destination.exists()
                or destination.parent.resolve() != destination.parent
            ):
                raise StateError("Configuration destination must be new")
            with destination.open("xb") as output:
                os.chmod(destination, 0o600)
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            if destination.read_bytes() != raw:
                raise StateError("Configuration snapshot readback failed")
        with tempfile.TemporaryDirectory(dir=scratch) as temporary:
            path = Path(temporary) / "fixture.sqlite3"
            with closing(sqlite3.connect(path)) as target:
                create_schema(target)
                for name, rows in json.loads(raw)["tables"].items():
                    for row in rows:
                        target.execute(f"INSERT INTO {name} VALUES ({','.join('?' for _ in row)})", row)
                target.commit()
            with closing(Store(path, 233382575)) as fixture:
                fixture.db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                if fixture.snapshot() != settings or fixture.enabled() != source.enabled():
                    raise StateError("Configuration restore rehearsal mismatch")
                writer = History(fixture)
                for kind, value in records.items():
                    with fixture.db:
                        writer.put(f"{kind}:{value[kind + '_id']}:{value['revision']:010d}", value)
                        writer.put(kind + "-head:" + value[kind + "_id"], value)
                    updated = dict(value, revision=value["revision"] + 1)
                    with fixture.db:
                        writer.version(kind, updated)
                    if writer.get(kind + "-head:" + value[kind + "_id"]) != updated:
                        raise StateError("Additive fixture write mismatch")
            with closing(Store(path, 233382575, readonly=True)) as fixture:
                for kind, value in records.items():
                    writer = History(fixture)
                    if writer.get(kind + "-head:" + value[kind + "_id"]) != dict(
                        value, revision=value["revision"] + 1
                    ):
                        raise StateError("Additive fixture restart mismatch")
                    if writer.get(f"{kind}:{value[kind + '_id']}:{value['revision']:010d}") != value:
                        raise StateError("Legacy fixture revision changed")
        if time.monotonic() >= deadline:
            raise StateError("Bounded compatibility deadline exceeded")
        return {
            "state": "BOUNDED_COMPATIBILITY_PASS",
            "full_history_integrity": "NOT_RUN_OWNER_EXCEPTION",
            "full_history_backup": "FULL_HISTORY_BACKUP_NOT_RUN_OWNER_EXCEPTION",
            "configuration_sha256": config_hash,
            "configuration_bytes": len(raw),
            "configuration_revision": settings.revision,
            "legacy_sha256": legacy[0][0],
            "sampled_public_records": sampled,
            "fixture_write_restart": "PASS",
            "recent_compact_events": len(recent) if after_ms else 0,
            "new_write_status": "PASS" if after_ms and recent else "NO_EVENT_IN_BOUNDED_WINDOW",
            "source_readonly": True,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--after-ms", type=int, default=0)
    args = parser.parse_args()
    try:
        value = probe(
            Path(os.environ["DATABASE_FILE"]),
            Path("/probe"),
            destination=args.destination,
            after_ms=args.after_ms,
        )
        print(json.dumps(value))
        return 0
    except Exception as exc:
        print(json.dumps({"state": "BLOCKED", "error_class": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
