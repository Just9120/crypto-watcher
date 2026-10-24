"""Persistent source/backup identity and negative full-integrity gates."""

import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from types import SimpleNamespace

import pytest
import zstandard

from cryptowatcher import native_disk_backup as framed_backup
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
    assert proof["phase"] == "COMPLETE" and proof["validation_mode"] == framed_backup.MODE
    assert proof["source_validation"]["status"] == "integrity_ok"
    assert proof["source_validation"]["schema"] == 2
    assert proof["source_validation"]["snapshot_sha256"] == proof["sha256"]
    assert proof["archive_sha256"] == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert proof["sqlite_cache_kib"] == 128 * 1024
    assert proof["mmap_limit_bytes"] == 3 * 1024**3
    assert proof["temporary_raw_bytes"] == proof["bytes"] and proof["raw_snapshot_removed"] is True
    assert not (framed_backup.capture_directory(destination) / "snapshot.sqlite3").exists()
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


def test_framed_backup_records_actual_configured_cache_and_refuses_mismatch(store, tmp_path, monkeypatch):
    from contextlib import contextmanager

    scratch, destination = environment(tmp_path, monkeypatch)
    snapshot = store.snapshot()

    original = framed_backup.validated_disk

    @contextmanager
    def wrong_cache(path, *, deadline):
        with original(path, deadline=deadline) as database:
            database.execute("PRAGMA cache_size=-8192")
            yield database

    monkeypatch.setattr(framed_backup, "validated_disk", wrong_cache)
    with pytest.raises(QualificationError, match="SQLITE_VALIDATION_ERROR") as failure:
        framed_backup.backup(
            store.path, destination, scratch, owner=store.owner_id, deadline=time.monotonic() + 20
        )
    assert failure.value.backup_metrics["phase"] == "FULL_INTEGRITY"
    assert store.snapshot() == snapshot
    assert (framed_backup.capture_directory(destination) / "snapshot.sqlite3").is_file()


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


def test_one_closed_file_digest_checks_capture_identity_without_a_duplicate_scan(
    store, tmp_path, monkeypatch
):
    scratch, destination = environment(tmp_path, monkeypatch)
    original = framed_backup.Image.reconstructed_hash
    readers = []

    def observed(image):
        readers.append(image.blocks)
        return original(image)

    monkeypatch.setattr(framed_backup.Image, "reconstructed_hash", observed)
    proof = framed_backup.backup(
        store.path, destination, scratch, owner=store.owner_id, deadline=time.monotonic() + 20
    )
    assert proof["phase"] == "COMPLETE"
    # Raw bytes are fingerprinted before CHECK and measured again while encoding;
    # one separately reopened archive reader proves the persisted representation.
    assert len(readers) == 1
    assert all(arena.file.closed for arena in readers)


@pytest.mark.parametrize("digest", [None, "0" * 64])
def test_independent_digest_mismatch_never_transfers_source_integrity(store, tmp_path, monkeypatch, digest):
    scratch, destination = environment(tmp_path, monkeypatch)
    original = framed_backup.Image.from_archive_store
    snapshot = store.snapshot()

    def mismatched(arena):
        image = original(arena)
        image.reconstructed_hash = lambda: digest
        return image

    monkeypatch.setattr(framed_backup.Image, "from_archive_store", mismatched)
    with pytest.raises(QualificationError, match="BYTE_IDENTITY_MISMATCH") as failure:
        framed_backup.backup(
            store.path, destination, scratch, owner=store.owner_id, deadline=time.monotonic() + 20
        )
    assert failure.value.backup_metrics["phase"] == "INDEPENDENT_PERSISTED_READBACK"
    assert store.snapshot() == snapshot and destination.exists()


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
    assert (framed_backup.capture_directory(destination) / "snapshot.sqlite3").exists()
    assert list(store.db.iterdump()) == original


def test_archive_mutation_during_final_fingerprint_cannot_inherit_source_integrity(
    store, tmp_path, monkeypatch
):
    scratch, destination = environment(tmp_path, monkeypatch)
    original = framed_backup.fingerprint

    def changed(path, deadline):
        if path == destination:
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


