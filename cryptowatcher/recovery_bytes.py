"""Exact archive bytes only; SQL qualification must precede any integrity proof reuse."""

from __future__ import annotations

import gzip
import hashlib
import math
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Iterator

import zstandard

from .history_rebuild import guard
from .storage import StateError


class RestoreRefused(StateError):
    """A byte proof is incomplete; never inherit an earlier SQL PASS."""


def identity(path: Path) -> tuple[int, int, int, int]:
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise RestoreRefused("SYMLINK")
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode):
        raise RestoreRefused("REGULAR_FILE_REQUIRED")
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


@contextmanager
def opened(path: Path, expected: tuple[int, int, int, int]) -> Iterator[BinaryIO]:
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as raw:
        value = os.fstat(raw.fileno())
        if (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns) != expected:
            raise RestoreRefused("SOURCE_CHANGED")
        yield raw


def strict_zstd_frame(raw: BinaryIO, deadline: float) -> None:
    """Check boundaries; the independent decoder must still validate content/checksum."""

    def exact(size: int) -> bytes:
        guard(deadline)
        value = raw.read(size)
        if len(value) != size:
            raise RestoreRefused("INCOMPLETE_ZSTD_FRAME")
        return value

    header = exact(4)
    if header != b"\x28\xb5\x2f\xfd":
        raise RestoreRefused("UNSUPPORTED_ZSTD_FRAME")
    descriptor = exact(1)
    flags = descriptor[0]
    if flags & 0x18 or flags & 3 or not flags & 4:
        raise RestoreRefused("UNSUPPORTED_ZSTD_FRAME")
    single = bool(flags & 32)
    fcs = flags >> 6
    header += descriptor
    if not single:
        header += exact(1)
    length = (1 if single else 0) if fcs == 0 else (2, 4, 8)[fcs - 1]
    header += exact(length)
    parameters = zstandard.get_frame_parameters(header)
    if parameters.dict_id or not parameters.has_checksum or parameters.window_size > 128 * 1024**2:
        raise RestoreRefused("UNSUPPORTED_ZSTD_FRAME")
    while True:
        block = int.from_bytes(exact(3), "little")
        kind, size = (block >> 1) & 3, block >> 3
        if kind == 3 or size > min(parameters.window_size, 128 * 1024):
            raise RestoreRefused("INVALID_ZSTD_BLOCK")
        exact(1 if kind == 1 else size)
        if block & 1:
            break
    exact(4)
    if raw.read(1):
        raise RestoreRefused("TRAILING_ZSTD_DATA")


def verify_archive_bytes(
    path: Path,
    *,
    encoding: str,
    encoded_sha256: str,
    encoded_bytes: int,
    decoded_sha256: str,
    decoded_bytes: int,
    deadline: float,
) -> dict[str, Any]:
    """Bounded independent restore, never a standalone database integrity assertion."""
    if (
        encoding not in ("gzip", "zstd")
        or any(type(size) is not int or size <= 0 for size in (encoded_bytes, decoded_bytes))
        or not math.isfinite(deadline)
        or any(
            not isinstance(h, str) or not re.fullmatch("[a-f0-9]{64}", h)
            for h in (encoded_sha256, decoded_sha256)
        )
    ):
        raise RestoreRefused("INVALID_PROOF_INPUT")
    guard(deadline)
    before = identity(path)
    if before[2] != encoded_bytes:
        raise RestoreRefused("ENCODED_IDENTITY_MISMATCH")
    encoded = hashlib.sha256()
    with opened(path, before) as raw:
        while block := raw.read(1024 * 1024):
            guard(deadline)
            encoded.update(block)
    if encoded.hexdigest() != encoded_sha256 or identity(path) != before:
        raise RestoreRefused("ENCODED_IDENTITY_MISMATCH")
    digest, size = hashlib.sha256(), 0
    try:
        if encoding == "zstd":
            with opened(path, before) as raw:
                strict_zstd_frame(raw, deadline)
        with opened(path, before) as raw:
            stream = (
                gzip.GzipFile(fileobj=raw, mode="rb")
                if encoding == "gzip"
                else zstandard.ZstdDecompressor().stream_reader(raw, read_across_frames=True, closefd=False)
            )
            with stream:
                while True:
                    guard(deadline)
                    block = stream.read(min(1024 * 1024, decoded_bytes - size + 1))
                    if not block:
                        break
                    size += len(block)
                    if size > decoded_bytes:
                        raise RestoreRefused("DECODED_SIZE_MISMATCH")
                    digest.update(block)
    except (OSError, EOFError, zstandard.ZstdError) as error:
        raise RestoreRefused("ARCHIVE_DECODE_REJECTED") from error
    if size != decoded_bytes or digest.hexdigest() != decoded_sha256:
        raise RestoreRefused("DECODED_IDENTITY_MISMATCH")
    if identity(path) != before:
        raise RestoreRefused("SOURCE_CHANGED")
    guard(deadline)
    return {
        "state": "BYTE_RESTORE_PASS_ONLY",
        "encoding": encoding,
        "encoded_sha256": encoded_sha256,
        "encoded_bytes": encoded_bytes,
        "decoded_sha256": decoded_sha256,
        "decoded_bytes": size,
        "database_integrity": "NOT_ASSERTED_BY_BYTE_VERIFIER",
    }
