"""Single-writer SQLite state and non-destructive legacy import."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .config import ConfigError, OwnerSettings, instrument_key, normalize_pair, number

SCHEMA_VERSION = 1
RUNTIME_UPSERT = """
INSERT INTO runtime (key, payload_json) VALUES (?, ?)
ON CONFLICT(key) DO UPDATE SET payload_json=excluded.payload_json
WHERE runtime.payload_json <> excluded.payload_json
"""
VALIDATION_CACHE_KIB = 131072
VALIDATION_MMAP_BYTES = 2147483648
VALIDATION_PROGRESS_STEPS = 10000


class StateError(RuntimeError):
    pass


class IntegrityError(StateError):
    """A safe, machine-readable failure without database contents or exception text."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.backup_metrics: dict[str, float | str] | None = None
        self.exit_status = {
            "TIME_BUDGET_EXCEEDED": 3,
            "DATABASE_CORRUPT": 4,
            "INTERRUPTED": 5,
            "IO_ERROR": 6,
            "DATABASE_ERROR": 7,
        }.get(code, 2)


def check_integrity(database: sqlite3.Connection, *, deadline: float) -> None:
    """Validate the whole database within the caller's shared operation budget."""
    if time.monotonic() >= deadline:
        raise IntegrityError("TIME_BUDGET_EXCEEDED", "Integrity time budget exceeded")
    expired = False
    previous_cache: int | None = None
    previous_mmap: int | None = None
    validation_failed = False

    def progress() -> int:
        nonlocal expired
        expired = time.monotonic() >= deadline
        return int(expired)

    database.set_progress_handler(progress, VALIDATION_PROGRESS_STEPS)
    try:
        from .owner_slots import install_functions as owner_functions

        owner_functions(database)
        from .catalogue_overlap import install_check as overlap_check

        overlap_check(database)
        from .shared_records import sql_signature as shared_signature

        database.create_function("cw_shared_digest", 5, shared_signature, deterministic=True)
        previous_cache = int(database.execute("PRAGMA cache_size").fetchone()[0])
        mmap_row = database.execute("PRAGMA mmap_size").fetchone()
        previous_mmap = int(mmap_row[0]) if mmap_row is not None else None
        # Dedicated validation connections only; these suggestions never change the file schema.
        database.execute(f"PRAGMA cache_size=-{VALIDATION_CACHE_KIB}")
        if previous_mmap is not None:
            database.execute(f"PRAGMA mmap_size={VALIDATION_MMAP_BYTES}")
        rows = database.execute("PRAGMA integrity_check").fetchall()
        from .record_catalog import valid_heads

        if len(rows) == 1 and rows[0][0] == "ok" and not valid_heads(database):
            rows = [("Conflicting current records",)]
    except sqlite3.DatabaseError as exc:
        validation_failed = True
        code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
        if code == sqlite3.SQLITE_INTERRUPT:
            if expired:
                raise IntegrityError("TIME_BUDGET_EXCEEDED", "Integrity time budget exceeded") from exc
            raise IntegrityError("INTERRUPTED", "Integrity validation interrupted") from exc
        if code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
            raise IntegrityError("DATABASE_CORRUPT", "Database integrity check failed") from exc
        if code == sqlite3.SQLITE_IOERR:
            raise IntegrityError("IO_ERROR", "Integrity validation encountered an I/O error") from exc
        raise IntegrityError("DATABASE_ERROR", "Integrity validation did not complete") from exc
    finally:
        database.set_progress_handler(None, 0)
        try:
            if previous_mmap is not None:
                database.execute(f"PRAGMA mmap_size={previous_mmap}")
            if previous_cache is not None:
                database.execute(f"PRAGMA cache_size={previous_cache}")
        except sqlite3.DatabaseError as exc:
            # Preserve the classified primary failure; callers close dedicated connections.
            if not validation_failed:
                raise IntegrityError("DATABASE_ERROR", "Validation connection cleanup failed") from exc
    if time.monotonic() >= deadline:
        raise IntegrityError("TIME_BUDGET_EXCEEDED", "Integrity time budget exceeded")
    if len(rows) != 1 or rows[0][0] != "ok":
        raise IntegrityError("DATABASE_CORRUPT", "Database integrity check failed")


