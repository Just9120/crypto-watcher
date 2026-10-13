"""Rebuild a closed backup into a new isolated candidate; never replace the source."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from collections import OrderedDict
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

from .events import HEAD_FIELDS, HEAD_SCHEMA, PREFIX, History
from .history_codec import BLOB_PREFIX, MARKER, MAX_BYTES, Codec, sql_value
from .native_history import Accelerator, load
from .storage import IntegrityError, StateError, Store, check_integrity, create_schema, encode

TABLES = {
    "owner_settings": ("owner_id", "revision", "settings_json"),
    "delivery_preferences": ("chat_id", "enabled"),
    "mutes": ("symbol", "mute_until"),
    "runtime": ("key", "payload_json"),
    "legacy_import": ("source_sha256", "original_json", "backup_name"),
}
DISK_RESERVE = 1536 * 1024 * 1024


def refusal_code(error: Exception) -> str:
    """Classify refusal without disclosing exception text or source payloads."""
    if isinstance(error, sqlite3.DatabaseError):
        code = getattr(error, "sqlite_errorcode", 0) & 0xFF
        return {
            sqlite3.SQLITE_FULL: "CANDIDATE_CAPACITY_EXCEEDED",
            sqlite3.SQLITE_CONSTRAINT: "CANDIDATE_CONSTRAINT_REJECTED",
            sqlite3.SQLITE_IOERR: "IO_ERROR",
            sqlite3.SQLITE_CORRUPT: "DATABASE_CORRUPT",
            sqlite3.SQLITE_NOTADB: "DATABASE_CORRUPT",
            sqlite3.SQLITE_BUSY: "DATABASE_BUSY",
            sqlite3.SQLITE_LOCKED: "DATABASE_BUSY",
        }.get(code, "DATABASE_ERROR")
    if isinstance(error, OSError):
        return "IO_ERROR"
    if isinstance(error, StateError):
        return {
            "Disk reserve exhausted; source preserved": "DISK_RESERVE_EXHAUSTED",
            "Candidate schema exceeds byte limit": "CANDIDATE_CAPACITY_EXCEEDED",
            "Candidate history conflicts with source rows": "LOGICAL_MANIFEST_MISMATCH",
            "Native extraction refused input": "NATIVE_INPUT_REJECTED",
            "Source changed; candidate not qualified": "SOURCE_CHANGED",
            "Source fingerprint mismatch": "SOURCE_FINGERPRINT_MISMATCH",
            "Decoded source fingerprint mismatch": "SOURCE_FINGERPRINT_MISMATCH",
        }.get(str(error), "STATE_REJECTED")
    return "INVALID_INPUT"


class _CopyCodec(Codec):
    """One exclusive copy transaction; every body/reference is checked after copy."""

    def __init__(self, database: sqlite3.Connection, accelerator: Accelerator | None = None):
        super().__init__(database)
        self.admitted: OrderedDict[str, None] = OrderedDict()
        self.accelerator = accelerator

    def pack_canonical(self, payload: str) -> Any:
        if self.accelerator is not None and MAX_BYTES == 32 * 1024 * 1024:
            packed = self.accelerator.pack(payload, self.save_bytes)
            if packed is not None:
                return packed
        return super().pack_canonical(payload)

    def canonical(self, raw: bytes) -> tuple[bool, int | None]:
        if self.accelerator is not None:
            result = self.accelerator.canonical(raw)
            if result is not None:
                return result
        return super().canonical(raw)

    def pack_window(self, item: str) -> dict[str, Any] | None:
        # Historical conversion preserves a complete frozen window without
        # repeatedly parsing every candle. Live writes still use shared blocks.
        raw = item.encode()
        if len(raw) > MAX_BYTES:
            return super().pack_window(item)
        key = self.save_bytes(raw)
        return {"mode": "value", "ids": [key], "sha256": key}

    def save_bytes(self, raw: bytes) -> str:
        cached = self.encoded.get(raw)
        if not self.db.in_transaction:
            self.admitted.clear()
        if cached is not None and cached[0] in self.admitted:
            self.encoded.move_to_end(raw)
            self.admitted.move_to_end(cached[0])
            return cached[0]
        key = super().save_bytes(raw)
        self.admitted[key] = None
        if len(self.admitted) > 4096:
            self.admitted.popitem(last=False)
        return key


def guard(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise IntegrityError("TIME_BUDGET_EXCEEDED", "Rebuild time budget exceeded")


def trim_unused_memory() -> None:
    """Release unused allocator pages without changing any live buffers."""
    if sys.platform == "linux":
        # SQLite frees its pages, but glibc can retain their arena allocation.
        # Trim only unused allocator pages; live image buffers are unaffected.
        try:
            trim = ctypes.CDLL(None).malloc_trim
        except AttributeError:
            pass  # Other libc implementations still get SQLite's cache release.
        else:
            trim.argtypes = [ctypes.c_size_t]
            trim.restype = ctypes.c_int
            trim(0)


def release_source_cache(database: Any, deadline: float) -> None:
    """Release validation-only caches before the next bounded conversion stage."""
    guard(deadline)
    database.execute("PRAGMA shrink_memory")
    trim_unused_memory()
    guard(deadline)


def configure_candidate_cache(database: sqlite3.Connection, deadline: float) -> None:
    """Admit verification cache only after the owner has released source resources."""
    from .compressed_validation import (
        VALIDATION_MEMORY_BYTES,
        VALIDATION_OVERHEAD_BYTES,
        resident_memory_bytes,
    )

    guard(deadline)
    resident = resident_memory_bytes()
    available = (
        256 * 1024**2
        if resident is None
        else (VALIDATION_MEMORY_BYTES - resident - VALIDATION_OVERHEAD_BYTES)
    )
    if available < 128 * 1024**2:
        raise IntegrityError("DATABASE_ERROR", "Insufficient verification cache capacity")
    cache_kib = min(1024**3, available) // 1024
    database.execute(f"PRAGMA cache_size=-{cache_kib}")
    database.execute("PRAGMA shrink_memory")
    guard(deadline)


def closed(path: Path) -> None:
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise StateError("Symlink refused")
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise StateError("Source must be a closed backup without sidecars")


def fingerprint(path: Path, deadline: float) -> str:
    closed(path)
    before = path.stat()
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise StateError("Source file identity changed")
        while block := stream.read(1024 * 1024):
            guard(deadline)
            digest.update(block)
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise StateError("Source changed during rebuild")
    closed(path)
    return digest.hexdigest()


@contextmanager
def archive_source(
    source: Path, directory: Path, expected: str, deadline: float, encoding: str
) -> Iterator[tuple[Any, dict[str, Any]]]:
    """Use a verified frozen archive image without an additional multi-GB raw copy."""
    import apsw

    from .compressed_validation import MAX_COMPRESSED_BYTES, Image, classified_errors, frozen_vfs, full_check

    if encoding not in ("gzip", "zstd"):
        raise StateError("Unknown archive encoding")
    archive_encoding: Literal["GZIP", "ZSTD"] = "GZIP" if encoding == "gzip" else "ZSTD"
    with classified_errors(deadline):
        image = Image(
            source, limit=MAX_COMPRESSED_BYTES, deadline=deadline, archive_encoding=archive_encoding
        )
        if image.sha256 != expected:
            raise StateError("Decoded source fingerprint mismatch")
    with (
        classified_errors(deadline),
        tempfile.TemporaryDirectory(prefix="cw-rebuild-source-", dir=directory) as work,
    ):
        sentinel = Path(work) / "source.sqlite3"
        sentinel.touch(mode=0o600)
        vfs = frozen_vfs(image, sentinel)
        original = None
        try:
            original = apsw.Connection(
                str(sentinel.resolve()), flags=apsw.SQLITE_OPEN_READWRITE, vfs=vfs.name
            )
            original.execute("PRAGMA query_only=ON")
            # Keep room for the standard SQLite candidate connection and native helper.
            original.execute("PRAGMA cache_size=-131072")
            full_check(original, deadline)
            yield (
                original,
                {
                    "mode": "COMPRESSED_RAM",
                    "bytes": image.size,
                    "sha256": image.sha256,
                    "compressed_bytes": image.retained,
                    "full_integrity": "PASS",
                    "sqlite_version": apsw.sqlitelibversion(),
                },
            )
            guard(deadline)
            if image.reconstructed_hash() != expected:
                raise StateError("Decoded source changed during rebuild")
            image.assert_source_unchanged()
        finally:
            if original is not None:
                original.close()
            vfs.unregister()


def head_record(
    source: sqlite3.Connection, key: str, payload: str, *, verify_revision: bool = True
) -> tuple[str, dict[str, Any] | None]:
    if not isinstance(key, str) or not isinstance(payload, str):
        raise StateError("Invalid runtime record")
    relative = key.removeprefix(PREFIX)
    kind, separator, record_id = relative.partition("-head:")
    if not key.startswith(PREFIX) or not separator or kind not in HEAD_FIELDS:
        return payload, None
    codec = Codec(source)
    value = codec.read(payload)
    revision = value.get("revision") if isinstance(value, dict) else None
    if type(revision) is not int or revision < 1 or value.get(f"{kind}_id") != record_id:
        raise StateError("Invalid legacy head identity")
    reference = f"{kind}:{record_id}:{revision:010d}"
    if not verify_revision and value.get("schema") != HEAD_SCHEMA:
        # The complete source value enters the logical manifest. Candidate head
        # dereferencing must reproduce it, so conflicting/missing revisions fail
        # the mandatory full readback without random source lookups per old head.
        return encode(History._head(kind, value, reference)), value
    row = source.execute("SELECT payload_json FROM runtime WHERE key=?", (PREFIX + reference,)).fetchone()
    if row is None:
        raise StateError("Legacy head revision missing")
    record = codec.read(row[0])
    if (
        not isinstance(record, dict)
        or record.get(f"{kind}_id") != record_id
        or record.get("revision") != revision
        or record.get("schema") == HEAD_SCHEMA
    ):
        raise StateError("Legacy head revision identity mismatch")
    compact = History._head(kind, record, reference)
    if value.get("schema") == HEAD_SCHEMA:
        if encode(value) != encode(compact):
            raise StateError("Compact head conflicts with immutable revision")
        return payload, record
    if encode(value) != encode(record):
        raise StateError("Legacy head conflicts with immutable revision")
    return encode(compact), record


def feed(manifest: Any, values: tuple[Any, ...]) -> None:
    # Strings (including canonical public JSON) are length framed directly, rather
    # than escaped into a second large JSON document. The type tag keeps a numeric
    # field distinct from the same text; JSON retains int/float distinctions.
    parts = [len(values).to_bytes(8, "big")]
    size = 8
    for value in values:
        raw = value.encode() if isinstance(value, str) else encode(value).encode()
        prefix = (b"s" if isinstance(value, str) else b"j") + len(raw).to_bytes(8, "big")
        if len(raw) > 4096:
            # Large public windows stay streamed rather than copied into a new buffer.
            parts.append(prefix)
            manifest.update(b"".join(parts))
            manifest.update(raw)
            parts, size = [], 0
        else:
            parts.append(prefix + raw)
            size += len(prefix) + len(raw)
            if size >= 65536:
                manifest.update(b"".join(parts))
                parts, size = [], 0
    if parts:
        manifest.update(b"".join(parts))


def canonical_head(
    source: sqlite3.Connection, key: str, payload: str, accelerator: Accelerator | None = None
) -> tuple[str, str] | None:
    """Extract a legacy projection in JSON1, retaining full canonical source evidence."""
    kind, separator, record_id = key.removeprefix(PREFIX).partition("-head:")
    if not key.startswith(PREFIX) or not separator or kind not in HEAD_FIELDS or MARKER in payload:
        return None
    if accelerator is not None:
        values = accelerator.head_metadata(payload, kind)
    else:
        fields = (*HEAD_FIELDS[kind], "schema")
        rows = source.execute(
            "SELECT key,CASE WHEN type='real' THEN ? -> fullkey ELSE value END,type "
            "FROM json_each(?) WHERE key IN (" + ",".join("?" for _ in fields) + ")",
            (payload, payload, *fields),
        ).fetchall()
        if any(type_ == "integer" and type(value) is not int for _, value, type_ in rows):
            return None
        values = {name: sql_value(value, type_) for name, value, type_ in rows}
    if values.get("schema") == HEAD_SCHEMA:
        return None
    revision = values.get("revision")
    if type(revision) is not int or revision < 1 or values.get(f"{kind}_id") != record_id:
        raise StateError("Invalid legacy head identity")
    return encode(History._head(kind, values, f"{kind}:{record_id}:{revision:010d}")), payload


def copy_snapshot(
    original: sqlite3.Connection,
    destination: Path,
    owner: int,
    maximum: int,
    deadline: float,
    *,
    backend: str = "auto",
    source_copied: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Copy every row from an already pinned source into an exclusive candidate."""
    counts: dict[str, int] = {}
    savings = converted = 0
    accelerator = load(backend)
    manifests = {table: hashlib.sha256() for table in TABLES}
    original.execute("PRAGMA query_only=ON")
    original.execute("PRAGMA cache_size=-131072")
    release_source_cache(original, deadline)
    if original.execute("PRAGMA user_version").fetchone()[0] != 1:
        raise StateError("Unsupported source schema")
    objects = original.execute(
        "SELECT type,name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"
    ).fetchall()
    if set(objects) != {("table", table) for table in TABLES}:
        raise StateError("Unsupported source objects; source preserved")
    with closing(sqlite3.connect(":memory:")) as reference_schema:
        create_schema(reference_schema)
        canonical = dict(reference_schema.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
    actual_schema = dict(original.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
    for table, columns in TABLES.items():
        if tuple(row[1] for row in original.execute(f"PRAGMA table_info({table})")) != columns:
            raise StateError("Unsupported source columns")
        if " ".join(actual_schema[table].lower().split()) != " ".join(canonical[table].lower().split()):
            raise StateError("Source constraints or collation differ from canonical schema")
    owners = original.execute("SELECT owner_id FROM owner_settings").fetchall()
    if owners != [(owner,)]:
        raise StateError("Source owner mismatch")
    # Only the exclusive new candidate is writable. Source is never opened by Store.
    # Its schema is created separately so malformed source constraints cannot carry over.
    with closing(sqlite3.connect(destination)) as schema:
        create_schema(schema)
    candidate = Store(destination, owner)
    try:
        candidate.db.execute("PRAGMA journal_mode=DELETE")
        candidate.db.execute("PRAGMA cache_size=-262144")
        source_codec = Codec(original)
        candidate_codec = _CopyCodec(candidate.db, accelerator)
        page_size = candidate.db.execute("PRAGMA page_size").fetchone()[0]
        allowed_pages = candidate.db.execute(f"PRAGMA max_page_count={maximum // page_size}").fetchone()[0]
        if allowed_pages * page_size > maximum:
            raise StateError("Candidate schema exceeds byte limit")
        with candidate.db:
            for table, columns in TABLES.items():
                count = 0

                def transformed_rows() -> Iterator[tuple[Any, ...]]:
                    nonlocal count, converted, savings
                    for row in original.execute(f"SELECT * FROM {table} ORDER BY rowid"):
                        guard(deadline)
                        if count % 1000 == 0 and shutil.disk_usage(destination.parent).free < DISK_RESERVE:
                            raise StateError("Disk reserve exhausted; source preserved")
                        values = tuple(row)
                        if table == "runtime":
                            fast = canonical_head(original, values[0], values[1], accelerator)
                            source_public: str | None
                            if fast is not None:
                                payload, source_public = fast
                            else:
                                payload, public = head_record(
                                    original, values[0], values[1], verify_revision=False
                                )
                                source_public = encode(public) if public is not None else None
                            if source_public is not None and payload != values[1]:
                                converted += 1
                                savings += len(values[1].encode()) - len(payload.encode())
                            kind, _, _ = values[0].removeprefix(PREFIX).partition(":")
                            logical = (values[0], source_public) if source_public is not None else values
                            if values[0].startswith(PREFIX) and kind in HEAD_FIELDS:
                                normalized = (
                                    source_codec.canonical_json(json.loads(payload)).decode()
                                    if MARKER in payload
                                    else payload
                                )
                                logical = (values[0], normalized)
                                payload = encode(candidate_codec.pack_canonical(normalized))
                            if not values[0].startswith(BLOB_PREFIX):
                                feed(manifests[table], logical)
                            values = (values[0], payload)
                        else:
                            feed(manifests[table], values)
                        if table == "runtime" and values[0].startswith(BLOB_PREFIX):
                            # Earlier revisions can already have admitted this body.
                            expected_body = Codec(original).load(values[0][len(BLOB_PREFIX) :])
                            if candidate.get_runtime(values[0]) is not None and encode(
                                Codec(candidate.db).load(values[0][len(BLOB_PREFIX) :])
                            ) != encode(expected_body):
                                raise StateError("Conflicting preserved shared body")
                            candidate.db.execute("INSERT OR REPLACE INTO runtime VALUES (?,?)", values)
                        else:
                            yield values
                        count += 1

                # One prepared insert consumes rows lazily: source order, per-row
                # checks and the transaction are retained without a growing buffer.
                candidate.db.executemany(
                    f"INSERT INTO {table} VALUES ({','.join('?' for _ in columns)})", transformed_rows()
                )
                counts[table] = count
        source_shared = original.execute(
            "SELECT COUNT(*) FROM runtime WHERE key>=? AND key<?", (BLOB_PREFIX, BLOB_PREFIX + "~")
        ).fetchone()[0]
        release_source_cache(original, deadline)
        if source_copied is not None:
            # The source owner completes its unchanged-byte proof and closes it.
            # Only fixed manifests/counts are needed by the candidate readback.
            source_copied()
            trim_unused_memory()
            configure_candidate_cache(candidate.db, deadline)
        check_integrity(candidate.db, deadline=deadline)
        candidate.snapshot()  # Current owner settings must be readable without resetting defaults.
        # This exclusive candidate is frozen before evidence is reused during readback.
        candidate.db.execute("PRAGMA query_only=ON")
        history = History(candidate)
        history.codec = _CopyCodec(candidate.db, accelerator)
        readback_codec = history.codec
        resolved: OrderedDict[str, tuple[str, str]] = OrderedDict()
        resolved_bytes = 0
        for table in TABLES:
            actual = candidate.db.execute(f"SELECT * FROM {table} ORDER BY rowid")
            verified = 0
            readback = hashlib.sha256()
            for row in actual:
                guard(deadline)
                values = tuple(row)
                if table == "runtime":
                    if values[0].startswith(BLOB_PREFIX):
                        # load_raw already verifies encoding, bounds, SHA and
                        # canonical JSON. No decoded object is consumed here.
                        readback_codec.load_raw(values[0][len(BLOB_PREFIX) :])
                        continue
                    kind = values[0].removeprefix(PREFIX).partition(":")[0]
                    head = values[0].removeprefix(PREFIX).partition("-head:")[0]
                    if values[0].startswith(PREFIX) and (kind in HEAD_FIELDS or head in HEAD_FIELDS):
                        relative = values[0][len(PREFIX) :]
                        stored = json.loads(values[1])
                        if not isinstance(stored, dict):
                            raise StateError("Invalid public history record")
                        reference = stored.get("revision_key")
                        cached = (
                            resolved.get(reference)
                            if head in HEAD_FIELDS and isinstance(reference, str)
                            else None
                        )
                        if cached is not None:
                            if (
                                relative != f"{head}-head:{stored.get(f'{head}_id')}"
                                or encode(stored) != cached[1]
                            ):
                                raise StateError("Conflicting cached head projection")
                            public_payload = cached[0]
                        else:
                            if kind in HEAD_FIELDS:
                                if stored.get("schema") == HEAD_SCHEMA:
                                    raise StateError("Immutable revision contains head reference")
                                public_payload = readback_codec.canonical_json(stored).decode()
                                projected = History._head(kind, stored, relative)
                                shared_fields = stored.get(MARKER, {}).get("fields", {})
                                if set(shared_fields).intersection(HEAD_FIELDS[kind]):
                                    projected = History._head(kind, readback_codec.unpack(stored), relative)
                                projection = encode(readback_codec.unpack(projected))
                                charge = len(public_payload.encode()) + len(projection.encode()) + 256
                                if charge <= 4 * 1024 * 1024:
                                    while resolved and resolved_bytes + charge > 4 * 1024 * 1024:
                                        _, (previous, cached_projection) = resolved.popitem(last=False)
                                        resolved_bytes -= (
                                            len(previous.encode()) + len(cached_projection.encode()) + 256
                                        )
                                    resolved[relative] = (public_payload, projection)
                                    resolved_bytes += charge
                            else:
                                history.resolve(relative, stored)  # Validate head identity/projection.
                                row = candidate.db.execute(
                                    "SELECT payload_json FROM runtime WHERE key=?",
                                    (PREFIX + stored["revision_key"],),
                                ).fetchone()
                                public_payload = readback_codec.canonical_json(json.loads(row[0])).decode()
                        values = (values[0], public_payload)
                feed(readback, values)
                verified += 1
            if readback.digest() != manifests[table].digest():
                raise StateError("Candidate history conflicts with source rows")
            if table != "runtime" and verified != counts[table]:
                raise StateError("Candidate row count mismatch")
        added = candidate.db.execute("SELECT COUNT(*) FROM runtime").fetchone()[0] - counts["runtime"]
        if added < 0:
            raise StateError("Candidate lost runtime records")
        candidate_shared = candidate.db.execute(
            "SELECT COUNT(*) FROM runtime WHERE key>=? AND key<?", (BLOB_PREFIX, BLOB_PREFIX + "~")
        ).fetchone()[0]
        if added != candidate_shared - source_shared:
            raise StateError("Unexpected candidate runtime additions")
    finally:
        candidate.close()
    return {
        "conversion_backend": "native-v1" if accelerator is not None else "python",
        "table_rows": counts,
        "shared_bodies_added": added,
        "heads_converted": converted,
        "payload_bytes_saved": savings,
        "logical_manifest_schema": "cryptowatcher.logical-rows.v2",
        "logical_row_manifests": {table: manifest.hexdigest() for table, manifest in manifests.items()},
    }


def rebuild(
    source: Path,
    destination: Path,
    owner: int,
    expected: str,
    deadline: float,
    *,
    max_candidate_bytes: int | None = None,
    backend: str = "auto",
    source_encoding: str = "sqlite",
) -> dict[str, Any]:
    if not math.isfinite(deadline) or deadline > time.monotonic() + 900:
        raise StateError("Invalid rebuild deadline")
    guard(deadline)
    if source_encoding not in ("sqlite", "gzip", "zstd"):
        raise StateError("Unknown source encoding")
    if source_encoding != "sqlite" and max_candidate_bytes is None:
        raise StateError("Archived source requires explicit candidate byte limit")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise StateError("Expected qualified source SHA256 required")
    if source.resolve() == destination.resolve() or destination.exists():
        raise StateError("Destination must be a new isolated file")
    closed(source)
    closed(destination)
    if not source.is_file() or not destination.parent.is_dir():
        raise StateError("Source or destination directory unavailable")
    source_identity = source.stat()
    if os.name != "nt":
        parent = destination.parent.stat()
        if parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
            raise StateError("Destination directory must be private and owned")
    maximum = source.stat().st_size if max_candidate_bytes is None else max_candidate_bytes
    if type(maximum) is not int or maximum < 32768:
        raise StateError("Invalid candidate byte limit")
    if shutil.disk_usage(destination.parent).free < maximum + DISK_RESERVE:
        raise StateError("Insufficient disk for bounded isolated candidate")
    source_hash = fingerprint(source, deadline)
    if source_encoding == "sqlite" and source_hash != expected:
        raise StateError("Source fingerprint mismatch")
    destination.touch(mode=0o600, exist_ok=False)
    source_proof = None
    if source_encoding == "sqlite":
        with ExitStack() as source_scope:
            original = source_scope.enter_context(
                closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro&immutable=1", uri=True))
            )
            copied = copy_snapshot(
                original,
                destination,
                owner,
                maximum,
                deadline,
                backend=backend,
                source_copied=source_scope.close,
            )
    else:
        with ExitStack() as source_scope:
            archived, source_proof = source_scope.enter_context(
                archive_source(source, destination.parent, expected, deadline, source_encoding)
            )
            copied = copy_snapshot(
                archived,
                destination,
                owner,
                maximum,
                deadline,
                backend=backend,
                source_copied=source_scope.close,
            )
    if fingerprint(source, deadline) != source_hash:
        raise StateError("Source changed; candidate not qualified")
    after = source.stat()
    if (
        source_identity.st_dev,
        source_identity.st_ino,
        source_identity.st_size,
        source_identity.st_mtime_ns,
    ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise StateError("Source metadata changed; candidate not qualified")
    candidate_hash = fingerprint(destination, deadline)
    revision = os.environ.get("SOURCE_REVISION", "UNSET")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        revision = "UNSET"
    return {
        "status": "ISOLATED_CANDIDATE_PASS",
        "at_utc": datetime.now(timezone.utc).isoformat(),
        "tool_source_revision": revision,
        "source_sha256": expected,
        "source_bytes": source_proof["bytes"] if source_proof else source_identity.st_size,
        "candidate_sha256": candidate_hash,
        "candidate_bytes": destination.stat().st_size,
        "max_candidate_bytes": maximum,
        **copied,
        "candidate_full_integrity": "PASS",
        "immutable_rows_and_resolved_heads": "PASS",
        "source_full_qualification": "PASS" if source_proof else "REQUIRED_SEPARATELY_FOR_EXPECTED_HASH",
        **(
            {
                "source_archive_sha256": source_hash,
                "source_archive_bytes": source_identity.st_size,
                "source_encoding": source_encoding,
                "source_validation": source_proof,
            }
            if source_proof
            else {}
        ),
        "production_migration": "NOT_RUN",
        "incomplete_output": "PRESERVED_ON_FAILURE",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--owner-id", type=int, required=True)
    parser.add_argument("--qualified-source-sha256", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=900)
    parser.add_argument("--max-candidate-bytes", type=int)
    parser.add_argument("--backend", choices=("auto", "python", "native"), default="auto")
    parser.add_argument("--source-encoding", choices=("sqlite", "gzip", "zstd"), default="sqlite")
    args = parser.parse_args()
    try:
        if not math.isfinite(args.timeout_seconds) or not 0 < args.timeout_seconds <= 900:
            raise StateError("Invalid rebuild budget")
        print(
            json.dumps(
                rebuild(
                    args.source,
                    args.destination,
                    args.owner_id,
                    args.qualified_source_sha256,
                    time.monotonic() + args.timeout_seconds,
                    max_candidate_bytes=args.max_candidate_bytes,
                    backend=args.backend,
                    source_encoding=args.source_encoding,
                )
            )
        )
        return 0
    except IntegrityError as exc:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "failure_code": exc.code,
                    "production_migration": "NOT_RUN",
                    "candidate": "UNQUALIFIED",
                }
            )
        )
        return exc.exit_status
    except (StateError, sqlite3.DatabaseError, ValueError, OSError) as exc:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "failure_code": refusal_code(exc),
                    "production_migration": "NOT_RUN",
                    "candidate": "UNQUALIFIED",
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
