"""Lossless shared snapshots; SQL projections stay small and public records stay complete."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import zlib
from collections import OrderedDict
from typing import Any

from .storage import StateError, encode

MARKER = "_history_storage"
SCHEMA = "cryptowatcher.shared-fields.v1"
BLOB_SCHEMA = "cryptowatcher.shared-json.v1"
BLOB_PREFIX = "historyblob:v1:"
MAX_BYTES = 32 * 1024 * 1024
CACHE_BYTES = 4 * 1024 * 1024
SHARED_FIELDS = {"settings_snapshot", "effective_parameters", "source_context", "enrichment"}
WINDOW_FIELDS = {"source_inputs", "future_inputs"}


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sql_value(value: Any, kind: str) -> Any:
    """JSON1 returns object/array text and integer representations of booleans."""
    if kind in ("object", "array"):
        return json.loads(value)
    if kind in ("true", "false"):
        return kind == "true"
    return value


class Codec:
    def __init__(self, database: sqlite3.Connection):
        self.db = database
        self.encoded: OrderedDict[bytes, tuple[str, str]] = OrderedDict()
        self.cache_bytes = 0
        self.validated: OrderedDict[str, tuple[str, bytes, int | None]] = OrderedDict()
        self.validated_bytes = 0

    def read(self, payload: str) -> Any:
        value = json.loads(payload)
        return self.unpack(value) if MARKER in payload else value

    def save(self, value: Any) -> str:
        return self.save_bytes(encode(value).encode())

    def save_bytes(self, raw: bytes) -> str:
        if len(raw) > MAX_BYTES:
            raise StateError("Shared snapshot exceeds decoder bound")
        cached = self.encoded.get(raw)
        if cached is not None:
            self.encoded.move_to_end(raw)
            key, payload = cached
        else:
            key = digest(raw)
            payload = encode(
                {
                    "schema": BLOB_SCHEMA,
                    "bytes": len(raw),
                    "zlib": base64.b64encode(zlib.compress(raw, 3)).decode(),
                }
            )
            charge = len(raw) + len(payload) + 256
            if charge <= CACHE_BYTES:
                while self.encoded and self.cache_bytes + charge > CACHE_BYTES:
                    old, (_, body) = self.encoded.popitem(last=False)
                    self.cache_bytes -= len(old) + len(body) + 256
                self.encoded[raw] = (key, payload)
                self.cache_bytes += charge
        # Cached bytes do not replace admission or corruption checks after rollback.
        result = self.db.execute("INSERT OR IGNORE INTO runtime VALUES (?,?)", (BLOB_PREFIX + key, payload))
        if not result.rowcount:
            existing = self.db.execute(
                "SELECT payload_json FROM runtime WHERE key=?", (BLOB_PREFIX + key,)
            ).fetchone()
            if existing is None or existing[0] != payload and encode(self.load(key)) != raw.decode():
                raise StateError("Conflicting shared snapshot")
        return key

    def load(self, key: str) -> Any:
        return json.loads(self.load_raw(key)[0])

    def load_raw(self, key: str) -> tuple[bytes, int | None]:
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
            raise StateError("Invalid shared snapshot identity")
        row = self.db.execute("SELECT payload_json FROM runtime WHERE key=?", (BLOB_PREFIX + key,)).fetchone()
        cached = self.validated.get(key)
        if row and cached is not None and row[0] == cached[0]:
            self.validated.move_to_end(key)
            # Return a new object: callers must not be able to mutate cached evidence.
            return cached[1], cached[2]
        try:
            value = json.loads(row[0]) if row else None
        except (ValueError, TypeError) as exc:
            raise StateError("Invalid shared snapshot encoding") from exc
        if not isinstance(value, dict) or value.get("schema") != BLOB_SCHEMA:
            raise StateError("Missing shared snapshot")
        size = value.get("bytes")
        if type(size) is not int or not 0 <= size <= MAX_BYTES:
            raise StateError("Invalid shared snapshot size")
        try:
            if (
                not isinstance(value.get("zlib"), str)
                or len(value["zlib"]) > (MAX_BYTES + 65536) * 4 // 3 + 4
            ):
                raise StateError("Invalid shared snapshot encoding")
            encoded = base64.b64decode(value["zlib"], validate=True)
            reader = zlib.decompressobj()
            raw = reader.decompress(encoded, size + 1)
            if len(raw) != size or not reader.eof or reader.unused_data or reader.unconsumed_tail:
                raise StateError("Invalid shared snapshot encoding")
            if digest(raw) != key:
                raise StateError("Shared snapshot hash mismatch")
            decoded = json.loads(raw)
            if encode(decoded).encode() != raw:
                raise StateError("Noncanonical shared snapshot")
            charge = len(raw) + len(row[0]) + 256
            if charge <= CACHE_BYTES:
                if cached is not None:
                    self.validated_bytes -= len(cached[0]) + len(cached[1]) + 256
                    del self.validated[key]
                while self.validated and self.validated_bytes + charge > CACHE_BYTES:
                    _, (body, previous, _) = self.validated.popitem(last=False)
                    self.validated_bytes -= len(body) + len(previous) + 256
                self.validated[key] = (row[0], raw, len(decoded) if isinstance(decoded, list) else None)
                self.validated_bytes += charge
            return raw, len(decoded) if isinstance(decoded, list) else None
        except (KeyError, ValueError, TypeError, zlib.error, UnicodeError) as exc:
            raise StateError("Invalid shared snapshot encoding") from exc

    def field(self, reference: Any) -> Any:
        return json.loads(self.field_raw(reference))

    def field_raw(self, reference: Any) -> bytes:
        if not isinstance(reference, dict) or set(reference) != {"mode", "ids", "sha256"}:
            raise StateError("Invalid shared field reference")
        keys = reference["ids"]
        if not isinstance(keys, list) or not 0 < len(keys) <= 4096:
            raise StateError("Invalid shared field parts")
        if reference["mode"] == "value" and len(keys) == 1:
            raw, _ = self.load_raw(keys[0])
        elif reference["mode"] == "window":
            parts = []
            for key in keys:
                part, count = self.load_raw(key)
                if count is None or not 0 < count <= 16:
                    raise StateError("Invalid source window block")
                parts.append(part[1:-1])
            raw = b"[" + b",".join(parts) + b"]"
        else:
            raise StateError("Unknown shared field mode")
        if digest(raw) != reference["sha256"]:
            raise StateError("Shared field hash mismatch")
        return raw

    def pack_canonical(self, payload: str) -> Any:
        """Pack producer-canonical legacy JSON without materializing large windows."""
        if MARKER in payload:
            return self.pack(self.read(payload))
        result: dict[str, Any] = {}
        references = {}
        for name, item, kind in self.db.execute("SELECT key,value,type FROM json_each(?)", (payload,)):
            if not isinstance(name, str):
                raise StateError("Public history must be an object")
            if kind == "integer" and type(item) is not int:
                # JSON1 exposes integers outside signed64 as floats. Preserve
                # Python's exact integers instead of rounding provenance.
                return self.pack(json.loads(payload))
            if name in SHARED_FIELDS and kind == "object" and len(item.encode()) > 256:
                key = self.save_bytes(item.encode())
                references[name] = {"mode": "value", "ids": [key], "sha256": key}
                if name == "settings_snapshot":
                    settings = json.loads(item)
                    result[name] = {
                        k: settings[k] for k in ("profile", "alert_universe_mode") if k in settings
                    }
            elif name in WINDOW_FIELDS and kind == "array" and item != "[]":
                parts: list[list[bytes]] = []
                bucket: Any = None
                candles = self.db.execute(
                    "SELECT value,type,CASE WHEN type='object' THEN json_extract(value,'$.start') END,"
                    "CASE WHEN type='object' THEN json_extract(value,'$.step') END FROM json_each(?)",
                    (item,),
                )
                for index, (candle, candle_kind, start, step) in enumerate(candles):
                    if candle_kind == "integer" and type(candle) is not int:
                        return self.pack(json.loads(payload))
                    current = (
                        (step, start // (step * 16))
                        if type(start) is int and type(step) is int and step > 0
                        else (None, index // 16)
                    )
                    if not parts or current != bucket or len(parts[-1]) == 16:
                        parts.append([])
                        bucket = current
                    raw = (
                        candle.encode()
                        if candle_kind in ("object", "array")
                        else encode(sql_value(candle, candle_kind)).encode()
                    )
                    parts[-1].append(raw)
                blocks = [b"[" + b",".join(part) + b"]" for part in parts]
                raw = b"[" + b",".join(block[1:-1] for block in blocks) + b"]"
                references[name] = {
                    "mode": "window",
                    "ids": [self.save_bytes(block) for block in blocks],
                    "sha256": digest(raw),
                }
            else:
                value = sql_value(item, kind)
                result[name] = self.pack(value) if isinstance(value, (dict, list)) else value
        if references:
            result[MARKER] = {"schema": SCHEMA, "fields": references}
        return result

    def canonical_json(self, value: Any) -> bytes:
        """Restore complete canonical public bytes, checking every shared reference."""
        nonce = os.urandom(12).hex()
        replacements: list[tuple[bytes, bytes]] = []

        def prepare(item: Any) -> Any:
            if isinstance(item, list):
                return [prepare(part) for part in item]
            if not isinstance(item, dict):
                return item
            fields = {}
            if MARKER in item:
                storage = item[MARKER]
                if (
                    not isinstance(storage, dict)
                    or set(storage) != {"schema", "fields"}
                    or storage["schema"] != SCHEMA
                    or not isinstance(storage["fields"], dict)
                ):
                    raise StateError("Invalid history storage schema")
                fields = storage["fields"]
            result = {k: prepare(v) for k, v in item.items() if k != MARKER and k not in fields}
            for name, reference in fields.items():
                if name not in SHARED_FIELDS | WINDOW_FIELDS:
                    raise StateError("Unknown shared history field")
                raw = self.field_raw(reference)
                if name in item:
                    restored = json.loads(raw)
                    projection = (
                        {k: restored[k] for k in ("profile", "alert_universe_mode") if k in restored}
                        if name == "settings_snapshot" and isinstance(restored, dict)
                        else None
                    )
                    if encode(item[name]) != encode(projection):
                        raise StateError("Conflicting shared field projection")
                token = f"\x00CWJSON:{nonce}:{len(replacements)}\x00"
                replacements.append((encode(token).encode(), raw))
                result[name] = token
            return result

        encoded = encode(prepare(value)).encode()
        # Tokens occupy complete JSON string values. Verify uniqueness in metadata
        # and restored bytes before splicing. The nonce is temporary only; complete
        # public output is deterministic and contains no temporary tokens.
        positions = []
        for token, raw in replacements:
            if encoded.count(token) != 1 or any(token in body for _, body in replacements):
                raise StateError("Ambiguous temporary JSON token")
            start = encoded.index(token)
            positions.append((start, start + len(token), raw))
        fragments: list[bytes] = []
        previous = 0
        for start, end, raw in sorted(positions):
            fragments.extend((encoded[previous:start], raw))
            previous = end
        fragments.append(encoded[previous:])
        return b"".join(fragments)

    def pack(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self.pack(item) for item in value]
        if not isinstance(value, dict):
            return value
        if MARKER in value:
            raise StateError("Reserved history storage marker")
        result: dict[str, Any] = {}
        references = {}
        for name, item in value.items():
            raw = encode(item).encode() if name in SHARED_FIELDS and isinstance(item, dict) else None
            if raw is not None and len(raw) > 256:
                key = self.save_bytes(raw)
                references[name] = {
                    "mode": "value",
                    "ids": [key],
                    "sha256": key,
                }
                if name == "settings_snapshot":
                    result[name] = {k: item[k] for k in ("profile", "alert_universe_mode") if k in item}
            elif name in WINDOW_FIELDS and isinstance(item, list) and item:
                parts: list[list[Any]] = []
                bucket: Any = None
                for index, candle in enumerate(item):
                    start = candle.get("start") if isinstance(candle, dict) else None
                    step = candle.get("step") if isinstance(candle, dict) else None
                    current = (
                        (step, start // (step * 16))
                        if type(start) is int and type(step) is int and step > 0
                        else (None, index // 16)
                    )
                    if not parts or current != bucket or len(parts[-1]) == 16:
                        parts.append([])
                        bucket = current
                    parts[-1].append(candle)
                encoded_parts = [encode(part).encode() for part in parts]
                # Canonical list bytes are the concatenation of its canonical blocks.
                # This avoids serializing every candle again just to hash the window.
                window = b"[" + b",".join(part[1:-1] for part in encoded_parts) + b"]"
                references[name] = {
                    "mode": "window",
                    "ids": [self.save_bytes(part) for part in encoded_parts],
                    "sha256": digest(window),
                }
            else:
                result[name] = self.pack(item) if isinstance(item, (dict, list)) else item
        if references:
            result[MARKER] = {"schema": SCHEMA, "fields": references}
        return result

    def unpack(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self.unpack(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {
            k: self.unpack(v) if isinstance(v, (dict, list)) else v for k, v in value.items() if k != MARKER
        }
        if MARKER not in value:
            return result
        storage = value[MARKER]
        if (
            not isinstance(storage, dict)
            or set(storage) != {"schema", "fields"}
            or storage["schema"] != SCHEMA
            or not isinstance(storage["fields"], dict)
        ):
            raise StateError("Invalid history storage schema")
        for name, reference in storage["fields"].items():
            if name not in SHARED_FIELDS | WINDOW_FIELDS:
                raise StateError("Unknown shared history field")
            restored = self.field(reference)
            if name in result:
                projection = (
                    {k: restored[k] for k in ("profile", "alert_universe_mode") if k in restored}
                    if name == "settings_snapshot" and isinstance(restored, dict)
                    else None
                )
                if result[name] != projection:
                    raise StateError("Conflicting shared field projection")
            result[name] = restored
        return result