def test_disk_admission_precedes_capture_and_charges_raw_archive_and_concurrent_headroom(
    store, tmp_path, monkeypatch
):
    scratch, destination = environment(tmp_path, monkeypatch)
    original = list(store.db.iterdump())
    monkeypatch.setattr(framed_backup.shutil, "disk_usage", lambda path: SimpleNamespace(free=6 * 1024**3))
    with pytest.raises(QualificationError, match="ARCHIVE_DISK_RESERVE"):
        framed_backup.backup(
            store.path, destination, scratch, owner=store.owner_id, deadline=time.monotonic() + 20
        )
    assert not destination.exists() and not framed_backup.capture_directory(destination).exists()
    assert list(store.db.iterdump()) == original
    raw = 10 * 1024**3
    free = raw + framed_backup.DISK_RESERVE_BYTES + framed_backup.CONCURRENT_HEADROOM_BYTES + 2 * 1024**3
    assert framed_backup.capacity(raw, free) == 2 * 1024**3


def test_foreign_key_failure_preserves_private_capture_and_never_qualifies_archive(
    store, tmp_path, monkeypatch
):
    scratch, destination = environment(tmp_path, monkeypatch)
    store.db.executescript(
        "PRAGMA foreign_keys=OFF; CREATE TABLE parent_proof(id INTEGER PRIMARY KEY); "
        "CREATE TABLE child_proof(value REFERENCES parent_proof(id)); INSERT INTO child_proof VALUES(7);"
    )
    original = list(store.db.iterdump())
    with pytest.raises(QualificationError, match="DATABASE_CORRUPT"):
        framed_backup.backup(
            store.path, destination, scratch, owner=store.owner_id, deadline=time.monotonic() + 20
        )
    capture = framed_backup.capture_directory(destination)
    raw = capture / "snapshot.sqlite3"
    assert raw.is_file() and not destination.exists()
    assert json.loads((capture / "receipt.json").read_text())["state"] == "INCOMPLETE_RETAINED"
    if os.name == "posix":
        assert capture.stat().st_mode & 0o777 == 0o700
        assert raw.stat().st_mode & 0o777 == 0o600
        assert (capture / "receipt.json").stat().st_mode & 0o777 == 0o600
    assert list(store.db.iterdump()) == original


def test_pinned_capture_ignores_later_committed_writes_without_blocking_writer(store, tmp_path, monkeypatch):
    scratch, destination = environment(tmp_path, monkeypatch)
    store.put_runtime("pinned-proof", {"value": "before"})
    original_capacity = framed_backup.capacity

    def later_write(raw_bytes, free):
        # Admission occurs after BEGIN + the first read, before native backup.
        store.put_runtime("pinned-proof", {"value": "after"})
        return original_capacity(raw_bytes, free)

    monkeypatch.setattr(framed_backup, "capacity", later_write)
    proof = framed_backup.backup(
        store.path, destination, scratch, owner=store.owner_id, deadline=time.monotonic() + 20
    )
    restored = tmp_path / "concurrent-restored.db"
    with destination.open("rb") as source, restored.open("xb") as output:
        zstandard.ZstdDecompressor().copy_stream(source, output)
    assert hashlib.sha256(restored.read_bytes()).hexdigest() == proof["sha256"]
    with closing(Store(restored, store.owner_id, readonly=True)) as reader:
        assert reader.get_runtime("pinned-proof") == {"value": "before"}
    assert store.get_runtime("pinned-proof") == {"value": "after"}


def test_mutation_after_full_check_cannot_inherit_integrity_and_preserves_capture(
    store, tmp_path, monkeypatch
):
    scratch, destination = environment(tmp_path, monkeypatch)
    original_image = framed_backup.Image

    def changed(path, **kwargs):
        with path.open("r+b") as stream:
            stream.seek(-1, 2)
            stream.write(b"x")
        return original_image(path, **kwargs)

    monkeypatch.setattr(framed_backup, "Image", changed)
    with pytest.raises(QualificationError, match="BYTE_IDENTITY_MISMATCH"):
        framed_backup.backup(
            store.path, destination, scratch, owner=store.owner_id, deadline=time.monotonic() + 20
        )
    assert (framed_backup.capture_directory(destination) / "snapshot.sqlite3").is_file()
    assert destination.exists()
