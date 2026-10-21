"""One full pinned source capture and an independently reopened standard ZSTD backup."""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

import apsw

from .compressed_validation import Image, QualificationError, classified_errors, snapshot_image
from .config import OwnerSettings
from .storage import IntegrityError, encode
from .validation import fingerprint, scratch_space
from .validation_archive import BLOCK, ENTRY, HEADER, PREFIX_BYTES, RESERVE_BYTES, TAIL, ArchiveArena


def source_facts(database: apsw.Connection, owner: int) -> dict[str, Any]:
    row = database.execute("SELECT owner_id,revision,settings_json FROM owner_settings").fetchone()
    if row is None or row[0] != owner:
        raise QualificationError("VALIDATED_OWNER_MISMATCH")
    imported = database.execute("SELECT source_sha256 FROM legacy_import").fetchone()
    preference = database.execute(
        "SELECT enabled FROM delivery_preferences WHERE chat_id=?", (owner,)
    ).fetchone()
    version = database.execute("PRAGMA user_version").fetchone()
    if version is None or version[0] not in (1, 2):
        raise QualificationError("UNKNOWN_VALIDATED_SCHEMA")
    settings = OwnerSettings.from_payload(json.loads(row[2]))
    return {
        "status": "integrity_ok",
        "schema": version[0],
        "owner_id": owner,
        "configuration_revision": row[1],
        "settings_sha256": hashlib.sha256(encode(settings.payload()).encode()).hexdigest(),
        "legacy_sha256": imported[0] if imported else None,
        "alerts_enabled": bool(preference and preference[0]),
        "validation_mode": "FRAMED_DISK",
    }


def backup(
    source: Path, destination: Path, directory: Path, *, owner: int, deadline: float
) -> dict[str, Any]:
    """Transfer full-integrity proof only after the persisted raw bytes match exactly.

    The full SQLite scan covers every page/index/CHECK/current head of the pinned
    snapshot. A separate closed-file reader reconstructs every persisted byte and
    verifies its SHA against that same scan; a second live capture is unnecessary.
    """
    started = time.monotonic()
    phase = "PIN_SOURCE"
    phase_started = started
    timings: dict[str, float] = {}

    def stage(value: str) -> None:
        nonlocal phase, phase_started
        now = time.monotonic()
        timings[phase] = timings.get(phase, 0) + max(0.0, now - phase_started)
        phase, phase_started = value, now

    arena: ArchiveArena | None = None
    reader: apsw.Connection | None = None
    try:
        with classified_errors(deadline):
            scratch_space(directory, 0)
            if source.resolve() != source or destination.resolve() != destination or source == destination:
                raise QualificationError("UNSAFE_VALIDATION_PATH")
            reader = apsw.Connection(
                source.as_uri() + "?mode=ro", flags=apsw.SQLITE_OPEN_READONLY | apsw.SQLITE_OPEN_URI
            )
            reader.execute("BEGIN")
            row = reader.execute("SELECT owner_id FROM owner_settings").fetchone()
            pages = reader.execute("PRAGMA page_count").fetchone()
            page_size = reader.execute("PRAGMA page_size").fetchone()
            if row is None or row[0] != owner or pages is None or page_size is None:
                raise QualificationError("VALIDATED_OWNER_MISMATCH")
            raw_size = pages[0] * page_size[0]
            if type(raw_size) is not int or raw_size <= 0:
                raise QualificationError("INVALID_ARCHIVE_BOUND")
            count = (raw_size + BLOCK - 1) // BLOCK
            overhead = PREFIX_BYTES + 8 + HEADER.size + ENTRY.size * count + 32 + TAIL.size + count * 128
            # The original1.5GiB free-disk reserve remains mandatory at every write.
            # Incompressible snapshots exceeding the admitted archive fail closed.
            capacity = min(raw_size + overhead, shutil.disk_usage(destination.parent).free - RESERVE_BYTES)
            arena = ArchiveArena(
                destination, max_raw_bytes=raw_size, max_archive_bytes=capacity, deadline=deadline
            )
            with snapshot_image(
                source,
                directory,
                deadline=deadline,
                archive_store=arena,
                source_snapshot=reader,
                progress=stage,
            ) as (database, image):
                facts = source_facts(database, owner)
                expected = arena.raw_sha256
                if image.size != raw_size:
                    raise QualificationError("BYTE_IDENTITY_MISMATCH")
            arena.assert_unchanged()
            arena.close()
            arena = None
            stage("INDEPENDENT_PERSISTED_READBACK")
            arena = ArchiveArena.open(
                destination, max_raw_bytes=raw_size, max_archive_bytes=capacity, deadline=deadline
            )
            reopened = Image.from_archive_store(arena)
            if (
                arena.raw_bytes != raw_size
                or arena.raw_sha256 != expected
                or reopened.reconstructed_hash() != expected
            ):
                raise QualificationError("BYTE_IDENTITY_MISMATCH")
            arena.assert_unchanged()
            stage("ARCHIVE_FINGERPRINT")
            digest, archive_bytes = fingerprint(destination, deadline)
            arena.assert_unchanged()
            facts["snapshot_sha256"] = expected
            stage("COMPLETE")
            return {
                "phase": "COMPLETE",
                "validation_mode": "FRAMED_DISK",
                "source_validation": facts,
                "sha256": expected,
                "bytes": raw_size,
                "archive_encoding": "ZSTD",
                "archive_bytes": archive_bytes,
                "archive_sha256": digest,
                "archive_limit_bytes": capacity,
                "disk_reserve_bytes": RESERVE_BYTES,
                "sqlite_cache_kib": 128 * 1024,
                "integrity_basis": "FULL_VALIDATED_CAPTURE_AND_INDEPENDENT_EQUAL_RAW_SHA256",
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "phase_seconds": {key: round(value, 3) for key, value in timings.items()},
            }
    except IntegrityError as error:
        error.backup_metrics = {
            "phase": phase,
            **{"completed_" + key.lower() + "_seconds": round(value, 3) for key, value in timings.items()},
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        raise
    finally:
        if reader is not None:
            reader.close()
        if arena is not None:
            arena.close()
