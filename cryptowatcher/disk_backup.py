"""Full snapshot/archive validation with private disk frames and bounded RAM cache."""

from __future__ import annotations

import hashlib
import math
import mmap
import os
import shutil
import sqlite3
import stat
import tempfile
import time
from array import array
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import IO, Any, BinaryIO, cast

import apsw
import zstandard

from . import compressed_validation as cv
from . import frame_archive
from .validation import fingerprint

DISK_RESERVE = 1536 * 1024**2
DISK_HEADROOM = 64 * 1024**2


@contextmanager
def indexed_image(
    descriptor: Path,
    *,
    deadline: float,
    progress: Callable[..., Any] = lambda **kw: None,
) -> Iterator[tuple[apsw.Connection, cv.Image]]:
    """Reopen verified disk frames directly, without a second encoded copy."""
    cv.budget(deadline)
    encoded_descriptor = descriptor.read_bytes()
    proof = frame_archive.verify(descriptor, deadline=deadline)
    record = frame_archive.manifest(descriptor)
    if descriptor.read_bytes() != encoded_descriptor:
        raise cv.QualificationError("BYTE_IDENTITY_MISMATCH")
    data = descriptor.parent / record["data"]
    index = descriptor.parent / record["index"]
    identities = [p.stat() for p in (data, index, descriptor)]
    image = cv.Image(None, limit=1, deadline=deadline)
    image.blocks.close()
    try:
        with data.open("rb") as raw:
            # Native VFS borrows a writable buffer; ACCESS_COPY can never change
            # the recovery file. Frozen Image and VFS reject database writes.
            image.blocks.data = mmap.mmap(raw.fileno(), 0, access=mmap.ACCESS_COPY)
        image.blocks.backing = data
        image.blocks.end = record["extent"]
        image.blocks.limit = record["data_bytes"]
        image.blocks.offsets = array("Q")
        image.blocks.lengths = array("Q")
        with index.open("rb") as entries:
            for entry in iter(lambda: entries.read(frame_archive.ENTRY.size), b""):
                cv.budget(deadline)
                offset, size = frame_archive.ENTRY.unpack(entry)
                image.blocks.offsets.append(offset)
                image.blocks.lengths.append(size)
        image.blocks.capacities = array("Q", image.blocks.lengths)
        image.size = proof["raw_bytes"]
        image.sha256 = proof["raw_sha256"]
        image.retained = record["extent"]
        image.mutable = False
        with tempfile.TemporaryDirectory(prefix="cw-indexed-check-", dir=descriptor.parent) as work:
            sentinel = Path(work) / "sentinel.sqlite3"
            sentinel.touch(mode=0o600)
            vfs = cv.frozen_vfs(image, sentinel)
            try:
                with closing(
                    apsw.Connection(str(sentinel), flags=apsw.SQLITE_OPEN_READWRITE, vfs=vfs.name)
                ) as db:
                    cache = cv.configure_cache(db, image)
                    db.execute("PRAGMA query_only=ON")
                    progress(phase="PERSISTED_FULL_INTEGRITY", bytes=image.size, sqlite_cache_kib=cache)
                    cv.full_check(db, deadline)
                    yield db, image
            finally:
                vfs.unregister()
        if image.reconstructed_hash() != proof["raw_sha256"]:
            raise cv.QualificationError("BYTE_IDENTITY_MISMATCH")
        for path, before in zip((data, index, descriptor), identities, strict=True):
            after = path.stat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise cv.QualificationError("BYTE_IDENTITY_MISMATCH")
        cv.budget(deadline)
    finally:
        image.blocks.close()


def indexed_backup(
    path: Path,
    destination: Path,
    *,
    seconds: float = 900,
    progress: Callable[..., Any] = lambda **kw: None,
) -> dict[str, Any]:
    """One bounded operation: online capture, full source and persisted readback."""
    if not math.isfinite(seconds) or not 0 < seconds <= 900:
        raise cv.QualificationError("INVALID_TIME_BUDGET")
    started = time.monotonic()
    deadline = started + seconds
    if destination.resolve() != destination or any(
        p.is_symlink() for p in (destination, *destination.parents)
    ):
        raise cv.QualificationError("BACKING_PATH_REFUSED")
    destination.mkdir(mode=0o700)  # Exclusive; incomplete archives remain for inspection.
    private_disk(destination)
    limit = disk_limit(destination, source_size(path))
    with cv.classified_errors(deadline):
        with cv.snapshot_image(
            path,
            destination,
            deadline=deadline,
            backing=destination / "source.frames",
            encoded_limit=limit,
            progress=progress,
        ) as (_, image):
            expected = (image.size, image.reconstructed_hash())
            descriptor = frame_archive.save_index(image, destination, deadline=deadline)
        # Both the live reader and first mapping are closed. Read the durable
        # frames via an independent decoder, then reopen their native VFS.
        with indexed_image(descriptor, deadline=deadline, progress=progress) as (db, readback):
            if (readback.size, readback.sha256) != expected:
                raise cv.QualificationError("BYTE_IDENTITY_MISMATCH")
            cache = db.execute("PRAGMA cache_size").fetchone()
            schema = db.execute("PRAGMA user_version").fetchone()
            if cache is None or schema is None:
                raise cv.QualificationError("CACHE_EVIDENCE_UNAVAILABLE")
            result = {
                "phase": "COMPLETE",
                "mode": "COMPRESSED_DISK_INDEXED",
                "validation_mode": "COMPRESSED_DISK_INDEXED",
                "archive_encoding": "INDEXED_ZSTD",
                "bytes": readback.size,
                "sha256": readback.sha256,
                "sqlite_cache_kib": -cache[0],
                "schema": schema[0],
            }
        record = frame_archive.manifest(descriptor)
        result.update(
            archive_bytes=record["data_bytes"],
            archive_sha256=record["data_sha256"],
            index_sha256=record["index_sha256"],
            descriptor_sha256=hashlib.sha256(descriptor.read_bytes()).hexdigest(),
            elapsed_seconds=round(time.monotonic() - started, 3),
            disk_reserve_bytes=DISK_RESERVE,
        )
        if os.name == "posix":
            for folder in (destination, destination.parent):
                fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        cv.budget(deadline)
        return result


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
