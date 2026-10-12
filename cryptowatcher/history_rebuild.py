"""Rebuild a closed backup into a new isolated candidate; never replace the source."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .events import HEAD_SCHEMA, PREFIX, History
from .storage import IntegrityError, StateError, Store, check_integrity, create_schema, encode

TABLES = {
    "owner_settings": ("owner_id", "revision", "settings_json"),
    "delivery_preferences": ("chat_id", "enabled"),
    "mutes": ("symbol", "mute_until"),
    "runtime": ("key", "payload_json"),
    "legacy_import": ("source_sha256", "original_json", "backup_name"),
}
DISK_RESERVE = 1536 * 1024 * 1024


def guard(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise IntegrityError("TIME_BUDGET_EXCEEDED", "Rebuild time budget exceeded")


def closed(path: Path) -> None:
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise StateError("Symlink refused")
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise StateError("Source must be a closed backup without sidecars")


def fingerprint(path: Path, deadline: float) -> str:
    closed(path)
    before = path.stat()
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise StateError("Source file identity changed")
        while block := stream.read(1024 * 1024):
            guard(deadline)
            digest.update(block)
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise StateError("Source changed during rebuild")
    closed(path)
    return digest.hexdigest()


def head_record(source: sqlite3.Connection, key: str, payload: str) -> tuple[str, dict[str, Any] | None]:
    if not isinstance(key, str) or not isinstance(payload, str):
        raise StateError("Invalid runtime record")
    relative = key.removeprefix(PREFIX)
    kind, separator, record_id = relative.partition("-head:")
    if not key.startswith(PREFIX) or not separator or kind not in ("event", "outcome"):
        return payload, None
    value = json.loads(payload)
    revision = value.get("revision") if isinstance(value, dict) else None
    if type(revision) is not int or revision < 1 or value.get(f"{kind}_id") != record_id:
        raise StateError("Invalid legacy head identity")
    reference = f"{kind}:{record_id}:{revision:010d}"
    row = source.execute("SELECT payload_json FROM runtime WHERE key=?", (PREFIX + reference,)).fetchone()
    if row is None:
        raise StateError("Legacy head revision missing")
    record = json.loads(row[0])
    if (
        not isinstance(record, dict)
        or record.get(f"{kind}_id") != record_id
        or record.get("revision") != revision
        or record.get("schema") == HEAD_SCHEMA
    ):
        raise StateError("Legacy head revision identity mismatch")
    compact = History._head(kind, record, reference)
    if value.get("schema") == HEAD_SCHEMA:
        if encode(value) != encode(compact):
            raise StateError("Compact head conflicts with immutable revision")
        return payload, record
    if encode(value) != encode(record):
        raise StateError("Legacy head conflicts with immutable revision")
    return encode(compact), record


def rebuild(
    source: Path,
    destination: Path,
    owner: int,
    expected: str,
    deadline: float,
    *,
    max_candidate_bytes: int | None = None,
) -> dict[str, Any]:
    if not math.isfinite(deadline) or deadline > time.monotonic() + 900:
        raise StateError("Invalid rebuild deadline")
    guard(deadline)
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise StateError("Expected qualified source SHA256 required")
    if source.resolve() == destination.resolve() or destination.exists():
        raise StateError("Destination must be a new isolated file")
    closed(source)
    closed(destination)
    if not source.is_file() or not destination.parent.is_dir():
        raise StateError("Source or destination directory unavailable")
    source_identity = source.stat()
    if os.name != "nt":
        parent = destination.parent.stat()
        if parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
            raise StateError("Destination directory must be private and owned")
    maximum = source.stat().st_size if max_candidate_bytes is None else max_candidate_bytes
    if type(maximum) is not int or maximum < 32768:
        raise StateError("Invalid candidate byte limit")
    if shutil.disk_usage(destination.parent).free < maximum + DISK_RESERVE:
        raise StateError("Insufficient disk for bounded isolated candidate")
    if fingerprint(source, deadline) != expected:
        raise StateError("Source fingerprint mismatch")
    destination.touch(mode=0o600, exist_ok=False)
    counts: dict[str, int] = {}
    savings = converted = 0
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as original:
        original.execute("PRAGMA query_only=ON")
        original.execute("PRAGMA cache_size=-131072")
        if original.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise StateError("Unsupported source schema")
        objects = original.execute(
            "SELECT type,name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"
        ).fetchall()
        if set(objects) != {("table", table) for table in TABLES}:
            raise StateError("Unsupported source objects; source preserved")
        with closing(sqlite3.connect(":memory:")) as reference_schema:
            create_schema(reference_schema)
            canonical = dict(
                reference_schema.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")
            )
        actual_schema = dict(original.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
        for table, columns in TABLES.items():
            if tuple(row[1] for row in original.execute(f"PRAGMA table_info({table})")) != columns:
                raise StateError("Unsupported source columns")
            if " ".join(actual_schema[table].lower().split()) != " ".join(canonical[table].lower().split()):
                raise StateError("Source constraints or collation differ from canonical schema")
        owners = original.execute("SELECT owner_id FROM owner_settings").fetchall()
        if owners != [(owner,)]:
            raise StateError("Source owner mismatch")
        # Only the exclusive new candidate is writable. Source is never opened by Store.
        # Its schema is created separately so malformed source constraints cannot carry over.
        with closing(sqlite3.connect(destination)) as schema:
            create_schema(schema)
        candidate = Store(destination, owner)
        try:
            candidate.db.execute("PRAGMA journal_mode=DELETE")
            candidate.db.execute("PRAGMA cache_size=-65536")
            page_size = candidate.db.execute("PRAGMA page_size").fetchone()[0]
            allowed_pages = candidate.db.execute(f"PRAGMA max_page_count={maximum // page_size}").fetchone()[
                0
            ]
            if allowed_pages * page_size > maximum:
                raise StateError("Candidate schema exceeds byte limit")
            with candidate.db:
                for table, columns in TABLES.items():
                    count = 0
                    for row in original.execute(f"SELECT * FROM {table} ORDER BY rowid"):
                        guard(deadline)
                        if count % 1000 == 0 and shutil.disk_usage(destination.parent).free < DISK_RESERVE:
                            raise StateError("Disk reserve exhausted; source preserved")
                        values = tuple(row)
                        if table == "runtime":
                            payload, public = head_record(original, values[0], values[1])
                            if public is not None and payload != values[1]:
                                converted += 1
                                savings += len(values[1].encode()) - len(payload.encode())
                            values = (values[0], payload)
                        candidate.db.execute(
                            f"INSERT INTO {table} VALUES ({','.join('?' for _ in columns)})", values
                        )
                        count += 1
                    counts[table] = count
            check_integrity(candidate.db, deadline=deadline)
            candidate.snapshot()  # Current owner settings must be readable without resetting defaults.
            history = History(candidate)
            for table in TABLES:
                actual = candidate.db.execute(f"SELECT * FROM {table} ORDER BY rowid")
                verified = 0
                for row in original.execute(f"SELECT * FROM {table} ORDER BY rowid"):
                    guard(deadline)
                    values = tuple(row)
                    if table == "runtime":
                        payload, public = head_record(original, values[0], values[1])
                        if public is not None and encode(history.get(values[0][len(PREFIX) :])) != encode(
                            public
                        ):
                            raise StateError("Resolved head readback mismatch")
                        values = (values[0], payload)
                    found = actual.fetchone()
                    if (
                        found is None
                        or len(found) != len(values)
                        or any(type(a) is not type(b) or a != b for a, b in zip(found, values))
                    ):
                        raise StateError("Candidate row equivalence failed")
                    verified += 1
                if actual.fetchone() is not None or verified != counts[table]:
                    raise StateError("Candidate row count mismatch")
        finally:
            candidate.close()
    if fingerprint(source, deadline) != expected:
        raise StateError("Source changed; candidate not qualified")
    after = source.stat()
    if (
        source_identity.st_dev,
        source_identity.st_ino,
        source_identity.st_size,
        source_identity.st_mtime_ns,
    ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise StateError("Source metadata changed; candidate not qualified")
    candidate_hash = fingerprint(destination, deadline)
    return {
        "status": "ISOLATED_CANDIDATE_PASS",
        "source_sha256": expected,
        "candidate_sha256": candidate_hash,
        "candidate_bytes": destination.stat().st_size,
        "max_candidate_bytes": maximum,
        "table_rows": counts,
        "heads_converted": converted,
        "payload_bytes_saved": savings,
        "candidate_full_integrity": "PASS",
        "immutable_rows_and_resolved_heads": "PASS",
        "source_full_qualification": "REQUIRED_SEPARATELY_FOR_EXPECTED_HASH",
        "production_migration": "NOT_RUN",
        "incomplete_output": "PRESERVED_ON_FAILURE",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--owner-id", type=int, required=True)
    parser.add_argument("--qualified-source-sha256", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=900)
    parser.add_argument("--max-candidate-bytes", type=int)
    args = parser.parse_args()
    try:
        if not math.isfinite(args.timeout_seconds) or not 0 < args.timeout_seconds <= 900:
            raise StateError("Invalid rebuild budget")
        print(
            json.dumps(
                rebuild(
                    args.source,
                    args.destination,
                    args.owner_id,
                    args.qualified_source_sha256,
                    time.monotonic() + args.timeout_seconds,
                    max_candidate_bytes=args.max_candidate_bytes,
                )
            )
        )
        return 0
    except (StateError, sqlite3.DatabaseError, ValueError, OSError):
        print('{"status":"BLOCKED","production_migration":"NOT_RUN","candidate":"UNQUALIFIED"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
