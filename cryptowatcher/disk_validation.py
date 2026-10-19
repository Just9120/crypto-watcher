"""Full readonly disk validation beyond the stdlib SQLite 2 GiB mmap ceiling."""

from __future__ import annotations

import math
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import apsw

from .storage import IntegrityError

LARGE_DISK_BYTES = 2 * 1024**3
MMAP_BYTES = 3 * 1024**3
CACHE_KIB = 128 * 1024


def large_database(path: Path) -> bool:
    return path.stat().st_size > LARGE_DISK_BYTES


def budget(deadline: float) -> None:
    if not math.isfinite(deadline) or time.monotonic() >= deadline:
        raise IntegrityError("TIME_BUDGET_EXCEEDED", "Integrity time budget exceeded")


@contextmanager
def validated_disk(path: Path, *, deadline: float) -> Iterator[apsw.Connection]:
    """Yield metadata from the same snapshot after every full integrity check passed."""
    budget(deadline)
    database = None
    try:
        # SQLite omits CHECK verification on a SQLITE_OPEN_READONLY handle.
        # A logical read/write handle with query_only still rejects every write,
        # while the full integrity checker includes CHECK constraints.
        database = apsw.Connection(str(path.resolve()), flags=apsw.SQLITE_OPEN_READWRITE)
        database.execute("PRAGMA query_only=ON")
        database.execute("BEGIN")
        database.execute("SELECT rootpage FROM sqlite_master LIMIT 1").fetchone()
        # The pinned APSW build supports >2 GiB mappings. The validation container
        # still has its existing 4 GiB cap; the application writer is unchanged.
        database.execute(f"PRAGMA cache_size=-{CACHE_KIB}")
        database.execute(f"PRAGMA mmap_size={MMAP_BYTES}")
        database.execute("PRAGMA ignore_check_constraints=OFF")
        database.set_progress_handler(lambda: time.monotonic() >= deadline, 10000)
        from .owner_slots import sql_signature

        database.create_scalar_function("cw_owner_digest", sql_signature, 3, deterministic=True)
        if list(database.execute("PRAGMA integrity_check")) != [("ok",)]:
            raise IntegrityError("DATABASE_CORRUPT", "Database integrity check failed")
        budget(deadline)
        yield database
        budget(deadline)
    except apsw.Error as exc:
        if time.monotonic() >= deadline:
            code = "TIME_BUDGET_EXCEEDED"
        elif isinstance(exc, (apsw.CorruptError, apsw.NotADBError)):
            code = "DATABASE_CORRUPT"
        elif isinstance(exc, apsw.InterruptError):
            code = "INTERRUPTED"
        elif isinstance(exc, apsw.IOError):
            code = "IO_ERROR"
        else:
            code = "DATABASE_ERROR"
        raise IntegrityError(code, "Disk integrity validation did not complete") from exc
    finally:
        if database is not None:
            database.set_progress_handler(None, 0)
            database.close()
