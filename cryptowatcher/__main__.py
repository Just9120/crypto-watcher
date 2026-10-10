"""Canonical run, migration, configuration and read-only health commands."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from . import __version__
from .config import ConfigError, Infrastructure, OwnerSettings
from .lease import RuntimeLease
from .logging_utils import configure_logging
from .storage import StateError, Store, encode, migrate_legacy


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m cryptowatcher")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("run", "check-config", "migrate", "diagnostics", "health", "backup", "state-check"),
        default="run",
    )
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--legacy", type=Path)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--owner-id", type=int)
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
        if args.command in ("diagnostics", "health", "backup", "state-check"):
            database = args.database or Path(os.environ.get("DATABASE_FILE", "/data/cryptowatcher.sqlite3"))
            owner = args.owner_id or int(os.environ.get("OWNER_USER_ID", "0"))
            store = Store(database, owner, readonly=True)
            try:
                snapshot = store.snapshot()
                heartbeat = store.get_runtime("heartbeat", {})
                if args.command == "state-check":
                    integrity = store.db.execute("PRAGMA integrity_check").fetchone()[0]
                    if integrity != "ok":
                        raise StateError("Database integrity check failed")
                    imported = store.db.execute("SELECT source_sha256 FROM legacy_import").fetchone()
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
                                "alerts_enabled": store.enabled(),
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
                    store.backup(args.destination)
                    print(json.dumps({"status": "backup_integrity_ok"}))
                else:
                    print(
                        json.dumps(
                            {
                                "configuration_revision": snapshot.revision,
                                "heartbeat": heartbeat,
                                "last_poll": store.get_runtime("last_poll_status", {"state": "PENDING"}),
                                "telegram_delivery": store.get_runtime(
                                    "telegram_delivery", {"state": "PENDING"}
                                ),
                                "ingestion": store.get_runtime("ingestion", {"state": "PENDING"}),
                            }
                        )
                    )
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
        print(json.dumps({"status": "blocked", "reason": message}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
