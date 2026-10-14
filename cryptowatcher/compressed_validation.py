"""Bounded exact-byte compressed validation images; production files are never rewritten."""

from __future__ import annotations

import gzip
import hashlib
import mmap
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import uuid
from array import array
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any, BinaryIO, Literal, cast

import apsw
import zstandard

from .storage import IntegrityError

BLOCK = 65536
CACHE_PAGES = 512
COMPRESSION_LEVEL = 1
WRITE_CACHE_BLOCKS = 8


MAX_COMPRESSED_BYTES = 3 * 1024**3
VALIDATION_MEMORY_BYTES = 4 * 1024**3
VALIDATION_OVERHEAD_BYTES = 512 * 1024**2
MAX_SQLITE_CACHE_BYTES = 1024**3


class QualificationError(IntegrityError):
    def __init__(self, code: str) -> None:
        category = code if code in ("TIME_BUDGET_EXCEEDED", "DATABASE_CORRUPT") else "DATABASE_ERROR"
        super().__init__(category, "Compressed validation refused: " + code)


def budget(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise QualificationError("TIME_BUDGET_EXCEEDED")


def disk_backing(path: Path) -> bool:
    if sys.platform == "win32":
        return True
    if sys.platform != "linux":
        return False
    mounts: list[tuple[int, str]] = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ", 1)
        mount = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), left.split()[4])
        if str(path) == mount or str(path).startswith(mount.rstrip("/") + "/"):
            mounts.append((len(mount), right.split()[0]))
    return bool(mounts) and max(mounts)[1] in {"ext4", "xfs", "btrfs", "overlay", "zfs"}


