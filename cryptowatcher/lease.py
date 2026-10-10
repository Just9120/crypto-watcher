"""Exclusive process lease; never deletes another process's lock file."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from .storage import StateError


class RuntimeLease:
    def __init__(self, database: Path) -> None:
        self.path = database.with_suffix(database.suffix + ".lock")
        self.handle: Any = None

    def __enter__(self) -> RuntimeLease:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                if self.path.stat().st_size == 0:
                    self.handle.write(b"0")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise StateError("Another CryptoWatcher writer is active") from exc
        return self

    def __exit__(self, *args: Any) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None
