"""Full validation on private tmpfs copies; persistent data is never rewritten."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import tempfile
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

import apsw

from .storage import IntegrityError, check_integrity

HEADROOM = 64 * 1024 * 1024


def budget(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise IntegrityError("TIME_BUDGET_EXCEEDED", "Validation time budget exceeded")


def scratch_space(directory: Path, required: int) -> None:
    """Only a private, explicitly mounted tmpfs is accepted for production validation."""
    if any(p.is_symlink() for p in (directory, *directory.parents)):
        raise IntegrityError("DATABASE_ERROR", "Validation directory must be a private tmpfs")
    try:
        metadata = directory.stat()
        mounted = any(
            line.split(" - ", 1)[0].split()[4] == str(directory.resolve())
            and line.split(" - ", 1)[1].split()[0] == "tmpfs"
            for line in Path("/proc/self/mountinfo").read_text().splitlines()
        )
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or not mounted
        ):
            raise IntegrityError("DATABASE_ERROR", "Validation directory must be a private tmpfs")
        available = os.statvfs(directory)
        if available.f_bavail * available.f_frsize < required + HEADROOM:
            raise IntegrityError("DATABASE_ERROR", "Insufficient validation scratch space")
    except (OSError, AttributeError, ValueError, IndexError) as exc:
        raise IntegrityError("DATABASE_ERROR", "Validation tmpfs is unavailable") from exc


def fingerprint(path: Path, deadline: float, *, destination: Path | None = None) -> tuple[str, int]:
    """Stream bytes with stable identity checks, without loading a second Python copy."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise IntegrityError("DATABASE_ERROR", "Validation source must be a regular file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    digest = hashlib.sha256()
    total = 0
    output = None
    try:
        with os.fdopen(descriptor, "rb", buffering=0) as source:
            if destination is not None:
                output = os.fdopen(
                    os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb", buffering=0
                )
            opened = os.fstat(source.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise IntegrityError("DATABASE_ERROR", "Validation source changed")
            while True:
                budget(deadline)
                block = source.read(1024 * 1024)
                if not block:
                    break
                if output is not None and output.write(block) != len(block):
                    raise IntegrityError("IO_ERROR", "Validation copy did not complete")
                digest.update(block)
                total += len(block)
        if output is not None:
            os.fsync(output.fileno())
    finally:
        if output is not None:
            output.close()
    after = path.lstat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or total != before.st_size:
        raise IntegrityError("DATABASE_ERROR", "Validation source changed")
    budget(deadline)
    return digest.hexdigest(), total


def validate_backup(
    path: Path, directory: Path, *, deadline: float, compressed: bool = False
) -> dict[str, str | int]:
    """The closed persistent backup and the fully validated copy must have identical bytes."""
    try:
        budget(deadline)
        if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise IntegrityError("DATABASE_ERROR", "Backup must be closed before RAM validation")
        if compressed:
            from .compressed_validation import classified_errors, validate_closed

            scratch_space(directory, 0)
            with classified_errors(deadline):
                return validate_closed(path, directory=directory, deadline=deadline)
        scratch_space(directory, path.stat().st_size)
        with tempfile.TemporaryDirectory(prefix="cw-backup-", dir=directory) as work:
            clone = Path(work) / "database.sqlite3"
            expected = fingerprint(path, deadline, destination=clone)
            if fingerprint(clone, deadline) != expected:
                raise IntegrityError("IO_ERROR", "Validation copy identity mismatch")
            with closing(sqlite3.connect(clone)) as connection:
                check_integrity(connection, deadline=deadline)
            if fingerprint(clone, deadline) != expected or fingerprint(path, deadline) != expected:
                raise IntegrityError("IO_ERROR", "Validated backup bytes changed")
            return {"mode": "TMPFS", "sha256": expected[0], "bytes": expected[1]}
    except OSError as exc:
        raise IntegrityError("IO_ERROR", "Backup validation I/O failed; original preserved") from exc


@contextmanager
def validated_snapshot(
    path: Path, directory: Path, *, deadline: float, compressed: bool = False
) -> Iterator[sqlite3.Connection | apsw.Connection]:
    """Release the live WAL snapshot before the expensive full integrity check."""
    try:
        budget(deadline)
        scratch_space(directory, 0)
        if compressed:
            from .compressed_validation import snapshot as compressed_snapshot

            with compressed_snapshot(path, directory, deadline=deadline) as connection:
                yield connection
            return
        with tempfile.TemporaryDirectory(prefix="cw-state-", dir=directory) as work:
            with closing(sqlite3.connect(Path(work) / "database.sqlite3")) as snapshot:
                with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                    source.execute("BEGIN")
                    source.execute("SELECT owner_id FROM owner_settings").fetchone()
                    size = (
                        source.execute("PRAGMA page_count").fetchone()[0]
                        * source.execute("PRAGMA page_size").fetchone()[0]
                    )
                    scratch_space(directory, size)

                    def progress(status: int, remaining: int, total: int) -> None:
                        budget(deadline)

                    source.backup(snapshot, pages=256, progress=progress, sleep=0.01)
                check_integrity(snapshot, deadline=deadline)
                yield snapshot
                budget(deadline)
    except OSError as exc:
        raise IntegrityError("IO_ERROR", "State validation I/O failed; original preserved") from exc
    except sqlite3.DatabaseError as exc:
        code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
        if code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
            raise IntegrityError("DATABASE_CORRUPT", "Database integrity check failed") from exc
        raise IntegrityError("DATABASE_ERROR", "State snapshot validation did not complete") from exc
