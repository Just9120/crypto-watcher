"""Persistent source/backup identity and negative full-integrity gates."""

import hashlib
import sqlite3
import time
from contextlib import closing

import pytest
import zstandard

from cryptowatcher import framed_backup
from cryptowatcher.compressed_validation import QualificationError
from cryptowatcher.market_storage import enable
from cryptowatcher.outcomes import OutcomeWorker
from cryptowatcher.storage import Store
from tests.test_outcomes import NOW, observe


def environment(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(framed_backup, "scratch_space", lambda *args: None)
    return scratch, tmp_path / "backup.sqlite3.zst"


def test_schema2_full_source_and_independent_persisted_backup_equal_every_table(store, tmp_path, monkeypatch):
    enable(store.db)
    store.bounded = True
    store.clock_ms = lambda: NOW
    observe(store, OutcomeWorker(store))
    original = list(store.db.iterdump())
    scratch, destination = environment(tmp_path, monkeypatch)
    proof = framed_backup.backup(
        store.path, destination, scratch, owner=store.owner_id, deadline=time.monotonic() + 20
    )
    assert proof["phase"] == "COMPLETE" and proof["validation_mode"] == "FRAMED_DISK"
    assert proof["source_validation"]["status"] == "integrity_ok"
    assert proof["source_validation"]["schema"] == 2
    assert proof["source_validation"]["snapshot_sha256"] == proof["sha256"]
    assert proof["archive_sha256"] == hashlib.sha256(destination.read_bytes()).hexdigest()
    from cryptowatcher.compressed_validation import validate_closed

    compatible = validate_closed(
        destination, directory=scratch, deadline=time.monotonic() + 20, archive_encoding="ZSTD"
    )
    assert compatible["sha256"] == proof["sha256"]
    restored = tmp_path / "restored.db"
    with destination.open("rb") as source, restored.open("xb") as target:
        zstandard.ZstdDecompressor().copy_stream(source, target)
    assert hashlib.sha256(restored.read_bytes()).hexdigest() == proof["sha256"]
    with closing(Store(restored, store.owner_id, readonly=True)) as reader:
        assert reader.snapshot() == store.snapshot()
        assert list(reader.db.iterdump()) == original
    with closing(sqlite3.connect(restored)) as reader:
        assert reader.execute("PRAGMA foreign_key_check").fetchall() == []
    assert list(store.db.iterdump()) == original and list(scratch.iterdir()) == []


def test_existing_archive_is_never_overwritten_after_repeated_request(store, tmp_path, monkeypatch):
    scratch, destination = environment(tmp_path, monkeypatch)
    framed_backup.backup(
        store.path, destination, scratch, owner=store.owner_id, deadline=time.monotonic() + 20
    )
    original = destination.read_bytes()
    with pytest.raises(QualificationError):
        framed_backup.backup(
            store.path, destination, scratch, owner=store.owner_id, deadline=time.monotonic() + 20
        )
    assert destination.read_bytes() == original


def test_corrupt_source_never_has_a_complete_backup_proof(store, tmp_path, monkeypatch):
    scratch, destination = environment(tmp_path, monkeypatch)
    store.db.executescript(
        "CREATE TABLE invalid_check(value CHECK(value>0)); PRAGMA ignore_check_constraints=ON; INSERT INTO invalid_check VALUES(-1); PRAGMA ignore_check_constraints=OFF;"
    )
    original = list(store.db.iterdump())
    with pytest.raises(QualificationError, match="DATABASE_CORRUPT") as result:
        framed_backup.backup(
            store.path, destination, scratch, owner=store.owner_id, deadline=time.monotonic() + 20
        )
    assert result.value.backup_metrics["phase"] == "FULL_INTEGRITY"
    assert destination.exists() and list(store.db.iterdump()) == original


def test_archive_mutation_during_final_fingerprint_cannot_inherit_source_integrity(
    store, tmp_path, monkeypatch
):
    scratch, destination = environment(tmp_path, monkeypatch)
    original = framed_backup.fingerprint

    def changed(path, deadline):
        with path.open("ab") as output:
            output.write(b"x")
        return original(path, deadline)

    monkeypatch.setattr(framed_backup, "fingerprint", changed)
    with pytest.raises(QualificationError, match="SOURCE_CHANGED") as result:
        framed_backup.backup(
            store.path, destination, scratch, owner=store.owner_id, deadline=time.monotonic() + 20
        )
    assert result.value.backup_metrics["phase"] == "ARCHIVE_FINGERPRINT"
    assert destination.exists()


def test_cli_selects_the_full_framed_backup_and_refuses_ambiguous_flags(
    store, infrastructure, tmp_path, monkeypatch, capsys
):
    import json

    from tests.test_cli import cli

    scratch, destination = environment(tmp_path, monkeypatch)
    args = [
        "backup",
        "--destination",
        str(destination),
        "--validation-directory",
        str(scratch),
        "--archive-backup",
        "--framed-archive-validation",
    ]
    assert cli(monkeypatch, infrastructure, *args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "backup_integrity_ok"
    assert result["backup"]["source_validation"]["status"] == "integrity_ok"
    original = destination.read_bytes()
    assert cli(monkeypatch, infrastructure, *args, "--compressed-validation") == 2
    assert "without RAM mode" in capsys.readouterr().err
    assert cli(monkeypatch, infrastructure, "state-check", "--framed-archive-validation") == 2
    assert capsys.readouterr().out == ""
    assert destination.read_bytes() == original


def test_wrong_owner_or_expired_deadline_never_creates_a_qualified_destination(store, tmp_path, monkeypatch):
    scratch, destination = environment(tmp_path, monkeypatch)
    with pytest.raises(QualificationError, match="VALIDATED_OWNER_MISMATCH"):
        framed_backup.backup(
            store.path, destination, scratch, owner=store.owner_id + 1, deadline=time.monotonic() + 20
        )
    assert not destination.exists()
    with pytest.raises(QualificationError, match="TIME_BUDGET"):
        framed_backup.backup(
            store.path, destination, scratch, owner=store.owner_id, deadline=time.monotonic() - 1
        )
    assert not destination.exists()
