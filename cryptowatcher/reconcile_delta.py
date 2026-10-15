"""Export exact changes against a qualified row manifest, from one WAL snapshot.

This module does not replace a database or acknowledge a consumer watermark.
Deletions carry original rowids, resolved only against the pinned old source.
"""

from __future__ import annotations

import hashlib
import math
import os
import struct
import time
from pathlib import Path
from typing import IO, Any, cast

import zstandard

from . import change_journal
from .compressed_validation import budget
from .disk_backup import ArchiveSink, private_disk
from .history_rebuild import TABLES, feed
from .storage import SCHEMA_VERSION, StateError, encode
from .validation import fingerprint

ENTRY = struct.Struct("<q32s32s32s")


def export_delta(
    db: Any,
    baseline: Path,
    destination: Path,
    *,
    epoch: str,
    owner: int,
    baseline_sha256: str,
    baseline_decoded_sha256: str,
    baseline_rows: int,
    max_archive_bytes: int,
    seconds: float = 900,
) -> dict[str, Any]:
    """Every old row is compared; a rowid high-water mark alone is insufficient."""
    if (
        type(baseline_rows) is not int
        or baseline_rows < 0
        or not math.isfinite(seconds)
        or not 0 < seconds <= 900
        or type(max_archive_bytes) is not int
        or max_archive_bytes <= 0
    ):
        raise StateError("Invalid delta bounds")
    deadline = time.monotonic() + seconds
    if (
        baseline.resolve() != baseline
        or destination.resolve() != destination
        or any(p.is_symlink() for path in (baseline, destination) for p in (path, *path.parents))
    ):
        raise StateError("Delta path refused")
    before = fingerprint(baseline, deadline)
    if before[0] != baseline_sha256:
        raise StateError("Baseline manifest identity refused")
    decoded_digest = hashlib.sha256()
    encoded_digest = hashlib.sha256()
    manifests = {table: hashlib.sha256() for table in TABLES}
    counts: dict[str, int] = {}
    inserts = deletes = compared = 0
    private_disk(destination.parent)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:

        class Sink(ArchiveSink):
            def write(self, data: bytes) -> int:
                if self.size + len(data) > max_archive_bytes:
                    raise StateError("Delta capacity exhausted")
                return super().write(data)

        sink = Sink(output, destination.parent, deadline)
        with zstandard.ZstdCompressor(level=1, write_checksum=True, threads=0).stream_writer(
            cast(IO[bytes], sink), closefd=False
        ) as encoded:

            def write(value: Any) -> None:
                budget(deadline)
                line = (encode(value) + "\n").encode()
                encoded_digest.update(line)
                encoded.write(line)

            with change_journal.changes(db, epoch=epoch, owner=owner, after=0, deadline=deadline) as (
                snapshot,
                _,
            ):
                if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                    raise StateError("Delta source version refused")
                write(
                    {
                        "kind": "begin",
                        "schema": "cryptowatcher.reconcile-delta.v1",
                        "epoch": epoch,
                        "watermark": snapshot["last_seq"],
                        "owner": owner,
                        "baseline_sha256": before[0],
                    }
                )
                for table in TABLES:
                    if table == "runtime":
                        continue
                    rows = [tuple(row) for row in db.execute(f"SELECT rowid,* FROM {table} ORDER BY rowid")]
                    counts[table] = len(rows)
                    for row in rows:
                        feed(manifests[table], row[1:])
                    write({"kind": "private_table", "table": table, "rows": rows})
                with baseline.open("rb") as raw, zstandard.ZstdDecompressor().stream_reader(raw) as reader:
                    previous: int | None = None

                    def next_old() -> tuple[Any, ...] | None:
                        nonlocal compared, previous
                        budget(deadline)
                        data = bytearray()
                        while len(data) < ENTRY.size:
                            block = reader.read(ENTRY.size - len(data))
                            if not block:
                                break
                            data.extend(block)
                        if not data:
                            return None
                        if len(data) != ENTRY.size or compared >= baseline_rows:
                            raise StateError("Baseline manifest bounds refused")
                        decoded_digest.update(data)
                        entry = ENTRY.unpack(data)
                        if previous is not None and entry[0] <= previous:
                            raise StateError("Baseline row order refused")
                        previous = entry[0]
                        compared += 1
                        return entry

                    old = next_old()
                    count = 0
                    for row in db.execute("SELECT rowid,key,payload_json FROM runtime ORDER BY rowid"):
                        budget(deadline)
                        rowid, key, payload = row
                        if not isinstance(key, str) or not isinstance(payload, str):
                            raise StateError("Delta runtime types refused")
                        feed(manifests["runtime"], (key, payload))
                        count += 1
                        while old is not None and old[0] < rowid:
                            write(["delete", old[0]])
                            deletes += 1
                            old = next_old()
                        changed = old is None or old[0] != rowid
                        if not changed:
                            assert old is not None
                            same_key = hashlib.sha256(key.encode()).digest() == old[1]
                            changed = not same_key or hashlib.sha256(payload.encode()).digest() != old[2]
                            if not same_key:
                                write(["delete", old[0]])
                                deletes += 1
                        if changed:
                            write(["upsert", rowid, key, payload])
                            inserts += 1
                        if old is not None and old[0] == rowid:
                            old = next_old()
                    while old is not None:
                        write(["delete", old[0]])
                        deletes += 1
                        old = next_old()
                    counts["runtime"] = count
                if compared != baseline_rows or decoded_digest.hexdigest() != baseline_decoded_sha256:
                    raise StateError("Baseline decoded identity refused")
                result = {
                    "state": "EXPORTED_SNAPSHOT_DELTA",
                    "epoch": epoch,
                    "watermark": snapshot["last_seq"],
                    "baseline_sha256": before[0],
                    "baseline_rows": compared,
                    "upserts": inserts,
                    "deletes": deletes,
                    "table_rows": counts,
                    "physical_row_manifests": {
                        table: value.hexdigest() for table, value in manifests.items()
                    },
                    "decoded_body_sha256": encoded_digest.hexdigest(),
                    "source_full_integrity": "PENDING",
                    "production_replacement": False,
                }
                write({"kind": "complete", **result})
        output.flush()
        os.fsync(output.fileno())
    if fingerprint(baseline, deadline) != before:
        raise StateError("Baseline manifest changed")
    actual = fingerprint(destination, deadline)
    if os.name == "posix":
        folder = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
    budget(deadline)
    return {**result, "archive_sha256": actual[0], "archive_bytes": actual[1]}
