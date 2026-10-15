"""Copy a fully validated frozen store without reserializing large JSON windows."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing
from itertools import zip_longest
from pathlib import Path
from typing import Any, Literal

from .compressed_validation import archived_image, snapshot_image
from .copy_checkpoint import Checkpoint, binding, initial_state
from .events import HEAD_FIELDS, HEAD_SCHEMA, PREFIX, History
from .frame_archive import manifest, save_index
from .frame_archive import verify as verify_recovery
from .history_codec import BLOB_PREFIX, MARKER, MAX_BYTES
from .history_rebuild import (
    TABLES,
    _CopyCodec,
    configure_copy_cache,
    feed,
    guard,
    head_record,
    release_source_cache,
)
from .native_history import Accelerator, load
from .record_codec import pack, pack_candle, sql_payload
from .storage import (
    COMPRESSED_RECORD_VERSION,
    RECORD_FORMAT,
    RECORD_FORMAT_KEY,
    StateError,
    check_integrity,
    create_schema,
    encode,
    known_record_format,
)


def original_payload(payload: str) -> str:
    return sql_payload(payload)


def runtime_namespace(key: str) -> str:
    return (
        "history-body"
        if key.startswith(BLOB_PREFIX)
        else "history:" + key.removeprefix(PREFIX).partition(":")[0]
        if key.startswith(PREFIX)
        else "source-candle"
        if key.startswith("source:v1:candle:")
        else "other-runtime"
    )


def metadata(db: Any, payload: str, kind: str, accelerator: Accelerator | None = None) -> dict[str, Any]:
    if accelerator is not None:
        result = accelerator.head_metadata(payload, kind)
        if result.get("schema") == HEAD_SCHEMA:
            result["revision_key"] = db.execute(
                "SELECT json_extract(?,'$.revision_key')", (payload,)
            ).fetchone()[0]
        return result
    fields = (*HEAD_FIELDS[kind], "schema", "revision_key")
    marks = ",".join("?" for _ in fields)
    if db.execute("SELECT json_valid(?) AND json_type(?)='object'", (payload, payload)).fetchone()[0] != 1:
        raise StateError("Invalid immutable JSON record")
    rows = db.execute(
        "WITH d AS MATERIALIZED (SELECT jsonb(?) AS value) "
        "SELECT j.key,d.value -> j.fullkey FROM d,json_each(d.value) AS j "
        f"WHERE j.key IN ({marks}) ORDER BY j.id",
        (payload, *fields),
    )
    return {name: json.loads(raw) for name, raw in rows}


def collapsed_head(
    source: Any, key: str, payload: str, accelerator: Accelerator | None = None
) -> tuple[str, bool]:
    kind, _, identifier = key.removeprefix(PREFIX).partition("-head:")
    if not key.startswith(PREFIX) or kind not in HEAD_FIELDS or not identifier:
        return payload, False
    values = metadata(source, payload, kind, accelerator)
    version = values.get("revision")
    if type(version) is not int or version < 1 or values.get(kind + "_id") != identifier:
        raise StateError("Invalid legacy head identity")
    reference = f"{kind}:{identifier}:{version:010d}"
    row = source.execute("SELECT payload_json FROM runtime WHERE key=?", (PREFIX + reference,)).fetchone()
    if row is None:
        raise StateError("Legacy head revision missing")
    if values.get("schema") == HEAD_SCHEMA:
        original = original_payload(row[0])
        projection = metadata(source, original, kind, accelerator)
        if (
            values.get("revision_key") != reference
            or projection.get("schema") == HEAD_SCHEMA
            or projection.get(kind + "_id") != identifier
            or projection.get("revision") != version
            or encode(History._head(kind, projection, reference)) != encode(json.loads(payload))
        ):
            raise StateError("Existing head projection refused")
        return payload, False
    if original_payload(row[0]) == original_payload(payload):
        return encode(History._head(kind, values, reference)), True
    # Preserve uncommon noncanonical representations after complete semantic validation.
    head_record(source, key, payload, verify_revision=True)
    return payload, False


def copy_validated(
    source: Any,
    destination: Path,
    *,
    owner: int,
    maximum: int,
    deadline: float,
    progress: Any = lambda **kw: None,
    source_identity: str | None = None,
    resume: bool = False,
    checkpoint_rows: int = 10000,
) -> dict[str, Any]:
    """Copy a full-checked frozen source; optional checkpoints never qualify partial data."""
    guard(deadline)
    if type(checkpoint_rows) is not int or not 1 <= checkpoint_rows <= 10000:
        raise StateError("Invalid copy checkpoint batch")
    if resume and source_identity is None:
        raise StateError("Frozen source fingerprint required for resume")
    source_version = source.execute("PRAGMA user_version").fetchone()[0]
    if source_version not in (1, COMPRESSED_RECORD_VERSION):
        raise StateError("Unsupported source version")
    declaration = source.execute(
        "SELECT payload_json FROM runtime WHERE key=?", (RECORD_FORMAT_KEY,)
    ).fetchone()
    if source_version == COMPRESSED_RECORD_VERSION and (
        declaration is None or not known_record_format(declaration[0])
    ):
        raise StateError("Unknown source record format")
    if source_version == 1 and declaration is not None:
        raise StateError("Inconsistent source record format")
    if destination.is_symlink() or any(p.is_symlink() for p in destination.parents):
        raise StateError("Exclusive new candidate required")
    if destination.exists() and not resume:
        raise StateError("Exclusive new candidate required")
    tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    objects = {
        tuple(row)
        for row in source.execute("SELECT type,name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'")
    }
    if objects != {("table", name) for name in TABLES}:
        raise StateError("Unknown source objects refused")
    if tables != set(TABLES) or [
        tuple(row) for row in source.execute("SELECT owner_id FROM owner_settings")
    ] != [(owner,)]:
        raise StateError("Source schema or owner refused")
    if source.execute("PRAGMA query_only").fetchone()[0] != 1:
        raise StateError("Frozen read-only source required")
    for name, columns in TABLES.items():
        if tuple(r[1] for r in source.execute(f"PRAGMA table_info({name})")) != columns:
            raise StateError("Source columns refused")
    with sqlite3.connect(":memory:") as schema:
        create_schema(schema)
        definitions = dict(schema.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
    for name, sql in source.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"):
        if " ".join(sql.lower().split()) != " ".join(definitions[name].lower().split()):
            raise StateError("Source constraints or collation refused")
    checkpoint = (
        Checkpoint(destination, binding(source_identity, owner, maximum), resume=resume)
        if source_identity is not None
        else None
    )
    accelerator = load("auto")
    codec = _CopyCodec(source, accelerator)
    source.execute("PRAGMA cache_size=-131072")
    release_source_cache(source, deadline)
    state = initial_state()
    with closing(sqlite3.connect(destination)) as target:
        if os.name != "nt":
            destination.chmod(0o600)
        if not resume:
            create_schema(target)
        else:
            public = dict(target.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
            if any(
                name not in public or " ".join(public[name].lower().split()) != " ".join(sql.lower().split())
                for name, sql in definitions.items()
            ):
                raise StateError("Candidate schema refused")
        target.execute("PRAGMA journal_mode=DELETE")
        target.execute("PRAGMA synchronous=FULL")
        target.execute("PRAGMA temp_store=MEMORY")
        writer_cache_kib = min(configure_copy_cache(target, deadline), 512 * 1024)
        target.execute(f"PRAGMA cache_size=-{writer_cache_kib}")
        page_size = target.execute("PRAGMA page_size").fetchone()[0]
        if (
            target.execute(f"PRAGMA max_page_count={maximum // page_size}").fetchone()[0] * page_size
            > maximum
        ):
            raise StateError("Candidate capacity refused")
        if checkpoint is not None:
            if not resume:
                checkpoint.create(target)
            saved = checkpoint.read(target)
            if saved is None:
                state["table_index"] = len(TABLES)
            else:
                state = saved
        names = list(TABLES)
        for table_index in range(state["table_index"], len(names)):
            name = names[table_index]
            columns = TABLES[name]
            count = state["rows"]
            cursor = state["cursor"]
            if cursor is not None:
                last = source.execute(f"SELECT * FROM {name} WHERE rowid=?", (cursor,)).fetchone()
                tail = target.execute(
                    f"SELECT {columns[0]} FROM {name} ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
                if last is None or tail is None or last[0] != tail[0]:
                    raise StateError("Copy checkpoint cursor mismatch")
            elif count:
                raise StateError("Copy checkpoint cursor mismatch")
            if checkpoint is not None:
                checkpoint.begin(target, state)

            def rows() -> Iterator[tuple[Any, ...]]:
                nonlocal count
                batch_rows = batch_bytes = 0
                query = f"SELECT rowid,* FROM {name}"
                args = () if cursor is None else (cursor,)
                if cursor is not None:
                    query += " WHERE rowid>?"
                for row in source.execute(query + " ORDER BY rowid", args):
                    guard(deadline)
                    if count % 1000 == 0 and shutil.disk_usage(destination.parent).free < 1536 * 1024**2:
                        raise StateError("Disk reserve exhausted; source preserved")
                    rowid, *fields = row
                    values = tuple(fields)
                    if name == "runtime":
                        key, payload = values
                        if key == RECORD_FORMAT_KEY:
                            continue
                        if not isinstance(key, str) or not isinstance(payload, str):
                            raise StateError("Runtime record types refused")
                        namespace = runtime_namespace(key)
                        state["runtime_counts"][namespace] = state["runtime_counts"].get(namespace, 0) + 1
                        state["runtime_payload_chars"][namespace] = state["runtime_payload_chars"].get(
                            namespace, 0
                        ) + len(payload)
                        original = original_payload(payload)
                        rewritten, changed = collapsed_head(source, key, original, accelerator)
                        state["heads"] += int(changed)
                        kind = key.removeprefix(PREFIX).partition(":")[0]
                        if key.startswith("source:v1:candle:"):
                            compact = pack_candle(original)
                            if len(compact.encode()) < len(original.encode()):
                                rewritten = compact
                                state["candles"] += 1
                        if key.startswith(BLOB_PREFIX):
                            codec.validate_raw(key[len(BLOB_PREFIX) :], payload)
                        elif key.startswith(PREFIX) and kind in HEAD_FIELDS:
                            values_meta = metadata(source, original, kind, accelerator)
                            revision = values_meta.get("revision")
                            if (
                                type(revision) is not int
                                or revision < 1
                                or key != PREFIX + f"{kind}:{values_meta.get(kind + '_id')}:{revision:010d}"
                            ):
                                raise StateError("Immutable record identity refused")
                            if MARKER in original:
                                codec.canonical_json(json.loads(original))
                            elif 1024 <= len(original.encode()) <= MAX_BYTES:
                                encoded = pack(original)
                                if len(encoded.encode()) < len(original.encode()):
                                    rewritten = encoded
                                    state["framed"] += 1
                        values = (key, rewritten)
                    yield values
                    # executemany has inserted this row before asking for the next one.
                    count += 1
                    state.update(cursor=rowid, rows=count)
                    batch_rows += 1
                    batch_bytes += sum(len(v.encode()) for v in values if isinstance(v, str))
                    if checkpoint is not None and (
                        batch_rows >= checkpoint_rows or batch_bytes >= 16 * 1024**2
                    ):
                        checkpoint.save(target, state)
                        progress(
                            phase="COPY",
                            table=name,
                            rows=count,
                            durable=True,
                            namespace_counts=state["runtime_counts"],
                            source_payload_chars=state["runtime_payload_chars"],
                        )
                        checkpoint.begin(target, state)
                        batch_rows = batch_bytes = 0
                    elif checkpoint is None and count % 10000 == 0:
                        progress(phase="COPY", table=name, rows=count)

            target.executemany(f"INSERT INTO {name} VALUES ({','.join('?' for _ in columns)})", rows())
            state["counts"][name] = count
            state.update(table_index=table_index + 1, cursor=None, rows=0)
            if checkpoint is not None:
                checkpoint.save(target, state)
        release_source_cache(source, deadline)
        # Re-read every source row, including committed prefixes on resume. No saved
        # counter/hash can replace this comparison with the actual frozen source.
        counts: dict[str, int] = {}
        runtime_counts: dict[str, int] = {}
        runtime_payload_chars: dict[str, int] = {}
        framed = heads = candles = 0
        for name in TABLES:
            expected, actual = hashlib.sha256(), hashlib.sha256()
            checked = 0
            query = f"SELECT * FROM {name}"
            args: tuple[Any, ...] = ()
            if name == "runtime":
                query += " WHERE key!=?"
                args = (RECORD_FORMAT_KEY,)
            query += " ORDER BY rowid"
            for original, copied in zip_longest(source.execute(query, args), target.execute(query, args)):
                guard(deadline)
                if original is None or copied is None:
                    raise StateError("Complete source/candidate manifest mismatch")
                before, after = tuple(original), tuple(copied)
                if name == "runtime":
                    key, payload = before
                    if key != after[0]:
                        raise StateError("Complete source/candidate manifest mismatch")
                    namespace = runtime_namespace(key)
                    runtime_counts[namespace] = runtime_counts.get(namespace, 0) + 1
                    runtime_payload_chars[namespace] = runtime_payload_chars.get(namespace, 0) + len(payload)
                    raw = original_payload(payload)
                    restored = original_payload(after[1])
                    kind, _, identifier = key.removeprefix(PREFIX).partition("-head:")
                    if raw != restored and key.startswith(PREFIX) and kind in HEAD_FIELDS and identifier:
                        header = json.loads(restored)
                        if not isinstance(header, dict) or header.get("schema") != HEAD_SCHEMA:
                            raise StateError("Candidate head projection mismatch")
                        reference = header.get("revision_key")
                        if not isinstance(reference, str):
                            raise StateError("Candidate head revision missing")
                        record = target.execute(
                            "SELECT payload_json FROM runtime WHERE key=?", (PREFIX + str(reference),)
                        ).fetchone()
                        if record is None:
                            raise StateError("Candidate head revision missing")
                        restored = original_payload(record[0])
                        projection = metadata(target, restored, kind, accelerator)
                        if (
                            projection.get(kind + "_id") != identifier
                            or encode(History._head(kind, projection, reference)) != after[1]
                        ):
                            raise StateError("Candidate head projection mismatch")
                        heads += 1
                    framed += int(
                        key.startswith(PREFIX)
                        and key.removeprefix(PREFIX).partition(":")[0] in HEAD_FIELDS
                        and after[1].startswith('{"_history_record_storage":')
                    )
                    candles += int(
                        key.startswith("source:v1:candle:") and after[1].startswith('{"_cw_candle_v1":')
                    )
                    before, after = (key, raw), (key, restored)
                feed(expected, before)
                feed(actual, after)
                checked += 1
                if checked % 10000 == 0:
                    progress(phase="CANDIDATE_READBACK", table=name, rows=checked)
            if actual.digest() != expected.digest():
                raise StateError("Complete source/candidate manifest mismatch")
            counts[name] = checked
        final_version = COMPRESSED_RECORD_VERSION if framed or candles or source_version == 2 else 1
        if resume and saved is None:
            marker = target.execute(
                "SELECT payload_json FROM runtime WHERE key=?", (RECORD_FORMAT_KEY,)
            ).fetchone()
            if target.execute("PRAGMA user_version").fetchone()[0] != final_version or (
                (marker is None) if final_version == COMPRESSED_RECORD_VERSION else (marker is not None)
            ):
                raise StateError("Finalized copy version mismatch")
            if marker is not None and not known_record_format(marker[0]):
                raise StateError("Unknown finalized record format")
        else:
            if final_version == COMPRESSED_RECORD_VERSION:
                target.execute(
                    "INSERT OR REPLACE INTO runtime VALUES (?,?)", (RECORD_FORMAT_KEY, encode(RECORD_FORMAT))
                )
            target.execute(f"PRAGMA user_version={final_version}")
            if checkpoint is not None:
                checkpoint.finish(target)
            else:
                target.commit()
        progress(phase="CANDIDATE_FULL_INTEGRITY", bytes=destination.stat().st_size)
        check_integrity(target, deadline=deadline)
    guard(deadline)
    candidate_digest = hashlib.sha256()
    with destination.open("rb") as encoded_candidate:
        for chunk in iter(lambda: encoded_candidate.read(1024 * 1024), b""):
            guard(deadline)
            candidate_digest.update(chunk)
    guard(deadline)
    return {
        "state": "CANDIDATE_ROWS_VERIFIED",
        "counts": counts,
        "runtime_counts": runtime_counts,
        "runtime_payload_chars": runtime_payload_chars,
        "framed_records": framed,
        "collapsed_heads": heads,
        "compact_candles": candles,
        "candidate_bytes": destination.stat().st_size,
        "candidate_sha256": candidate_digest.hexdigest(),
        "source_schema_integrity": "CALLER_REQUIRED",
        "source_history_deleted": 0,
        "copy_resumed": resume,
    }


def build_candidate(
    source: Path,
    directory: Path,
    *,
    owner: int,
    encoded_limit: int,
    maximum: int,
    seconds: float = 900,
    archive_encoding: Literal["GZIP", "ZSTD"] | None = None,
    closed_source: bool = False,
    one_time_maintenance: bool = False,
    progress: Any = lambda **kw: None,
) -> dict[str, Any]:
    """One bounded read-only qualification; never replaces or edits production."""
    started = time.monotonic()
    from .maintenance import ONE_TIME_SECONDS

    limit = ONE_TIME_SECONDS if one_time_maintenance else 900
    if not 0 < seconds <= limit:
        raise StateError(f"Whole conversion budget must not exceed{limit}s")
    if one_time_maintenance and (not closed_source or archive_encoding is not None):
        raise StateError("One-time budget requires closed-source maintenance")
    deadline = started + seconds
    if source.resolve() != source or source.is_symlink() or not source.is_file():
        raise StateError("Source path refused")
    if directory.resolve() != directory or directory.is_symlink() or not directory.is_dir():
        raise StateError("Private qualification directory required")
    candidate = directory / "candidate.sqlite3"
    backing = directory / "source.frames"
    if any(
        path.exists()
        for path in (
            candidate,
            candidate.with_name(candidate.name + ".copy.json"),
            backing,
            directory / "source.index",
            directory / "source.recovery.json",
            directory / "qualification.json",
        )
    ):
        raise StateError("Fresh qualification directory required")
    if type(encoded_limit) is not int or type(maximum) is not int or min(encoded_limit, maximum) <= 0:
        raise StateError("Explicit allocation bounds required")
    if shutil.disk_usage(directory).free < encoded_limit + maximum + 1536 * 1024**2:
        raise StateError("Disk reserve exhausted; source preserved")
    if archive_encoding not in (None, "GZIP", "ZSTD"):
        raise StateError("Explicit archive encoding required")
    capture = (
        snapshot_image(
            source,
            directory,
            deadline=deadline,
            backing=backing,
            encoded_limit=encoded_limit,
            progress=progress,
        )
        if archive_encoding is None and not closed_source
        else archived_image(
            source,
            directory,
            deadline=deadline,
            backing=backing,
            encoded_limit=encoded_limit,
            encoding=archive_encoding,
            progress=progress,
        )
    )
    with capture as (validated, image):
        # Persist the frozen recovery identity before any copy progress is committed.
        descriptor = save_index(image, directory, deadline=deadline)
        report = copy_validated(
            validated,
            candidate,
            owner=owner,
            maximum=maximum,
            deadline=deadline,
            progress=progress,
            source_identity=manifest(descriptor)["raw_sha256"],
        )
        progress(phase="RECOVERY_VERIFY", bytes=image.size)
        recovery = verify_recovery(descriptor, deadline=deadline)
        report.update(source_full_integrity="PASS", recovery=recovery, recovery_descriptor=str(descriptor))
    guard(deadline)
    report.update(
        state="QUALIFIED_SNAPSHOT_CANDIDATE",
        elapsed_seconds=round(time.monotonic() - started, 3),
        production_replacement=False,
        snapshot_not_current_state_after_capture=True,
        one_time_maintenance=one_time_maintenance,
        whole_budget_seconds=seconds,
    )
    receipt = directory / "qualification.json"
    with receipt.open("x", encoding="utf-8") as output:
        os.chmod(receipt, 0o600)
        output.write(encode(report))
        output.flush()
        os.fsync(output.fileno())
    guard(deadline)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Qualify a smaller private history copy; never replace production"
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--owner", type=int, required=True)
    parser.add_argument("--encoded-max-bytes", type=int, required=True)
    parser.add_argument("--candidate-max-bytes", type=int, required=True)
    parser.add_argument("--seconds", type=float, default=900)
    parser.add_argument("--archive-encoding", choices=("GZIP", "ZSTD"))
    parser.add_argument("--closed-source", action="store_true")
    parser.add_argument("--one-time-maintenance", action="store_true")
    args = parser.parse_args()
    last = time.monotonic()

    def progress(**fields: Any) -> None:
        nonlocal last
        current = time.monotonic()
        if current - last >= 20:
            print(encode({"kind": "qualification_progress", **fields}), flush=True)
            last = current

    try:
        result = build_candidate(
            args.source.resolve(),
            args.directory.resolve(),
            owner=args.owner,
            encoded_limit=args.encoded_max_bytes,
            maximum=args.candidate_max_bytes,
            seconds=args.seconds,
            archive_encoding=args.archive_encoding,
            closed_source=args.closed_source,
            one_time_maintenance=args.one_time_maintenance,
            progress=progress,
        )
    except (StateError, OSError, ValueError, sqlite3.Error) as error:
        from .history_rebuild import refusal_code

        print(
            encode({"state": "UNQUALIFIED", "reason": refusal_code(error), "production_replacement": False}),
            flush=True,
        )
        return 3
    print(encode(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
