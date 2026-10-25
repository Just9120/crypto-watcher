"""Optional pure frozen-format operations; installed incompatible code fails closed."""

from __future__ import annotations

import ctypes
import importlib.machinery
import importlib.util
import struct
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any


def artifact() -> Path:
    return Path(__file__).with_name("_native") / "frozen_codec.so"


@lru_cache(maxsize=1)
def load() -> Any:
    path = artifact()
    if path.is_symlink():
        raise ValueError("Invalid frozen operation artifact")
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError("Invalid frozen operation artifact")
    name = "cryptowatcher._native.frozen_codec"
    try:
        library = ctypes.CDLL(str(path.resolve()))
        build = library.cw_frozen_python_minor
        build.argtypes = []
        build.restype = ctypes.c_uint
        bits = library.cw_frozen_pointer_bits
        bits.argtypes = []
        bits.restype = ctypes.c_uint
        if (
            sys.implementation.name != "cpython"
            or build() != (sys.version_info.major << 8 | sys.version_info.minor)
            or bits() != ctypes.sizeof(ctypes.c_void_p) * 8
        ):
            raise ValueError("Frozen operation Python runtime mismatch")
        loader = importlib.machinery.ExtensionFileLoader(name, str(path.resolve()))
        spec = importlib.util.spec_from_file_location(name, path, loader=loader)
        if spec is None:
            raise ValueError("Invalid frozen operation module")
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        version = module.abi()
        if (
            type(version) is not int
            or version != 1
            or any(
                not callable(getattr(module, name, None))
                for name in (
                    "lease_counts",
                    "typed_pack",
                    "typed_unpack",
                    "source_values",
                    "source_stats",
                    "source_preserved",
                    "column_values",
                )
            )
        ):
            raise ValueError("Unknown frozen operation ABI")
        return module
    except (AttributeError, ImportError, OSError) as error:
        raise ValueError("Incompatible frozen operation artifact") from error


def available() -> bool:
    return load() is not None


def leases(raw: bytes) -> tuple[list[int], list[int]] | None:
    module = load()
    if module is None:
        return None
    value = module.lease_counts(raw)
    if value is NotImplemented:
        return None
    if (
        type(value) is not tuple
        or len(value) != 2
        or any(type(part) is not list or len(part) != 64 for part in value)
    ):
        raise ValueError("Invalid native lease result")
    return value


def typed_pack(value: Any, words: dict[str, int]) -> bytes | None:
    module = load()
    if module is None:
        return None
    result = module.typed_pack(value, words)
    if result is NotImplemented:
        return None
    if type(result) is not bytes:
        raise ValueError("Invalid native typed bytes")
    return result


def typed_unpack(raw: bytes, words: tuple[str, ...]) -> Any:
    module = load()
    return NotImplemented if module is None else module.typed_unpack(raw, words)


def _source_call(name: str, tile: int, step: int, *bodies: tuple[tuple[int, ...], bytes]) -> Any:
    module = load()
    if module is None:
        return NotImplemented
    from .source_guard_native import _coordinates

    _coordinates(tile, step)
    arguments: list[bytes] = []
    for offsets, raw in bodies:
        if (
            type(raw) is not bytes
            or not 0 < len(raw) <= 512 * 1024
            or type(offsets) is not tuple
            or len(offsets) != 33
            or offsets[0] != 0
            or offsets[-1] != len(raw)
            or any(type(n) is not int or not 0 <= n <= len(raw) for n in offsets)
            or any(a >= b for a, b in zip(offsets, offsets[1:]))
        ):
            raise ValueError("Invalid frozen source body bounds")
        arguments.extend((raw, struct.pack(">33I", *offsets)))
    return getattr(module, name)(tile, step, *arguments)


def source_values(tile: int, step: int, offsets: tuple[int, ...], raw: bytes) -> tuple[Any, ...] | None:
    result = _source_call("source_values", tile, step, (offsets, raw))
    if result is NotImplemented:
        return None
    if type(result) is not tuple or len(result) != 32:
        raise ValueError("Invalid frozen source tree")
    return result


def source_stats(tile: int, step: int, offsets: tuple[int, ...], raw: bytes) -> tuple[int, int] | None:
    result = _source_call("source_stats", tile, step, (offsets, raw))
    if result is NotImplemented:
        return None
    if type(result) is not tuple or len(result) != 2 or any(type(n) is not int for n in result):
        raise ValueError("Invalid frozen source statistics")
    return result


def source_preserved(
    tile: int, step: int, old: tuple[tuple[int, ...], bytes], current: tuple[tuple[int, ...], bytes]
) -> bool:
    result = _source_call("source_preserved", tile, step, old, current)
    if result is not NotImplemented and result is not True:
        raise ValueError("Invalid frozen source preservation")
    return result is True


def column_values(counts: list[int], kinds: list[str], values: list[Any], count: int) -> list[Any] | None:
    module = load()
    if module is None:
        return None
    result = module.column_values(counts, kinds, values, count)
    if result is NotImplemented:
        return None
    if type(result) is not list or len(result) != 32:
        raise ValueError("Invalid frozen column result")
    return result
