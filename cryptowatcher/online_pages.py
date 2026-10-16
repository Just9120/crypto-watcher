"""Pinned SQLite backup to a bounded stream; SQL/history qualification stays separate.

Only the destination's current page and header are retained. Every copied page
has a hash, so a receiver can verify unchanged baseline bytes as well as packets.
The producer never stops the application or keeps a second large VPS spool.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, cast

import apsw
import zstandard

from . import compressed_validation as cv
from .change_journal import state as journal_state
from .disk_backup import DISK_RESERVE, ArchiveSink, private_disk
from .lease import RuntimeLease
from .page_delta import PACKET, _deadline, _identity, _json
from .storage import StateError
from .validation import fingerprint

PAGE = 4096
MAX_BYTES = 64 * 1024**3
PENDING_PAGE = 0x40000000 // PAGE


def make_base(source: Path, directory: Path, *, expected_sha256: str, seconds: float = 900) -> dict[str, Any]:
    deadline = _deadline(seconds)
    before = _identity(source)
    if before[2] % PAGE or not 0 < before[2] <= MAX_BYTES:
        raise StateError("Online baseline bounds refused")
    directory.mkdir(mode=0o700)
    private_disk(directory)
    raw, hashes = hashlib.sha256(), hashlib.sha256()
    with source.open("rb") as incoming, (directory / "base.hashes").open("xb") as output:
        for page in iter(lambda: incoming.read(PAGE), b""):
            cv.budget(deadline)
            raw.update(page)
            value = hashlib.sha256(page).digest()
            hashes.update(value)
            output.write(value)
        output.flush()
        os.fsync(output.fileno())
    if raw.hexdigest() != expected_sha256 or _identity(source) != before:
        raise StateError("Online baseline identity refused")
    result = {
        "schema": "cryptowatcher.online-page-base.v1",
        "page_bytes": PAGE,
        "raw_bytes": before[2],
        "raw_sha256": raw.hexdigest(),
        "hashes": "base.hashes",
        "hashes_sha256": hashes.hexdigest(),
    }
    _json(directory / "base.json", result)
    return result


class StreamImage:
    """SQLite backup destination. Unexpected reads/writes fail without a footer."""

    def __init__(
        self,
        output: BinaryIO,
        base: bytes,
        pages: int,
        *,
        maximum: int,
        deadline: float,
        directory: Path,
        progress: Callable[..., Any],
    ) -> None:
        self.size = 0
        self.base = base
        self.pages = pages
        self.maximum = maximum
        self.deadline = deadline
        self.progress = progress
        self.sink = ArchiveSink(output, directory, deadline)
        self.hashes = bytearray(pages * 32)
        self.seen = bytearray(pages)
        self.header = b""
        self.compressor = zstandard.ZstdCompressor(level=1, write_checksum=True, threads=0)
        self.writes = self.changed = 0

    def read(self, amount: int, offset: int) -> bytes:
        cv.budget(self.deadline)
        if not 0 <= offset <= PAGE or not 0 <= amount <= PAGE or offset + amount > PAGE:
            raise StateError("Unexpected backup destination read")
        return self.header[offset : offset + amount]

    def write(self, data: bytes, offset: int) -> None:
        cv.budget(self.deadline)
        if len(data) != PAGE or offset % PAGE or not 0 <= offset < self.pages * PAGE:
            raise StateError("Unexpected backup destination write")
        index = offset // PAGE
        value = hashlib.sha256(data).digest()
        repeated = bool(self.seen[index])
        self.hashes[index * 32 : (index + 1) * 32] = value
        self.seen[index] = 1
        if index == 0:
            self.header = data
        if repeated or value != self.base[index * 32 : (index + 1) * 32]:
            encoded = self.compressor.compress(data)
            packet = PACKET.pack(index, PAGE, len(encoded)) + encoded
            if self.sink.size + len(packet) > self.maximum:
                raise StateError("Online stream capacity exhausted")
            self.sink.write(packet)
            self.changed += 1
        self.size = max(self.size, offset + PAGE)
        self.writes += 1
        self.progress(
            phase="ONLINE_CAPTURE",
            pages_written=self.writes,
            total_pages=self.pages,
            encoded_bytes=self.sink.size,
        )

    def truncate(self, size: int) -> None:
        if size != self.pages * PAGE:
            raise StateError("Unexpected backup destination truncate")
        self.size = size

    def finish(self) -> dict[str, Any]:
        cv.budget(self.deadline)
        for index, present in enumerate(self.seen):
            if index % 10000 == 0:
                cv.budget(self.deadline)
            if not present:
                # SQLite's reserved locking page is not a database page. Its
                # destination hole is zero-filled; no other omission is accepted.
                if index != PENDING_PAGE:
                    raise StateError("Online backup omitted a page")
                self.write(bytes(PAGE), index * PAGE)
        if self.size != self.pages * PAGE:
            raise StateError("Online backup extent refused")
        self.sink.flush()
        return {
            "raw_bytes": self.size,
            "page_root_sha256": hashlib.sha256(self.hashes).hexdigest(),
            "data_sha256": self.sink.digest.hexdigest(),
            "data_bytes": self.sink.size,
            "packets": self.changed,
            "page_bytes": PAGE,
            "pages": self.pages,
        }


def capture(
    source: Path,
    baseline: Path,
    directory: Path,
    output: BinaryIO,
    *,
    expected_baseline_sha256: str,
    epoch: str,
    owner: int,
    maximum: int,
    seconds: float = 900,
    progress: Callable[..., Any] = lambda **kw: None,
) -> dict[str, Any]:
    deadline = _deadline(seconds)
    before = _identity(source)
    if type(maximum) is not int or not 0 < maximum <= 8 * 1024**3:
        raise StateError("Explicit online stream bound required")
    private_disk(directory)
    if fingerprint(baseline, deadline)[0] != expected_baseline_sha256:
        raise StateError("Online baseline descriptor refused")
    record = json.loads(baseline.read_text(encoding="utf-8"))
    if (
        record.get("schema") != "cryptowatcher.online-page-base.v1"
        or record.get("page_bytes") != PAGE
        or record.get("hashes") != "base.hashes"
        or type(record.get("raw_bytes")) is not int
        or not 0 < record["raw_bytes"] <= MAX_BYTES
        or record["raw_bytes"] % PAGE
    ):
        raise StateError("Online baseline bounds refused")
    hashes = baseline.parent / "base.hashes"
    _identity(hashes)
    if hashes.stat().st_size != record["raw_bytes"] // PAGE * 32:
        raise StateError("Online baseline hash extent refused")
    base = hashes.read_bytes()
    if hashlib.sha256(base).hexdigest() != record.get("hashes_sha256"):
        raise StateError("Online baseline hashes refused")
    with tempfile.TemporaryDirectory(prefix="cw-online-page-", dir=directory) as scratch:
        path = Path(scratch) / "sentinel.sqlite3"
        path.touch(mode=0o600)
        with closing(
            apsw.Connection(
                source.as_uri() + "?mode=ro", flags=apsw.SQLITE_OPEN_READONLY | apsw.SQLITE_OPEN_URI
            )
        ) as reader:
            reader.execute(f"PRAGMA mmap_size={MAX_BYTES}")
            mapped = reader.execute("PRAGMA mmap_size").fetchone()
            reader.execute("BEGIN")
            # The first read pins one WAL snapshot; journal metadata and every
            # copied row belong to this source point, regardless of later writes.
            mark = journal_state(cast(sqlite3.Connection, reader), epoch, owner)
            observed_at = datetime.now(timezone.utc).isoformat()
            size_row = reader.execute("PRAGMA page_size").fetchone()
            count_row = reader.execute("PRAGMA page_count").fetchone()
            version_row = reader.execute("PRAGMA user_version").fetchone()
            if size_row is None or count_row is None or version_row is None:
                raise StateError("Online source metadata unavailable")
            page_size, count, version = size_row[0], count_row[0], version_row[0]
            if (
                page_size != PAGE
                or type(count) is not int
                or not 0 < count * PAGE <= MAX_BYTES
                or version not in (1, 2)
            ):
                raise StateError("Online source extent or version refused")
            image = StreamImage(
                output,
                base,
                count,
                maximum=maximum,
                deadline=deadline,
                directory=directory,
                progress=progress,
            )
            vfs = cv.VFS(cast(cv.Image, image), path)
            try:
                with closing(
                    apsw.Connection(str(path), flags=apsw.SQLITE_OPEN_READWRITE, vfs=vfs.name)
                ) as target:
                    target.execute("PRAGMA journal_mode=MEMORY")
                    target.execute("PRAGMA mmap_size=0")
                    with target.backup("main", reader, "main") as backup:
                        while not backup.done:
                            cv.budget(deadline)
                            if shutil.disk_usage(directory).free < DISK_RESERVE + 64 * 1024**2:
                                raise StateError("Online capture disk reserve exhausted")
                            backup.step(256)
                result = image.finish()
            finally:
                vfs.unregister()
    after = _identity(source)
    if before[:2] != after[:2]:
        raise StateError("Online source file identity changed")
    result.update(
        schema="cryptowatcher.online-page-stream.v1",
        state="CAPTURED_SNAPSHOT_NOT_QUALIFIED",
        base_raw_sha256=record["raw_sha256"],
        base_raw_bytes=record["raw_bytes"],
        baseline_descriptor_sha256=expected_baseline_sha256,
        data="pages.delta",
        journal_epoch=mark["epoch"],
        journal_watermark=mark["last_seq"],
        owner=owner,
        full_source_integrity="PENDING",
        receiver_fsync="REQUIRED",
        production_replacement=False,
        application_stopped=False,
        source_schema_version=version,
        snapshot_observed_at_utc=observed_at,
        source_mmap_bytes=0 if mapped is None else mapped[0],
    )
    _json(directory / "snapshot.json", result)
    return result


def apply_to_working_copy(
    work: Path,
    descriptor: Path,
    *,
    expected_descriptor_sha256: str,
    seconds: float = 900,
    resume: bool = False,
    progress: Callable[..., Any] = lambda **kw: None,
) -> dict[str, Any]:
    """Replay only into a disposable PC copy; every resulting page is verified.

    Backup may rewrite the header after other pages. Ordered delivery is not
    assumed: the encoded stream and final complete page-root bind all writes.
    A durable identity admits replay after interruption without losing progress.
    """
    deadline = _deadline(seconds)
    identity = _identity(work)
    if work.stat().st_nlink != 1 or any(Path(str(work) + s).exists() for s in ("-wal", "-shm", "-journal")):
        raise StateError("Exclusive disposable PC copy required")
    private_disk(work.parent)
    if fingerprint(descriptor, deadline)[0] != expected_descriptor_sha256:
        raise StateError("Online snapshot descriptor identity refused")
    value = json.loads(descriptor.read_text(encoding="utf-8"))
    if (
        value.get("schema") != "cryptowatcher.online-page-stream.v1"
        or value.get("state") != "CAPTURED_SNAPSHOT_NOT_QUALIFIED"
        or value.get("page_bytes") != PAGE
        or value.get("data") != "pages.delta"
        or any(
            type(value.get(k)) is not int or value[k] < 0
            for k in ("raw_bytes", "pages", "packets", "data_bytes")
        )
        or not 0 < value["raw_bytes"] <= MAX_BYTES
        or value["raw_bytes"] != value["pages"] * PAGE
        or value["data_bytes"] > 8 * 1024**3
    ):
        raise StateError("Online snapshot bounds refused")
    data = descriptor.parent / "pages.delta"
    before = _identity(data)
    if fingerprint(data, deadline) != (value["data_sha256"], value["data_bytes"]):
        raise StateError("Online packet stream identity refused")
    marker = work.with_name(work.name + ".online-pages.json")
    bound = {
        "schema": "cryptowatcher.online-page-progress.v1",
        "descriptor_sha256": expected_descriptor_sha256,
        "device": identity[0],
        "inode": identity[1],
        "page_root_sha256": value["page_root_sha256"],
    }
    with RuntimeLease(work):
        if resume:
            _identity(marker)
            if json.loads(marker.read_text()) != bound:
                raise StateError("Online resume identity refused")
        else:
            if marker.exists():
                raise StateError("Explicit online replay resume required")
            if fingerprint(work, deadline) != (value["base_raw_sha256"], value["base_raw_bytes"]):
                raise StateError("Online working baseline refused")
            _json(marker, bound)
        if (
            shutil.disk_usage(work.parent).free
            < max(0, value["raw_bytes"] - work.stat().st_size) + DISK_RESERVE
        ):
            raise StateError("Online PC reserve unavailable")
        packets = 0
        with data.open("rb") as incoming, work.open("r+b") as output:
            while header := incoming.read(PACKET.size):
                cv.budget(deadline)
                if len(header) != PACKET.size:
                    raise StateError("Online packet header truncated")
                page, raw_size, size = PACKET.unpack(header)
                if not 0 <= page < value["pages"] or raw_size != PAGE or not 0 < size <= PAGE + 256:
                    raise StateError("Online packet bounds refused")
                encoded = incoming.read(size)
                if len(encoded) != size:
                    raise StateError("Online packet truncated")
                params = zstandard.get_frame_parameters(encoded)
                if (
                    params.content_size != PAGE
                    or params.window_size > PAGE
                    or params.dict_id
                    or not params.has_checksum
                ):
                    raise StateError("Online packet frame refused")
                raw = zstandard.ZstdDecompressor(max_window_size=1024).decompress(
                    encoded, max_output_size=PAGE, allow_extra_data=False
                )
                if len(raw) != PAGE:
                    raise StateError("Online packet decode refused")
                output.seek(page * PAGE)
                if output.write(raw) != PAGE:
                    raise StateError("Online page write incomplete")
                packets += 1
                if packets > value["packets"]:
                    raise StateError("Online packet count refused")
                if packets % 8192 == 0:
                    output.flush()
                    os.fsync(output.fileno())
                    progress(phase="APPLY_ONLINE_PAGES", packets=packets, durable=True)
            if packets != value["packets"]:
                raise StateError("Online packet count refused")
            output.truncate(value["raw_bytes"])
            output.flush()
            os.fsync(output.fileno())
        raw_hash, root = hashlib.sha256(), hashlib.sha256()
        with work.open("rb") as source:
            for page in iter(lambda: source.read(PAGE), b""):
                cv.budget(deadline)
                raw_hash.update(page)
                root.update(hashlib.sha256(page).digest())
        if root.hexdigest() != value["page_root_sha256"] or _identity(data) != before:
            raise StateError("Complete online page-root mismatch")
        return {
            "state": "RECONSTRUCTED_BYTES_NOT_QUALIFIED",
            "raw_sha256": raw_hash.hexdigest(),
            "raw_bytes": value["raw_bytes"],
            "page_root_sha256": root.hexdigest(),
            "journal_epoch": value["journal_epoch"],
            "journal_watermark": value["journal_watermark"],
            "full_source_integrity": "PENDING",
            "production_replacement": False,
        }