class _FrameArena:
    """Keep compressed frames out of the interleaved Python allocation heap."""

    def __init__(self, limit: int, backing: Path | None = None) -> None:
        self.backing = backing
        self.next_space_check = 0
        if backing is None:
            self.data = mmap.mmap(-1, limit)
        else:
            if any(p.is_symlink() for p in (backing, *backing.parents)):
                raise QualificationError("BACKING_PATH_REFUSED")
            if backing.resolve() != backing or not disk_backing(backing):
                raise QualificationError("DISK_BACKING_REQUIRED")
            metadata = backing.parent.stat()
            if os.name == "posix" and (
                metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise QualificationError("PRIVATE_BACKING_DIRECTORY_REQUIRED")
            if shutil.disk_usage(backing.parent).free < limit + 1536 * 1024**2:
                raise QualificationError("DISK_RESERVE_EXHAUSTED")
            descriptor = os.open(
                backing, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
            )
            try:
                os.ftruncate(descriptor, limit)
                self.data = mmap.mmap(descriptor, limit, access=mmap.ACCESS_WRITE)
                opened = os.fstat(descriptor)
                self.backing_identity = (opened.st_dev, opened.st_ino)
            finally:
                os.close(descriptor)
        self.limit = limit
        self.end = 0
        self.offsets = array("Q")
        self.lengths = array("Q")
        self.capacities = array("Q")

    def __len__(self) -> int:
        return len(self.offsets)

    def __sizeof__(self) -> int:
        return object.__sizeof__(self) + sum(
            sys.getsizeof(value) for value in (self.data, self.offsets, self.lengths, self.capacities)
        )

    def __getitem__(self, index: int) -> bytes:
        offset, length = self.offsets[index], self.lengths[index]
        return self.data[offset : offset + length]

    def __iter__(self) -> Iterator[bytes]:
        for index in range(len(self)):
            yield self[index]

    def __setitem__(self, index: int, value: bytes) -> None:
        length = len(value)
        if self.backing is not None and self.end >= self.next_space_check:
            if shutil.disk_usage(self.backing.parent).free < self.limit - self.end + 1536 * 1024**2:
                raise QualificationError("DISK_RESERVE_EXHAUSTED")
            self.next_space_check = self.end + 64 * 1024**2
        if length <= self.capacities[index]:
            offset = self.offsets[index]
        else:
            if self.end + length > self.limit:
                raise QualificationError("COMPRESSED_MEMORY_LIMIT")
            offset = self.end
            self.end += length
            self.offsets[index] = offset
            self.capacities[index] = length
        self.data[offset : offset + length] = value
        self.lengths[index] = length

    def append(self, value: bytes) -> None:
        if self.end + len(value) > self.limit:
            raise QualificationError("COMPRESSED_MEMORY_LIMIT")
        self.offsets.append(self.end)
        self.lengths.append(0)
        self.capacities.append(0)
        self[len(self) - 1] = value

    def pop(self) -> bytes:
        value = self[-1]
        offset, capacity = self.offsets.pop(), self.capacities.pop()
        self.lengths.pop()
        if offset + capacity == self.end:
            self.end = offset
        return value

    def flush_private(self) -> None:
        if self.backing is not None:
            self.data.flush()
            # Drop only the owned scratch mapping, never production file caches.
            if hasattr(self.data, "madvise") and hasattr(mmap, "MADV_DONTNEED"):
                self.data.madvise(mmap.MADV_DONTNEED)

    def close(self) -> None:
        self.data.close()

    def seal_private(self) -> None:
        """Persist just the frozen arena extent, without touching any source file."""
        if self.backing is None:
            raise QualificationError("DISK_BACKING_REQUIRED")
        self.flush_private()
        # Windows prohibits truncating a mapped file; retaining the sparse tail
        # is lossless and remains covered by the encoded-file hash there.
        if os.name == "nt":
            return
        descriptor = os.open(self.backing, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != self.backing_identity:
                raise QualificationError("BACKING_PATH_REFUSED")
            os.ftruncate(descriptor, self.end)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class Image:
    def __init__(
        self,
        path: Path | None,
        *,
        limit: int,
        deadline: float,
        progress: Any = lambda **kw: None,
        archive_encoding: Literal["GZIP", "ZSTD"] | None = None,
        backing: Path | None = None,
    ) -> None:
        self.blocks = _FrameArena(limit, backing)
        self.size = 0
        self.retained = 0
        self.deadline = deadline
        self.limit = limit
        self.mutable = False
        self.cache: OrderedDict[int, bytes] = OrderedDict()
        self.write_cache: OrderedDict[int, bytearray] = OrderedDict()
        self.compressor = zstandard.ZstdCompressor(level=COMPRESSION_LEVEL, write_checksum=True, threads=0)
        self.decompressor = zstandard.ZstdDecompressor()
        self.archive_encoding = archive_encoding
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
        with self.source_reader() as source:
            while True:
                budget(deadline)
                data = source.read(BLOCK)
                if not data:
                    break
                blob = self.compressor.compress(data)
                self.retained += sys.getsizeof(blob)
                if self.retained + sys.getsizeof(self.blocks) + BLOCK + 128 > limit:
                    raise QualificationError("COMPRESSED_MEMORY_LIMIT")
                self.blocks.append(blob)
                self.size += len(data)
                self.hash.update(data)
                progress(phase="COMPRESS", bytes=self.size, compressed_bytes=self.retained)
        self.assert_source_unchanged()
        if archive_encoding is None and self.size != self.before.st_size:
            raise QualificationError("SOURCE_CHANGED")
        self.sha256 = self.hash.hexdigest()
        self.blocks.flush_private()
        if self.reconstructed_hash() != self.sha256:
            raise QualificationError("BYTE_IDENTITY_MISMATCH")

    @contextmanager
    def source_reader(self) -> Iterator[BinaryIO | gzip.GzipFile | zstandard.ZstdDecompressionReader]:
        with os.fdopen(
            os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb", buffering=1024 * 1024
        ) as source:
            opened = os.fstat(source.fileno())
            if (opened.st_dev, opened.st_ino) != (self.before.st_dev, self.before.st_ino):
                raise QualificationError("SOURCE_CHANGED")
            if self.archive_encoding == "GZIP":
                with gzip.GzipFile(fileobj=source, mode="rb") as decoded:
                    yield decoded
            elif self.archive_encoding == "ZSTD":
                with self.decompressor.stream_reader(source, closefd=False) as reader:
                    yield reader
            else:
                yield source

    def set_block(self, index: int, value: bytes) -> None:
        budget(self.deadline)
        if len(value) > BLOCK:
            raise QualificationError("INVALID_PAGE_SIZE")
        blob = self.compressor.compress(value)
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
            value = self.write_cache.pop(index, None)
            if value is None:
                if index == len(self.blocks):
                    self.blocks.append(b"")
                    self.retained += sys.getsizeof(b"")
                    value = bytearray(BLOCK)
                else:
                    value = bytearray(self.decompressor.decompress(self.blocks[index], max_output_size=BLOCK))
            if len(value) < BLOCK:
                value.extend(bytes(BLOCK - len(value)))
            value[start : start + n] = data[pos - offset : pos - offset + n]
            self.write_cache[index] = value
            self.cache.pop(index, None)
            while len(self.write_cache) > WRITE_CACHE_BLOCKS:
                old_index, old_value = self.write_cache.popitem(last=False)
                self.set_block(old_index, bytes(old_value))
            pos += n

    def freeze(self) -> None:
        for index, value in self.write_cache.items():
            self.set_block(index, bytes(value))
        self.write_cache.clear()
        self.mutable = False
        self.blocks.flush_private()
        budget(self.deadline)

    def truncate(self, size: int) -> None:
        budget(self.deadline)
        if size < 0:
            raise QualificationError("INVALID_TRUNCATE")
        if not self.mutable:
            raise apsw.ReadOnlyError("IMMUTABLE_VALIDATION_IMAGE")
        if size > self.size:
            self.write(bytes(1), size - 1)
        while len(self.blocks) > (size + BLOCK - 1) // BLOCK:
            self.write_cache.pop(len(self.blocks) - 1, None)
            self.retained -= sys.getsizeof(self.blocks.pop())
        if size and size % BLOCK:
            index, boundary = divmod(size, BLOCK)
            value = self.write_cache.pop(index, None)
            if value is None:
                value = bytearray(self.block(index))
            value.extend(bytes(BLOCK - len(value)))
            value[boundary:] = bytes(BLOCK - boundary)
            self.write_cache[index] = value
            while len(self.write_cache) > WRITE_CACHE_BLOCKS:
                old_index, old_value = self.write_cache.popitem(last=False)
                self.set_block(old_index, bytes(old_value))
        self.size = size
        self.cache.clear()

    def assert_source_unchanged(self) -> None:
        after = self.path.lstat()
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        if any(getattr(self.before, k) != getattr(after, k) for k in fields):
            raise QualificationError("SOURCE_CHANGED")

    def block(self, index: int) -> bytes:
        budget(self.deadline)
        if index in self.write_cache:
            return bytes(self.write_cache[index])[: min(BLOCK, self.size - index * BLOCK)]
        value = self.cache.pop(index, None)
        if value is None:
            value = self.decompressor.decompress(self.blocks[index], max_output_size=BLOCK)
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
        end = min(offset + amount, self.size)
        if end <= offset:
            return b""
        index, start = divmod(offset, BLOCK)
        if end - offset <= BLOCK - start:
            return self.block(index)[start : start + end - offset]
        pieces = []
        pos = offset
        while pos < end:
            index, start = divmod(pos, BLOCK)
            n = min(end - pos, BLOCK - start)
            pieces.append(self.block(index)[start : start + n])
            pos += n
        return b"".join(pieces)

    def reconstructed_hash(self) -> str:
        result = hashlib.sha256()
        for value in self.iter_bytes():
            result.update(value)
        return result.hexdigest()

    def iter_bytes(self) -> Iterator[bytes]:
        if self.mutable:
            raise QualificationError("FROZEN_IMAGE_REQUIRED")
        self.cache.clear()
        for index, blob in enumerate(self.blocks):
            budget(self.deadline)
            value = self.decompressor.decompress(blob, max_output_size=BLOCK)
            expected = min(BLOCK, self.size - index * BLOCK)
            if len(value) not in (expected, BLOCK):
                raise QualificationError("BYTE_IDENTITY_MISMATCH")
            yield value[:expected]


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
        return 4096

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


def resident_memory_bytes() -> int | None:
    """Include native allocations and allocator overhead in Linux cache admission."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                fields = line.split()
                if len(fields) == 3 and fields[2] == "kB" and int(fields[1]) >= 0:
                    return int(fields[1]) * 1024
    except (OSError, ValueError):
        pass
    return None  # Portable local checks retain the existing logical-byte bound.


def frozen_vfs(image: Image, path: Path) -> Any:
    """Select native only for a frozen image; a present broken helper fails closed."""
    from .native_validation import NativeVFS, available

    if image.mutable:
        raise QualificationError("FROZEN_IMAGE_REQUIRED")
    return NativeVFS(image, path) if available() else VFS(image, path)


def configure_cache(db: apsw.Connection, image: Image) -> int:
    """Use only capacity left by the actual frozen image, preserving the container cap."""
    budget(image.deadline)
    if image.mutable:
        raise QualificationError("FROZEN_IMAGE_REQUIRED")
    image_bytes = sys.getsizeof(image.blocks)
    if getattr(image.blocks, "backing", None) is None:
        image_bytes += image.retained
    else:
        image.blocks.flush_private()
    cache_bytes = min(
        MAX_SQLITE_CACHE_BYTES, VALIDATION_MEMORY_BYTES - image_bytes - VALIDATION_OVERHEAD_BYTES
    )
    resident = resident_memory_bytes()
    if resident is not None:
        cache_bytes = min(cache_bytes, VALIDATION_MEMORY_BYTES - resident - VALIDATION_OVERHEAD_BYTES)
    if cache_bytes < 512 * 1024**2:
        raise QualificationError("COMPRESSED_MEMORY_LIMIT")
    cache_kib = cache_bytes // 1024
    db.execute(f"PRAGMA cache_size=-{cache_kib}")
    return cache_kib


def validate_closed(
    path: Path,
    *,
    directory: Path,
    deadline: float,
    limit: int = MAX_COMPRESSED_BYTES,
    progress: Any = lambda **kw: None,
    archive_encoding: Literal["GZIP", "ZSTD"] | None = None,
) -> dict[str, Any]:
    image = Image(path, limit=limit, deadline=deadline, progress=progress, archive_encoding=archive_encoding)
    progress(phase="FULL_INTEGRITY", bytes=image.size, compressed_bytes=image.retained)
    with tempfile.TemporaryDirectory(prefix="cw-cvfs-", dir=directory or Path(path).parent) as work:
        path = Path(work) / "sentinel.sqlite3"
        path.touch(mode=0o600)
        vfs = frozen_vfs(image, path)
        db = None
        try:
            db = apsw.Connection(str(path.resolve()), flags=apsw.SQLITE_OPEN_READWRITE, vfs=vfs.name)
            db.execute("PRAGMA query_only=ON")
            cache_kib = configure_cache(db, image)
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
    with image.source_reader() as source:
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
        "sqlite_cache_kib": cache_kib,
    }


def full_check(db: apsw.Connection, deadline: float) -> None:
    budget(deadline)

    def progress() -> bool:
        budget(deadline)
        return False

    db.set_progress_handler(progress, 10000)
    try:
        db.execute("PRAGMA mmap_size=0")
        db.execute("PRAGMA ignore_check_constraints=OFF")
        rows = list(db.execute("PRAGMA integrity_check"))
        # A bounded native VFS can return an I/O diagnostic when its deadline
        # expires. An incomplete scan is not evidence of database corruption.
        budget(deadline)
        if rows != [("ok",)]:
            raise QualificationError("DATABASE_CORRUPT")
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
    except (OSError, EOFError, zstandard.ZstdError) as error:
        raise QualificationError("VALIDATION_IO_ERROR") from error


@contextmanager
def snapshot_image(
    path: Path,
    directory: Path,
    *,
    deadline: float,
    backing: Path | None = None,
    encoded_limit: int | None = None,
    progress: Any = lambda **kw: None,
) -> Iterator[tuple[apsw.Connection, Image]]:
    """Pinned online backup, full writable validation, then metadata from that same image."""
    if encoded_limit is None:
        encoded_limit = MAX_COMPRESSED_BYTES
    if backing is None and encoded_limit != MAX_COMPRESSED_BYTES:
        raise QualificationError("DEFAULT_MEMORY_LIMIT_REQUIRED")
    if type(encoded_limit) is not int or encoded_limit <= 0:
        raise QualificationError("INVALID_ENCODED_LIMIT")
    image = Image(None, limit=encoded_limit, deadline=deadline, backing=backing)
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
                    progress(phase="CAPTURE", bytes=image.size)
            source.close()
            source = None
            destination.close()
            destination = None
            image.freeze()
            original = image.reconstructed_hash()
            vfs.unregister()
            vfs = frozen_vfs(image, sentinel)
            destination = apsw.Connection(
                str(sentinel.resolve()), flags=apsw.SQLITE_OPEN_READWRITE, vfs=vfs.name
            )
            configure_cache(destination, image)
            destination.execute("PRAGMA query_only=ON")
            progress(phase="SOURCE_FULL_INTEGRITY", bytes=image.size)
            full_check(destination, deadline)
            yield destination, image
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
            if backing is not None:
                image.blocks.close()


@contextmanager
def archived_image(
    path: Path,
    directory: Path,
    *,
    deadline: float,
    backing: Path,
    encoded_limit: int,
    encoding: Literal["GZIP", "ZSTD"] | None,
    progress: Any = lambda **kw: None,
) -> Iterator[tuple[apsw.Connection, Image]]:
    """Full-check a closed archive and keep its exact decoded image pinned."""
    image = Image(
        path,
        limit=encoded_limit,
        deadline=deadline,
        backing=backing,
        archive_encoding=encoding,
        progress=progress,
    )
    try:
        with (
            classified_errors(deadline),
            tempfile.TemporaryDirectory(prefix="cw-cvfs-archive-", dir=directory) as work,
        ):
            sentinel = Path(work) / "sentinel.sqlite3"
            sentinel.touch(mode=0o600)
            vfs = frozen_vfs(image, sentinel)
            db = None
            try:
                db = apsw.Connection(str(sentinel.resolve()), flags=apsw.SQLITE_OPEN_READWRITE, vfs=vfs.name)
                db.execute("PRAGMA query_only=ON")
                configure_cache(db, image)
                progress(phase="SOURCE_FULL_INTEGRITY", bytes=image.size)
                full_check(db, deadline)
                yield db, image
            finally:
                if db is not None:
                    db.close()
                vfs.unregister()
            image.assert_source_unchanged()
            if image.reconstructed_hash() != image.sha256:
                raise QualificationError("BYTE_IDENTITY_MISMATCH")
    finally:
        image.blocks.close()


@contextmanager
def snapshot(path: Path, directory: Path, *, deadline: float) -> Iterator[apsw.Connection]:
    with snapshot_image(path, directory, deadline=deadline) as (connection, image):
        yield connection


class ArchiveWriter:
    """A bounded durable sink; an incomplete archive is never promoted or silently removed."""

    def __init__(self, output: BinaryIO, deadline: float) -> None:
        self.output = output
        self.deadline = deadline
        self.size = 0
        self.digest = hashlib.sha256()

    def write(self, data: bytes) -> int:
        budget(self.deadline)
        if self.size + len(data) > MAX_COMPRESSED_BYTES:
            raise QualificationError("COMPRESSED_MEMORY_LIMIT")
        count = self.output.write(data)
        if count != len(data):
            raise QualificationError("ARCHIVE_WRITE_INCOMPLETE")
        self.size += count
        self.digest.update(data)
        return count

    def flush(self) -> None:
        self.output.flush()


def archive_backup(path: Path, destination: Path, directory: Path, *, deadline: float) -> dict[str, Any]:
    """Native online snapshot → lossless archive → full persisted readback, within one deadline."""
    with classified_errors(deadline):
        handle = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(handle, "wb") as output:
            sink = ArchiveWriter(output, deadline)
            with snapshot_image(path, directory, deadline=deadline) as (connection, image):
                expected_size = image.size
                expected_hash = image.reconstructed_hash()
                compressor = zstandard.ZstdCompressor(level=COMPRESSION_LEVEL, write_checksum=True, threads=0)
                # Zstandard uses only write()/flush() from this bounded sink.
                with compressor.stream_writer(
                    cast(IO[bytes], sink), size=expected_size, closefd=False
                ) as writer:
                    for block in image.iter_bytes():
                        writer.write(block)
            del image
            del connection
            output.flush()
            os.fsync(output.fileno())
        if os.name != "nt":
            fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        result = validate_closed(destination, directory=directory, deadline=deadline, archive_encoding="ZSTD")
        if result["sha256"] != expected_hash or result["bytes"] != expected_size:
            raise QualificationError("BYTE_IDENTITY_MISMATCH")
        # The archive receipt also identifies the actual durable representation.
        actual = hashlib.sha256()
        with destination.open("rb") as encoded:
            for block in iter(lambda: encoded.read(1024 * 1024), b""):
                budget(deadline)
                actual.update(block)
        if destination.stat().st_size != sink.size or actual.hexdigest() != sink.digest.hexdigest():
            raise QualificationError("BYTE_IDENTITY_MISMATCH")
        budget(deadline)
        return {
            **result,
            "archive_encoding": "ZSTD",
            "archive_bytes": sink.size,
            "archive_sha256": actual.hexdigest(),
        }
