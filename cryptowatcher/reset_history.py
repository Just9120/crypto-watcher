"""Prepare a fresh market generation. Never switches or deletes a live database.

The separate operator cutover binds this candidate to the exact source private
state, image/unit and paths. Old market history is intentionally not copied.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
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
LIGHT_POLICY = "EPISODE_ORIGIN_V2_72H"


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


def light_private(value: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Only the agreed policy fields change; original recovery stays untouched."""
    from .config import ConfigError, OwnerSettings
    from .detector_config import validate_effective_configuration

    normalized = copy.deepcopy(value)
    owners = [r for r in normalized["tables"]["owner_settings"] if r[0] == value["owner_id"]]
    if len(owners) != 1:
        raise StateError("Private transition owner row mismatch")
    row = owners[0]
    payload = json.loads(row[2])
    original = OwnerSettings.from_payload(payload)
    changed = {}
    for key, target in (("candle_retention_days", 3), ("active_detectors", list(original.active_detectors))):
        if key not in payload or payload[key] != target:
            changed[key] = {"before": payload.get(key, "ABSENT"), "after": target}
            payload[key] = target
    try:
        validate_effective_configuration(OwnerSettings.from_payload(payload))
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if changed:
        row[1] += 1
        row[2] = encode(payload)
    return normalized, changed


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
    source: Path,
    destination: Path,
    recovery: Path,
    owner: int,
    *,
    now_ms: int | None = None,
    expected_generation: str | None = None,
    preserved_archive_sha256: str | None = None,
    discard_test_history: bool = False,
    light_radar_transition: bool = False,
) -> dict[str, Any]:
    if any(
        not p.is_absolute() or p.resolve() != p or any(q.is_symlink() for q in (p, *p.parents))
        for p in (source, destination, recovery)
    ):
        raise StateError("Unsafe reset path")
    if destination.exists() or recovery.exists() or source == destination:
        raise StateError("Reset destination already exists")
    if light_radar_transition and not discard_test_history:
        raise StateError("Light radar transition requires explicit exact-generation test-history discard")
    started = time.monotonic()
    deadline = started + 120
    if discard_test_history and (
        expected_generation is None
        or re.fullmatch("[0-9a-f]{32}", expected_generation) is None
        or preserved_archive_sha256 is not None
    ):
        raise StateError("Explicit discard needs exact generation and no preserved archive claim")
    measurement = not discard_test_history and (
        expected_generation is not None or preserved_archive_sha256 is not None
    )
    if measurement:
        if (
            not expected_generation
            or not preserved_archive_sha256
            or re.fullmatch("[0-9a-f]{64}", preserved_archive_sha256) is None
        ):
            raise StateError("Fresh measurement needs exact generation and preserved archive identity")
        digest = hashlib.sha256()
        with source.open("rb") as archive:
            for block in iter(lambda: archive.read(1024 * 1024), b""):
                if time.monotonic() >= deadline:
                    raise StateError("Archive identity time budget exceeded")
                digest.update(block)
        if digest.hexdigest() != preserved_archive_sha256:
            raise StateError("Preserved archive identity mismatch")
    with closing(Store(source, owner, readonly=True)) as old:
        old.db.execute("BEGIN")
        previous_generation = old.get_runtime("history_generation")
        if measurement or discard_test_history:
            if not previous_generation or previous_generation.get("id") != expected_generation:
                raise StateError("Fresh measurement predecessor generation mismatch")
        elif previous_generation is not None:
            raise StateError("History reset already performed; no automatic repeat")
        if isinstance(previous_generation, dict) and previous_generation.get("model") == LIGHT_POLICY:
            raise StateError("Light radar history already exists; reset refused")
        preserved = private_state(old)
    expected_private, configuration_changes = (
        light_private(preserved) if light_radar_transition else (preserved, {})
    )
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
        if configuration_changes:
            expected_row = next(r for r in expected_private["tables"]["owner_settings"] if r[0] == owner)
            with fresh.db:
                cursor = fresh.db.execute(
                    "UPDATE owner_settings SET revision=?,settings_json=? WHERE owner_id=? AND revision=?",
                    (expected_row[1], expected_row[2], owner, expected_row[1] - 1),
                )
                if cursor.rowcount != 1:
                    raise StateError("Private transition CAS mismatch")
        start = now_ms if now_ms is not None else int(time.time() * 1000)
        generation = {
            "schema": "cryptowatcher.history-generation.v1",
            "id": uuid.uuid4().hex,
            "started_ms": start,
            "reason": "OWNER_AUTHORIZED_TEST_HISTORY_DISCARD_20261022"
            if discard_test_history
            else (
                "OWNER_AUTHORIZED_FRESH_MEASUREMENT_20261017"
                if measurement
                else "OWNER_AUTHORIZED_TEST_HISTORY_RESET_20261016"
            ),
            "previous_market_history": "PRESERVED_IN_ARCHIVE"
            if measurement
            else "DISCARDED_BY_OWNER_DECISION",
            "bootstrap": "CALCULATION_WINDOW_ONLY",
            "prior_detail_available": False,
            "comparator_limitations": "NEW_HISTORY_INSUFFICIENT_PRIOR_DATA",
        }
        if light_radar_transition:
            generation.update(
                model=LIGHT_POLICY,
                reason="OWNER_AUTHORIZED_LIGHT_RADAR_TRANSITION_20261030",
                candle_window_hours=72,
                comparator_limitations="COMPARISONS_DISABLED_LEGACY_READ_ONLY",
            )
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
        expected = copy.deepcopy(expected_private)
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
        if light_radar_transition:
            receipt.update(light_radar_transition=True, configuration_changes=configuration_changes)
        if measurement:
            receipt.update(
                previous_generation_id=expected_generation,
                preserved_archive_sha256=preserved_archive_sha256,
                archive_source=str(source),
            )
        if discard_test_history:
            receipt.update(previous_generation_id=expected_generation, discard_test_history=True)
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
        generation = store.get_runtime("history_generation", {})
        if generation.get("model") == LIGHT_POLICY:
            expected, _ = light_private(expected)
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
    parser.add_argument("--expected-generation")
    parser.add_argument("--preserved-archive-sha256")
    parser.add_argument("--discard-test-history", action="store_true")
    parser.add_argument("--light-radar-transition", action="store_true")
    args = parser.parse_args()
    receipt = prepare(
        args.source,
        args.candidate,
        args.private_recovery,
        args.owner_id,
        expected_generation=args.expected_generation,
        preserved_archive_sha256=args.preserved_archive_sha256,
        discard_test_history=args.discard_test_history,
        light_radar_transition=args.light_radar_transition,
    )
    write_new(args.receipt, encode(receipt).encode())
    print(encode(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
