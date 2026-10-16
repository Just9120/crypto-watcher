"""Refresh an isolated qualified compact copy from a complete frozen source.

The old per-row manifest proves unchanged representations. Discovery compares
every current row and records deletions before any upsert, including rowid reuse.
An attached DELETE-journal ledger commits progress with candidate writes through
SQLite's super-journal. It is retained for independent complete readback.
This module never installs a candidate or touches a running application.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, cast

import apsw
import zstandard

from . import change_journal as journal
from .compressed_validation import budget, full_check
from .disk_backup import DISK_RESERVE, private_disk
from .events import HEAD_FIELDS, PREFIX
from .framed_rebuild import collapsed_head, metadata, original_payload
from .history_codec import BLOB_PREFIX, MARKER, MAX_BYTES
from .history_rebuild import TABLES, _CopyCodec
from .lease import RuntimeLease
from .reconcile_delta import ENTRY
from .record_codec import pack, pack_candle
from .storage import RECORD_FORMAT_KEY, StateError, encode, known_record_format
from .validation import fingerprint

SCHEMA = "cryptowatcher.candidate-refresh.v1"


@contextmanager
def manifest_rows(path: Path, rows: int, expected: str, deadline: float) -> Iterator[Iterator[tuple]]:
    """Always consume and hash the complete manifest, including resumed prefixes."""
    digest = hashlib.sha256()
    count = 0
    previous = None
    with path.open("rb") as source, zstandard.ZstdDecompressor().stream_reader(source) as reader:

        def entries() -> Iterator[tuple]:
            nonlocal count, previous
            while raw := reader.read(ENTRY.size):
                budget(deadline)
                if len(raw) != ENTRY.size or count >= rows:
                    raise StateError("Refresh manifest extent refused")
                item = ENTRY.unpack(raw)
                if previous is not None and item[0] <= previous:
                    raise StateError("Refresh manifest order refused")
                previous = item[0]
                digest.update(raw)
                count += 1
                yield item

        stream = entries()
        yield stream
        for _ in stream:
            pass
    if count != rows or digest.hexdigest() != expected:
        raise StateError("Refresh decoded manifest identity refused")


def converted(source: Any, codec: _CopyCodec, key: str, payload: str) -> str:
    """Use the established lossless transformations, only for changed rows."""
    raw = original_payload(payload)
    rewritten, _ = collapsed_head(source, key, raw)
    if key.startswith("source:v1:candle:"):
        value = pack_candle(raw)
        if len(value.encode()) < len(raw.encode()):
            rewritten = value
    kind = key.removeprefix(PREFIX).partition(":")[0]
    if key.startswith(BLOB_PREFIX):
        codec.validate_raw(key[len(BLOB_PREFIX) :], payload)
    elif key.startswith(PREFIX) and kind in HEAD_FIELDS:
        values = metadata(source, raw, kind)
        version = values.get("revision")
        if (
            type(version) is not int
            or version < 1
            or key != PREFIX + f"{kind}:{values.get(kind + '_id')}:{version:010d}"
        ):
            raise StateError("Refresh immutable identity refused")
        if MARKER in raw:
            codec.canonical_json(json.loads(raw))
        elif 1024 <= len(raw.encode()) <= MAX_BYTES:
            value = pack(raw)
            if len(value.encode()) < len(raw.encode()):
                rewritten = value
    return rewritten


def refresh(
    source: Path,
    candidate: Path,
    manifest: Path,
    *,
    source_sha256: str,
    candidate_sha256: str,
    manifest_sha256: str,
    decoded_manifest_sha256: str,
    baseline_rows: int,
    owner: int,
    maximum: int,
    seconds: float = 900,
    resume: bool = False,
    progress: Callable[..., Any] = lambda **kw: None,
) -> dict[str, Any]:
    if not 0 < seconds <= 900 or type(baseline_rows) is not int or baseline_rows < 0:
        raise StateError("Bounded refresh required")
    deadline = time.monotonic() + seconds
    for path in (source, candidate, manifest):
        if path.resolve() != path or any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
            raise StateError("Refresh private path refused")
    if source == candidate or candidate.stat().st_nlink != 1:
        raise StateError("Exclusive disposable candidate required")
    if any(Path(str(source) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise StateError("Closed frozen source without sidecars required")
    private_disk(candidate.parent)
    ledger = candidate.with_name(candidate.name + ".refresh.sqlite3")
    if ledger.exists() != resume or ledger.is_symlink():
        raise StateError("Explicit refresh resume required")
    identity = encode(
        {
            "schema": SCHEMA,
            "source_sha256": source_sha256,
            "candidate_sha256": candidate_sha256,
            "manifest_sha256": manifest_sha256,
            "decoded_manifest_sha256": decoded_manifest_sha256,
            "baseline_rows": baseline_rows,
            "owner": owner,
            "maximum": maximum,
            "algorithm": hashlib.sha256(
                b"".join(
                    Path(__file__).with_name(name).read_text(encoding="utf-8").encode()
                    for name in (
                        "refresh_candidate.py",
                        "framed_rebuild.py",
                        "history_rebuild.py",
                        "record_codec.py",
                        "history_codec.py",
                        "storage.py",
                        "events.py",
                    )
                )
            ).hexdigest(),
        }
    )
    if (
        fingerprint(source, deadline)[0] != source_sha256
        or fingerprint(manifest, deadline)[0] != manifest_sha256
    ):
        raise StateError("Refresh source or manifest identity refused")
    if not resume and fingerprint(candidate, deadline)[0] != candidate_sha256:
        raise StateError("Qualified baseline candidate identity refused")
    with RuntimeLease(candidate), closing(sqlite3.connect(candidate, isolation_level=None)) as target:
        target.execute("PRAGMA journal_mode=DELETE")
        target.execute("PRAGMA synchronous=FULL")
        target.execute("PRAGMA cache_size=-131072")
        target.execute("ATTACH DATABASE ? AS rec", (str(ledger),))
        os.chmod(ledger, 0o600)
        target.execute("PRAGMA rec.journal_mode=DELETE")
        target.execute("PRAGMA rec.synchronous=FULL")
        target.execute("PRAGMA rec.cache_size=-32768")
        page = target.execute("PRAGMA page_size").fetchone()[0]
        if target.execute(f"PRAGMA max_page_count={maximum // page}").fetchone()[0] * page > maximum:
            raise StateError("Refresh candidate capacity refused")
        if not resume:
            marker = target.execute(
                "SELECT rowid,payload_json FROM runtime WHERE key=?", (RECORD_FORMAT_KEY,)
            ).fetchone()
            extent = target.execute("SELECT min(rowid),max(rowid),count(*) FROM runtime").fetchone()
            if (
                extent != (1, baseline_rows + 1, baseline_rows + 1)
                or marker is None
                or marker[0] != baseline_rows + 1
                or not known_record_format(marker[1])
            ):
                raise StateError("Qualified baseline ordinal mapping refused")
            target.execute("BEGIN IMMEDIATE")
            target.execute(
                "CREATE TABLE rec.meta(id INTEGER PRIMARY KEY CHECK(id=1),identity_json TEXT NOT NULL,state_json TEXT NOT NULL)"
            )
            target.execute("CREATE TABLE rec.deletes(ordinal INTEGER PRIMARY KEY)")
            target.execute(
                "CREATE TABLE rec.upserts(source_id INTEGER PRIMARY KEY,key TEXT NOT NULL,source_hash BLOB NOT NULL,target_id INTEGER UNIQUE,key_hash BLOB,payload_hash BLOB)"
            )
            state = {"phase": "CHECK_SOURCE", "cursor": None, "old": 0, "rows": 0}
            target.execute("INSERT INTO rec.meta VALUES(1,?,?)", (identity, encode(state)))
            target.execute("PRAGMA user_version=0")
            target.commit()
        row = target.execute("SELECT identity_json,state_json FROM rec.meta WHERE id=1").fetchone()
        if row is None or row[0] != identity:
            raise StateError("Refresh checkpoint identity refused")
        state = json.loads(row[1])
        journal._canonical(target, journal=False)
        if target.execute("PRAGMA user_version").fetchone()[0] != (2 if state["phase"] == "DONE" else 0):
            raise StateError("Refresh candidate version refused")

        def save() -> None:
            budget(deadline)
            target.execute("UPDATE rec.meta SET state_json=? WHERE id=1", (encode(state),))
            target.commit()
            progress(phase=state["phase"], rows=state["rows"], durable=True)

        def begin() -> None:
            budget(deadline)
            import shutil

            if shutil.disk_usage(candidate.parent).free < DISK_RESERVE + 64 * 1024**2:
                raise StateError("Refresh disk reserve exhausted")
            target.execute("BEGIN IMMEDIATE")

        try:
            with closing(apsw.Connection(str(source), flags=apsw.SQLITE_OPEN_READWRITE)) as incoming:
                incoming.execute("PRAGMA query_only=ON")
                incoming.execute("PRAGMA cache_size=-262144")
                incoming.execute("BEGIN")
                has_journal = (
                    incoming.execute("SELECT 1 FROM sqlite_master WHERE name=?", (journal.META,)).fetchone()
                    is not None
                )
                journal._canonical(cast(sqlite3.Connection, incoming), journal=has_journal)
                version = incoming.execute("PRAGMA user_version").fetchone()
                if version is None or version[0] not in (1, 2):
                    raise StateError("Refresh source version refused")
                if list(incoming.execute("SELECT owner_id FROM owner_settings")) != [(owner,)]:
                    raise StateError("Refresh source owner refused")
                if state["phase"] == "CHECK_SOURCE":
                    progress(phase="SOURCE_FULL_INTEGRITY")
                    full_check(incoming, deadline)
                    begin()
                    state.update(phase="DISCOVER")
                    save()
                if state["phase"] == "DISCOVER":
                    with manifest_rows(manifest, baseline_rows, decoded_manifest_sha256, deadline) as entries:
                        ordinal = 0
                        old = None
                        for item in entries:
                            ordinal += 1
                            if ordinal > state["old"]:
                                old = item
                                break
                        begin()
                        batch_rows = 0
                        query = (
                            "SELECT rowid,key,payload_json FROM runtime"
                            + (" WHERE rowid>?" if state["cursor"] is not None else "")
                            + " ORDER BY rowid"
                        )
                        for rowid, key, payload in incoming.execute(
                            query, () if state["cursor"] is None else (state["cursor"],)
                        ):
                            budget(deadline)
                            if key == RECORD_FORMAT_KEY:
                                if not known_record_format(payload):
                                    raise StateError("Unknown incoming record format")
                                state["cursor"] = rowid
                                continue
                            while old is not None and old[0] < rowid:
                                target.execute("INSERT INTO rec.deletes VALUES(?)", (ordinal,))
                                state["old"] = ordinal
                                old = next(entries, None)
                                ordinal += 1
                            same = old is not None and old[0] == rowid
                            key_hash = hashlib.sha256(key.encode()).digest()
                            payload_hash = hashlib.sha256(payload.encode()).digest()
                            if same:
                                assert old is not None
                            if same and old is not None and old[1] != key_hash:
                                target.execute("INSERT INTO rec.deletes VALUES(?)", (ordinal,))
                            if not same or old is not None and old[1:3] != (key_hash, payload_hash):
                                target.execute(
                                    "INSERT INTO rec.upserts(source_id,key,source_hash) VALUES(?,?,?)",
                                    (rowid, key, payload_hash),
                                )
                            if same:
                                state["old"] = ordinal
                                old = next(entries, None)
                                ordinal += 1
                            state.update(cursor=rowid, rows=state["rows"] + 1)
                            batch_rows += 1
                            if batch_rows >= 10000:
                                save()
                                begin()
                                batch_rows = 0
                        while old is not None:
                            target.execute("INSERT INTO rec.deletes VALUES(?)", (ordinal,))
                            state["old"] = ordinal
                            old = next(entries, None)
                            ordinal += 1
                        state.update(phase="DELETE", cursor=None)
                        save()
                if state["phase"] == "DELETE":
                    while True:
                        batch = target.execute(
                            "SELECT ordinal FROM rec.deletes WHERE ordinal>? ORDER BY ordinal LIMIT 1000",
                            (state["cursor"] or 0,),
                        ).fetchall()
                        if not batch:
                            break
                        begin()
                        target.executemany("DELETE FROM runtime WHERE rowid=?", batch)
                        state["cursor"] = batch[-1][0]
                        save()
                    begin()
                    state.update(phase="UPSERT", cursor=None)
                    save()
                if state["phase"] == "UPSERT":
                    codec = _CopyCodec(cast(sqlite3.Connection, incoming))
                    while True:
                        batch = target.execute(
                            "SELECT source_id,key,source_hash FROM rec.upserts"
                            + (" WHERE source_id>?" if state["cursor"] is not None else "")
                            + " ORDER BY source_id LIMIT 500",
                            () if state["cursor"] is None else (state["cursor"],),
                        ).fetchall()
                        if not batch:
                            break
                        begin()
                        for source_id, key, source_hash in batch:
                            budget(deadline)
                            original = incoming.execute(
                                "SELECT key,payload_json FROM runtime WHERE rowid=?", (source_id,)
                            ).fetchone()
                            if (
                                original is None
                                or original[0] != key
                                or hashlib.sha256(original[1].encode()).digest() != source_hash
                            ):
                                raise StateError("Refresh changed source refused")
                            physical = converted(incoming, codec, key, original[1])
                            target.execute(
                                "INSERT INTO runtime VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload_json=excluded.payload_json",
                                (key, physical),
                            )
                            target_id = target.execute(
                                "SELECT rowid FROM runtime WHERE key=?", (key,)
                            ).fetchone()[0]
                            target.execute(
                                "UPDATE rec.upserts SET target_id=?,key_hash=?,payload_hash=? WHERE source_id=?",
                                (
                                    target_id,
                                    hashlib.sha256(key.encode()).digest(),
                                    hashlib.sha256(physical.encode()).digest(),
                                    source_id,
                                ),
                            )
                            state["cursor"] = source_id
                        save()
                    begin()
                    for table, columns in TABLES.items():
                        if table == "runtime":
                            continue
                        target.execute(f"DELETE FROM {table}")
                        target.executemany(
                            f"INSERT INTO {table} VALUES({','.join('?' for _ in columns)})",
                            incoming.execute(f"SELECT * FROM {table} ORDER BY rowid"),
                        )
                    state.update(phase="VERIFY", cursor=None)
                    save()
                if state["phase"] in ("VERIFY", "DONE"):
                    _verify(target, manifest, baseline_rows, decoded_manifest_sha256, deadline, progress)
                    for table in TABLES:
                        if table != "runtime" and target.execute(
                            f"SELECT * FROM {table} ORDER BY rowid"
                        ).fetchall() != list(incoming.execute(f"SELECT * FROM {table} ORDER BY rowid")):
                            raise StateError("Refresh private readback refused")
                    progress(phase="CANDIDATE_FULL_INTEGRITY")
                    target.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10000)
                    try:
                        if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                            raise StateError("Refresh candidate integrity refused")
                    finally:
                        target.set_progress_handler(None, 0)
                    if state["phase"] != "DONE":
                        begin()
                        target.execute("PRAGMA user_version=2")
                        state.update(phase="DONE")
                        save()
                else:
                    raise StateError("Refresh phase refused")
            if fingerprint(source, deadline)[0] != source_sha256:
                raise StateError("Refresh frozen source changed")
            result = {
                "schema": SCHEMA,
                "state": "QUALIFIED_REFRESHED_SNAPSHOT",
                "source_sha256": source_sha256,
                "source_full_integrity": "PASS",
                "candidate_full_integrity": "PASS",
                "complete_row_readback": "PASS",
                "rows": state["rows"],
                "production_replacement": False,
                "ledger": str(ledger),
            }
            result["candidate_sha256"], result["candidate_bytes"] = fingerprint(candidate, deadline)
            return result
        except BaseException:
            target.rollback()
            raise


def _verify(
    target: sqlite3.Connection,
    manifest: Path,
    rows: int,
    decoded: str,
    deadline: float,
    progress: Callable[..., Any],
) -> None:
    """Every candidate row is matched, not a sample or a saved progress counter."""
    if target.execute("SELECT 1 FROM rec.upserts WHERE target_id IS NULL LIMIT 1").fetchone():
        raise StateError("Refresh has unapplied rows")
    cursor = iter(
        target.execute(
            "SELECT r.rowid,r.key,r.payload_json,u.key_hash,u.payload_hash,d.ordinal FROM runtime r LEFT JOIN rec.upserts u ON u.target_id=r.rowid LEFT JOIN rec.deletes d ON d.ordinal=r.rowid ORDER BY r.rowid"
        )
    )
    current = next(cursor, None)
    checked = 0
    with manifest_rows(manifest, rows, decoded, deadline) as entries:
        for ordinal, old in enumerate(entries, 1):
            budget(deadline)
            if current is not None and current[0] < ordinal:
                raise StateError("Unexpected candidate row")
            if current is not None and current[0] == ordinal:
                if current[5] is not None:
                    raise StateError("Deleted candidate row survived")
                expected = (current[3], current[4]) if current[3] is not None else (old[1], old[3])
                actual = (
                    hashlib.sha256(current[1].encode()).digest(),
                    hashlib.sha256(current[2].encode()).digest(),
                )
                if actual != expected:
                    raise StateError("Complete candidate row hash refused")
                checked += 1
                current = next(cursor, None)
            elif target.execute("SELECT 1 FROM rec.deletes WHERE ordinal=?", (ordinal,)).fetchone() is None:
                raise StateError("Candidate baseline row missing")
            if ordinal % 100000 == 0:
                progress(phase="COMPLETE_ROW_READBACK", rows=ordinal)
    if (
        current is None
        or current[0] != rows + 1
        or current[1] != RECORD_FORMAT_KEY
        or not known_record_format(current[2])
    ):
        raise StateError("Candidate format row refused")
    current = next(cursor, None)
    while current is not None:
        budget(deadline)
        if current[3] is None or (
            hashlib.sha256(current[1].encode()).digest(),
            hashlib.sha256(current[2].encode()).digest(),
        ) != (current[3], current[4]):
            raise StateError("New candidate row not proven")
        checked += 1
        current = next(cursor, None)
    expected = target.execute("SELECT state_json FROM rec.meta WHERE id=1").fetchone()[0]
    if checked != json.loads(expected)["rows"]:
        raise StateError("Complete candidate row count refused")
