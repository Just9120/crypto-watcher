"""Bounded exact-byte compressed validation images; production files are never rewritten."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import tempfile
import time
import uuid
import zlib
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import apsw

from .storage import IntegrityError

BLOCK = 4096
CACHE_PAGES = 512
COMPRESSION_LEVEL = 2


MAX_COMPRESSED_BYTES = 3 * 1024**3


class QualificationError(IntegrityError):
    def __init__(self, code: str) -> None:
        category = code if code in ("TIME_BUDGET_EXCEEDED", "DATABASE_CORRUPT") else "DATABASE_ERROR"
        super().__init__(category, "Compressed validation refused: " + code)


def budget(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise QualificationError("TIME_BUDGET_EXCEEDED")


class Image:
    def __init__(
        self, path: Path | None, *, limit: int, deadline: float, progress: Any = lambda **kw: None
    ) -> None:
        self.blocks: list[bytes] = []
        self.size = 0
        self.retained = 0
        self.deadline = deadline
        self.limit = limit
        self.mutable = False
        self.cache: OrderedDict[int, bytes] = OrderedDict()
        if path is None:
            self.mutable = True
            return
        self.path = Path(path)
        self.before = self.path.lstat()
        if not stat.S_ISREG(self.before.st_mode):
            raise QualificationError("REGULAR_CLOSED_FILE_REQUIRED")
        if any(Path(str(path) + s).exists() for s in ("-wal", "-shm", "-journal")):
            raise QualificationError("CLOSED_FILE_REQUIRED")
        self.hash = hashlib.sha256()
        with os.fdopen(
            os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb", buffering=1024 * 1024
        ) as source:
            opened = os.fstat(source.fileno())
            if (opened.st_dev, opened.st_ino) != (self.before.st_dev, self.before.st_ino):
                raise QualificationError("SOURCE_CHANGED")
            while True:
                budget(deadline)
                data = source.read(BLOCK)
                if not data:
                    break
                blob = zlib.compress(data, COMPRESSION_LEVEL)
                self.retained += sys.getsizeof(blob)
                if self.retained + sys.getsizeof(self.blocks) + BLOCK + 128 > limit:
                    raise QualificationError("COMPRESSED_MEMORY_LIMIT")
                self.blocks.append(blob)
                self.size += len(data)
                self.hash.update(data)
                progress(phase="COMPRESS", bytes=self.size, compressed_bytes=self.retained)
        self.assert_source_unchanged()
        if self.size != self.before.st_size:
            raise QualificationError("SOURCE_CHANGED")
        self.sha256 = self.hash.hexdigest()
        if self.reconstructed_hash() != self.sha256:
            raise QualificationError("BYTE_IDENTITY_MISMATCH")

    def set_block(self, index: int, value: bytes) -> None:
        budget(self.deadline)
        if len(value) > BLOCK:
            raise QualificationError("INVALID_PAGE_SIZE")
        blob = zlib.compress(value, COMPRESSION_LEVEL)
        previous = self.blocks[index] if index < len(self.blocks) else None
        new = self.retained + sys.getsizeof(blob) - (sys.getsizeof(previous) if previous is not None else 0)
        if new + sys.getsizeof(self.blocks) + BLOCK + 128 > self.limit:
            raise QualificationError("COMPRESSED_MEMORY_LIMIT")
        if previous is None:
            self.blocks.append(blob)
        else:
            self.blocks[index] = blob
        self.retained = new
        self.cache.pop(index, None)

    def write(self, data: bytes, offset: int) -> None:
        budget(self.deadline)
        if offset < 0:
            raise QualificationError("INVALID_WRITE")
        if not self.mutable:
            raise apsw.ReadOnlyError("IMMUTABLE_VALIDATION_IMAGE")
        end = offset + len(data)
        while len(self.blocks) < offset // BLOCK:
            self.set_block(len(self.blocks), bytes(BLOCK))
        self.size = max(self.size, end)
        pos = offset
        while pos < end:
            index, start = divmod(pos, BLOCK)
            n = min(end - pos, BLOCK - start)
            value = (
                bytearray(zlib.decompress(self.blocks[index]))
                if index < len(self.blocks)
                else bytearray(BLOCK)
            )
            if len(value) < BLOCK:
                value.extend(bytes(BLOCK - len(value)))
            value[start : start + n] = data[pos - offset : pos - offset + n]
            self.set_block(index, bytes(value))
            pos += n

    def truncate(self, size: int) -> None:
        if not self.mutable:
            raise apsw.ReadOnlyError("IMMUTABLE_VALIDATION_IMAGE")
        if size > self.size:
            self.write(bytes(1), size - 1)
        while len(self.blocks) > (size + BLOCK - 1) // BLOCK:
            self.retained -= sys.getsizeof(self.blocks.pop())
        self.size = size
        self.cache.clear()

    def assert_source_unchanged(self) -> None:
        after = self.path.lstat()
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        if any(getattr(self.before, k) != getattr(after, k) for k in fields):
            raise QualificationError("SOURCE_CHANGED")

    def block(self, index: int) -> bytes:
        budget(self.deadline)
        value = self.cache.pop(index, None)
        if value is None:
            value = zlib.decompress(self.blocks[index])
            expected = min(BLOCK, self.size - index * BLOCK)
            if len(value) not in (expected, BLOCK):
                raise QualificationError("BYTE_IDENTITY_MISMATCH")
            value = value[:expected]
        self.cache[index] = value
        while len(self.cache) > CACHE_PAGES:
            self.cache.popitem(last=False)
        return value

    def read(self, amount: int, offset: int) -> bytes:
        budget(self.deadline)
        if offset < 0 or amount < 0 or amount > 1024 * 1024:
            raise QualificationError("INVALID_READ")
        pieces = []
        end = min(offset + amount, self.size)
        pos = offset
        while pos < end:
            index, start = divmod(pos, BLOCK)
            n = min(end - pos, BLOCK - start)
            pieces.append(self.block(index)[start : start + n])
            pos += n
        return b"".join(pieces)

    def reconstructed_hash(self) -> str:
        self.cache.clear()
        result = hashlib.sha256()
        for index in range(len(self.blocks)):
            result.update(self.block(index))
        return result.hexdigest()


class File(apsw.VFSFile):
    def __init__(self, image: Image, name: Any, flags: list[int]) -> None:
        self.image = image
        super().__init__("", name, flags)

    def xRead(self, amount: int, offset: int) -> bytes:
        return self.image.read(amount, offset)

    def xFileSize(self) -> int:
        return self.image.size

    def xFileControl(self, op: int, ptr: int) -> bool:
        return False

    def xSectorSize(self) -> int:
        return BLOCK

    def xDeviceCharacteristics(self) -> int:
        return 0

    def xSync(self, flags: int) -> None:
        pass

    def xWrite(self, data: bytes, offset: int) -> None:
        self.image.write(data, offset)

    def xTruncate(self, size: int) -> None:
        self.image.truncate(size)


class VFS(apsw.VFS):
    def __init__(self, image: Image, path: Path) -> None:
        self.image = image
        self.path = str(path.resolve())
        self.name = "cw-validation-" + uuid.uuid4().hex
        super().__init__(self.name, "")

    def xOpen(self, name: Any, flags: list[int]) -> apsw.VFSFile:
        filename = name.filename() if isinstance(name, apsw.URIFilename) else name
        if flags[0] & apsw.SQLITE_OPEN_MAIN_DB and filename == self.path:
            return File(self.image, name, flags)
        if filename in (self.path + "-wal", self.path + "-journal"):
            return apsw.VFSFile("", name, flags)
        raise apsw.CantOpenError("VALIDATION_FILES_ONLY")

    def xDelete(self, name: str, syncdir: bool) -> None:
        if name not in (self.path + "-wal", self.path + "-journal"):
            raise apsw.ReadOnlyError("IMMUTABLE_VALIDATION_IMAGE")
        return super().xDelete(name, syncdir)


def validate_closed(
    path: Path,
    *,
    directory: Path,
    deadline: float,
    limit: int = MAX_COMPRESSED_BYTES,
    progress: Any = lambda **kw: None,
) -> dict[str, Any]:
    image = Image(path, limit=limit, deadline=deadline, progress=progress)
    progress(phase="FULL_INTEGRITY", bytes=image.size, compressed_bytes=image.retained)
    with tempfile.TemporaryDirectory(prefix="cw-cvfs-", dir=directory or Path(path).parent) as work:
        path = Path(work) / "sentinel.sqlite3"
        path.touch(mode=0o600)
        vfs = VFS(image, path)
        db = None
        try:
            db = apsw.Connection(str(path.resolve()), flags=apsw.SQLITE_OPEN_READWRITE, vfs=vfs.name)
            full_check(db, deadline)
        finally:
            if db is not None:
                db.close()
            vfs.unregister()
    progress(phase="BYTE_IDENTITY", bytes=image.size, compressed_bytes=image.retained)
    if image.reconstructed_hash() != image.sha256:
        raise QualificationError("BYTE_IDENTITY_MISMATCH")
    actual = hashlib.sha256()
    image.assert_source_unchanged()
    with os.fdopen(os.open(image.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as source:
        opened = os.fstat(source.fileno())
        if (opened.st_dev, opened.st_ino) != (image.before.st_dev, image.before.st_ino):
            raise QualificationError("SOURCE_CHANGED")
        while True:
            budget(deadline)
            data = source.read(1024 * 1024)
            if not data:
                break
            actual.update(data)
    image.assert_source_unchanged()
    if actual.hexdigest() != image.sha256:
        raise QualificationError("BYTE_IDENTITY_MISMATCH")
    budget(deadline)
    return {
        "mode": "COMPRESSED_RAM",
        "bytes": image.size,
        "sha256": image.sha256,
        "compressed_bytes": image.retained + sys.getsizeof(image.blocks),
        "sqlite_version": apsw.sqlitelibversion(),
        "apsw_version": apsw.apswversion(),
    }


def full_check(db: apsw.Connection, deadline: float) -> None:
    budget(deadline)

    def progress() -> bool:
        budget(deadline)
        return False

    db.set_progress_handler(progress, 10000)
    try:
        db.execute("PRAGMA cache_size=-131072")
        db.execute("PRAGMA mmap_size=0")
        db.execute("PRAGMA ignore_check_constraints=OFF")
        if list(db.execute("PRAGMA integrity_check")) != [("ok",)]:
            raise QualificationError("DATABASE_CORRUPT")
        budget(deadline)
    finally:
        db.set_progress_handler(None, 0)


@contextmanager
def classified_errors(deadline: float) -> Iterator[None]:
    try:
        yield
    except apsw.Error as error:
        if time.monotonic() >= deadline:
            raise QualificationError("TIME_BUDGET_EXCEEDED") from error
        if isinstance(error, (apsw.CorruptError, apsw.NotADBError)):
            raise QualificationError("DATABASE_CORRUPT") from error
        raise QualificationError("SQLITE_VALIDATION_ERROR") from error
    except (OSError, zlib.error) as error:
        raise QualificationError("VALIDATION_IO_ERROR") from error


@contextmanager
def snapshot(path: Path, directory: Path, *, deadline: float) -> Iterator[apsw.Connection]:
    """Pinned online backup, full writable validation, then metadata from that same image."""
    image = Image(None, limit=MAX_COMPRESSED_BYTES, deadline=deadline)
    with (
        classified_errors(deadline),
        tempfile.TemporaryDirectory(prefix="cw-cvfs-live-", dir=directory) as work,
    ):
        sentinel = Path(work) / "sentinel.sqlite3"
        sentinel.touch(mode=0o600)
        vfs = VFS(image, sentinel)
        destination: apsw.Connection | None = None
        source: apsw.Connection | None = None
        try:
            destination = apsw.Connection(
                str(sentinel.resolve()), flags=apsw.SQLITE_OPEN_READWRITE, vfs=vfs.name
            )
            destination.execute("PRAGMA journal_mode=MEMORY")
            destination.execute("PRAGMA mmap_size=0")
            source = apsw.Connection(
                path.resolve().as_uri() + "?mode=ro", flags=apsw.SQLITE_OPEN_READONLY | apsw.SQLITE_OPEN_URI
            )
            source.execute("BEGIN")
            source.execute("SELECT owner_id FROM owner_settings").fetchone()
            with destination.backup("main", source, "main") as backup:
                while not backup.done:
                    budget(deadline)
                    backup.step(256)
            source.close()
            source = None
            destination.close()
            destination = None
            image.mutable = False
            original = image.reconstructed_hash()
            destination = apsw.Connection(
                str(sentinel.resolve()), flags=apsw.SQLITE_OPEN_READWRITE, vfs=vfs.name
            )
            full_check(destination, deadline)
            destination.execute("PRAGMA query_only=ON")
            yield destination
            destination.close()
            destination = None
            if image.reconstructed_hash() != original:
                raise QualificationError("BYTE_IDENTITY_MISMATCH")
        finally:
            if source is not None:
                source.close()
            if destination is not None:
                destination.close()
            vfs.unregister()
