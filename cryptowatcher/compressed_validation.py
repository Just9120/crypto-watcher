"""Bounded exact-byte compressed validation images; production files are never rewritten."""

from __future__ import annotations

import gzip
import hashlib
import io
import mmap
import os
import stat
import sys
import tempfile
import time
import uuid
from array import array
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any, BinaryIO, Literal, cast

import apsw
import zstandard

from .storage import IntegrityError
from .validation_archive import ArchiveArena
from .validation_codes import DETAIL_CODES

BLOCK = 65536
CACHE_PAGES = 512
COMPRESSION_LEVEL = 1
WRITE_CACHE_BLOCKS = 8


MAX_COMPRESSED_BYTES = 3 * 1024**3
VALIDATION_MEMORY_BYTES = 4 * 1024**3
VALIDATION_OVERHEAD_BYTES = 512 * 1024**2
MAX_SQLITE_CACHE_BYTES = 1024**3
FRAMED_SQLITE_CACHE_BYTES = 2 * 1024**3


class QualificationError(IntegrityError):
    def __init__(self, code: str) -> None:
        self.detail_code = code if code in DETAIL_CODES else None
        category = code if code in ("TIME_BUDGET_EXCEEDED", "DATABASE_CORRUPT") else "DATABASE_ERROR"
        super().__init__(category, "Compressed validation refused: " + (self.detail_code or "UNCLASSIFIED"))


