"""Prepare a fresh market generation. Never switches or deletes a live database.

The separate operator cutover binds this candidate to the exact source private
state, image/unit and paths. Old market history is intentionally not copied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from .market_storage import enable
from .storage import StateError, Store, check_integrity, encode

PRIVATE_TABLES = ("owner_settings", "delivery_preferences", "mutes", "legacy_import")
PREFERENCES = ("radar:v1:digest-preferences", "radar:v1:agent:preference")
MAX_PRIVATE = 8 * 1024 * 1024


def private_state(store: Store) -> dict[str, Any]:
    store.snapshot()  # Authenticated owner/schema/typed settings validation.
    tables = {}
    for name in PRIVATE_TABLES:
        rows = [list(r) for r in store.db.execute(f"SELECT * FROM {name} ORDER BY 1 LIMIT 10001")]
        if len(rows) > 10000:
            raise StateError("Private state row bound")
        tables[name] = rows
    preferences = {k: store.get_runtime(k) for k in PREFERENCES}
    result = {
        "schema": "cryptowatcher.reset-private.v1",
        "owner_id": store.owner_id,
        "tables": tables,
        "preferences": preferences,
    }
    if len(encode(result).encode()) > MAX_PRIVATE:
        raise StateError("Private state byte bound")
    return result


def signature(value: Any) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


def write_new(path: Path, raw: bytes) -> None:
    if not path.is_absolute() or path.resolve() != path or any(p.is_symlink() for p in (path, *path.parents)):
        raise StateError("Unsafe reset artifact path")
    handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(handle, "wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())


def restore_private(store: Store, value: dict[str, Any]) -> None:
    if (
        value.get("schema") != "cryptowatcher.reset-private.v1"
        or value.get("owner_id") != store.owner_id
        or set(value["tables"]) != set(PRIVATE_TABLES)
        or set(value["preferences"]) != set(PREFERENCES)
    ):
        raise StateError("Invalid private recovery identity")
    with store.db:
        for name, rows in value["tables"].items():
            for row in rows:
                store.db.execute(f"INSERT INTO {name} VALUES (" + ",".join("?" for _ in row) + ")", row)
        for key, payload in value["preferences"].items():
            if payload is not None:
                store.put_runtime(key, payload)
    if private_state(store) != value:
        raise StateError("Private restore mismatch")


def prepare(
    source: Path, destination: Path, recovery: Path, owner: int, *, now_ms: int | None = None
) -> dict[str, Any]:
    if any(
        not p.is_absolute() or p.resolve() != p or any(q.is_symlink() for q in (p, *p.parents))
        for p in (source, destination, recovery)
    ):
        raise StateError("Unsafe reset path")
    if destination.exists() or recovery.exists() or source == destination:
        raise StateError("Reset destination already exists")
    started = time.monotonic()
    deadline = started + 120
    with closing(Store(source, owner, readonly=True)) as old:
        old.db.execute("BEGIN")
        if old.get_runtime("history_generation") is not None:
            raise StateError("History reset already performed; no automatic repeat")
        preserved = private_state(old)
    raw = encode(preserved).encode()
    write_new(recovery, raw)
    # Independently parse and restore exact preserved bytes before any source cutover.
    parsed = json.loads(recovery.read_bytes())
    if signature(parsed) != signature(preserved):
        raise StateError("Private recovery hash mismatch")
    rehearsal = destination.with_name(destination.name + ".private-restore")
    if rehearsal.exists():
        raise StateError("Private restore destination exists")
    with closing(Store(rehearsal, owner)) as restored:
        restore_private(restored, parsed)
        check_integrity(restored.db, deadline=deadline)
    with closing(Store(destination, owner)) as fresh:
        enable(fresh.db)
        fresh.bounded = True
        restore_private(fresh, parsed)
        start = now_ms if now_ms is not None else int(time.time() * 1000)
        generation = {
            "schema": "cryptowatcher.history-generation.v1",
            "id": uuid.uuid4().hex,
            "started_ms": start,
            "reason": "OWNER_AUTHORIZED_TEST_HISTORY_RESET_20261016",
            "previous_market_history": "DISCARDED_BY_OWNER_DECISION",
            "bootstrap": "CALCULATION_WINDOW_ONLY",
            "prior_detail_available": False,
            "comparator_limitations": "NEW_HISTORY_INSUFFICIENT_PRIOR_DATA",
        }
        fresh.put_runtime("history_generation", generation)
        # Preserve toggles; replace only the old digest schedule cursor. No old
        # queue, event delivery identity or source gap cursor is copied.
        pref = fresh.get_runtime(PREFERENCES[0])
        if pref is not None:
            step = fresh.snapshot().settings.digest_interval_min * 60000
            pref = {**pref, "next_end_ms": (start // step + 1) * step}
            fresh.put_runtime(PREFERENCES[0], pref)
        fresh.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        check_integrity(fresh.db, deadline=deadline)
        actual = private_state(fresh)
        expected = json.loads(raw)
        if expected["preferences"][PREFERENCES[0]] is not None:
            expected["preferences"][PREFERENCES[0]] = pref
        if actual != expected:
            raise StateError("Fresh private state mismatch")
        receipt = {
            "schema": "cryptowatcher.market-reset-candidate.v1",
            "state": "PASS",
            "private_source_sha256": signature(preserved),
            "private_fresh_sha256": signature(actual),
            "private_recovery_sha256": hashlib.sha256(raw).hexdigest(),
            "private_restore": "PASS",
            "integrity": "PASS",
            "owner_id": owner,
            "configuration_revision": fresh.snapshot().revision,
            "generation": generation,
            "removed_scope": [
                "all old market events/revisions/episodes/outcomes/comparisons/source windows/context",
                "market runtime/admission/recovery/delivery queues/cursors/attempts",
                "old conversion service journal/cursors",
            ],
            "preserved_tables": list(PRIVATE_TABLES),
            "preserved_runtime_preferences": list(PREFERENCES),
            "only_preference_cursor_changed": PREFERENCES[0] + ".next_end_ms",
            "backups_and_experiments": "NOT_TOUCHED",
            "source_modified_by_this_procedure": False,
            "source_live_writer_may_advance_market_data": True,
            "candidate_bytes": destination.stat().st_size,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    with closing(Store(destination, owner)) as reopened:
        if (
            signature(private_state(reopened)) != receipt["private_fresh_sha256"]
            or reopened.get_runtime("history_generation") != generation
        ):
            raise StateError("Fresh reopen proof failed")
    return receipt


def verify_private(database: Path, recovery: Path, owner: int) -> dict[str, Any]:
    """Post-start readback; only the digest scheduling cursor may advance."""
    expected = json.loads(recovery.read_bytes())
    with closing(Store(database, owner, readonly=True)) as store:
        actual = private_state(store)
    for value in (expected, actual):
        pref = value["preferences"][PREFERENCES[0]]
        if isinstance(pref, dict):
            pref.pop("next_end_ms", None)
    if actual != expected:
        raise StateError("Post-start private configuration mismatch")
    return {"state": "PASS", "preserved_configuration_sha256": signature(actual)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--private-recovery", type=Path, required=True)
    parser.add_argument("--owner-id", type=int, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    receipt = prepare(args.source, args.candidate, args.private_recovery, args.owner_id)
    write_new(args.receipt, encode(receipt).encode())
    print(encode(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
