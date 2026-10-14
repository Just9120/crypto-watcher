"""Independent exact-byte recovery verification for a private compressed snapshot."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import zstandard

from .compressed_validation import BLOCK, Image, budget
from .storage import StateError, encode

SCHEMA = "cryptowatcher.indexed-snapshot.v1"
ENTRY = struct.Struct("<QQ")


def save_index(image: Image, directory: Path, *, deadline: float) -> Path:
    budget(deadline)
    if image.mutable or image.blocks.backing is None:
        raise StateError("Frozen disk snapshot required")
    image.blocks.seal_private()
    index = directory / "source.index"
    descriptor = directory / "source.recovery.json"
    if index.exists() or descriptor.exists():
        raise StateError("Exclusive recovery metadata required")
    digest = hashlib.sha256()
    with index.open("xb") as output:
        os.chmod(index, 0o600)
        for offset, length in zip(image.blocks.offsets, image.blocks.lengths):
            budget(deadline)
            raw = ENTRY.pack(offset, length)
            output.write(raw)
            digest.update(raw)
        output.flush()
        os.fsync(output.fileno())
    encoded = hashlib.sha256()
    with image.blocks.backing.open("rb") as data:
        for chunk in iter(lambda: data.read(1024 * 1024), b""):
            budget(deadline)
            encoded.update(chunk)
    record = {
        "schema": SCHEMA,
        "data": image.blocks.backing.name,
        "index": index.name,
        "blocks": len(image.blocks),
        "block_bytes": BLOCK,
        "raw_bytes": image.size,
        "extent": image.blocks.end,
        "data_bytes": image.blocks.backing.stat().st_size,
        "data_sha256": encoded.hexdigest(),
        "raw_sha256": image.reconstructed_hash(),
        "index_sha256": digest.hexdigest(),
        "source_full_integrity": "CALLER_REQUIRED",
    }
    with descriptor.open("x", encoding="utf-8") as output:
        os.chmod(descriptor, 0o600)
        output.write(encode(record))
        output.flush()
        os.fsync(output.fileno())
    return descriptor


def manifest(descriptor: Path) -> dict[str, Any]:
    if (
        any(p.is_symlink() for p in (descriptor, *descriptor.parents))
        or descriptor.resolve() != descriptor
        or not descriptor.is_file()
        or descriptor.stat().st_size > 65536
    ):
        raise StateError("Recovery path refused")
    try:
        record = json.loads(descriptor.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as error:
        raise StateError("Recovery schema refused") from error
    return record


def decoded_blocks(descriptor: Path, *, deadline: float) -> Iterator[bytes]:
    budget(deadline)
    record = manifest(descriptor)
    descriptor_bytes = descriptor.read_bytes()
    fields = {
        "schema",
        "data",
        "index",
        "blocks",
        "block_bytes",
        "raw_bytes",
        "extent",
        "data_bytes",
        "data_sha256",
        "raw_sha256",
        "index_sha256",
        "source_full_integrity",
    }
    if not isinstance(record, dict) or set(record) != fields or record["schema"] != SCHEMA:
        raise StateError("Recovery schema refused")
    size, count, extent = record["raw_bytes"], record["blocks"], record["extent"]
    if (
        type(size) is not int
        or size < 0
        or type(count) is not int
        or count != (size + BLOCK - 1) // BLOCK
        or type(extent) is not int
        or extent < 0
        or type(record["data_bytes"]) is not int
        or record["data_bytes"] < 0
        or type(record["block_bytes"]) is not int
        or record["block_bytes"] != BLOCK
        or record["source_full_integrity"] != "CALLER_REQUIRED"
        or any(
            not isinstance(record[k], str) or re.fullmatch(r"[a-f0-9]{64}", record[k]) is None
            for k in ("data_sha256", "index_sha256", "raw_sha256")
        )
    ):
        raise StateError("Recovery bounds refused")
    paths = []
    for field in ("data", "index"):
        name = record[field]
        if not isinstance(name, str) or not name or name in (".", "..") or Path(name).name != name:
            raise StateError("Recovery filename refused")
        path = descriptor.parent / name
        if path.is_symlink() or path.resolve() != path or not path.is_file():
            raise StateError("Recovery path refused")
        paths.append(path)
    data_path, index_path = paths

    def identity(path: Path) -> tuple[int, int, int, int]:
        value = path.stat()
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns

    originals = [identity(path) for path in paths]
    if (
        data_path.stat().st_size != record["data_bytes"]
        or record["data_bytes"] < extent
        or index_path.stat().st_size != count * ENTRY.size
    ):
        raise StateError("Recovery size mismatch")
    encoded_digest = hashlib.sha256()
    with data_path.open("rb") as data:
        for chunk in iter(lambda: data.read(1024 * 1024), b""):
            budget(deadline)
            encoded_digest.update(chunk)
    if encoded_digest.hexdigest() != record["data_sha256"]:
        raise StateError("Recovery encoded identity mismatch")
    digest = hashlib.sha256()
    # The library's configurable window floor is 1 MiB; frame admission below
    # separately enforces the stricter 64 KiB snapshot-block bound.
    decoder = zstandard.ZstdDecompressor(max_window_size=1024)
    with data_path.open("rb") as data, index_path.open("rb") as index:
        for block in range(count):
            budget(deadline)
            entry = index.read(ENTRY.size)
            digest.update(entry)
            offset, length = ENTRY.unpack(entry)
            if length <= 0 or length > BLOCK + 65536 or offset + length > extent:
                raise StateError("Recovery frame bounds refused")
            data.seek(offset)
            encoded = data.read(length)
            try:
                parameters = zstandard.get_frame_parameters(encoded)
                expected = min(BLOCK, size - block * BLOCK)
                if (
                    parameters.content_size not in (expected, BLOCK)
                    or parameters.window_size > BLOCK
                    or parameters.dict_id
                    or not parameters.has_checksum
                ):
                    raise StateError("Recovery frame refused")
                raw = decoder.decompress(encoded, max_output_size=BLOCK, allow_extra_data=False)
            except zstandard.ZstdError as error:
                raise StateError("Recovery frame corrupted") from error
            if len(raw) not in (expected, BLOCK):
                raise StateError("Recovery decoded size mismatch")
            yield raw[:expected]
    if digest.hexdigest() != record["index_sha256"]:
        raise StateError("Recovery index identity mismatch")
    if descriptor.read_bytes() != descriptor_bytes or [identity(path) for path in paths] != originals:
        raise StateError("Recovery files changed")


def verify(descriptor: Path, *, deadline: float) -> dict[str, Any]:
    original = descriptor.read_bytes()
    digest = hashlib.sha256()
    size = 0
    for block in decoded_blocks(descriptor, deadline=deadline):
        digest.update(block)
        size += len(block)
    record = manifest(descriptor)
    if descriptor.read_bytes() != original:
        raise StateError("Recovery files changed")
    if size != record["raw_bytes"] or digest.hexdigest() != record["raw_sha256"]:
        raise StateError("Recovery byte identity mismatch")
    budget(deadline)
    return {
        "state": "RECOVERY_BYTES_VERIFIED",
        "raw_bytes": size,
        "raw_sha256": digest.hexdigest(),
        "source_full_integrity": "CALLER_REQUIRED",
    }


def restore(descriptor: Path, destination: Path, *, maximum: int, deadline: float) -> dict[str, Any]:
    """Recover every raw byte to an exclusive private file, without overwriting state."""
    budget(deadline)
    if (
        type(maximum) is not int
        or maximum <= 0
        or destination.resolve() != destination
        or any(p.is_symlink() for p in (destination, *destination.parents))
        or destination.exists()
    ):
        raise StateError("Exclusive recovery destination required")
    record = manifest(descriptor)
    original = descriptor.read_bytes()
    if type(record.get("raw_bytes")) is not int or not 0 <= record["raw_bytes"] <= maximum:
        raise StateError("Recovery capacity refused")
    if shutil.disk_usage(destination.parent).free < record["raw_bytes"] + 1536 * 1024**2:
        raise StateError("Recovery disk reserve exhausted")
    total = 0
    digest = hashlib.sha256()
    with destination.open("xb") as output:
        os.chmod(destination, 0o600)
        for raw in decoded_blocks(descriptor, deadline=deadline):
            budget(deadline)
            total += len(raw)
            if total > maximum:
                raise StateError("Recovery capacity refused")
            output.write(raw)
            digest.update(raw)
        output.flush()
        os.fsync(output.fileno())
    if (
        descriptor.read_bytes() != original
        or total != record["raw_bytes"]
        or digest.hexdigest() != record["raw_sha256"]
    ):
        raise StateError("Recovery byte identity mismatch")
    budget(deadline)
    return {
        "state": "RECOVERY_FILE_VERIFIED",
        "raw_bytes": total,
        "raw_sha256": digest.hexdigest(),
        "source_full_integrity": "CALLER_REQUIRED",
    }
