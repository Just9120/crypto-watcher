"""Canonical run, migration, configuration and read-only health commands."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from . import __version__
from .config import ConfigError, Infrastructure, OwnerSettings
from .events import History
from .lease import RuntimeLease
from .logging_utils import configure_logging
from .storage import IntegrityError, StateError, Store, check_integrity, encode, migrate_legacy


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m cryptowatcher")
    parser.add_argument(
        "command",
        nargs="?",
        choices=(
            "run",
            "check-config",
            "migrate",
            "diagnostics",
            "health",
            "backup",
            "state-check",
            "export",
            "analytics",
            "context",
        ),
        default="run",
    )
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--legacy", type=Path)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=900)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--validation-directory", type=Path)
    parser.add_argument("--compressed-validation", action="store_true")
    parser.add_argument("--owner-id", type=int)
    parser.add_argument("--format", choices=("ndjson", "csv"), default="ndjson")
    parser.add_argument("--start-ms", type=int, default=0)
    parser.add_argument("--end-ms", type=int, default=9_999_999_999_999_999)
    parser.add_argument("--detector")
    parser.add_argument("--detector-version")
    parser.add_argument("--market", choices=("spot", "linear"))
    parser.add_argument("--groups")
    parser.add_argument("--event-id")
    parser.add_argument("--cutoff-ms", type=int)
    args = parser.parse_args()
    if args.version:
        revision = os.getenv("SOURCE_REVISION", "UNSET")
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            revision = "UNSET"
        print(json.dumps({"version": __version__, "source_revision": revision}))
        return 0
    load_dotenv(override=False)
    configure_logging(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    try:
        if args.validation_directory is not None and args.command not in ("state-check", "backup"):
            raise ConfigError("validation-directory is only supported for state-check and backup")
        if args.compressed_validation and args.validation_directory is None:
            raise ConfigError("compressed-validation requires validation-directory")
        if args.command == "migrate":
            if (
                args.database is None
                or args.legacy is None
                or args.backup_dir is None
                or args.owner_id is None
            ):
                raise ConfigError("migrate requires database, legacy, backup-dir and owner-id")
            with RuntimeLease(args.database):
                result = migrate_legacy(
                    args.legacy,
                    args.database,
                    args.owner_id,
                    args.backup_dir,
                    OwnerSettings.from_env(os.environ),
                )
            print(json.dumps(result))
            return 0
        if args.command in (
            "diagnostics",
            "health",
            "backup",
            "state-check",
            "export",
            "analytics",
            "context",
        ):
            database = args.database or Path(os.environ.get("DATABASE_FILE", "/data/cryptowatcher.sqlite3"))
            owner = args.owner_id or int(os.environ.get("OWNER_USER_ID", "0"))
            store = Store(database, owner, readonly=True)
            try:
                if args.command == "export":
                    from .export import Selection, csv_summary, ndjson

                    selection = Selection(
                        args.start_ms, args.end_ms, args.detector, args.detector_version, args.market
                    )
                    selection.validate()
                    stream = (
                        ndjson(store, selection) if args.format == "ndjson" else csv_summary(store, selection)
                    )
                    output = (
                        args.destination.open("x", encoding="utf-8", newline="")
                        if args.destination
                        else sys.stdout
                    )
                    try:
                        if args.destination and os.name != "nt":
                            args.destination.chmod(0o600)
                        for chunk in stream:
                            output.write(chunk)
                    finally:
                        if args.destination:
                            output.close()
                    return 0
                if args.command == "analytics":
                    from .analytics import GROUPS, cohorts

                    groups = tuple(args.groups.split(",")) if args.groups else GROUPS
                    print(encode(cohorts(store, groups, cutoff_ms=args.cutoff_ms)))
                    return 0
                if args.command == "context":
                    from .analytics import decision_context

                    if args.event_id is None or args.cutoff_ms is None:
                        raise ConfigError("context requires event-id and cutoff-ms")
                    print(encode(decision_context(History(store), args.event_id, args.cutoff_ms)))
                    return 0
                snapshot = store.snapshot()
                heartbeat = store.get_runtime("heartbeat", {})
                if args.command == "state-check":
                    if not math.isfinite(args.timeout_seconds) or not 0 < args.timeout_seconds <= 900:
                        raise ConfigError("Invalid integrity time budget")
                    deadline = time.monotonic() + args.timeout_seconds
                    if args.validation_directory is None:
                        check_integrity(store.db, deadline=deadline)
                        imported = store.db.execute("SELECT source_sha256 FROM legacy_import").fetchone()
                        enabled = store.enabled()
                    else:
                        from .storage import Snapshot
                        from .validation import validated_snapshot

                        with validated_snapshot(
                            database,
                            args.validation_directory,
                            deadline=deadline,
                            compressed=args.compressed_validation,
                        ) as validated:
                            owner_row = validated.execute(
                                "SELECT owner_id,revision,settings_json FROM owner_settings"
                            ).fetchone()
                            if owner_row is None:
                                raise StateError("Validated owner settings unavailable")
                            owner_id, revision, payload = owner_row
                            if owner_id != store.owner_id:
                                raise StateError("Validated snapshot owner mismatch")
                            snapshot = Snapshot(revision, OwnerSettings.from_payload(json.loads(payload)))
                            imported = validated.execute("SELECT source_sha256 FROM legacy_import").fetchone()
                            preference = validated.execute(
                                "SELECT enabled FROM delivery_preferences WHERE chat_id=?", (owner_id,)
                            ).fetchone()
                            enabled = bool(preference and preference[0])
                    print(
                        json.dumps(
                            {
                                "status": "integrity_ok",
                                "schema": 1,
                                "owner_id": store.owner_id,
                                "configuration_revision": snapshot.revision,
                                "settings_sha256": hashlib.sha256(
                                    encode(snapshot.settings.payload()).encode()
                                ).hexdigest(),
                                "legacy_sha256": imported[0] if imported else None,
                                "alerts_enabled": enabled,
                                "validation_mode": "COMPRESSED_RAM"
                                if args.compressed_validation
                                else ("TMPFS" if args.validation_directory else "DISK"),
                            }
                        )
                    )
                    return 0
                if args.command == "health":
                    alive = (
                        0 <= time.time() - heartbeat.get("at", 0) <= 120
                        and heartbeat.get("components_healthy") is not False
                    )
                    print(json.dumps({"live": alive, "source_revision": heartbeat.get("revision", "UNSET")}))
                    return 0 if alive else 1
                if args.command == "backup":
                    if args.destination is None:
                        raise ConfigError("backup requires destination")
                    if not math.isfinite(args.timeout_seconds) or not 0 < args.timeout_seconds <= 900:
                        raise ConfigError("Invalid backup time budget")
                    metrics = store.backup(
                        args.destination,
                        timeout_seconds=args.timeout_seconds,
                        validation_directory=args.validation_directory,
                        compressed_validation=args.compressed_validation,
                    )
                    print(json.dumps({"status": "backup_integrity_ok", "backup": metrics}))
                else:
                    from .diagnostics import snapshot as diagnostic_snapshot

                    print(json.dumps(diagnostic_snapshot(store)))
                return 0
            finally:
                store.close()
        infrastructure = Infrastructure.from_env()
        if args.command == "check-config":
            print(
                json.dumps(
                    {
                        "status": "valid",
                        "owner_private_chat": True,
                        "source_revision": infrastructure.source_revision,
                    }
                )
            )
            return 0
        from .bot import Controller, application
        from .ingestion import BybitRadar

        with RuntimeLease(infrastructure.database):
            store = Store(infrastructure.database, infrastructure.owner_id)
            try:
                store.initialize(infrastructure.defaults, legacy=infrastructure.legacy_state)
                controller = Controller(infrastructure, store, BybitRadar(store))
                application(controller).run_polling()
            finally:
                store.close()
        return 0
    except Exception as exc:
        message = str(exc) if isinstance(exc, (ConfigError, StateError)) else type(exc).__name__
        failure: dict[str, object] = {"status": "blocked", "reason": message}
        if isinstance(exc, IntegrityError):
            failure["failure_code"] = exc.code
            if exc.backup_metrics is not None:
                failure["backup"] = exc.backup_metrics
        print(json.dumps(failure), file=sys.stderr)
        return exc.exit_status if isinstance(exc, IntegrityError) else 2


if __name__ == "__main__":
    raise SystemExit(main())
