"""Native frozen-image VFS; the market runtime does not load this extension."""

from __future__ import annotations

import ctypes
import math
import sys
import uuid
from array import array
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
        self.disk_prefetch = False
        self.pinned: Any = None
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
            self.borrow = ctypes.c_char.from_buffer(arena.data)
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
            raise QualificationError("NATIVE_VFS_LOAD_FAILED") from error
        if self.pointer is None:
            raise QualificationError("NATIVE_VFS_REGISTRATION_REFUSED")
        # Do not alter anonymous RAM images or require a new ABI from an older
        # installed helper. A file-backed frame gets one explicit prefetch,
        # avoiding kernel read-ahead of unrelated compressed neighbours.
        prefetch = getattr(self.lib, "cwcvfs_disk_prefetch", None)
        if getattr(image.blocks, "backing", None) is not None and prefetch is not None:
            prefetch.argtypes = [ctypes.c_void_p]
            prefetch.restype = ctypes.c_int
            self.disk_prefetch = bool(prefetch(self.pointer))

    def pin_pages(self, data: Any, extent: int, pages: array, offsets: array, lengths: array) -> None:
        """Install exact-byte verified auxiliary pages before opening SQLite handles."""
        from .compressed_validation import QualificationError, budget

        budget(self.image.deadline)
        function = getattr(self.lib, "cwcvfs_pin_pages", None)
        if (
            self.pointer is None
            or self.pinned is not None
            or function is None
            or type(extent) is not int
            or not 0 < extent <= min(len(data), 1024**3)
            or not pages
            or any(
                a.typecode != "Q" or a.itemsize != 8 or len(a) != len(pages)
                for a in (pages, offsets, lengths)
            )
        ):
            raise QualificationError("NATIVE_PAGE_CACHE_REFUSED")
        pointer = ctypes.POINTER(ctypes.c_uint64)
        function.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            pointer,
            pointer,
            pointer,
            ctypes.c_size_t,
        ]
        function.restype = ctypes.c_int
        borrow = ctypes.c_char.from_buffer(data)
        result = function(
            self.pointer,
            ctypes.addressof(borrow),
            extent,
            *(ctypes.cast(a.buffer_info()[0], pointer) for a in (pages, offsets, lengths)),
            len(pages),
        )
        if result == apsw.SQLITE_OK:
            self.pinned = (data, borrow)
        budget(self.image.deadline)
        if result != apsw.SQLITE_OK:
            raise QualificationError("NATIVE_PAGE_CACHE_REFUSED")

    def unregister(self) -> None:
        from .compressed_validation import QualificationError

        if self.pointer is None:
            return
        if self.lib.cwcvfs_destroy(self.pointer) != apsw.SQLITE_OK:
            raise QualificationError("NATIVE_VFS_HANDLES_OPEN")
        self.pointer = None
        # The native context has released its borrowed data pointer.
        del self.borrow
        self.pinned = None

    def stats(self) -> dict[str, Any]:
        from .compressed_validation import QualificationError

        if self.pointer is None:
            raise QualificationError("NATIVE_VFS_CONTEXT_CLOSED")
        reads, decodes = ctypes.c_uint64(), ctypes.c_uint64()
        self.lib.cwcvfs_stats(self.pointer, ctypes.byref(reads), ctypes.byref(decodes))
        result = {
            "vfs_backend": "native",
            "reads": reads.value,
            "decodes": decodes.value,
            "disk_prefetch": self.disk_prefetch,
        }
        extra = getattr(self.lib, "cwcvfs_prefetch_stats", None)
        if extra is not None:
            extra.argtypes = [ctypes.c_void_p, *([ctypes.POINTER(ctypes.c_uint64)] * 3)]
            extra.restype = None
            calls, failures, encoded = ctypes.c_uint64(), ctypes.c_uint64(), ctypes.c_uint64()
            extra(self.pointer, ctypes.byref(calls), ctypes.byref(failures), ctypes.byref(encoded))
            result.update(
                prefetches=calls.value, prefetch_failures=failures.value, encoded_bytes=encoded.value
            )
        shape = getattr(self.lib, "cwcvfs_prefetch_shape", None)
        if shape is not None:
            shape.argtypes = [ctypes.c_void_p, *([ctypes.POINTER(ctypes.c_uint64)] * 3)]
            shape.restype = None
            wide, covered, requested = ctypes.c_uint64(), ctypes.c_uint64(), ctypes.c_uint64()
            shape(self.pointer, ctypes.byref(wide), ctypes.byref(covered), ctypes.byref(requested))
            result.update(
                wide_prefetches=wide.value,
                covered_prefetches=covered.value,
                prefetch_requested_bytes=requested.value,
            )
        pinned = getattr(self.lib, "cwcvfs_pinned_stats", None)
        if pinned is not None:
            pinned.argtypes = [ctypes.c_void_p, *([ctypes.POINTER(ctypes.c_uint64)] * 2)]
            pinned.restype = None
            pages, calls = ctypes.c_uint64(), ctypes.c_uint64()
            pinned(self.pointer, ctypes.byref(pages), ctypes.byref(calls))
            result.update(pinned_pages=pages.value, pinned_reads=calls.value)
        return result