class RevisionConflict(StateError):
    pass


def json_payload(payload: Any) -> Any:
    from .record_codec import json_value

    return json_value(payload)


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _reject_constant(value: str) -> None:
    raise StateError("Non-finite legacy JSON")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StateError("Duplicate legacy JSON key")
        result[key] = value
    return result


def read_legacy(path: Path) -> tuple[dict[str, Any], bytes, str]:
    try:
        content = path.read_bytes()
        raw = json.loads(content, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
    except (OSError, ValueError, UnicodeError) as exc:
        raise StateError("Cannot read legacy state; original preserved") from exc
    if not isinstance(raw, dict):
        raise StateError("Invalid legacy state root")
    version = raw.get("version")
    if isinstance(version, bool) or version not in (None, 1, 2):
        raise StateError("Unsupported legacy schema; original preserved")
    if version == 2 and not isinstance(raw.get("chats"), dict):
        raise StateError("Invalid legacy chats")
    if version != 2 and not any(key in raw for key in ("chat_id", "settings", "baselines", "chats")):
        raise StateError("Unrecognized legacy state")
    return raw, content, hashlib.sha256(content).hexdigest()


def _legacy_settings(raw: dict[str, Any], defaults: OwnerSettings) -> OwnerSettings:
    values = defaults.payload()
    for field in OwnerSettings.__dataclass_fields__:
        # Legacy "watchlist" is an untyped alias, not the new market-identity field.
        if field in raw and field not in ("watchlist", "watchlist_multiplier"):
            values[field] = raw[field]
    if "custom_pairs" in raw:
        pairs = raw["custom_pairs"]
    else:
        pairs = raw.get("bybit_pairs") or raw.get("watchlist") or []
    if not isinstance(pairs, list) or any(not isinstance(x, str) for x in pairs):
        raise StateError("Invalid legacy custom list")
    values["custom_pairs"] = tuple(dict.fromkeys(normalize_pair(x) for x in pairs))
    if "alert_universe_mode" not in raw:
        limit = raw.get("bybit_top_limit", raw.get("cmc_top_limit"))
        if limit is not None:
            try:
                if isinstance(limit, bool) or not float(limit).is_integer() or float(limit) < 0:
                    raise ValueError
                values["alert_universe_mode"] = "top" if int(limit) > 0 else "custom"
            except (ValueError, TypeError, OverflowError) as exc:
                raise StateError("Invalid legacy universe mode") from exc
        else:
            values["alert_universe_mode"] = "custom" if pairs else "top"
    # The legacy contract explicitly uses 5m when persisted TF is absent.
    values["signal_timeframe"] = raw.get("signal_timeframe", "5m")
    try:
        return OwnerSettings.from_payload(values)
    except ConfigError as exc:
        raise StateError(str(exc)) from exc


@dataclass(frozen=True)
class Snapshot:
    revision: int
    settings: OwnerSettings


def create_schema(database: sqlite3.Connection) -> None:
    """Canonical schema for a new store or an exclusive isolated recovery candidate."""
    database.executescript("""
        CREATE TABLE IF NOT EXISTS owner_settings (
            owner_id INTEGER PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision>0),
            settings_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS delivery_preferences (
            chat_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)));
        CREATE TABLE IF NOT EXISTS mutes (
            symbol TEXT PRIMARY KEY, mute_until REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS runtime (
            key TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS legacy_import (
            source_sha256 TEXT PRIMARY KEY, original_json TEXT NOT NULL,
            backup_name TEXT NOT NULL);
        PRAGMA user_version=1;
    """)


class Store:
    def __init__(self, path: Path, owner_id: int, *, readonly: bool = False) -> None:
        number(owner_id, "owner_id", integer=True)
        self.path = path
        self.owner_id = owner_id
        self.readonly = readonly
        # One population, scoped to a single fixed read transaction. Never used
        # by the writer or carried across snapshots.
        self.comparison_population: tuple[Any, Any] | None = None
        self.analytics_ranges: dict[tuple[str, int, int, int], tuple[tuple[Any, ...], int]] = {}
        self.analytics_range_bytes = 0
        self.reference_cache: dict[tuple[str, str, str | bytes], tuple[str | bytes, int]] = {}
        self.reference_cache_bytes = 0
        self.clock_ms = lambda: int(time.time() * 1000)
        if path.exists():
            probe = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
            try:
                version = probe.execute("PRAGMA user_version").fetchone()[0]
                if version not in (SCHEMA_VERSION, 2):
                    raise StateError("Unsupported database schema; database preserved")
                binding = probe.execute("SELECT owner_id FROM owner_settings").fetchone()
                if binding is not None and binding[0] != owner_id:
                    raise StateError("Database owner mismatch")
            except sqlite3.DatabaseError as exc:
                raise StateError("Invalid database; database preserved") from exc
            finally:
                probe.close()
        elif readonly:
            raise StateError("Database missing")
        else:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro" if readonly else str(path),
            uri=readonly,
            timeout=5,
        )
        from .key_registry import install_functions as registry_functions

        registry_functions(self.db, self._cached_value)
        from .cohort_meter import install_functions as cohort_functions

        cohort_functions(self.db)

        self.db.create_function("cw_record_json", 1, self.cached_record_json, deterministic=True)
        self.db.create_function("cw_clock_ms", 0, lambda: int(self.clock_ms()))
        from .catalogue_sql import install_functions as catalogue_sql_functions

        catalogue_sql_functions(self)
        from .source_windows import references

        self.db.create_function(
            "cw_window_refs",
            2,
            lambda key, raw: self.cached_references("windows", key, raw, references),
            deterministic=True,
        )
        from .runtime_meter import namespace

        self.db.create_function("cw_namespace", 1, namespace, deterministic=True)
        from .numeric_pool import install_functions as numeric_functions
        from .numeric_pool import projection
        from .numeric_pool import references as numeric_references

        numeric_functions(self.db)
        from .owner_slots import install_functions as owner_functions

        owner_functions(self.db)
        from .context_tiles import install_functions as context_tile_functions

        context_tile_functions(self.db, self.cached_bytes)
        from .parent_affinity import install_functions as parent_affinity_functions

        parent_affinity_functions(self.db, self.cached_bytes)
        # Pure functions also serve a read connection opened before an explicit
        # layout upgrade. This registers no tables, markers or migration work.
        from .numeric_blocks import install_functions as numeric_block_functions

        numeric_block_functions(self.db, self.cached_bytes)
        from .record_ownership import install_functions as catalogue_functions

        catalogue_functions(self.db, self.cached_text, self.cached_bytes)

        self.db.create_function(
            "cw_numeric_refs",
            2,
            lambda key, raw: self.cached_references("numeric", key, raw, numeric_references),
            deterministic=True,
        )
        self.db.create_function(
            "cw_numeric_json", 1, lambda raw: encode(projection(self.db, json_payload(raw)))
        )
        self.db.row_factory = sqlite3.Row
        self.bounded = self.db.execute("PRAGMA user_version").fetchone()[0] == 2
        if self.bounded:
            from .quality_summary import install

            install(self.db, binary_cache=self.cached_bytes)
            numeric_blocks = self.db.execute(
                "SELECT value FROM market_meta WHERE key='numeric_blocks'"
            ).fetchone()
            if numeric_blocks is not None:
                if numeric_blocks[0] not in ("1", "2", "3"):
                    raise StateError("Unknown numeric block layout; database preserved")
                if numeric_blocks[0] == "3":
                    from .numeric_search import header as search_header

                    search_header(self.db)
                from .numeric_blocks import summary as numeric_summary

                numeric_summary(self.db)
            catalogue = self.db.execute("SELECT value FROM market_meta WHERE key='record_catalog'").fetchone()
            if catalogue is not None:
                if catalogue[0] not in ("2", "3"):
                    raise StateError("Unknown record catalogue; database preserved")
                overlap = self.db.execute(
                    "SELECT value FROM market_meta WHERE key='catalogue_overlap'"
                ).fetchone()
                if overlap is not None:
                    from .catalogue_overlap import header as overlap_header

                    try:
                        overlap_header(self.db)
                    except ValueError as exc:
                        raise StateError("Invalid catalogue overlap counters; database preserved") from exc
            source_tiles = self.db.execute(
                "SELECT value FROM market_meta WHERE key='candle_blocks'"
            ).fetchone()
            if source_tiles is not None:
                if source_tiles[0] not in ("1", "2"):
                    raise StateError("Unknown source tile layout; database preserved")
                from .candle_blocks import install_functions as source_tile_functions

                source_tile_functions(self.db, self.cached_text, self.cached_bytes)
            if self.db.execute("SELECT 1 FROM market_meta WHERE key='packet_bundles'").fetchone():
                from .packet_bundles import validate as validate_bundles

                validate_bundles(self.db)
            if self.db.execute("SELECT 1 FROM market_meta WHERE key='numeric_refs_compression'").fetchone():
                from .numeric_refs import installed as validate_numeric_refs

                validate_numeric_refs(self.db)
            if self.db.execute("SELECT 1 FROM market_meta WHERE key='numeric_frames'").fetchone():
                from .numeric_frames import header as validate_numeric_frames

                validate_numeric_frames(self.db)
            if self.db.execute("SELECT 1 FROM market_meta WHERE key='numeric_frame_queue'").fetchone():
                from .numeric_frame_work import header as validate_numeric_frame_queue

                validate_numeric_frame_queue(self.db)
            if self.db.execute("SELECT 1 FROM market_meta WHERE key='quality_frames'").fetchone():
                from .quality_frames import installed as validate_quality_frames

                validate_quality_frames(self.db)
            if self.db.execute("SELECT 1 FROM market_meta WHERE key='quality_frame_columns'").fetchone():
                from .quality_frames import column_writing

                column_writing(self.db)
            from .context_tiles import installed as validate_context_tiles

            validate_context_tiles(self.db)
            from .parent_affinity import header as validate_parent_affinity

            validate_parent_affinity(self.db)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        if not readonly:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            if self.bounded:
                from .expiry_index import install as install_expiry
                from .quality_layout import install as install_quality

                install_expiry(self.db)
                install_quality(self.db)
                from .source_windows import install as install_windows

                install_windows(self.db)
                from .numeric_pool import install as install_numeric

                install_numeric(self.db)
                from .quality_packets import install as install_packets

                install_packets(self.db)
                from .context_integrity import install as install_context_integrity

                install_context_integrity(self.db)
                from .reference_layout import install as install_references

                install_references(self.db)
                from .quality_meter import install as install_quality_meter

                install_quality_meter(self.db)
                from .cohort_meter import install as install_cohort_meter

                install_cohort_meter(self.db)
                from .runtime_meter import install as install_runtime_meter

                install_runtime_meter(self.db)
                from .cohort_meter import align_empty

                align_empty(self.db)
            if not self.bounded:
                create_schema(self.db)
            if os.name != "nt":
                path.chmod(0o600)

    def close(self) -> None:
        self.db.close()
        self.comparison_population = None
        self.analytics_ranges.clear()
        self.analytics_range_bytes = 0
        self.reference_cache.clear()
        self.reference_cache_bytes = 0

    def cached_references(self, kind: str, key: str, raw: Any, read: Any) -> str:
        # Only pure extraction is cached, never body existence or payload data.
        # Every owner INSERT still checks the current referenced rows in SQL.
        if not isinstance(raw, (str, bytes)) or not key.startswith(
            ("radar:v1:event:", "radar:v1:outcome:", "radar:v1:comparison:")
        ):
            return str(read(key, raw))
        return self.cached_text(kind, key, raw, lambda: str(read(key, raw)))

    def cached_record_json(self, raw: Any) -> Any:
        from .record_codec import sql_payload

        if not isinstance(raw, bytes):
            return sql_payload(raw)
        return self.cached_text("record-json", "", raw, lambda: str(sql_payload(raw)))

    def cached_text(self, kind: str, key: str, raw: str | bytes, read: Any) -> str:
        value = self._cached_value(kind, key, raw, lambda: str(read()))
        if not isinstance(value, str):
            raise StateError("Invalid pure text cache result")
        return value

    def cached_bytes(self, kind: str, key: str, raw: bytes, read: Any) -> bytes:
        value = self._cached_value("bytes/" + kind, key, raw, read)
        if not isinstance(value, bytes):
            raise StateError("Invalid pure binary cache result")
        return value

    def _cached_value(self, kind: str, key: str, raw: str | bytes, read: Any) -> str | bytes:
        # One existing1MiB budget shared by pure text/binary transforms. No
        # referenced body lookup, DB row or object is cached by this method.
        token = (kind, key, raw)
        cached = self.reference_cache.get(token)
        if cached is not None:
            return cached[0]
        result = read()
        if not isinstance(result, (str, bytes)):
            raise StateError("Invalid pure cache result")
        import sys

        # Unicode can use four bytes per character even when almost all of a
        # JSON string is ASCII. Charge Python objects, not UTF8 lengths.
        size = sys.getsizeof(key) + sys.getsizeof(raw) + sys.getsizeof(result) + 384
        if size <= 1024 * 1024:
            while self.reference_cache and self.reference_cache_bytes + size > 1024 * 1024:
                removed = self.reference_cache.pop(next(iter(self.reference_cache)))
                self.reference_cache_bytes -= removed[1]
            self.reference_cache[token] = (result, size)
            self.reference_cache_bytes += size
        return result

    def initialize(self, defaults: OwnerSettings, *, legacy: Path | None = None) -> None:
        if self.db.execute("SELECT 1 FROM owner_settings").fetchone():
            self.snapshot()  # Validate persisted values before any runtime writes.
            return
        if legacy is not None and legacy.exists():
            raise StateError("Legacy state needs reviewed explicit migration")
        with self.db:
            self.db.execute(
                "INSERT INTO owner_settings VALUES (?,1,?)", (self.owner_id, encode(defaults.payload()))
            )
            self.db.execute("INSERT INTO delivery_preferences VALUES (?,1)", (self.owner_id,))

    def snapshot(self) -> Snapshot:
        row = self.db.execute(
            "SELECT revision,settings_json FROM owner_settings WHERE owner_id=?", (self.owner_id,)
        ).fetchone()
        if row is None:
            raise StateError("Owner configuration missing")
        try:
            return Snapshot(
                int(row["revision"]), OwnerSettings.from_payload(json.loads(row["settings_json"]))
            )
        except (ValueError, TypeError) as exc:
            raise StateError("Invalid persisted owner configuration") from exc

    def update(self, expected_revision: int, settings: OwnerSettings) -> Snapshot:
        # Revalidate even objects supplied by an internal caller.
        settings = OwnerSettings.from_payload(settings.payload())
        current = self.snapshot()
        if current.revision != expected_revision:
            raise RevisionConflict("Owner settings changed; reread before updating")
        if current.settings == settings:
            return current
        from .detector_config import validate_effective_configuration

        try:
            validate_effective_configuration(settings)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        with self.db:
            cursor = self.db.execute(
                "UPDATE owner_settings SET revision=revision+1,settings_json=? WHERE owner_id=? AND revision=?",
                (encode(settings.payload()), self.owner_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise RevisionConflict("Owner settings changed; reread before updating")
        return self.snapshot()

    def enabled(self) -> bool:
        row = self.db.execute(
            "SELECT enabled FROM delivery_preferences WHERE chat_id=?", (self.owner_id,)
        ).fetchone()
        return bool(row and row[0])

    def set_enabled(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ConfigError("Invalid delivery preference")
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO delivery_preferences VALUES (?,?)", (self.owner_id, int(enabled))
            )

    def muted(self, symbol: str, now: float) -> bool:
        row = self.db.execute("SELECT mute_until FROM mutes WHERE symbol=?", (symbol,)).fetchone()
        return bool(row and row[0] > now)

    def mute(self, symbol: str, until: float) -> None:
        symbol = instrument_key(symbol) if "/" in symbol else normalize_pair(symbol)
        if not math.isfinite(until) or until < 0:
            raise ConfigError("Invalid mute duration")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO mutes VALUES (?,?)", (symbol, until))

    def get_runtime(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT payload_json FROM runtime WHERE key=?", (key,)).fetchone()
        if self.bounded:
            from .catalogue_reader import MISSING, lookup

            value = lookup(self.db, key, json_payload(row[0]) if row else MISSING, self.cached_bytes)
            return default if value is MISSING else value
        return json_payload(row[0]) if row else default

    def scan_runtime(
        self,
        prefix: str,
        limit: int | None = None,
        *,
        reverse: bool = False,
        after: str | None = None,
        before: str | None = None,
    ) -> Iterator[tuple[str, Any]]:
        from .catalogue_store import scan

        yield from scan(
            self.db,
            prefix,
            bounded=self.bounded,
            limit=limit,
            reverse=reverse,
            after=after,
            before=before,
            cache=self.cached_bytes,
        )

    def put_runtime(self, key: str, payload: Any) -> None:
        from .catalogue_write import mutation

        with self.db:
            with mutation(self, key, payload) as write:
                if write:
                    self.db.execute(RUNTIME_UPSERT, (key, encode(payload)))

    def count_runtime(self, prefix: str) -> int:
        from .catalogue_store import count

        return count(self.db, prefix, bounded=self.bounded)

    def backup(
        self,
        destination: Path,
        *,
        timeout_seconds: float = 900,
        validation_directory: Path | None = None,
        compressed_validation: bool = False,
        archive: bool = False,
        framed_validation: bool = False,
    ) -> dict[str, Any]:
        if framed_validation and (not archive or compressed_validation or validation_directory is None):
            raise StateError("Framed backup requires archive and a private validation directory")
        if compressed_validation and validation_directory is None:
            raise StateError("Compressed validation requires a private validation directory")
        if archive and (not (compressed_validation or framed_validation) or validation_directory is None):
            raise StateError("Archived backup requires compressed validation")
        if destination.exists() or destination.resolve() == self.path.resolve():
            raise StateError("Backup destination must be new")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if framed_validation:
            from .framed_backup import backup as framed_backup

            assert validation_directory is not None
            return framed_backup(
                self.path.absolute(),
                destination.absolute(),
                validation_directory,
                owner=self.owner_id,
                deadline=time.monotonic() + timeout_seconds,
            )
        if archive:
            from .compressed_validation import archive_backup
            from .validation import scratch_space

            started = time.monotonic()
            assert validation_directory is not None
            scratch_space(validation_directory, 0)
            try:
                proof = archive_backup(
                    self.path,
                    destination,
                    validation_directory,
                    deadline=started + timeout_seconds,
                )
            except IntegrityError as exc:
                exc.backup_metrics = {
                    "phase": "ARCHIVE_AND_VALIDATE",
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "validation_mode": "COMPRESSED_RAM",
                }
                raise
            return {
                **proof,
                "phase": "COMPLETE",
                "validation_mode": "COMPRESSED_RAM",
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        # Exclusive creation also rejects dangling links; an existing backup is never replaced.
        handle = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(handle)
        started = time.monotonic()
        deadline = started + timeout_seconds
        phase = "COPY"
        copied_at: float | None = None
        validated_hash: str | None = None
        validation_details: dict[str, float | str] = {}

        def metrics() -> dict[str, float | str]:
            now = time.monotonic()
            result: dict[str, float | str] = {
                "phase": phase,
                "copy_seconds": round((copied_at if copied_at is not None else now) - started, 3),
                "integrity_seconds": round(now - copied_at, 3) if copied_at is not None else 0.0,
                "elapsed_seconds": round(now - started, 3),
            }
            if validation_directory is not None:
                result["validation_mode"] = "COMPRESSED_RAM" if compressed_validation else "TMPFS"
            if validated_hash is not None:
                result["sha256"] = validated_hash
            result.update(validation_details)
            return result

        def progress(status: int, remaining: int, total: int) -> None:
            if time.monotonic() >= deadline:
                raise IntegrityError(
                    "TIME_BUDGET_EXCEEDED", "Backup time budget exceeded; incomplete destination preserved"
                )

        try:
            from .disk_validation import large_database, validated_disk

            native_disk = False
            with closing(sqlite3.connect(destination)) as backup:
                with closing(
                    sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=1)
                ) as source:
                    # Pin a WAL snapshot only while copying, then release it before validation.
                    source.execute("BEGIN")
                    source.execute("SELECT owner_id FROM owner_settings").fetchone()
                    source.backup(backup, pages=256, progress=progress, sleep=0.01)
                copied_at = time.monotonic()
                phase = "INTEGRITY"
                native_disk = validation_directory is None and large_database(destination)
                if validation_directory is None and not native_disk:
                    check_integrity(backup, deadline=deadline)
            if native_disk:
                with validated_disk(destination, deadline=deadline) as validated:
                    row = validated.execute("SELECT sqlite_version()").fetchone()
                    if row is None:
                        raise IntegrityError("DATABASE_ERROR", "Validation engine unavailable")
                    version = str(row[0])
                validation_details.update(sqlite_version=version, integrity_backend="APSW_DISK_MMAP")
            if validation_directory is not None:
                from .validation import validate_backup

                validation = validate_backup(
                    destination, validation_directory, deadline=deadline, compressed=compressed_validation
                )
                validated_hash = str(validation["sha256"])
                if compressed_validation:
                    validation_details = {
                        "bytes": int(validation["bytes"]),
                        "compressed_bytes": int(validation["compressed_bytes"]),
                        "sqlite_version": str(validation["sqlite_version"]),
                        "apsw_version": str(validation["apsw_version"]),
                    }
            if validated_hash is None:
                phase = "HASH"
                digest = hashlib.sha256()
                with destination.open("rb") as verified:
                    for block in iter(lambda: verified.read(1024 * 1024), b""):
                        if time.monotonic() >= deadline:
                            raise IntegrityError("TIME_BUDGET_EXCEEDED", "Backup hash time budget exceeded")
                        digest.update(block)
                validated_hash = digest.hexdigest()
                validation_details.update(bytes=destination.stat().st_size, validation_mode="DISK_STREAMING")
        except IntegrityError as exc:
            exc.backup_metrics = metrics()
            raise
        except sqlite3.DatabaseError as exc:
            raise StateError("Backup validation failed; incomplete destination preserved") from exc
        if os.name != "nt":
            destination.chmod(0o600)
        phase = "COMPLETE"
        return metrics()


def migrate_legacy(
    source: Path, database: Path, owner_id: int, backup_dir: Path, defaults: OwnerSettings
) -> dict[str, Any]:
    """Caller must quiesce the old writer. Validate before creating the database."""
    raw, content, fingerprint = read_legacy(source)
    chats = raw.get("chats")
    if not isinstance(chats, dict):
        legacy_id = str(raw.get("chat_id", ""))
        if not legacy_id:
            raise StateError("Legacy chat identity missing; explicit owner mapping required")
        chats = {legacy_id: raw}
    owner = chats.get(str(owner_id))
    if not isinstance(owner, dict):
        raise StateError("Owner absent from legacy state; original preserved")
    settings_raw = owner.get("settings", {})
    if not isinstance(settings_raw, dict):
        raise StateError("Invalid legacy owner settings")
    settings = _legacy_settings(settings_raw, defaults)
    enabled = owner.get("enabled", True)
    if not isinstance(enabled, bool):
        raise StateError("Invalid legacy delivery preference")
    mutes = owner.get("mutes", {})
    if not isinstance(mutes, dict):
        raise StateError("Invalid legacy mutes")
    normalized_mutes: dict[str, float] = {}
    for key, value in mutes.items():
        if not isinstance(key, str):
            raise StateError("Invalid legacy mute identity")
        try:
            until = float(value)
            if isinstance(value, bool) or not math.isfinite(until) or until < 0:
                raise ValueError
            symbol = normalize_pair(key.rsplit(":", 1)[-1])
        except (ValueError, TypeError) as exc:
            raise StateError("Invalid legacy mute; original preserved") from exc
        normalized_mutes[symbol] = max(until, normalized_mutes.get(symbol, 0))
    baselines = owner.get("baselines", {})
    if not isinstance(baselines, dict):
        raise StateError("Invalid legacy baselines")
    normalized_baselines = json.loads(encode(baselines))
    for value in normalized_baselines.values():
        if isinstance(value, dict) and "last_alert" in value:
            try:
                timestamp = float(value["last_alert"])
                if isinstance(value["last_alert"], bool) or not math.isfinite(timestamp) or timestamp < 0:
                    raise ValueError
                value["last_alert"] = timestamp
            except (ValueError, TypeError) as exc:
                raise StateError("Invalid legacy last_alert") from exc
    if source.resolve() == database.resolve() or backup_dir.resolve() == source.resolve():
        raise StateError("Migration paths must be distinct")
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup_file = backup_dir / f"legacy-{fingerprint}.json"
    if backup_file.exists():
        if backup_file.read_bytes() != content:
            raise StateError("Existing backup differs; preserved")
    else:
        with backup_file.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            backup_file.chmod(0o600)
    if source.read_bytes() != content:
        raise StateError("Legacy writer still active; stop before migration")
    store = Store(database, owner_id)
    try:
        existing = store.db.execute("SELECT source_sha256 FROM legacy_import").fetchone()
        if existing:
            if existing[0] != fingerprint:
                raise StateError("Different legacy import already exists")
            store.snapshot()
            return {"status": "already_imported", "source_sha256": fingerprint}
        if store.db.execute("SELECT 1 FROM owner_settings").fetchone():
            raise StateError("Existing owner configuration must not be overwritten")
        with store.db:
            store.db.execute(
                "INSERT INTO owner_settings VALUES (?,1,?)", (owner_id, encode(settings.payload()))
            )
            store.db.execute("INSERT INTO delivery_preferences VALUES (?,?)", (owner_id, int(enabled)))
            store.db.executemany("INSERT INTO mutes VALUES (?,?)", normalized_mutes.items())
            store.db.execute(
                "INSERT INTO legacy_import VALUES (?,?,?)", (fingerprint, encode(raw), backup_file.name)
            )
            store.db.execute(
                "INSERT INTO runtime VALUES (?,?)", ("legacy_baselines", encode(normalized_baselines))
            )
        store.snapshot()
        return {"status": "imported", "source_sha256": fingerprint, "archived_chats": len(chats) - 1}
    finally:
        store.close()
