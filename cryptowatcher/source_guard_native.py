"""Optional stateless SB2 admission checks; portable guards own every fallback.

Headers, hashes, decompression and deletion authority stay in the existing
Python/SQLite boundaries. An installed incompatible artifact fails closed.
"""

from __future__ import annotations

import ctypes
import struct
from functools import lru_cache
from pathlib import Path
from typing import Any


def artifact() -> Path:
    return Path(__file__).with_name("_native") / "source_guard.so"


@lru_cache(maxsize=1)
def load() -> Any:
    path = artifact()
    if path.is_symlink():
        raise ValueError("Invalid native source guard artifact")
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError("Invalid native source guard artifact")
    try:
        library = ctypes.PyDLL(str(path.resolve()))
        library.cw_source_guard_abi.argtypes = []
        library.cw_source_guard_abi.restype = ctypes.c_uint
        if library.cw_source_guard_abi() != 1:
            raise ValueError("Unknown native source guard ABI")
        data: list[Any] = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t]
        coordinates: list[Any] = [ctypes.c_uint64, ctypes.c_uint64]
        library.cw_source_guard_stats.argtypes = [*data, *coordinates, ctypes.POINTER(ctypes.c_uint64)]
        library.cw_source_guard_stats.restype = ctypes.c_int
        library.cw_source_guard_preserved.argtypes = [*data, *data, *coordinates]
        library.cw_source_guard_preserved.restype = ctypes.c_int
        return library
    except (AttributeError, OSError) as error:
        raise ValueError("Incompatible native source guard artifact") from error


def available() -> bool:
    return load() is not None


def _body(offsets: tuple[int, ...], raw: bytes) -> tuple[bytes, int, bytes, int]:
    if (
        not isinstance(raw, bytes)
        or not 0 < len(raw) <= 512 * 1024
        or not isinstance(offsets, tuple)
        or len(offsets) != 33
        or any(type(v) is not int or not 0 <= v <= len(raw) for v in offsets)
        or offsets[0] != 0
        or offsets[-1] != len(raw)
        or any(a >= b for a, b in zip(offsets, offsets[1:]))
    ):
        raise ValueError("Invalid native source body bounds")
    index = struct.pack(">33I", *offsets)
    return raw, len(raw), index, len(index)


def _coordinates(tile: int, step: int) -> None:
    if (
        type(tile) is not int
        or not 0 <= tile < 2**63
        or type(step) is not int
        or step
        not in (
            60000,
            180000,
            300000,
            900000,
        )
    ):
        raise ValueError("Invalid native source coordinates")


def _status(value: int) -> bool:
    if value == 2:
        raise MemoryError("Native source guard allocation failed")
    if value not in (0, 1):
        raise ValueError("Invalid native source guard result")
    return value == 0


def stats(tile: int, step: int, offsets: tuple[int, ...], raw: bytes) -> tuple[int, int] | None:
    library = load()
    if library is None:
        return None
    _coordinates(tile, step)
    result = (ctypes.c_uint64 * 2)()
    if not _status(library.cw_source_guard_stats(*_body(offsets, raw), tile, step, result)):
        return None
    if not 0 < result[0] <= 256 or not 0 <= result[1] < 2**63:
        raise ValueError("Invalid native source statistics")
    return result[0], result[1]


def preserved(
    tile: int,
    step: int,
    old: tuple[tuple[int, ...], bytes],
    current: tuple[tuple[int, ...], bytes],
) -> bool:
    library = load()
    if library is None:
        return False
    _coordinates(tile, step)
    return _status(library.cw_source_guard_preserved(*_body(*old), *_body(*current), tile, step))
