"""Per-handle hints for a one-pass, pinned read-only backup source on Linux."""

from __future__ import annotations

import os
import platform
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import apsw


def _supported() -> bool:
    version = re.match(r"^(\d+)\.(\d+)", platform.release())
    return (
        sys.platform == "linux"
        and version is not None
        and tuple(map(int, version.groups())) >= (6, 3)
        and all(
            hasattr(os, name)
            for name in ("posix_fadvise", "POSIX_FADV_NORMAL", "POSIX_FADV_SEQUENTIAL", "POSIX_FADV_NOREUSE")
        )
    )


def _readonly_handle(path: Path) -> tuple[int, tuple[int, int]] | None:
    import fcntl

    identity = path.stat()
    expected = (identity.st_dev, identity.st_ino)
    matches = []
    # Do not open or duplicate the database: closing an unrelated descriptor
    # could release SQLite's process-owned POSIX locks. This CLI has one reader.
    for name in os.listdir("/proc/self/fd"):
        if not name.isdecimal():
            continue
        handle = int(name)
        try:
            current = os.fstat(handle)
            if (current.st_dev, current.st_ino) == expected:
                if fcntl.fcntl(handle, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY:
                    return None
                matches.append(handle)
        except OSError:
            continue
    return (matches[0], expected) if len(matches) == 1 else None


@contextmanager
def source_scan(database: apsw.Connection, path: Path) -> Iterator[dict[str, Any]]:
    """Hint only a unique existing source handle; never evict caches or change data.

    The connection must remain open and exclusively used throughout this scope.
    NORMAL is restored before its caller closes it, including on backup failure.
    Unsupported or ambiguous handles keep SQLite's ordinary safe behavior.
    """
    status: dict[str, Any] = {"policy": "DEFAULT", "reason": "UNAVAILABLE"}
    handle: int | None = None
    identity: tuple[int, int] | None = None
    attempted = False
    try:
        if (
            _supported()
            and database.readonly("main")
            and database.db_filename("main") == str(path.resolve())
            and database.txn_state("main") == apsw.SQLITE_TXN_READ
        ):
            try:
                selected = _readonly_handle(path)
                if selected is not None:
                    handle, identity = selected
                    attempted = True
                    os.posix_fadvise(handle, 0, 0, os.POSIX_FADV_SEQUENTIAL)
                    os.posix_fadvise(handle, 0, 0, os.POSIX_FADV_NOREUSE)
                    status.update(policy="SEQUENTIAL_NOREUSE", reason="APPLIED")
            except OSError:
                status.update(policy="DEFAULT", reason="ADVICE_UNAVAILABLE")
        yield status
    finally:
        if attempted and handle is not None:
            try:
                current = os.fstat(handle)
                if (current.st_dev, current.st_ino) == identity:
                    os.posix_fadvise(handle, 0, 0, os.POSIX_FADV_NORMAL)
                    status["restored"] = True
                else:
                    status["restored"] = False
            except OSError:
                status["restored"] = False