def budget(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise QualificationError("TIME_BUDGET_EXCEEDED")


class _FrameArena:
    """Keep compressed frames out of the interleaved Python allocation heap."""

    def __init__(self, limit: int) -> None:
        self.data = mmap.mmap(-1, limit)
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


class Image:
    def __init__(
        self,
        path: Path | None,
        *,
        limit: int,
        deadline: float,
        progress: Any = lambda **kw: None,
        archive_encoding: Literal["GZIP", "ZSTD"] | None = None,
        archive_store: ArchiveArena | None = None,
    ) -> None:
        if archive_store is not None and (path is not None or limit != archive_store.limit):
            raise QualificationError("INVALID_ARCHIVE_BOUND")
        self.blocks = _FrameArena(limit) if archive_store is None else archive_store
        self.size = 0
        self.retained = 0
        self.deadline = deadline
        self.limit = limit
        self.mutable = False
        self.cache: OrderedDict[int, bytes] = OrderedDict()
        self.write_cache: OrderedDict[int, bytearray] = OrderedDict()
        self.compressor = zstandard.ZstdCompressor(level=COMPRESSION_LEVEL, write_checksum=True, threads=0)
        self.decompressor = zstandard.ZstdDecompressor()
        self.page_decompressor = zstandard.ZstdDecompressor(max_window_size=BLOCK)
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
                while len(data) < BLOCK:
                    budget(deadline)
                    more = source.read(BLOCK - len(data))
                    if not more:
                        break
                    data += more
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
        if self.reconstructed_hash() != self.sha256:
            raise QualificationError("BYTE_IDENTITY_MISMATCH")

    @classmethod
    def from_archive_store(cls, store: ArchiveArena) -> Image:
        """Reopen a sealed, checked disk frame index without allocating a RAM image."""
        if not store.sealed or store.data is None:
            raise QualificationError("FROZEN_IMAGE_REQUIRED")
        store.assert_unchanged()
        image = cls(None, limit=store.limit, deadline=store.deadline, archive_store=store)
        image.path, image.before = store.path, store.identity
        image.size, image.sha256 = store.raw_bytes, store.raw_sha256
        image.retained = sum(store.lengths)
        image.mutable = False
        image.archive_encoding = "ZSTD"
        return image

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
                with self.decompressor.stream_reader(
                    source, closefd=False, read_across_frames=True
                ) as reader:
                    yield reader
            else:
                yield source

    def set_block(self, index: int, value: bytes) -> None:
        budget(self.deadline)
        if len(value) > BLOCK:
            raise QualificationError("INVALID_PAGE_SIZE")
        page = self.blocks.page_bytes if isinstance(self.blocks, ArchiveArena) else BLOCK
        blob = (
            b"".join(
                self.compressor.compress(value[start : start + page]) for start in range(0, len(value), page)
            )
            if value
            else self.compressor.compress(value)
        )
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

    def unpack_block(self, blob: bytes) -> bytes:
        if not isinstance(self.blocks, ArchiveArena):
            return self.decompressor.decompress(blob, max_output_size=BLOCK, allow_extra_data=False)
        # Bound both output and the decoder's window while consuming every
        # independent page frame, including checksum/trailing-data validation.
        parts: list[bytes] = []
        total = 0
        with self.page_decompressor.stream_reader(io.BytesIO(blob), read_across_frames=True) as reader:
            while value := reader.read(BLOCK + 1 - total):
                total += len(value)
                if total > BLOCK:
                    raise QualificationError("BYTE_IDENTITY_MISMATCH")
                parts.append(value)
        return b"".join(parts)

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
                    value = bytearray(self.unpack_block(self.blocks[index]))
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
        if isinstance(self.blocks, ArchiveArena) and self.size % BLOCK:
            index, tail = divmod(self.size, BLOCK)
            raw = self.unpack_block(self.blocks[index])
            self.set_block(index, raw[:tail])
        self.mutable = False
        if isinstance(self.blocks, ArchiveArena):
            self.blocks.finish(self.size, self.reconstructed_hash())

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
            value = self.unpack_block(self.blocks[index])
            expected = min(BLOCK, self.size - index * BLOCK)
            if len(value) not in (expected, BLOCK) or (
                isinstance(self.blocks, ArchiveArena) and len(value) != expected
            ):
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
            value = self.unpack_block(blob)
            expected = min(BLOCK, self.size - index * BLOCK)
            if len(value) not in (expected, BLOCK) or (
                isinstance(self.blocks, ArchiveArena) and len(value) != expected
            ):
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
    if isinstance(image.blocks, ArchiveArena):
        if not image.blocks.sealed:
            raise QualificationError("FROZEN_IMAGE_REQUIRED")
        # File pages remain reclaimable by the unchanged4GiB cgroup limit.
        # Reallocate cache inside the same hard4GiB cgroup. The file mapping is
        # reclaimable; keeping hot index pages avoids repeated frame decoding.
        cache_kib = FRAMED_SQLITE_CACHE_BYTES // 1024
        db.execute(f"PRAGMA cache_size=-{cache_kib}")
        return cache_kib
    image_bytes = image.retained + sys.getsizeof(image.blocks)
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
        from .owner_slots import sql_signature

        db.create_scalar_function("cw_owner_digest", sql_signature, 3, deterministic=True)
        from .catalogue_overlap import sql_signature as overlap_signature

        db.create_scalar_function("cw_catalogue_shadow_digest", overlap_signature, 2, deterministic=True)
        db.execute("PRAGMA mmap_size=0")
        db.execute("PRAGMA ignore_check_constraints=OFF")
        integrity = list(db.execute("PRAGMA integrity_check"))
        # Native xRead can expire inside SQLite's integrity opcode. SQLite may
        # return those read failures as rows without running our progress hook.
        budget(deadline)
        if integrity != [("ok",)]:
            raise QualificationError("DATABASE_CORRUPT")
        from .record_catalog import valid_heads

        heads_valid = valid_heads(db)
        budget(deadline)
        if not heads_valid:
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
    except (OSError, EOFError, zstandard.ZstdError) as error:
        raise QualificationError("VALIDATION_IO_ERROR") from error


@contextmanager
def snapshot_image(
    path: Path,
    directory: Path,
    *,
    deadline: float,
    archive_store: ArchiveArena | None = None,
    source_snapshot: apsw.Connection | None = None,
    progress: Callable[[str], None] | None = None,
    identity_reader: Callable[[], str] | None = None,
) -> Iterator[tuple[apsw.Connection, Image]]:
    """Full pinned snapshot validation; ownership of a supplied reader is transferred."""
    image = Image(
        None,
        limit=MAX_COMPRESSED_BYTES if archive_store is None else archive_store.limit,
        deadline=deadline,
        archive_store=archive_store,
    )
    with (
        classified_errors(deadline),
        tempfile.TemporaryDirectory(prefix="cw-cvfs-live-", dir=directory) as work,
    ):
        sentinel = Path(work) / "sentinel.sqlite3"
        sentinel.touch(mode=0o600)
        vfs = VFS(image, sentinel)
        destination: apsw.Connection | None = None
        source: apsw.Connection | None = source_snapshot
        try:
            destination = apsw.Connection(
                str(sentinel.resolve()), flags=apsw.SQLITE_OPEN_READWRITE, vfs=vfs.name
            )
            destination.execute("PRAGMA journal_mode=MEMORY")
            destination.execute("PRAGMA mmap_size=0")
            if source is None:
                source = apsw.Connection(
                    path.resolve().as_uri() + "?mode=ro",
                    flags=apsw.SQLITE_OPEN_READONLY | apsw.SQLITE_OPEN_URI,
                )
                source.execute("BEGIN")
                source.execute("SELECT owner_id FROM owner_settings").fetchone()
            elif (
                not source.readonly("main")
                or source.db_filename("main") != str(path.resolve())
                or source.txn_state("main") != apsw.SQLITE_TXN_READ
            ):
                raise QualificationError("PINNED_SOURCE_REQUIRED")
            if archive_store is not None:
                pages = source.execute("PRAGMA page_count").fetchone()
                page_size = source.execute("PRAGMA page_size").fetchone()
                if pages is None or page_size is None:
                    raise QualificationError("ARCHIVE_SIZE_LIMIT")
                size = pages[0] * page_size[0]
                if size > archive_store.max_raw_bytes:
                    raise QualificationError("ARCHIVE_SIZE_LIMIT")
            if progress is not None:
                progress("SOURCE_COPY")
            with destination.backup("main", source, "main") as backup:
                while not backup.done:
                    budget(deadline)
                    backup.step(256)
            source.close()
            source = None
            destination.close()
            destination = None
            if progress is not None:
                progress("SNAPSHOT_SEAL")
            image.freeze()
            original = (
                image.blocks.raw_sha256
                if isinstance(image.blocks, ArchiveArena)
                else image.reconstructed_hash()
            )
            vfs.unregister()
            vfs = frozen_vfs(image, sentinel)
            destination = apsw.Connection(
                str(sentinel.resolve()), flags=apsw.SQLITE_OPEN_READWRITE, vfs=vfs.name
            )
            configure_cache(destination, image)
            destination.execute("PRAGMA query_only=ON")
            if progress is not None:
                progress("FULL_INTEGRITY")
            full_check(destination, deadline)
            if progress is not None:
                progress("SOURCE_METADATA")
            yield destination, image
            destination.close()
            destination = None
            if progress is not None:
                progress("FULL_BYTE_IDENTITY")
            # Release native mmap borrows before a caller closes and independently
            # reopens the persisted archive. Its complete digest is still compared
            # against the exact captured bytes; no identity gate is optional.
            vfs.unregister()
            actual = image.reconstructed_hash() if identity_reader is None else identity_reader()
            budget(deadline)
            if actual != original:
                raise QualificationError("BYTE_IDENTITY_MISMATCH")
        finally:
            if source is not None:
                source.close()
            if destination is not None:
                destination.close()
            vfs.unregister()


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
