"""Optional isolated-conversion accelerator; runtime radar never selects it."""

from __future__ import annotations

import ctypes
import json
import re
from pathlib import Path
from typing import Any, Callable

from .history_codec import MARKER
from .storage import StateError

KINDS = ("event", "outcome", "episode", "comparison")
_SaveCallback = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p
)


class _Part(ctypes.Structure):
    _fields_ = [("data", ctypes.c_void_p), ("length", ctypes.c_size_t)]


class _Result(ctypes.Structure):
    _fields_ = [
        ("status", ctypes.c_int),
        ("packed", ctypes.c_void_p),
        ("packed_length", ctypes.c_size_t),
        ("parts", ctypes.POINTER(_Part)),
        ("count", ctypes.c_size_t),
        ("capacity", ctypes.c_size_t),
    ]


class Accelerator:
    def __init__(self, path: Path) -> None:
        self.library = ctypes.CDLL(str(path))
        self.library.cw_extract.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
        self.library.cw_extract.restype = ctypes.POINTER(_Result)
        self.library.cw_release.argtypes = [ctypes.POINTER(_Result)]
        self.library.cw_release.restype = None
        self.library.cw_head.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint]
        self.library.cw_head.restype = ctypes.POINTER(_Result)
        self.saved_extractor = getattr(self.library, "cw_extract_saved", None)
        if self.saved_extractor is not None:
            self.saved_extractor.argtypes = [ctypes.c_char_p, ctypes.c_size_t, _SaveCallback, ctypes.c_void_p]
            self.saved_extractor.restype = ctypes.POINTER(_Result)
        # Stable CPython conversion APIs require the GIL throughout validation.
        self.python_library = ctypes.PyDLL(str(path))
        self.python_library.cw_validate.argtypes = [
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self.python_library.cw_validate.restype = ctypes.c_int

    def pack_serialized(self, payload: str, save: Callable[[bytes], str]) -> str | None:
        """Bind admitted keys in the original parse; caller owns rollback on fallback/refusal."""
        if self.saved_extractor is None:
            return None
        failures: list[BaseException] = []

        def admitted(data: int, size: int, output: int, context: int) -> int:
            try:
                key = save(ctypes.string_at(data, size))
                if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
                    raise StateError("Native reference binding refused input")
                ctypes.memmove(output, key.encode(), 64)
                return 1
            except BaseException as error:
                # ctypes callbacks cannot propagate Python exceptions through C.
                failures.append(error)
                return 0

        callback = _SaveCallback(admitted)
        raw = payload.encode()
        result = self.saved_extractor(raw, len(raw), callback, None)
        if not result:
            raise MemoryError("Native extraction allocation failed")
        try:
            if failures:
                raise failures[0]
            if result.contents.status == 1:
                return None
            if result.contents.status != 0:
                raise StateError("Native extraction refused input")
            return ctypes.string_at(result.contents.packed, result.contents.packed_length).decode()
        finally:
            self.library.cw_release(result)

    def pack(self, payload: str, save: Callable[[bytes], str]) -> dict[str, Any] | None:
        raw = payload.encode()
        result = self.library.cw_extract(raw, len(raw))
        if not result:
            raise MemoryError("Native extraction allocation failed")
        try:
            data = result.contents
            if data.status == 1:
                return None  # Existing format/depth/size continues through the full Python path.
            if data.status != 0:
                raise StateError("Native extraction refused input")
            keys = []
            for index in range(data.count):
                part = data.parts[index]
                keys.append(save(ctypes.string_at(part.data, part.length)))
            value = json.loads(ctypes.string_at(data.packed, data.packed_length))
            if not isinstance(value, dict):
                raise StateError("Native history must be an object")
            pending: list[Any] = [value]
            while pending:
                node = pending.pop()
                if isinstance(node, list):
                    pending.extend(v for v in node if isinstance(v, (dict, list)))
                elif isinstance(node, dict):
                    if MARKER in node:
                        for reference in node[MARKER]["fields"].values():
                            index = reference["sha256"]
                            if (
                                type(index) is not int
                                or reference["ids"] != [index]
                                or not 0 <= index < len(keys)
                            ):
                                raise StateError("Native reference mismatch")
                            reference["ids"] = [keys[index]]
                            reference["sha256"] = keys[index]
                    pending.extend(v for k, v in node.items() if k != MARKER and isinstance(v, (dict, list)))
            return value
        finally:
            self.library.cw_release(result)

    def head_metadata(self, payload: str, kind: str) -> dict[str, Any]:
        raw = payload.encode()
        result = self.library.cw_head(raw, len(raw), KINDS.index(kind))
        if not result:
            raise MemoryError("Native projection allocation failed")
        try:
            if result.contents.status:
                raise StateError("Native projection refused input")
            value = json.loads(ctypes.string_at(result.contents.packed, result.contents.packed_length))
            if not isinstance(value, dict):
                raise StateError("Invalid native projection")
            return value
        finally:
            self.library.cw_release(result)

    def canonical(self, raw: bytes) -> tuple[bool, int | None] | None:
        import sys

        count = ctypes.c_size_t()
        status = self.python_library.cw_validate(
            raw, len(raw), sys.get_int_max_str_digits(), ctypes.byref(count)
        )
        if status == 1:
            return None
        if status == 2:
            raise MemoryError("Native validation allocation failed")
        return status == 0, count.value if count.value != ctypes.c_size_t(-1).value else None


def load(backend: str) -> Accelerator | None:
    if backend not in ("auto", "python", "native"):
        raise StateError("Unknown history conversion backend")
    if backend == "python":
        return None
    path = Path(__file__).with_name("_native") / "history.so"
    if not path.is_file():
        if backend == "native":
            raise StateError("Required native converter artifact is unavailable")
        return None
    if path.is_symlink():
        raise StateError("Native converter artifact symlink refused")
    try:
        accelerator = Accelerator(path)
        from .events import HEAD_FIELDS

        for kind, fields in HEAD_FIELDS.items():
            value: dict[str, Any] = {field: None for field in fields}
            value["schema"] = "verify-field-map"
            if set(accelerator.head_metadata(json.dumps(value), kind)) != set(value):
                raise StateError("Native field map differs from current history contract")
        return accelerator
    except (OSError, AttributeError) as exc:
        raise StateError("Native converter artifact is incompatible") from exc
