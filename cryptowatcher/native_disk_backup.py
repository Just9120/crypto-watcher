"""Native pinned disk capture/check, then a separately verified framed ZSTD archive."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import apsw

from .compressed_validation import Image, QualificationError, budget, classified_errors
from .disk_validation import CACHE_KIB, MMAP_BYTES, validated_disk
from .framed_backup import source_facts
from .storage import IntegrityError
from .validation import fingerprint, scratch_space
from .validation_archive import BLOCK, ENTRY, HEADER, PREFIX_BYTES, TAIL, ArchiveArena

MODE = "NATIVE_DISK_FRAMED"
BASIS = "FULL_NATIVE_DISK_CHECK_AND_INDEPENDENT_EQUAL_RAW_SHA256"
DISK_RESERVE_BYTES = 6 * 1024**3
CONCURRENT_HEADROOM_BYTES = 1024**3


def capture_directory(destination: Path) -> Path:
    return destination.with_name(destination.name + ".capture")


def capacity(raw_bytes: int, free: int) -> int:
    count = (raw_bytes + BLOCK - 1) // BLOCK
    overhead = PREFIX_BYTES + 8 + HEADER.size + ENTRY.size * count + 32 + TAIL.size + count * 128
    admitted = min(raw_bytes + overhead, free - raw_bytes - DISK_RESERVE_BYTES - CONCURRENT_HEADROOM_BYTES)
    if raw_bytes <= 0 or admitted <= overhead:
        raise QualificationError("ARCHIVE_DISK_RESERVE")
    return admitted


def backup(
    source: Path, destination: Path, directory: Path, *, owner: int, deadline: float
) -> dict[str, Any]:
    started = phase_started = time.monotonic()
    phase = "PIN_SOURCE"
    timings: dict[str, float] = {}
    reader: apsw.Connection | None = None
    writer: apsw.Connection | None = None
    arena: ArchiveArena | None = None
    capture = capture_directory(destination)
    raw = capture / "snapshot.sqlite3"
    raw_size = 0
    raw_removed = False

    def receipt(state: str) -> None:
        if not capture.is_dir() or capture.is_symlink():
            return
        temporary = capture / "receipt.new"
        payload = {
            "schema": "cryptowatcher.native-disk-capture.v1",
            "state": state,
            "phase": phase,
            "raw_bytes": raw_size,
            "raw_snapshot_removed": raw_removed,
            "archive": destination.name,
        }
        with os.fdopen(
            os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w", encoding="utf-8"
        ) as output:
            output.write(json.dumps(payload, sort_keys=True))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, capture / "receipt.json")

    def stage(value: str) -> None:
        nonlocal phase, phase_started
        now = time.monotonic()
        timings[phase] = timings.get(phase, 0) + max(0.0, now - phase_started)
        phase, phase_started = value, now
        receipt("IN_PROGRESS")

    try:
        with classified_errors(deadline):
            budget(deadline)
            scratch_space(directory, 0)
            if (
                source.resolve() != source
                or destination.resolve() != destination
                or source == destination
                or destination.exists()
                or destination.is_symlink()
                or capture.exists()
                or capture.is_symlink()
                or capture.parent.resolve() != capture.parent
            ):
                raise QualificationError("UNSAFE_VALIDATION_PATH")
            reader = apsw.Connection(
                source.as_uri() + "?mode=ro", flags=apsw.SQLITE_OPEN_READONLY | apsw.SQLITE_OPEN_URI
            )
            reader.execute("BEGIN")
            row = reader.execute("SELECT owner_id FROM owner_settings").fetchone()
            pages = reader.execute("PRAGMA page_count").fetchone()
            page = reader.execute("PRAGMA page_size").fetchone()
            if row is None or row[0] != owner or pages is None or page is None:
                raise QualificationError("VALIDATED_OWNER_MISMATCH")
            raw_size = pages[0] * page[0]
            admitted = capacity(raw_size, shutil.disk_usage(destination.parent).free)
            capture.mkdir(mode=0o700)
            handle = os.open(raw, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            created = os.fstat(handle)
            os.close(handle)
            writer = apsw.Connection(str(raw), flags=apsw.SQLITE_OPEN_READWRITE)
            writer.execute("PRAGMA journal_mode=MEMORY")
            writer.execute("PRAGMA synchronous=FULL")
            writer.execute("PRAGMA cache_size=-32768")
            stage("SOURCE_COPY")
            with writer.backup("main", reader, "main") as transfer:
                while not transfer.done:
                    budget(deadline)
                    transfer.step(256)
                    required = transfer.remaining * page[0] + admitted + DISK_RESERVE_BYTES
                    if shutil.disk_usage(destination.parent).free < required:
                        raise QualificationError("ARCHIVE_DISK_RESERVE")
            reader.close()
            reader = None
            writer.close()
            writer = None
            stage("SNAPSHOT_SEAL")
            metadata = raw.lstat()
            if (
                (metadata.st_dev, metadata.st_ino) != (created.st_dev, created.st_ino)
                or raw.resolve() != raw
                or metadata.st_size != raw_size
                or any(Path(str(raw) + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))
            ):
                raise QualificationError("SOURCE_CHANGED")
            expected, measured_size = fingerprint(raw, deadline)
            if measured_size != raw_size:
                raise QualificationError("BYTE_IDENTITY_MISMATCH")
            stage("FULL_INTEGRITY")
            with validated_disk(raw, deadline=deadline) as database:
                if database.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise QualificationError("DATABASE_CORRUPT")
                cache = database.execute("PRAGMA cache_size").fetchone()
                mmap_limit = database.execute("PRAGMA mmap_size").fetchone()
                if (
                    cache is None
                    or cache[0] != -CACHE_KIB
                    or mmap_limit is None
                    or mmap_limit[0] != MMAP_BYTES
                    or database.total_changes() != 0
                    or database.readonly("main")
                ):
                    raise QualificationError("SQLITE_VALIDATION_ERROR")
                stage("SOURCE_METADATA")
                facts = source_facts(database, owner)
                facts["validation_mode"] = MODE
                facts["snapshot_sha256"] = expected
            stage("FULL_BYTE_IDENTITY")
            if shutil.disk_usage(destination.parent).free < admitted + DISK_RESERVE_BYTES:
                raise QualificationError("ARCHIVE_DISK_RESERVE")
            arena = ArchiveArena(
                destination,
                max_raw_bytes=raw_size,
                max_archive_bytes=admitted,
                deadline=deadline,
                reserve_bytes=DISK_RESERVE_BYTES,
            )
            image = Image(raw, limit=arena.limit, deadline=deadline, archive_store=arena)
            if image.size != raw_size or image.sha256 != expected:
                raise QualificationError("BYTE_IDENTITY_MISMATCH")
            arena.assert_unchanged()
            arena.close()
            arena = None
            del image
            stage("INDEPENDENT_PERSISTED_READBACK")
            arena = ArchiveArena.open(
                destination, max_raw_bytes=raw_size, max_archive_bytes=admitted, deadline=deadline
            )
            if arena.raw_bytes != raw_size or arena.raw_sha256 != expected:
                raise QualificationError("BYTE_IDENTITY_MISMATCH")
            restored = Image.from_archive_store(arena)
            if restored.reconstructed_hash() != expected:
                raise QualificationError("BYTE_IDENTITY_MISMATCH")
            del restored
            arena.assert_unchanged()
            stage("ARCHIVE_FINGERPRINT")
            archive_hash, archive_bytes = fingerprint(destination, deadline)
            arena.assert_unchanged()
            arena.close()
            arena = None
            budget(deadline)
            # Only the new byte-identical raw duplicate is removed. The receipt,
            # complete archive, source and every pre-existing artifact are retained.
            current = raw.lstat()
            if (
                raw.resolve() != raw
                or raw.parent != capture
                or raw.parent.resolve() != capture
                or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
                != (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
            ):
                raise QualificationError("UNSAFE_VALIDATION_PATH")
            raw.unlink()
            raw_removed = not raw.exists()
            if not raw_removed:
                raise QualificationError("VALIDATION_IO_ERROR")
            stage("COMPLETE")
            receipt("COMPLETE")
            return {
                "phase": "COMPLETE",
                "validation_mode": MODE,
                "source_validation": facts,
                "sha256": expected,
                "bytes": raw_size,
                "archive_encoding": "ZSTD",
                "archive_bytes": archive_bytes,
                "archive_sha256": archive_hash,
                "archive_limit_bytes": admitted,
                "disk_reserve_bytes": DISK_RESERVE_BYTES,
                "concurrent_disk_headroom_bytes": CONCURRENT_HEADROOM_BYTES,
                "temporary_raw_bytes": raw_size,
                "raw_snapshot_removed": raw_removed,
                "sqlite_cache_kib": CACHE_KIB,
                "mmap_limit_bytes": MMAP_BYTES,
                "sqlite_version": apsw.sqlitelibversion(),
                "apsw_version": apsw.apswversion(),
                "integrity_basis": BASIS,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "phase_seconds": {k: round(v, 3) for k, v in timings.items()},
            }
    except IntegrityError as error:
        error.backup_metrics = {
            "phase": phase,
            **{"completed_" + k.lower() + "_seconds": round(v, 3) for k, v in timings.items()},
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        try:
            receipt("INCOMPLETE_RETAINED")
        except OSError:
            pass
        if not isinstance(error, QualificationError) and error.code in (
            "DATABASE_CORRUPT",
            "TIME_BUDGET_EXCEEDED",
        ):
            compatible = QualificationError(error.code)
            compatible.backup_metrics = error.backup_metrics
            raise compatible from error
        raise
    finally:
        if writer is not None:
            writer.close()
        if reader is not None:
            reader.close()
        if arena is not None:
            arena.close()
