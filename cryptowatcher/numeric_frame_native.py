"""Optional bounded byte copy and read-only NF proof artifact.

Absence selects the portable decoder. An installed but invalid artifact fails
closed. Byte transposition has no allocation or I/O. The optional SQL proof
uses current rows and Python's OpenSSL; it retains no persistent state.
"""

from __future__ import annotations

import ctypes
from functools import lru_cache
from pathlib import Path
from typing import Any

MAX_SLOTS = 4096
MAX_BYTES = MAX_SLOTS * 34
MAX_BODY = 5 + 64 * (73 + 64 * 34)
MAX_FLAT = 1 + 65 * 4 + 64 * (73 + 64 * 34)


def artifact() -> Path:
    return Path(__file__).with_name("_native") / "numeric_frame.so"


@lru_cache(maxsize=1)
def load() -> Any:
    path = artifact()
    if not path.is_file():
        return None
    try:
        library = ctypes.CDLL(str(path))
        library.cw_nf_abi.argtypes = []
        library.cw_nf_abi.restype = ctypes.c_uint32
        if library.cw_nf_abi() != 3:
            raise ValueError("Unknown numeric decoder ABI")
        for name in ("cw_nf_crypto", "cw_nf_digest", "sqlite3_extension_init"):
            getattr(library, name)
        library.cw_nf_untranspose.argtypes = [
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        library.cw_nf_untranspose.restype = ctypes.c_int
        library.cw_nf_flatten.argtypes = [
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        library.cw_nf_flatten.restype = ctypes.c_int
        return library
    except (OSError, AttributeError) as error:
        raise ValueError("Invalid installed numeric decoder artifact") from error


def available() -> bool:
    return load() is not None


def flatten(body: bytes) -> bytes | None:
    if not isinstance(body, bytes) or not 5 <= len(body) <= MAX_BODY:
        raise ValueError("Numeric native frame allocation bound")
    library = load()
    if library is None:
        return None
    output = ctypes.create_string_buffer(MAX_FLAT)
    size = ctypes.c_size_t()
    result = library.cw_nf_flatten(body, len(body), output, MAX_FLAT, ctypes.byref(size))
    if result != 1:
        if result == -4:
            raise ValueError("Invalid numeric frame metric lengths")
        raise ValueError("Invalid numeric native frame shape/identity/bound")
    if not 9 <= size.value <= MAX_FLAT:
        raise ValueError("Invalid numeric native frame result bound")
    return output.raw[: size.value]


def transpose(lengths: bytes, planes: bytes) -> bytes | None:
    if (
        not isinstance(lengths, bytes)
        or not 0 < len(lengths) <= MAX_SLOTS
        or not isinstance(planes, bytes)
        or not 0 < len(planes) <= MAX_BYTES
    ):
        raise ValueError("Numeric native transpose allocation bound")
    library = load()
    if library is None:
        return None
    output = ctypes.create_string_buffer(len(planes))
    if library.cw_nf_untranspose(lengths, len(lengths), planes, len(planes), output, len(planes)) != 1:
        raise ValueError("Numeric native transpose input bound")
    return bytes(output.raw)
