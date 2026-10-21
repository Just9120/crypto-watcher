import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager

import apsw
import pytest

from cryptowatcher import disk_validation
from cryptowatcher.disk_validation import validated_disk
from cryptowatcher.storage import IntegrityError, Store

from .test_cli import cli


def test_full_disk_check_pins_metadata_snapshot_and_cannot_write(store):
    before = store.snapshot()
    with validated_disk(store.path, deadline=time.monotonic() + 5) as checked:
        assert checked.execute("SELECT revision FROM owner_settings").fetchone()[0] == before.revision
        store.update(before.revision, before.settings.changed(price_move_min=9))
        assert checked.execute("SELECT revision FROM owner_settings").fetchone()[0] == before.revision
        with pytest.raises(apsw.ReadOnlyError):
            checked.execute("UPDATE owner_settings SET revision=99")
    assert store.snapshot().revision == before.revision + 1


def test_full_disk_check_rejects_committed_constraint_violation_without_data_leak(tmp_path):
    path = tmp_path / "invalid.sqlite"
    with sqlite3.connect(path) as database:
        database.execute("CREATE TABLE checked(value TEXT CHECK(value='valid'))")
        database.execute("PRAGMA ignore_check_constraints=ON")
        database.execute("INSERT INTO checked VALUES ('secret-never-print')")
    original = path.read_bytes()
    with pytest.raises(IntegrityError) as failure, validated_disk(path, deadline=time.monotonic() + 5):
        pytest.fail("A full check must reject the constraint violation")
    assert failure.value.code == "DATABASE_CORRUPT"
    assert "secret" not in str(failure.value) and path.read_bytes() == original


def test_disk_check_rejects_expired_budget_before_opening(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(disk_validation.apsw, "Connection", lambda *a, **k: opened.append(True))
    with pytest.raises(IntegrityError) as failure, validated_disk(tmp_path / "unused", deadline=0):
        pytest.fail("An expired budget cannot yield a validated connection")
    assert failure.value.code == "TIME_BUDGET_EXCEEDED" and not opened


def test_disk_check_interrupts_full_query_when_shared_budget_expires(store, monkeypatch):
    now = [0.0]
    original = apsw.Connection

    class SlowConnection(original):
        def execute(self, sql, *args, **kwargs):
            if sql == "PRAGMA integrity_check":
                now[0] = 2.0
            return super().execute(sql, *args, **kwargs)

    monkeypatch.setattr(disk_validation.apsw, "Connection", SlowConnection)
    monkeypatch.setattr(disk_validation.time, "monotonic", lambda: now[0])
    with pytest.raises(IntegrityError) as failure, validated_disk(store.path, deadline=1):
        pytest.fail("A completed query cannot turn an expired operation into PASS")
    assert failure.value.code == "TIME_BUDGET_EXCEEDED"
    assert store.snapshot().revision == 1


@pytest.mark.parametrize("stage", ["integrity", "heads"])
def test_late_invalid_disk_results_are_timeouts_not_corruption(store, monkeypatch, stage):
    from cryptowatcher import record_catalog

    now = [0.0]
    original = apsw.Connection

    class LateConnection(original):
        def execute(self, sql, *args, **kwargs):
            if sql == "PRAGMA integrity_check" and stage == "integrity":
                now[0] = 2.0
                return [("late read failure",)]
            return super().execute(sql, *args, **kwargs)

    def late_heads(db):
        now[0] = 2.0
        return False

    monkeypatch.setattr(disk_validation.apsw, "Connection", LateConnection)
    monkeypatch.setattr(disk_validation.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(record_catalog, "valid_heads", late_heads)
    with pytest.raises(IntegrityError) as failure, validated_disk(store.path, deadline=1):
        pytest.fail("A late invalid result cannot qualify or diagnose corruption")
    assert failure.value.code == "TIME_BUDGET_EXCEEDED"
    assert store.snapshot().revision == 1


@pytest.mark.parametrize(
    "error,code",
    [
        (apsw.IOError, "IO_ERROR"),
        (apsw.CorruptError, "DATABASE_CORRUPT"),
        (apsw.InterruptError, "INTERRUPTED"),
        (apsw.BusyError, "DATABASE_ERROR"),
    ],
)
def test_disk_check_classifies_errors_without_raw_messages(store, monkeypatch, error, code):
    original = apsw.Connection

    class FailedConnection(original):
        def execute(self, sql, *args, **kwargs):
            if sql == "PRAGMA integrity_check":
                raise error("secret-never-print")
            return super().execute(sql, *args, **kwargs)

    monkeypatch.setattr(disk_validation.apsw, "Connection", FailedConnection)
    with pytest.raises(IntegrityError) as failure, validated_disk(store.path, deadline=time.monotonic() + 5):
        pytest.fail("An unsuccessful full check cannot yield metadata")
    assert failure.value.code == code and "secret" not in str(failure.value)


def test_large_disk_cli_and_backup_keep_exact_values_and_release_live_wal(
    infrastructure, store, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(disk_validation, "LARGE_DISK_BYTES", 0)
    store.put_runtime("protected", {"precise": 0.12345678901234567, "identity": "unchanged"})
    before = store.snapshot()
    assert cli(monkeypatch, infrastructure, "state-check") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "integrity_ok" and result["configuration_revision"] == before.revision
    validate = disk_validation.validated_disk

    @contextmanager
    def released_source(path, **kwargs):
        assert store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        with validate(path, **kwargs) as checked:
            yield checked

    monkeypatch.setattr(disk_validation, "validated_disk", released_source)
    destination = tmp_path / "backup.sqlite"
    proof = store.backup(destination)
    assert proof["phase"] == "COMPLETE" and proof["integrity_backend"] == "APSW_DISK_MMAP"
    assert proof["sha256"] == hashlib.sha256(destination.read_bytes()).hexdigest()
    reopened = Store(destination, store.owner_id, readonly=True)
    try:
        assert reopened.snapshot() == before
        assert reopened.get_runtime("protected") == store.get_runtime("protected")
    finally:
        reopened.close()
