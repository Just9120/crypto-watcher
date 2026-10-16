"""Durably archive one maintenance-consumer prefix before acknowledging its keys.

Application tables are read only. Short writer transactions remove only journal
entries whose sequence is included in the verified archive; later writes survive.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import IO, Any, cast

import zstandard

from . import change_journal as journal
from .compressed_validation import budget
from .disk_backup import DISK_RESERVE, ArchiveSink, private_disk
from .page_delta import _deadline, _json
from .storage import StateError, encode
from .validation import fingerprint


def archive_and_ack(
    db: sqlite3.Connection,
    directory: Path,
    *,
    epoch: str,
    owner: int,
    after: int = 0,
    seconds: float = 900,
    maximum: int = 1024**3,
    key_only_before_first_snapshot: bool = False,
    no_prior_capture: bool = False,
) -> dict[str, Any]:
    """One durable consumer, not a reset, epoch renewal or data retention rule."""
    deadline = _deadline(seconds)
    if (
        db.in_transaction
        or type(after) is not int
        or after != 0
        or type(maximum) is not int
        or not 0 < maximum <= 1024**3
    ):
        raise StateError("Explicit journal archive bounds required")
    if db.execute("PRAGMA synchronous").fetchone()[0] < 2:
        raise StateError("Durable journal acknowledgement required")
    if key_only_before_first_snapshot and no_prior_capture is not True:
        raise StateError("Unconsumed initial source point proof required")
    directory.mkdir(mode=0o700)
    private_disk(directory)
    path = directory / "tail.zst"
    raw = hashlib.sha256()
    raw_size = count = charge_total = 0

    class Sink(ArchiveSink):
        def write(self, data: bytes) -> int:
            if self.size + len(data) > maximum:
                raise StateError("Journal archive capacity exceeded")
            return super().write(data)

    with path.open("xb") as output:
        os.chmod(path, 0o600)
        sink = Sink(output, directory, deadline)
        with zstandard.ZstdCompressor(level=1, write_checksum=True, threads=0).stream_writer(
            cast(IO[bytes], sink), closefd=False
        ) as encoded:

            def line(value: Any) -> None:
                nonlocal raw_size
                budget(deadline)
                body = (encode(value) + "\n").encode()
                raw.update(body)
                raw_size += len(body)
                encoded.write(body)

            with journal.changes(db, epoch=epoch, owner=owner, after=after, deadline=deadline) as (
                snapshot,
                rows,
            ):
                upper = snapshot["last_seq"]
                line(
                    {
                        "schema": "cryptowatcher.maintenance-tail.v1",
                        "epoch": epoch,
                        "owner": owner,
                        "after": after,
                        "through": upper,
                        "journal_state": snapshot,
                        "mode": "INITIAL_UNCONSUMED_KEYS"
                        if key_only_before_first_snapshot
                        else "VALUES_AND_TOMBSTONES",
                    }
                )
                entries = rows
                if key_only_before_first_snapshot:
                    entries = (
                        {"table": r[0], "key": r[1], "seq": r[2]}
                        for r in db.execute(f"SELECT table_name,key,seq FROM {journal.KEYS} ORDER BY seq")
                    )
                for item in entries:
                    line(item)
                    count += 1
                    charge_total += len(item["key"].encode()) + 128
                if (
                    count != snapshot["key_count"]
                    or charge_total != snapshot["key_bytes"]
                    or not 0 <= count <= snapshot["max_keys"]
                    or not 0 <= charge_total <= snapshot["max_bytes"]
                ):
                    raise StateError("Journal archive counters refused")
                line({"state": "COMPLETE", "rows": count, "through": upper})
        output.flush()
        os.fsync(output.fileno())
    decoded = hashlib.sha256()
    decoded_size = 0
    with path.open("rb") as source, zstandard.ZstdDecompressor().stream_reader(source) as reader:
        for body in iter(lambda: reader.read(1024**2), b""):
            budget(deadline)
            decoded.update(body)
            decoded_size += len(body)
    identity = fingerprint(path, deadline)
    if (decoded.hexdigest(), decoded_size) != (raw.hexdigest(), raw_size) or identity != (
        sink.digest.hexdigest(),
        sink.size,
    ):
        raise StateError("Journal archive independent readback refused")
    proof = {
        "schema": "cryptowatcher.maintenance-tail-proof.v1",
        "state": "ARCHIVE_VERIFIED_ACK_PENDING",
        "epoch": epoch,
        "owner": owner,
        "after": after,
        "through": upper,
        "journal_snapshot": snapshot,
        "rows": count,
        "raw_sha256": raw.hexdigest(),
        "raw_bytes": raw_size,
        "archive_sha256": identity[0],
        "archive_bytes": identity[1],
        "application_rows_deleted": 0,
    }
    _json(directory / "archive.json", proof)

    def authorize(action: int, table: str | None, *args: Any) -> int:
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE) and table not in (
            journal.META,
            journal.KEYS,
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    acknowledged = 0
    db.set_authorizer(authorize)
    db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    try:
        while True:
            budget(deadline)
            private_disk(directory)
            if shutil.disk_usage(directory).free < DISK_RESERVE + 64 * 1024**2:
                raise StateError("Journal acknowledgement reserve exhausted")
            db.execute("BEGIN IMMEDIATE")
            try:
                current = journal.state(db, epoch, owner)
                if upper > current["last_seq"]:
                    raise StateError("Journal acknowledgement cursor refused")
                selected = db.execute(
                    f"SELECT key,seq FROM {journal.KEYS} WHERE seq<=? ORDER BY seq LIMIT 500", (upper,)
                ).fetchall()
                if not selected:
                    db.rollback()
                    break
                charge = sum(len(row[0].encode()) + 128 for row in selected)
                if len(selected) > current["key_count"] or charge > current["key_bytes"]:
                    raise StateError("Journal prefix counters changed")
                marks = ",".join("?" for _ in selected)
                db.execute(
                    f"DELETE FROM {journal.KEYS} WHERE seq IN({marks})", tuple(row[1] for row in selected)
                )
                db.execute(
                    f"UPDATE {journal.META} SET key_count=key_count-?,key_bytes=key_bytes-? WHERE id=1",
                    (len(selected), charge),
                )
                budget(deadline)
                db.commit()
                acknowledged += len(selected)
                # Give the application a writer opportunity between bounded
                # commits; this maintenance consumer must not starve alerts.
                time.sleep(min(0.005, max(0, deadline - time.monotonic())))
            except BaseException:
                db.rollback()
                raise
    finally:
        db.set_authorizer(None)
        db.set_progress_handler(None, 0)
    result = {
        **proof,
        "state": "PREPARED_UNCONSUMED_JOURNAL_PREFIX"
        if key_only_before_first_snapshot
        else "ARCHIVED_ACKNOWLEDGED_PREFIX",
        "requires_fresh_full_source_snapshot": key_only_before_first_snapshot,
        "acknowledged_keys": acknowledged,
        "last_seq_preserved": True,
        "epoch_preserved": True,
        "bounds_preserved": True,
    }
    _json(directory / "acknowledgement.json", result)
    return result
