"""One full pinned source capture and an independently reopened standard ZSTD backup."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import apsw

from .compressed_validation import (
    QualificationError,
)
from .config import OwnerSettings
from .storage import encode


def source_facts(database: apsw.Connection, owner: int) -> dict[str, Any]:
    row = database.execute("SELECT owner_id,revision,settings_json FROM owner_settings").fetchone()
    if row is None or row[0] != owner:
        raise QualificationError("VALIDATED_OWNER_MISMATCH")
    imported = database.execute("SELECT source_sha256 FROM legacy_import").fetchone()
    preference = database.execute(
        "SELECT enabled FROM delivery_preferences WHERE chat_id=?", (owner,)
    ).fetchone()
    version = database.execute("PRAGMA user_version").fetchone()
    if version is None or version[0] not in (1, 2):
        raise QualificationError("UNKNOWN_VALIDATED_SCHEMA")
    settings = OwnerSettings.from_payload(json.loads(row[2]))
    return {
        "status": "integrity_ok",
        "schema": version[0],
        "owner_id": owner,
        "configuration_revision": row[1],
        "settings_sha256": hashlib.sha256(encode(settings.payload()).encode()).hexdigest(),
        "legacy_sha256": imported[0] if imported else None,
        "alerts_enabled": bool(preference and preference[0]),
        "validation_mode": "FRAMED_DISK",
    }


def backup(
    source: Path, destination: Path, directory: Path, *, owner: int, deadline: float
) -> dict[str, Any]:
    """Keep the existing CLI boundary while native capture/check avoids compressed xRead."""
    from .native_disk_backup import backup as native_backup

    return native_backup(source, destination, directory, owner=owner, deadline=deadline)
