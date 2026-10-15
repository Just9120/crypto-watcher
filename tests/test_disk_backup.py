import hashlib
import json
import math
import os
import time
from contextlib import closing, contextmanager

import pytest
import zstandard

from cryptowatcher import compressed_validation as cv
from cryptowatcher import disk_backup as disk
from cryptowatcher.storage import IntegrityError, StateError, Store
from cryptowatcher.validation import validated_snapshot

from .test_cli import cli


def scratch(tmp_path):
    directory = (tmp_path / "scratch").resolve()
    directory.mkdir(mode=0o700)
    return directory


def test_disk_backup_survives_ram_ceiling_and_wal_changes(store, tmp_path, monkeypatch):
    directory = scratch(tmp_path)
    target = tmp_path / "backup.sqlite3.zst"
    store.put_runtime("evidence", {"at": "before", "data": "x" * 10000})
    before_settings = store.snapshot()
    monkeypatch.setattr(cv, "MAX_COMPRESSED_BYTES", 16)
    original = cv.full_check
    calls = 0

    def checked(db, deadline):
        nonlocal calls
        calls += 1
        # A later write must neither invalidate nor leak into the pinned snapshot.
        store.put_runtime("evidence", {"at": "after"})
        assert store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        original(db, deadline)

    monkeypatch.setattr(cv, "full_check", checked)
    proof = store.backup(target, validation_directory=directory, archive=True, disk_validation=True)
    assert calls == 2 and proof["phase"] == "COMPLETE"
    assert proof["validation_mode"] == "COMPRESSED_DISK" and proof["archive_encoding"] == "ZSTD"
    assert proof["archive_sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert proof["archive_bytes"] == target.stat().st_size
    assert 0 < proof["sqlite_cache_kib"] <= cv.MAX_SQLITE_CACHE_BYTES // 1024
    restored = tmp_path / "restored.sqlite3"
    with target.open("rb") as source, restored.open("xb") as out:
        zstandard.ZstdDecompressor().copy_stream(source, out)
    assert hashlib.sha256(restored.read_bytes()).hexdigest() == proof["sha256"]
    assert restored.stat().st_size == proof["bytes"]
    with closing(Store(restored, store.owner_id, readonly=True)) as readback:
        assert readback.snapshot() == before_settings
        assert readback.get_runtime("evidence")["at"] == "before"
        assert readback.db.execute("PRAGMA integrity_check").fetchall()[0][0] == "ok"
    assert store.get_runtime("evidence") == {"at": "after"}
    assert not list(directory.iterdir())
    previous = target.read_bytes()
    with pytest.raises(StateError, match="destination must be new"):
        store.backup(target, validation_directory=directory, archive=True, disk_validation=True)
    assert target.read_bytes() == previous


def test_disk_state_snapshot_releases_live_reader(store, tmp_path, monkeypatch):
    directory = scratch(tmp_path)
    original = cv.full_check

    def checked(db, deadline):
        store.put_runtime("late", True)
        assert store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        original(db, deadline)

    monkeypatch.setattr(cv, "full_check", checked)
    with validated_snapshot(store.path, directory, deadline=time.monotonic() + 20, disk=True) as db:
        assert db.execute("SELECT revision FROM owner_settings").fetchone()[0] == 1
        assert db.execute("SELECT payload_json FROM runtime WHERE key='late'").fetchone() is None
    assert store.get_runtime("late") is True and not list(directory.iterdir())


def test_disk_backup_rejects_check_violation_and_keeps_source(store, tmp_path):
    directory = scratch(tmp_path)
    store.db.execute("PRAGMA ignore_check_constraints=ON")
    store.db.execute("UPDATE owner_settings SET revision=0")
    store.db.commit()
    with pytest.raises(IntegrityError, match="DATABASE_CORRUPT"):
        disk.backup(store.path, tmp_path / "bad.zst", directory, seconds=20)
    assert store.db.execute("SELECT revision FROM owner_settings").fetchone()[0] == 0
    assert not list(directory.iterdir())


def test_disk_backup_detects_persisted_corruption(store, tmp_path, monkeypatch):
    directory = scratch(tmp_path)
    target = tmp_path / "bad.zst"
    original = disk.validate_archive

    def corrupt(path, *args, **kwargs):
        with path.open("r+b") as data:
            data.seek(-1, 2)
            value = data.read(1)
            data.seek(-1, 2)
            data.write(bytes([value[0] ^ 0x80]))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(disk, "validate_archive", corrupt)
    with pytest.raises(IntegrityError):
        disk.backup(store.path, target, directory, seconds=20)
    assert target.is_file() and store.snapshot().revision == 1
    assert not list(directory.iterdir())


def test_disk_backup_uses_one_deadline_and_preserves_partial_archive(store, tmp_path, monkeypatch):
    directory = scratch(tmp_path)
    target = tmp_path / "expired.zst"
    original = disk.snapshot
    deadlines = []

    @contextmanager
    def captured(*args, **kwargs):
        deadlines.append(kwargs["deadline"])
        with original(*args, **kwargs) as value:
            yield value

    def expired(*args, **kwargs):
        assert kwargs["deadline"] == deadlines[0]
        cv.budget(time.monotonic() - 1)

    monkeypatch.setattr(disk, "snapshot", captured)
    monkeypatch.setattr(disk, "validate_archive", expired)
    with pytest.raises(IntegrityError, match="TIME_BUDGET_EXCEEDED"):
        disk.backup(store.path, target, directory, seconds=20)
    assert target.stat().st_size > 0 and not list(directory.iterdir())


@pytest.mark.parametrize("seconds", [0, -1, 901, math.inf, math.nan])
def test_disk_backup_refuses_extended_or_invalid_budget(store, tmp_path, seconds):
    directory = scratch(tmp_path)
    target = tmp_path / "refused.zst"
    with pytest.raises(IntegrityError, match="INVALID_TIME_BUDGET"):
        disk.backup(store.path, target, directory, seconds=seconds)
    assert not target.exists()


def test_disk_root_rejects_unknown_filesystem_and_unprotected_directory(tmp_path, monkeypatch):
    directory = scratch(tmp_path)
    monkeypatch.setattr(cv, "disk_backing", lambda path: False)
    with pytest.raises(IntegrityError, match="DISK_BACKING_REQUIRED"):
        disk.private_disk(directory)
    monkeypatch.setattr(cv, "disk_backing", lambda path: True)
    if os.name == "posix":
        directory.chmod(0o755)
        with pytest.raises(IntegrityError, match="PRIVATE_BACKING_DIRECTORY_REQUIRED"):
            disk.private_disk(directory)


def test_disk_exhaustion_never_uses_ram_fallback(store, tmp_path, monkeypatch):
    directory = scratch(tmp_path)
    usage = disk.shutil.disk_usage(directory)
    monkeypatch.setattr(
        disk.shutil, "disk_usage", lambda path: type(usage)(usage.total, usage.used, disk.DISK_RESERVE)
    )
    with pytest.raises(IntegrityError, match="DISK_RESERVE_EXHAUSTED"):
        disk.backup(store.path, tmp_path / "partial.zst", directory, seconds=20)
    assert store.snapshot().revision == 1 and not list(directory.iterdir())


def test_cli_reports_actual_schema_for_disk_snapshot(store, infrastructure, tmp_path, monkeypatch, capsys):
    from cryptowatcher.storage import RECORD_FORMAT, RECORD_FORMAT_KEY

    directory = scratch(tmp_path)
    store.put_runtime(RECORD_FORMAT_KEY, RECORD_FORMAT)
    store.db.execute("PRAGMA user_version=2")
    assert (
        cli(
            monkeypatch,
            infrastructure,
            "state-check",
            "--disk-validation",
            "--validation-directory",
            str(directory),
        )
        == 0
    )
    proof = json.loads(capsys.readouterr().out)
    assert proof["schema"] == 2 and proof["validation_mode"] == "COMPRESSED_DISK"
    assert proof["configuration_revision"] == 1


def test_cli_disk_archive_and_mutually_exclusive_mode(store, infrastructure, tmp_path, monkeypatch, capsys):
    directory = scratch(tmp_path)
    target = tmp_path / "cli.zst"
    arguments = (
        "backup",
        "--disk-validation",
        "--validation-directory",
        str(directory),
        "--archive-backup",
        "--destination",
        str(target),
    )
    assert cli(monkeypatch, infrastructure, *arguments, "--compressed-validation") == 2
    assert not target.exists()
    capsys.readouterr()
    assert cli(monkeypatch, infrastructure, *arguments) == 0
    proof = json.loads(capsys.readouterr().out)
    assert (
        proof["status"] == "backup_integrity_ok" and proof["backup"]["validation_mode"] == "COMPRESSED_DISK"
    )
