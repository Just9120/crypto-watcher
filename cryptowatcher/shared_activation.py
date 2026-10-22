"""Explicit offline shared-storage adoption; no startup install, reset or delete."""

from __future__ import annotations

import math
import os
import re
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from . import (
    catalogue_reader,
    packet_bundles,
    packet_store,
    record_catalog,
    shared_record_work,
    shared_records,
    source_column_work,
)
from .lease import RuntimeLease
from .reset_history import private_state, signature
from .storage import StateError, Store


def _identity(store: Store) -> dict[str, Any]:
    return {
        "history_generation": store.get_runtime("history_generation"),
        "configuration_revision": store.snapshot().revision,
        "private_sha256": signature(private_state(store)),
    }


def status(store: Store) -> dict[str, Any]:
    """A coherent readonly identity token, never an activation or ready claim."""
    own_snapshot = not store.db.in_transaction
    if own_snapshot:
        store.db.execute("BEGIN")
    try:
        value = _identity(store)
        marker = (
            store.db.execute("SELECT value FROM market_meta WHERE key='shared_records'").fetchone()
            if store.bounded
            else None
        )
        if marker is not None:
            shared_records.validate(store.db)
        revision = os.environ.get("SOURCE_REVISION", "UNSET")
        return {
            "schema": "cryptowatcher.shared-storage-preflight.v1",
            "phase": "READ_ONLY_SNAPSHOT",
            "source_revision": revision if re.fullmatch("[0-9a-f]{40}", revision) else "UNSET",
            **value,
            "bounded_storage": store.bounded,
            "layout": marker[0] if marker else None,
            "shared_work": shared_record_work.header(store.db) if marker and marker[0] == "3" else None,
            "source_columns": source_column_work.status(store.db) if store.bounded else {"enabled": False},
            "scope": "identity/schema/work header only; no full integrity/capacity or readiness claim",
        }
    finally:
        if own_snapshot:
            store.db.rollback()


def _guard(store: Store, generation: str, revision: int, private: str) -> dict[str, Any]:
    value = _identity(store)
    if (
        not store.bounded
        or not isinstance(value["history_generation"], dict)
        or value["history_generation"].get("id") != generation
        or value["configuration_revision"] != revision
        or value["private_sha256"] != private
    ):
        raise StateError("Shared activation source identity changed")
    if record_catalog.layout(store.db) != 3 or not catalogue_reader.reading(store.db):
        raise StateError("Shared activation requires the current owned catalogue")
    packet_bundles.validate(store.db)
    source_column_work.guards(store.db)
    if source_column_work.enabled(store.db):
        source_column_work.checkpoint(store.db)
    marker = store.db.execute("SELECT value FROM market_meta WHERE key='shared_records'").fetchone()
    if marker is not None:
        shared_records.validate(store.db)
    else:
        guard = store.db.execute("SELECT sql FROM sqlite_master WHERE name='market_packet_move'").fetchone()
        if guard is None or " ".join(guard[0].split()) != " ".join(packet_store.MOVE_GUARD.split()):
            raise StateError("Unknown source packet relocation guard")
    return value


def activate(
    database: Path,
    backup: Path,
    *,
    owner: int,
    expected_source: str,
    expected_generation: str,
    expected_revision: int,
    expected_private: str,
    timeout_seconds: float = 900,
) -> dict[str, Any]:
    """Resume only known layout2/3, preserving all previous files and settings.

    Each existing installer commits its guarded atomic phase. After an error or
    an unknown outcome inspect the layout first; a known partial layout2 remains
    readable by this artifact. Forward recovery is required, never an old image
    that does not understand the installed shared schema.
    """
    if (
        type(owner) is not int
        or owner <= 0
        or re.fullmatch("[0-9a-f]{40}", expected_source or "") is None
        or os.environ.get("SOURCE_REVISION") != expected_source
        or re.fullmatch("[0-9a-f]{32}", expected_generation or "") is None
        or type(expected_revision) is not int
        or expected_revision < 1
        or re.fullmatch("[0-9a-f]{64}", expected_private or "") is None
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 900
    ):
        raise StateError("Invalid shared activation identity or budget")
    if (
        database == backup
        or not database.is_file()
        or any(
            not path.is_absolute()
            or path.resolve() != path
            or any(parent.is_symlink() for parent in (path, *path.parents))
            for path in (database, backup)
        )
        or not backup.parent.is_dir()
    ):
        raise StateError("Unsafe shared activation path")
    started = time.monotonic()
    deadline = started + timeout_seconds

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise StateError("Shared activation time budget exceeded; inspect the retained layout")
        return value

    with RuntimeLease(database):
        # Refuse stale identity before opening a writer or creating a backup.
        with closing(Store(database, owner, readonly=True)) as reader:
            remaining()
            reader.db.execute("BEGIN")
            before = _guard(reader, expected_generation, expected_revision, expected_private)
            if shared_record_work.enabled(reader.db) and source_column_work.enabled(reader.db):
                shared_record_work.validate(reader.db)
                state = shared_record_work.header(reader.db)
                remaining()
                return {
                    "operation": "ENABLE_SHARED_SB2_STORAGE",
                    "phase": "ALREADY_ENABLED",
                    "source_revision": expected_source,
                    **before,
                    "writer_enabled": state["writer_enabled"],
                    "source_columns": source_column_work.status(reader.db),
                }
        if backup.exists() or any(
            Path(str(backup) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")
        ):
            raise StateError("Shared activation backup destination already exists")
        with closing(Store(database, owner)) as writer:
            writer.db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            try:
                if _guard(writer, expected_generation, expected_revision, expected_private) != before:
                    raise StateError("Shared activation source identity changed")
                proof = writer.backup(backup, timeout_seconds=remaining())
                if proof.get("phase") != "COMPLETE":
                    raise StateError("Shared activation backup is incomplete; source preserved")
                remaining()
                if _guard(writer, expected_generation, expected_revision, expected_private) != before:
                    raise StateError("Shared activation source identity changed after backup")
                shared_records.install(writer.db, writer.cached_bytes)
                remaining()
                shared_record_work.install(writer.db, int(time.time() * 1000), writer.cached_bytes)
                with writer.db:
                    source_column_work.enable(writer.db)
                if _guard(writer, expected_generation, expected_revision, expected_private) != before:
                    raise StateError("Shared activation private state changed; preserve backup and inspect")
            finally:
                writer.db.set_progress_handler(None, 0)
        with closing(Store(database, owner, readonly=True)) as reopened:
            remaining()
            reopened.db.execute("BEGIN")
            if _guard(reopened, expected_generation, expected_revision, expected_private) != before:
                raise StateError("Shared activation reopen identity mismatch")
            shared_record_work.validate(reopened.db)
            state = shared_record_work.header(reopened.db)
            column_state = source_column_work.status(reopened.db)
            if not column_state["enabled"]:
                raise StateError("Source column adoption not observed after reopen")
        remaining()
        return {
            "operation": "ENABLE_SHARED_SB2_STORAGE",
            "phase": "COMPLETE",
            "source_revision": expected_source,
            **before,
            "writer_enabled": state["writer_enabled"],
            "source_columns": column_state,
            "backup": proof,
            "elapsed_seconds": time.monotonic() - started,
            "recovery": "FORWARD_COMPATIBLE_ARTIFACT_ONLY; inspect retained layout/files before retry",
        }
