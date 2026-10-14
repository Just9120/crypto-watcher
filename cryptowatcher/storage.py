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
from typing import Any

from .config import ConfigError, OwnerSettings, instrument_key, normalize_pair, number

SCHEMA_VERSION = 1
COMPRESSED_RECORD_VERSION = 2
RECORD_FORMAT_KEY = "historyrecord:format:v1"
LEGACY_RECORD_FORMAT = {"schema": "cryptowatcher.compressed-record.v1", "physical_schema": 1}
RECORD_FORMAT = {**LEGACY_RECORD_FORMAT, "candle_schema": "cryptowatcher.compact-candle.v1"}


def known_record_format(payload: str) -> bool:
    return payload in (encode(RECORD_FORMAT), encode(LEGACY_RECORD_FORMAT))


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
        previous_cache = int(database.execute("PRAGMA cache_size").fetchone()[0])
        mmap_row = database.execute("PRAGMA mmap_size").fetchone()
        previous_mmap = int(mmap_row[0]) if mmap_row is not None else None
        # Dedicated validation connections only; these suggestions never change the file schema.
        database.execute(f"PRAGMA cache_size=-{VALIDATION_CACHE_KIB}")
        if previous_mmap is not None:
            database.execute(f"PRAGMA mmap_size={VALIDATION_MMAP_BYTES}")
        rows = database.execute("PRAGMA integrity_check").fetchall()
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


def create_schema(database: sqlite3.Connection, *, version: int = SCHEMA_VERSION) -> None:
    """Canonical schema for a new store or an exclusive isolated recovery candidate."""
    if version not in (SCHEMA_VERSION, COMPRESSED_RECORD_VERSION):
        raise StateError("Unsupported database schema; database preserved")
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
    """)
    database.execute(f"PRAGMA user_version={version}")


class Store:
    def __init__(self, path: Path, owner_id: int, *, readonly: bool = False) -> None:
        number(owner_id, "owner_id", integer=True)
        self.path = path
        self.owner_id = owner_id
        self.readonly = readonly
        version = SCHEMA_VERSION
        if path.exists():
            probe = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
            try:
                version = probe.execute("PRAGMA user_version").fetchone()[0]
                if version == COMPRESSED_RECORD_VERSION:
                    marker = probe.execute(
                        "SELECT payload_json FROM runtime WHERE key=?", (RECORD_FORMAT_KEY,)
                    ).fetchone()
                    if marker is None or not known_record_format(marker[0]):
                        raise StateError("Unsupported database schema; database preserved")
                elif version != SCHEMA_VERSION:
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
        # Only unwrap exact-byte historical frames. Shared-field resolution still
        # belongs to History, and ordinary runtime/config JSON remains untouched.
        from .record_codec import sql_payload

        self.db.create_function("cw_record_json", 1, sql_payload, deterministic=True)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        if not readonly:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            create_schema(self.db, version=version)
            if os.name != "nt":
                path.chmod(0o600)

    def close(self) -> None:
        self.db.close()

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
        from .record_codec import sql_payload

        return json.loads(sql_payload(row[0])) if row else default

    def put_runtime(self, key: str, payload: Any) -> None:
        with self.db:
            self.db.execute(RUNTIME_UPSERT, (key, encode(payload)))

    def backup(
        self,
        destination: Path,
        *,
        timeout_seconds: float = 900,
        validation_directory: Path | None = None,
        compressed_validation: bool = False,
        archive: bool = False,
    ) -> dict[str, float | str]:
        if compressed_validation and validation_directory is None:
            raise StateError("Compressed validation requires a private validation directory")
        if archive and (not compressed_validation or validation_directory is None):
            raise StateError("Archived backup requires compressed validation")
        if destination.exists() or destination.resolve() == self.path.resolve():
            raise StateError("Backup destination must be new")
        destination.parent.mkdir(parents=True, exist_ok=True)
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
                if validation_directory is None:
                    check_integrity(backup, deadline=deadline)
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
