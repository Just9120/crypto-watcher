"""One bounded native read-ahead buffer under SQLite's original source locks."""

from __future__ import annotations

import ctypes
import uuid
from pathlib import Path
from typing import Any

import apsw

from . import native_validation


class SourceVFS:
    def __init__(self, path: Path, deadline: float) -> None:
        from .compressed_validation import QualificationError, budget

        budget(deadline)
        self.name = "cw-native-source-" + uuid.uuid4().hex
        self.pointer: int | None = None
        try:
            self.lib = native_validation.load_library()
            create = self.lib.cwsource_create
            create.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_double]
            create.restype = ctypes.c_void_p
            self.lib.cwsource_destroy.argtypes = [ctypes.c_void_p]
            self.lib.cwsource_destroy.restype = ctypes.c_int
            self.lib.cwsource_stats.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_uint64),
                ctypes.POINTER(ctypes.c_uint64),
                ctypes.POINTER(ctypes.c_uint64),
            ]
            self.pointer = create(self.name.encode(), str(path.resolve()).encode(), deadline)
        except (OSError, AttributeError, apsw.Error) as error:
            raise QualificationError("NATIVE_VFS_LOAD_FAILED") from error
        if self.pointer is None:
            budget(deadline)
            raise QualificationError("NATIVE_VFS_REGISTRATION_REFUSED")

    def unregister(self) -> None:
        from .compressed_validation import QualificationError

        if self.pointer is None:
            return
        if self.lib.cwsource_destroy(self.pointer) != apsw.SQLITE_OK:
            raise QualificationError("NATIVE_VFS_HANDLES_OPEN")
        self.pointer = None

    def stats(self) -> dict[str, Any]:
        from .compressed_validation import QualificationError

        if self.pointer is None:
            raise QualificationError("NATIVE_VFS_CONTEXT_CLOSED")
        reads, parent_reads, fetched = ctypes.c_uint64(), ctypes.c_uint64(), ctypes.c_uint64()
        self.lib.cwsource_stats(
            self.pointer, ctypes.byref(reads), ctypes.byref(parent_reads), ctypes.byref(fetched)
        )
        return {
            "vfs_backend": "native-read-ahead",
            "sqlite_reads": reads.value,
            "parent_reads": parent_reads.value,
            "fetched_bytes": fetched.value,
            "buffer_bytes": 1024 * 1024,
        }


def open_source(
    path: Path, deadline: float, *, native: bool = True
) -> tuple[apsw.Connection, SourceVFS | None]:
    vfs = SourceVFS(path, deadline) if native and native_validation.available() else None
    reader = None
    try:
        reader = apsw.Connection(
            path.as_uri() + "?mode=ro",
            flags=apsw.SQLITE_OPEN_READONLY | apsw.SQLITE_OPEN_URI,
            vfs=None if vfs is None else vfs.name,
        )
        reader.execute("PRAGMA query_only=ON")
        return reader, vfs
    except BaseException:
        if reader is not None:
            reader.close()
        if vfs is not None:
            vfs.unregister()
        raise
