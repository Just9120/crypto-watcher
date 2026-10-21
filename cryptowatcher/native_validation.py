"""Native frozen-image VFS; the market runtime does not load this extension."""

from __future__ import annotations

import ctypes
import math
import sys
import uuid
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any

import apsw

if TYPE_CHECKING:
    from .compressed_validation import Image

LIBRARY = Path(__file__).with_name("_native") / "compressed_vfs.so"


def available() -> bool:
    return sys.platform == "linux" and ctypes.sizeof(ctypes.c_void_p) == 8 and LIBRARY.is_file()


class NativeVFS:
    """Own borrowed arena storage until every SQLite handle has closed."""

    def __init__(self, image: Image, path: Path) -> None:
        from .compressed_validation import QualificationError, budget

        budget(image.deadline)
        if image.mutable or not math.isfinite(image.deadline):
            raise QualificationError("FROZEN_IMAGE_REQUIRED")
        self.image = image
        self.path = str(path.resolve())
        self.name = "cw-native-validation-" + uuid.uuid4().hex
        self.pointer: int | None = None
        try:
            self.lib = ctypes.CDLL(str(LIBRARY))
            # Load only the pinned local helper into an ephemeral database.
            with closing(apsw.Connection(":memory:")) as bootstrap:
                bootstrap.enable_load_extension(True)
                try:
                    bootstrap.load_extension(str(LIBRARY), "sqlite3_cwcvfs_init")
                finally:
                    bootstrap.enable_load_extension(False)
            create = self.lib.cwcvfs_create
            create.argtypes = [
                ctypes.c_char_p,
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.POINTER(ctypes.c_uint64),
                ctypes.POINTER(ctypes.c_uint64),
                ctypes.c_size_t,
                ctypes.c_uint64,
                ctypes.c_double,
            ]
            create.restype = ctypes.c_void_p
            self.lib.cwcvfs_destroy.argtypes = [ctypes.c_void_p]
            self.lib.cwcvfs_destroy.restype = ctypes.c_int
            self.lib.cwcvfs_stats.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_uint64),
                ctypes.POINTER(ctypes.c_uint64),
            ]
            arena = image.blocks
            # This exported buffer and image reference keep the mmap alive.
            data = arena.data
            if data is None:
                raise QualificationError("FROZEN_IMAGE_REQUIRED")
            self.borrow = ctypes.c_char.from_buffer(data)
            offsets = ctypes.cast(arena.offsets.buffer_info()[0], ctypes.POINTER(ctypes.c_uint64))
            lengths = ctypes.cast(arena.lengths.buffer_info()[0], ctypes.POINTER(ctypes.c_uint64))
            self.pointer = create(
                self.name.encode(),
                ctypes.addressof(self.borrow),
                arena.end,
                offsets,
                lengths,
                len(arena),
                image.size,
                image.deadline,
            )
        except (OSError, AttributeError, apsw.Error) as error:
            if hasattr(self, "borrow"):
                del self.borrow
            raise QualificationError("NATIVE_VFS_LOAD_FAILED") from error
        if self.pointer is None:
            if hasattr(self, "borrow"):
                del self.borrow
            budget(image.deadline)
            raise QualificationError("NATIVE_VFS_REGISTRATION_REFUSED")

    def unregister(self) -> None:
        from .compressed_validation import QualificationError

        if self.pointer is None:
            return
        if self.lib.cwcvfs_destroy(self.pointer) != apsw.SQLITE_OK:
            raise QualificationError("NATIVE_VFS_HANDLES_OPEN")
        self.pointer = None
        del self.borrow

    def stats(self) -> dict[str, Any]:
        from .compressed_validation import QualificationError

        if self.pointer is None:
            raise QualificationError("NATIVE_VFS_CONTEXT_CLOSED")
        reads, decodes = ctypes.c_uint64(), ctypes.c_uint64()
        self.lib.cwcvfs_stats(self.pointer, ctypes.byref(reads), ctypes.byref(decodes))
        return {"vfs_backend": "native", "reads": reads.value, "decodes": decodes.value}
