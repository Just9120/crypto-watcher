"""Lossless envelopes for old immutable JSON records; no market behavior changes."""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Any

import zstandard

from .storage import StateError, encode

MARKER = "_history_record_storage"
SCHEMA = "cryptowatcher.compressed-record.v1"
MAX_BYTES = 32 * 1024 * 1024
FIELDS = {"schema", "bytes", "sha256", "zstd"}


def pack(payload: str) -> str:
    """Preserve the exact original bytes; caller selects validated immutable records."""
    raw = payload.encode("utf-8")
    if not 0 < len(raw) <= MAX_BYTES:
        raise StateError("Compressed record size refused")
    encoded = zstandard.ZstdCompressor(level=1, write_checksum=True, threads=0).compress(raw)
    return encode(
        {
            MARKER: {
                "schema": SCHEMA,
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "zstd": base64.b64encode(encoded).decode("ascii"),
            }
        }
    )


def unpack_bytes(value: Any) -> bytes | None:
    """Check every encoded byte, bounds and identity before returning original JSON."""
    if not isinstance(value, dict) or MARKER not in value:
        return None
    descriptor = value[MARKER]
    if (
        set(value) != {MARKER}
        or not isinstance(descriptor, dict)
        or set(descriptor) != FIELDS
        or descriptor.get("schema") != SCHEMA
    ):
        raise StateError("Invalid compressed record schema")
    size, fingerprint, text = descriptor["bytes"], descriptor["sha256"], descriptor["zstd"]
    if (
        type(size) is not int
        or not 0 < size <= MAX_BYTES
        or not isinstance(fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
        or not isinstance(text, str)
        or len(text) > (MAX_BYTES + 65536) * 4 // 3 + 4
    ):
        raise StateError("Invalid compressed record bounds")
    try:
        encoded = base64.b64decode(text, validate=True)
        parameters = zstandard.get_frame_parameters(encoded)
        if (
            parameters.content_size != size
            or parameters.window_size > MAX_BYTES
            or parameters.dict_id
            or not parameters.has_checksum
        ):
            raise StateError("Invalid compressed record frame")
        raw = zstandard.ZstdDecompressor(max_window_size=MAX_BYTES // 1024).decompress(
            encoded, max_output_size=size, allow_extra_data=False
        )
    except (ValueError, zstandard.ZstdError) as error:
        raise StateError("Invalid compressed record encoding") from error
    if len(raw) != size or hashlib.sha256(raw).hexdigest() != fingerprint:
        raise StateError("Compressed record identity mismatch")
    return raw
