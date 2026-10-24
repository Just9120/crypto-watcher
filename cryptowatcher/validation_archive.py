"""Disk-backed frozen frames inside a standard, independently restorable ZSTD stream.

SQLite backup writes the header again at completion. Its first64KiB is reserved;
other completed frames must arrive in order or remain byte-identical. Skippable
ZSTD frames hold padding and a checked private index. No market writer uses this.
"""

from __future__ import annotations

import hashlib
import math
import mmap
import os
import re
import shutil
import struct
import sys
from array import array
from pathlib import Path
from typing import Iterator, Never

BLOCK = 65536
PAGE_BYTES = BLOCK
PREFIX_BYTES = 2 * BLOCK
RESERVE_BYTES = 1536 * 1024**2
SKIP = 0x184D2A50
INDEX_MAGIC = b"CWZIDX01"
TAIL_MAGIC = b"CWZEND01"
HEADER = struct.Struct("<8sQQQ32s")
ENTRY = struct.Struct("<QQ")
TAIL = struct.Struct("<Q8s")


def refused(code: str) -> Never:
    from .compressed_validation import QualificationError

    raise QualificationError(code)


class ArchiveArena:
    """Bounded file storage with the same borrowed-buffer interface as RAM frames."""

    def __init__(
        self,
        path: Path,
        *,
        max_raw_bytes: int,
        max_archive_bytes: int,
        deadline: float,
        reserve_bytes: int = RESERVE_BYTES,
    ) -> None:
        if (
            type(max_raw_bytes) is not int
            or max_raw_bytes <= 0
            or type(max_archive_bytes) is not int
            or max_archive_bytes <= PREFIX_BYTES
            or not math.isfinite(deadline)
            or type(reserve_bytes) is not int
            or reserve_bytes < RESERVE_BYTES
        ):
            refused("INVALID_ARCHIVE_BOUND")
        self.path = path
        self.page_bytes = PAGE_BYTES
        self.max_raw_bytes = max_raw_bytes
        self.limit = max_archive_bytes
        self.deadline = deadline
        self.reserve_bytes = reserve_bytes
        self.footer_reserve = (
            8 + HEADER.size + ENTRY.size * ((max_raw_bytes + BLOCK - 1) // BLOCK) + 32 + TAIL.size
        )
        self.offsets = array("Q")
        self.lengths = array("Q")
        self.header = b""
        self.published = 0
        self.sealed = False
        self.data: mmap.mmap | None = None
        self.end = PREFIX_BYTES
        self._admit(0)
        if shutil.disk_usage(self.path.parent).free < self.reserve_bytes + PREFIX_BYTES:
            refused("ARCHIVE_DISK_RESERVE")
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        self.file = os.fdopen(handle, "r+b", buffering=0)
        self.file.truncate(PREFIX_BYTES)

    def _admit(self, additional: int) -> None:
        from .compressed_validation import budget

        budget(self.deadline)
        if additional < 0 or self.end + additional + self.footer_reserve > self.limit:
            refused("ARCHIVE_SIZE_LIMIT")
        if shutil.disk_usage(self.path.parent).free < self.reserve_bytes + additional:
            refused("ARCHIVE_DISK_RESERVE")

    def __len__(self) -> int:
        return len(self.offsets)

    def __sizeof__(self) -> int:
        return object.__sizeof__(self) + sys.getsizeof(self.offsets) + sys.getsizeof(self.lengths)

    def __getitem__(self, index: int) -> bytes:
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        if not self.sealed and index == 0:
            return self.header
        offset, length = self.offsets[index], self.lengths[index]
        if not length:
            return b""
        if self.data is not None:
            return self.data[offset : offset + length]
        self.file.seek(offset)
        result = self.file.read(length)
        if len(result) != length:
            refused("ARCHIVE_READ_INCOMPLETE")
        return result

    def __iter__(self) -> Iterator[bytes]:
        for index in range(len(self)):
            yield self[index]

    def _write(self, offset: int, value: bytes) -> None:
        self.file.seek(offset)
        if self.file.write(value) != len(value):
            refused("ARCHIVE_WRITE_INCOMPLETE")

    def __setitem__(self, index: int, value: bytes) -> None:
        if self.sealed or not 0 <= index < len(self) or len(value) > 2 * BLOCK:
            refused("INVALID_ARCHIVE_WRITE")
        if index == 0:
            if len(value) + 8 > PREFIX_BYTES:
                refused("INVALID_ARCHIVE_HEADER")
            self.header = value
            self.lengths[0] = len(value)
            return
        if index <= self.published:
            if self[index] == value:
                return
            if index != self.published:
                refused("NONSEQUENTIAL_SNAPSHOT_WRITE")
            offset = self.offsets[index]
        elif index == self.published + 1:
            offset = self.end
        else:
            refused("NONSEQUENTIAL_SNAPSHOT_WRITE")
        extra = max(0, offset + len(value) - self.end)
        self._admit(extra)
        self._write(offset, value)
        self.end = offset + len(value)
        self.file.truncate(self.end)
        self.offsets[index], self.lengths[index] = offset, len(value)
        self.published = index

    def append(self, value: bytes) -> None:
        if self.sealed or len(self) >= (self.max_raw_bytes + BLOCK - 1) // BLOCK:
            refused("ARCHIVE_SIZE_LIMIT")
        self.offsets.append(0)
        self.lengths.append(0)
        if value:
            self[len(self) - 1] = value

    def pop(self) -> bytes:
        if self.sealed or not len(self):
            refused("INVALID_ARCHIVE_TRUNCATE")
        index = len(self) - 1
        result = self[index]
        if index and index == self.published:
            self.end = self.offsets[index]
            self.file.truncate(self.end)
            self.published -= 1
        self.offsets.pop()
        self.lengths.pop()
        if not index:
            self.header = b""
        return result

    def finish(self, raw_bytes: int, raw_sha256: str) -> None:
        if (
            self.sealed
            or type(raw_bytes) is not int
            or not 0 < raw_bytes <= self.max_raw_bytes
            or len(self) != (raw_bytes + BLOCK - 1) // BLOCK
            or self.published != len(self) - 1
            or not self.header
            or not re.fullmatch(r"[0-9a-f]{64}", raw_sha256)
        ):
            refused("INVALID_ARCHIVE_FINALIZATION")
        expected_hash = bytes.fromhex(raw_sha256)
        if len(expected_hash) != 32:
            refused("INVALID_ARCHIVE_FINALIZATION")
        padding = PREFIX_BYTES - len(self.header) - 8
        self._write(0, self.header + struct.pack("<II", SKIP, padding) + bytes(padding))
        header = HEADER.pack(INDEX_MAGIC, raw_bytes, len(self), self.end, expected_hash)
        entries = b"".join(ENTRY.pack(offset, length) for offset, length in zip(self.offsets, self.lengths))
        body = header + entries
        payload_size = len(body) + 32 + TAIL.size
        footer = (
            struct.pack("<II", SKIP, payload_size)
            + body
            + hashlib.sha256(body).digest()
            + TAIL.pack(payload_size, TAIL_MAGIC)
        )
        self._admit(0)
        if self.end + len(footer) > self.limit:
            refused("ARCHIVE_SIZE_LIMIT")
        self._write(self.end, footer)
        self.end += len(footer)
        self.file.flush()
        os.fsync(self.file.fileno())
        if os.name != "nt":
            handle = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(handle)
            finally:
                os.close(handle)
        self.sealed = True
        self.data = mmap.mmap(self.file.fileno(), 0, access=mmap.ACCESS_COPY)
        self.raw_bytes, self.raw_sha256 = raw_bytes, raw_sha256
        self.identity = self.path.stat()

    @classmethod
    def open(cls, path: Path, *, max_raw_bytes: int, max_archive_bytes: int, deadline: float) -> ArchiveArena:
        from .compressed_validation import budget

        budget(deadline)
        if (
            path.is_symlink()
            or not path.is_file()
            or type(max_raw_bytes) is not int
            or max_raw_bytes <= 0
            or type(max_archive_bytes) is not int
            or not math.isfinite(deadline)
        ):
            refused("REGULAR_CLOSED_FILE_REQUIRED")
        before = path.stat()
        if not PREFIX_BYTES < before.st_size <= max_archive_bytes:
            refused("ARCHIVE_SIZE_LIMIT")
        instance = cls.__new__(cls)
        handle = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        instance.file = os.fdopen(handle, "rb", buffering=0)
        instance.data = None
        try:
            opened = os.fstat(instance.file.fileno())
            if any(
                getattr(before, key) != getattr(opened, key)
                for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns")
            ):
                refused("SOURCE_CHANGED")
            instance.file.seek(-TAIL.size, os.SEEK_END)
            tail = instance.file.read(TAIL.size)
            if len(tail) != TAIL.size:
                refused("INVALID_ARCHIVE_INDEX")
            size, marker = TAIL.unpack(tail)
            max_count = (max_raw_bytes + BLOCK - 1) // BLOCK
            if (
                marker != TAIL_MAGIC
                or not HEADER.size + 32 + TAIL.size
                <= size
                <= HEADER.size + ENTRY.size * max_count + 32 + TAIL.size
            ):
                refused("INVALID_ARCHIVE_INDEX")
            start = before.st_size - size - 8
            if start < PREFIX_BYTES:
                refused("INVALID_ARCHIVE_INDEX")
            instance.file.seek(start)
            if instance.file.read(8) != struct.pack("<II", SKIP, size):
                refused("INVALID_ARCHIVE_INDEX")
            payload = instance.file.read(size)
            if len(payload) != size:
                refused("INVALID_ARCHIVE_INDEX")
            body, checksum = payload[: -32 - TAIL.size], payload[-32 - TAIL.size : -TAIL.size]
            if hashlib.sha256(body).digest() != checksum:
                refused("INVALID_ARCHIVE_INDEX")
            magic, raw_bytes, count, extent, raw_hash = HEADER.unpack(body[: HEADER.size])
            if (
                magic != INDEX_MAGIC
                or not 0 < raw_bytes <= max_raw_bytes
                or count != (raw_bytes + BLOCK - 1) // BLOCK
                or extent != start
                or len(body) != HEADER.size + count * ENTRY.size
            ):
                refused("INVALID_ARCHIVE_INDEX")
            offsets, lengths = array("Q"), array("Q")
            expected = PREFIX_BYTES
            for index in range(count):
                budget(deadline)
                offset, length = ENTRY.unpack_from(body, HEADER.size + index * ENTRY.size)
                if not 0 < length <= 2 * BLOCK or (offset != 0 if index == 0 else offset != expected):
                    refused("INVALID_ARCHIVE_INDEX")
                if index == 0:
                    if length + 8 > PREFIX_BYTES:
                        refused("INVALID_ARCHIVE_INDEX")
                    instance.file.seek(length)
                    if instance.file.read(8) != struct.pack("<II", SKIP, PREFIX_BYTES - length - 8):
                        refused("INVALID_ARCHIVE_INDEX")
                else:
                    expected = offset + length
                if offset + length > start:
                    refused("INVALID_ARCHIVE_INDEX")
                offsets.append(offset)
                lengths.append(length)
            if expected != extent:
                refused("INVALID_ARCHIVE_INDEX")
            instance.path, instance.offsets, instance.lengths = path, offsets, lengths
            instance.page_bytes = BLOCK  # Encoding is detected from existing frames when reading.
            instance.max_raw_bytes, instance.limit, instance.deadline = (
                max_raw_bytes,
                max_archive_bytes,
                deadline,
            )
            instance.reserve_bytes = RESERVE_BYTES
            instance.end, instance.sealed, instance.header = before.st_size, True, b""
            instance.raw_bytes, instance.raw_sha256 = raw_bytes, raw_hash.hex()
            instance.data = mmap.mmap(instance.file.fileno(), 0, access=mmap.ACCESS_COPY)
            instance.identity = before
            instance.assert_unchanged()
            return instance
        except BaseException:
            instance.close()
            raise

    def assert_unchanged(self) -> None:
        from .compressed_validation import budget

        budget(self.deadline)
        before, after = self.identity, self.path.stat()
        if any(
            getattr(before, name) != getattr(after, name)
            for name in ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        ):
            refused("SOURCE_CHANGED")

    def close(self) -> None:
        if self.data is not None:
            self.data.close()
            self.data = None
        self.file.close()
