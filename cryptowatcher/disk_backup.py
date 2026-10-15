"""Full snapshot/archive validation with private disk frames and bounded RAM cache."""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import sqlite3
import stat
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import IO, Any, BinaryIO, cast

import apsw
import zstandard

from . import compressed_validation as cv
from .validation import fingerprint

DISK_RESERVE = 1536 * 1024**2
DISK_HEADROOM = 64 * 1024**2


def private_disk(directory: Path) -> None:
    if directory.resolve() != directory or any(p.is_symlink() for p in (directory, *directory.parents)):
        raise cv.QualificationError("BACKING_PATH_REFUSED")
    directory.mkdir(mode=0o700, exist_ok=True)
    metadata = directory.stat()
    if not stat.S_ISDIR(metadata.st_mode) or not cv.disk_backing(directory):
        raise cv.QualificationError("DISK_BACKING_REQUIRED")
    if os.name == "posix" and (metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077):
        raise cv.QualificationError("PRIVATE_BACKING_DIRECTORY_REQUIRED")


def disk_limit(directory: Path, raw_size: int) -> int:
    private_disk(directory)
    if type(raw_size) is not int or raw_size <= 0:
        raise cv.QualificationError("INVALID_SOURCE_SIZE")
    available = shutil.disk_usage(directory).free - DISK_RESERVE - DISK_HEADROOM
    if available < DISK_HEADROOM:
        raise cv.QualificationError("DISK_RESERVE_EXHAUSTED")
    # The output remains disk-backed; this bound never changes the RAM/cache cap.
    return min(raw_size + DISK_HEADROOM, available)


def source_size(path: Path) -> int:
    if path.resolve() != path or any(p.is_symlink() for p in (path, *path.parents)):
        raise cv.QualificationError("BACKING_PATH_REFUSED")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as source:
        source.execute("BEGIN")
        return (
            source.execute("PRAGMA page_count").fetchone()[0]
            * source.execute("PRAGMA page_size").fetchone()[0]
        )


@contextmanager
def snapshot(
    path: Path,
    directory: Path,
    *,
    deadline: float,
    progress: Callable[..., Any] = lambda **kw: None,
) -> Iterator[tuple[apsw.Connection, cv.Image]]:
    cv.budget(deadline)
    limit = disk_limit(directory, source_size(path))
    with tempfile.TemporaryDirectory(prefix="cw-disk-state-", dir=directory) as work:
        folder = Path(work)
        with cv.snapshot_image(
            path,
            folder,
            deadline=deadline,
            backing=folder / "snapshot.frames",
            encoded_limit=limit,
            progress=progress,
        ) as value:
            yield value
        cv.budget(deadline)


class ArchiveSink:
    def __init__(self, output: BinaryIO, directory: Path, deadline: float) -> None:
        self.output = output
        self.directory = directory
        self.deadline = deadline
        self.size = 0
        self.digest = hashlib.sha256()
        self.next_check = 0

    def write(self, data: bytes) -> int:
        cv.budget(self.deadline)
        if self.size >= self.next_check:
            if shutil.disk_usage(self.directory).free < DISK_RESERVE + DISK_HEADROOM:
                raise cv.QualificationError("DISK_RESERVE_EXHAUSTED")
            self.next_check = self.size + 1024**2
        count = self.output.write(data)
        if count != len(data):
            raise cv.QualificationError("ARCHIVE_WRITE_INCOMPLETE")
        self.size += count
        self.digest.update(data)
        return count

    def flush(self) -> None:
        self.output.flush()


def validate_archive(
    path: Path,
    directory: Path,
    *,
    raw_size: int,
    deadline: float,
    progress: Callable[..., Any] = lambda **kw: None,
) -> dict[str, Any]:
    cv.budget(deadline)
    before = fingerprint(path, deadline)
    limit = disk_limit(directory, raw_size)
    with tempfile.TemporaryDirectory(prefix="cw-disk-readback-", dir=directory) as work:
        folder = Path(work)
        with cv.archived_image(
            path,
            folder,
            deadline=deadline,
            backing=folder / "readback.frames",
            encoded_limit=limit,
            encoding="ZSTD",
            progress=progress,
        ) as (db, image):
            if image.size != raw_size:
                raise cv.QualificationError("BYTE_IDENTITY_MISMATCH")
            cache = db.execute("PRAGMA cache_size").fetchone()
            if cache is None:
                raise cv.QualificationError("CACHE_EVIDENCE_UNAVAILABLE")
            result = {
                "mode": "COMPRESSED_DISK",
                "bytes": image.size,
                "sha256": image.sha256,
                "compressed_bytes": image.retained,
                "sqlite_cache_kib": -cache[0],
                "sqlite_version": apsw.sqlitelibversion(),
                "apsw_version": apsw.apswversion(),
            }
    if fingerprint(path, deadline) != before:
        raise cv.QualificationError("BYTE_IDENTITY_MISMATCH")
    cv.budget(deadline)
    return {**result, "archive_bytes": before[1], "archive_sha256": before[0]}


def backup(
    path: Path,
    destination: Path,
    directory: Path,
    *,
    seconds: float = 900,
    progress: Callable[..., Any] = lambda **kw: None,
) -> dict[str, Any]:
    if not math.isfinite(seconds) or not 0 < seconds <= 900:
        raise cv.QualificationError("INVALID_TIME_BUDGET")
    started = time.monotonic()
    deadline = started + seconds
    private_disk(directory)
    if (
        destination.resolve() != destination
        or any(p.is_symlink() for p in (destination, *destination.parents))
        or destination == path
    ):
        raise cv.QualificationError("BACKING_PATH_REFUSED")
    with cv.classified_errors(deadline):
        handle = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(handle, "wb") as output:
            # The live WAL reader is released by snapshot_image before full_check.
            with snapshot(path, directory, deadline=deadline, progress=progress) as (_, image):
                expected_size, expected_hash = image.size, image.reconstructed_hash()
                sink = ArchiveSink(output, destination.parent, deadline)
                compressor = zstandard.ZstdCompressor(
                    level=cv.COMPRESSION_LEVEL, write_checksum=True, threads=0
                )
                with compressor.stream_writer(
                    cast(IO[bytes], sink), size=expected_size, closefd=False
                ) as encoded:
                    for block in image.iter_bytes():
                        cv.budget(deadline)
                        encoded.write(block)
                output.flush()
                os.fsync(output.fileno())
        # The first spool has closed and is gone before persisted readback starts.
        result = validate_archive(
            destination, directory, raw_size=expected_size, deadline=deadline, progress=progress
        )
        if (
            result["sha256"] != expected_hash
            or result["archive_sha256"] != sink.digest.hexdigest()
            or result["archive_bytes"] != sink.size
        ):
            raise cv.QualificationError("BYTE_IDENTITY_MISMATCH")
        if os.name == "posix":
            fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        cv.budget(deadline)
        return {
            **result,
            "phase": "COMPLETE",
            "validation_mode": "COMPRESSED_DISK",
            "archive_encoding": "ZSTD",
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "disk_reserve_bytes": DISK_RESERVE,
        }
