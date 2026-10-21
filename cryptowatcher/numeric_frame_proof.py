"""Optional native, read-only owner proof for already authenticated BF1 rows.

The bridge borrows opaque EVP APIs from Python's loaded OpenSSL. It implements
no cryptographic algorithm and keeps no database pointer, statement or row cache.
"""

from __future__ import annotations

import _hashlib
import ctypes
import hashlib
import sqlite3
import struct
import sys
from pathlib import Path
from threading import RLock
from typing import Any

from . import numeric_frame_native

_LOCK = RLock()
_CRYPTO: Any = None
_NEW = ctypes.CFUNCTYPE(ctypes.c_void_p)
_FREE = ctypes.CFUNCTYPE(None, ctypes.c_void_p)
_MD = ctypes.CFUNCTYPE(ctypes.c_void_p)
_INIT = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
_UPDATE = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t)
_FINAL = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint))


class _Crypto(ctypes.Structure):
    _fields_ = [
        ("new", _NEW),
        ("free", _FREE),
        ("md", _MD),
        ("init", _INIT),
        ("update", _UPDATE),
        ("final", _FINAL),
    ]


def configure(library: Any) -> None:
    global _CRYPTO
    if _CRYPTO is None:
        # dlsym searches this already loaded module's dependencies on Linux.
        # Windows GetProcAddress requires its exact Python-bundled dependency.
        path = Path(_hashlib.__file__)
        if sys.platform == "win32":
            path = Path(sys.base_prefix) / "DLLs" / "libcrypto-3.dll"
        try:
            dependency = ctypes.CDLL(str(path))
            table = _Crypto(
                _NEW(("EVP_MD_CTX_new", dependency)),
                _FREE(("EVP_MD_CTX_free", dependency)),
                _MD(("EVP_sha256", dependency)),
                _INIT(("EVP_DigestInit_ex", dependency)),
                _UPDATE(("EVP_DigestUpdate", dependency)),
                _FINAL(("EVP_DigestFinal_ex", dependency)),
            )
        except (OSError, AttributeError) as error:
            raise ValueError("Missing Python OpenSSL digest APIs for installed numeric proof") from error
        _CRYPTO = dependency, table
    _, table = _CRYPTO
    library.cw_nf_crypto.argtypes = [ctypes.POINTER(_Crypto)]
    library.cw_nf_crypto.restype = ctypes.c_int
    library.cw_nf_digest.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
    library.cw_nf_digest.restype = ctypes.c_int
    if library.cw_nf_crypto(ctypes.byref(table)) != 1:
        raise ValueError("Invalid numeric proof digest API table")
    for value in (b"", bytes(range(256)), b"NS2\0" + bytes(range(256)) * 9):
        actual = ctypes.create_string_buffer(32)
        if (
            library.cw_nf_digest(value, len(value), actual, 32) != 1
            or actual.raw != hashlib.sha256(value).digest()
        ):
            raise ValueError("Numeric proof SHA256 known-answer mismatch")


def install(db: sqlite3.Connection) -> bool:
    library = numeric_frame_native.load()
    if library is None:
        return False
    with _LOCK:
        configure(library)
        # The trusted artifact is the only extension loaded, only on this
        # private connection. Restore the default immediately, even on failure.
        db.enable_load_extension(True)
        try:
            db.load_extension(str(numeric_frame_native.artifact().resolve()))
        finally:
            db.enable_load_extension(False)
    return True


def owners(db: sqlite3.Connection, identity: int, flat: bytes, mask: bytes, compressed: bool) -> int:
    return _owners(db, "SELECT cw_nf_proof(?,?,?,?)", (identity, flat, mask, int(compressed)), 64)


def owners_batch(db: sqlite3.Connection, rows: list[tuple[int, bytes, bytes]], compressed: bool) -> int:
    if not isinstance(rows, list) or not 1 <= len(rows) <= 8:
        raise ValueError("Native numeric owner batch cardinality")
    parts = [bytes([len(rows)])]
    for identity, flat, mask in rows:
        if (
            type(identity) is not int
            or not 0 < identity < 2**63
            or not isinstance(flat, bytes)
            or not 9 <= len(flat) <= numeric_frame_native.MAX_FLAT
            or not isinstance(mask, bytes)
            or len(mask) != 8
        ):
            raise ValueError("Native numeric owner batch bound")
        parts.extend((struct.pack(">Q", identity), mask, struct.pack(">I", len(flat)), flat))
    return _owners(db, "SELECT cw_nf_proof_batch(?,?)", (b"".join(parts), int(compressed)), len(rows) * 64)


def _owners(db: sqlite3.Connection, query: str, args: tuple[Any, ...], maximum: int) -> int:
    try:
        row = db.execute(query, args).fetchone()
    except sqlite3.OperationalError as error:
        if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_ERROR and str(error).startswith(
            "cw_nf:"
        ):
            raise ValueError(str(error)) from error
        raise
    if row is None or type(row[0]) is not int or not 0 <= row[0] <= maximum:
        raise ValueError("Invalid native numeric frame proof result")
    return row[0]
