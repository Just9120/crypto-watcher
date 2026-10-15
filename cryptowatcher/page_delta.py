"""Bounded physical transport of a closed SQLite snapshot, never a live DB writer.

The operator must fence the writer before capture. Captured bytes still require
full SQLite/history validation; checksums alone do not qualify a migration.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import stat
import struct
import time
from pathlib import Path
from typing import Any, Callable

import zstandard

from .compressed_validation import BLOCK, budget
from .disk_backup import DISK_RESERVE, ArchiveSink, private_disk
from .lease import RuntimeLease
from .storage import StateError, encode
from .validation import fingerprint

PACKET = struct.Struct("<QII")


def _deadline(seconds: float) -> float:
    if not math.isfinite(seconds) or not 0 < seconds <= 900:
        raise StateError("Invalid page transport deadline")
    return time.monotonic() + seconds


def _identity(path: Path) -> tuple[int, int, int, int]:
    if path.resolve() != path or any(p.is_symlink() for p in (path, *path.parents)):
        raise StateError("Page transport path refused")
    value = path.stat()
    if not stat.S_ISREG(value.st_mode):
        raise StateError("Regular page transport file required")
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _json(path: Path, value: dict[str, Any]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(encode(value))
        output.flush()
        os.fsync(output.fileno())
    if os.name == "posix":
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def make_index(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str,
    seconds: float = 900,
    progress: Callable[..., Any] = lambda **kw: None,
) -> dict[str, Any]:
    deadline = _deadline(seconds)
    before = _identity(source)
    destination.mkdir(mode=0o700)
    private_disk(destination)
    raw, encoded = hashlib.sha256(), hashlib.sha256()
    count = size = 0
    with source.open("rb") as incoming, (destination / "base.index").open("xb") as output:
        for block in iter(lambda: incoming.read(BLOCK), b""):
            budget(deadline)
            digest = hashlib.sha256(block).digest()
            output.write(digest)
            encoded.update(digest)
            raw.update(block)
            size += len(block)
            count += 1
            progress(phase="INDEX_BASE", bytes=size)
        output.flush()
        os.fsync(output.fileno())
    if raw.hexdigest() != expected_sha256 or _identity(source) != before:
        raise StateError("Page baseline changed or fingerprint refused")
    record = {
        "schema": "cryptowatcher.page-base.v1",
        "raw_sha256": raw.hexdigest(),
        "raw_bytes": size,
        "block_bytes": BLOCK,
        "blocks": count,
        "index": "base.index",
        "index_sha256": encoded.hexdigest(),
    }
    _json(destination / "base.json", record)
    budget(deadline)
    return record


def capture_closed(
    source: Path,
    baseline: Path,
    destination: Path,
    *,
    expected_baseline_sha256: str,
    max_archive_bytes: int,
    seconds: float = 900,
    progress: Callable[..., Any] = lambda **kw: None,
) -> dict[str, Any]:
    """Sequential file reads avoid decoding or walking millions of SQL rows."""
    deadline = _deadline(seconds)
    before = _identity(source)
    if any(Path(str(source) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise StateError("Fenced closed source required")
    if type(max_archive_bytes) is not int or max_archive_bytes <= 0:
        raise StateError("Explicit page transport capacity required")
    _identity(baseline)
    if fingerprint(baseline, deadline)[0] != expected_baseline_sha256:
        raise StateError("Page baseline descriptor identity refused")
    record = json.loads(baseline.read_text(encoding="utf-8"))
    if (
        record.get("schema") != "cryptowatcher.page-base.v1"
        or record.get("block_bytes") != BLOCK
        or record.get("index") != "base.index"
        or type(record.get("raw_bytes")) is not int
        or type(record.get("blocks")) is not int
        or record["blocks"] != (record["raw_bytes"] + BLOCK - 1) // BLOCK
        or not 0 <= record["blocks"] * 32 <= 64 * 1024**2
    ):
        raise StateError("Page baseline bounds refused")
    index = baseline.parent / "base.index"
    _identity(index)
    hashes = index.read_bytes()
    if len(hashes) != record["blocks"] * 32 or hashlib.sha256(hashes).hexdigest() != record["index_sha256"]:
        raise StateError("Page baseline index identity refused")
    destination.mkdir(mode=0o700)
    private_disk(destination)
    raw = hashlib.sha256()
    changed = size = block_number = 0
    compressor = zstandard.ZstdCompressor(level=1, write_checksum=True, threads=0)
    with source.open("rb") as incoming, (destination / "pages.delta").open("xb") as output:
        sink = ArchiveSink(output, destination, deadline)
        for block in iter(lambda: incoming.read(BLOCK), b""):
            budget(deadline)
            if block_number % 1024 == 0 and shutil.disk_usage(destination).free < DISK_RESERVE + 64 * 1024**2:
                raise StateError("Page transport disk reserve exhausted")
            raw.update(block)
            start = block_number * 32
            if hashlib.sha256(block).digest() != hashes[start : start + 32]:
                encoded = compressor.compress(block)
                packet = PACKET.pack(block_number, len(block), len(encoded)) + encoded
                if sink.size + len(packet) > max_archive_bytes:
                    raise StateError("Page transport capacity exhausted")
                sink.write(packet)
                changed += 1
            block_number += 1
            size += len(block)
            progress(
                phase="CAPTURE_CLOSED_PAGES", bytes=size, changed_blocks=changed, encoded_bytes=sink.size
            )
        output.flush()
        os.fsync(output.fileno())
    if _identity(source) != before or any(
        Path(str(source) + s).exists() for s in ("-wal", "-shm", "-journal")
    ):
        raise StateError("Closed page source changed")
    result = {
        "schema": "cryptowatcher.page-delta.v1",
        "state": "CAPTURED_BYTES_NOT_QUALIFIED",
        "raw_sha256": raw.hexdigest(),
        "raw_bytes": size,
        "base_raw_sha256": record["raw_sha256"],
        "base_raw_bytes": record["raw_bytes"],
        "baseline_descriptor_sha256": expected_baseline_sha256,
        "data": "pages.delta",
        "data_sha256": sink.digest.hexdigest(),
        "data_bytes": sink.size,
        "blocks": block_number,
        "changed_blocks": changed,
        "block_bytes": BLOCK,
        "full_source_integrity": "PENDING",
        "production_replacement": False,
    }
    _json(destination / "delta.json", result)
    budget(deadline)
    return result


def apply_to_working_copy(
    work: Path,
    descriptor: Path,
    *,
    expected_descriptor_sha256: str,
    resume: bool = False,
    seconds: float = 900,
    progress: Callable[..., Any] = lambda **kw: None,
) -> dict[str, Any]:
    """Explicit disposable PC copy only; its original recovery must be retained.

    Replay is idempotent after interruption. A private binding admits resume,
    and the final whole-file SHA rejects a damaged unchanged block. This is not
    a production promotion API or a full SQLite integrity result.
    """
    deadline = _deadline(seconds)
    original = _identity(work)
    private_disk(work.parent)
    if work.stat().st_nlink != 1 or any(Path(str(work) + s).exists() for s in ("-wal", "-shm", "-journal")):
        raise StateError("Exclusive closed working copy required")
    _identity(descriptor)
    if fingerprint(descriptor, deadline)[0] != expected_descriptor_sha256:
        raise StateError("Page delta descriptor identity refused")
    meta = json.loads(descriptor.read_text(encoding="utf-8"))
    if (
        meta.get("schema") != "cryptowatcher.page-delta.v1"
        or meta.get("state") != "CAPTURED_BYTES_NOT_QUALIFIED"
        or meta.get("data") != "pages.delta"
        or meta.get("block_bytes") != BLOCK
        or any(
            type(meta.get(k)) is not int or meta[k] < 0
            for k in ("raw_bytes", "base_raw_bytes", "data_bytes", "blocks", "changed_blocks")
        )
        or meta["blocks"] != (meta["raw_bytes"] + BLOCK - 1) // BLOCK
        or meta["changed_blocks"] > meta["blocks"]
    ):
        raise StateError("Page delta bounds refused")
    data = descriptor.parent / "pages.delta"
    before = _identity(data)
    if fingerprint(data, deadline) != (meta["data_sha256"], meta["data_bytes"]):
        raise StateError("Page delta encoded identity refused")
    marker = work.with_name(work.name + ".page-delta.json")
    identity = {
        "schema": "cryptowatcher.page-delta-progress.v1",
        "descriptor_sha256": expected_descriptor_sha256,
        "base_raw_sha256": meta["base_raw_sha256"],
        "raw_sha256": meta["raw_sha256"],
        "device": original[0],
        "inode": original[1],
        "raw_bytes": meta["raw_bytes"],
    }
    with RuntimeLease(work):
        if resume:
            _identity(marker)
            saved = json.loads(marker.read_text(encoding="utf-8"))
            if saved.get("identity") != identity:
                raise StateError("Page delta resume identity refused")
        else:
            if marker.exists():
                raise StateError("Explicit page delta resume required")
            if fingerprint(work, deadline) != (meta["base_raw_sha256"], meta["base_raw_bytes"]):
                raise StateError("Working copy baseline identity refused")
            _json(marker, {"identity": identity, "state": "COPYING"})
        needed = max(0, meta["raw_bytes"] - work.stat().st_size)
        if shutil.disk_usage(work.parent).free < needed + DISK_RESERVE:
            raise StateError("Working copy disk reserve exhausted")
        decoder = zstandard.ZstdDecompressor(max_window_size=1024)
        previous = -1
        count = 0
        with data.open("rb") as source, work.open("r+b") as output:
            while header := source.read(PACKET.size):
                budget(deadline)
                if len(header) != PACKET.size:
                    raise StateError("Page delta packet truncated")
                block, raw_size, encoded_size = PACKET.unpack(header)
                if (
                    not previous < block < meta["blocks"]
                    or not 0 < encoded_size <= BLOCK + 65536
                    or raw_size != min(BLOCK, meta["raw_bytes"] - block * BLOCK)
                ):
                    raise StateError("Page delta packet bounds refused")
                encoded = source.read(encoded_size)
                if len(encoded) != encoded_size:
                    raise StateError("Page delta packet truncated")
                params = zstandard.get_frame_parameters(encoded)
                if (
                    params.content_size != raw_size
                    or params.window_size > BLOCK
                    or params.dict_id
                    or not params.has_checksum
                ):
                    raise StateError("Page delta frame refused")
                raw = decoder.decompress(encoded, max_output_size=BLOCK, allow_extra_data=False)
                if len(raw) != raw_size:
                    raise StateError("Page delta decoded size refused")
                output.seek(block * BLOCK)
                if output.write(raw) != len(raw):
                    raise StateError("Page delta write incomplete")
                previous = block
                count += 1
                if count % 1024 == 0:
                    output.flush()
                    os.fsync(output.fileno())
                    progress(phase="APPLY_PAGES", changed_blocks=count, durable=True)
            if count != meta["changed_blocks"]:
                raise StateError("Page delta packet count refused")
            output.truncate(meta["raw_bytes"])
            output.flush()
            os.fsync(output.fileno())
        if _identity(data) != before or fingerprint(work, deadline) != (
            meta["raw_sha256"],
            meta["raw_bytes"],
        ):
            raise StateError("Reconstructed page identity refused")
        budget(deadline)
        return {
            "state": "RECONSTRUCTED_BYTES_NOT_QUALIFIED",
            "raw_sha256": meta["raw_sha256"],
            "raw_bytes": meta["raw_bytes"],
            "changed_blocks": count,
            "full_source_integrity": "PENDING",
            "production_replacement": False,
        }
