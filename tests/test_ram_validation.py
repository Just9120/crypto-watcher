import hashlib
import json
import sqlite3
import sys
import threading
import time
from contextlib import closing

import pytest

from cryptowatcher import validation
from cryptowatcher.__main__ import main
from cryptowatcher.storage import IntegrityError, check_integrity
from scripts import deploy_receiver as receiver


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    directory = tmp_path / "scratch"
    directory.mkdir()
    # Cross-platform fixtures exercise real SQLite/copy/hash, not Linux mount ownership.
    monkeypatch.setattr(validation, "scratch_space", lambda *args: None)
    return directory


def test_persistent_backup_ram_proof_and_cleanup(store, tmp_path, scratch):
    with store.db:
        store.db.execute("INSERT INTO runtime VALUES ('protected', '{\"data\":\"preserved\"}')")
    destination = tmp_path / "snapshot.sqlite3"
    result = store.backup(destination, validation_directory=scratch)
    assert result["phase"] == "COMPLETE" and result["validation_mode"] == "TMPFS"
    assert result["sha256"] == hashlib.sha256(destination.read_bytes()).hexdigest()
    with closing(sqlite3.connect(destination)) as copy:
        assert (
            copy.execute("SELECT payload_json FROM runtime WHERE key='protected'").fetchone()[0]
            == '{"data":"preserved"}'
        )
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize("damage", ["header", "constraint"])
def test_ram_validation_rejects_corruption_with_original_preserved(tmp_path, scratch, damage):
    source = tmp_path / "corrupt.sqlite3"
    with closing(sqlite3.connect(source)) as db:
        db.execute("CREATE TABLE checked(value TEXT CHECK(value='valid'))")
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute("INSERT INTO checked VALUES ('secret-never-print')")
        db.commit()
    if damage == "header":
        with source.open("r+b") as output:
            output.write(b"Invalid database")
    before = source.read_bytes()
    with pytest.raises(IntegrityError) as error:
        validation.validate_backup(source, scratch, deadline=time.monotonic() + 5)
    assert error.value.code == "DATABASE_CORRUPT" and "secret" not in str(error.value)
    assert source.read_bytes() == before and list(scratch.iterdir()) == []


@pytest.mark.parametrize("target", ["original", "clone"])
def test_hash_mismatch_after_successful_integrity_blocks_delivery(
    store, tmp_path, scratch, monkeypatch, target
):
    source = tmp_path / "snapshot.sqlite3"
    store.backup(source)

    def changed(db, **kwargs):
        check_integrity(db, **kwargs)
        path = source if target == "original" else next(scratch.glob("cw-backup-*/database.sqlite3"))
        with path.open("ab") as output:
            output.write(b"changed")

    monkeypatch.setattr(validation, "check_integrity", changed)
    with pytest.raises(IntegrityError) as error:
        validation.validate_backup(source, scratch, deadline=time.monotonic() + 5)
    assert error.value.code == "IO_ERROR" and list(scratch.iterdir()) == []


def test_live_wal_snapshot_releases_reader_and_returns_matching_metadata(store, scratch, monkeypatch):
    original_connect = sqlite3.connect
    changed = []

    class Source(sqlite3.Connection):
        def backup(self, destination, **kwargs):
            progress = kwargs["progress"]

            def concurrent(status, remaining, total):
                if not changed:

                    def writer():
                        with closing(original_connect(store.path)) as db:
                            db.execute("UPDATE owner_settings SET revision=revision+1")
                            db.commit()
                        changed.append(True)

                    thread = threading.Thread(target=writer)
                    thread.start()
                    thread.join(2)
                    assert not thread.is_alive()
                progress(status, remaining, total)

            super().backup(destination, **{**kwargs, "progress": concurrent})

    def connect(path, **kwargs):
        if "mode=ro" in str(path):
            kwargs["factory"] = Source
        return original_connect(path, **kwargs)

    def full_check(db, **kwargs):
        assert store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        check_integrity(db, **kwargs)

    monkeypatch.setattr(validation.sqlite3, "connect", connect)
    monkeypatch.setattr(validation, "check_integrity", full_check)
    with validation.validated_snapshot(store.path, scratch, deadline=time.monotonic() + 5) as db:
        assert db.execute("SELECT revision FROM owner_settings").fetchone()[0] == 1
    assert changed and store.snapshot().revision == 2 and list(scratch.iterdir()) == []


def test_cli_state_check_metadata_comes_from_validated_snapshot(store, scratch, monkeypatch, capsys):
    def update_live(db, **kwargs):
        check_integrity(db, **kwargs)
        with store.db:
            store.db.execute("UPDATE owner_settings SET revision=revision+1")

    monkeypatch.setattr(validation, "check_integrity", update_live)
    monkeypatch.setenv("OWNER_USER_ID", str(store.owner_id))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cryptowatcher",
            "state-check",
            "--database",
            str(store.path),
            "--validation-directory",
            str(scratch),
        ],
    )
    assert main() == 0
    value = json.loads(capsys.readouterr().out)
    assert value["validation_mode"] == "TMPFS" and value["configuration_revision"] == 1
    assert store.snapshot().revision == 2


@pytest.mark.parametrize("operation", ["backup", "state"])
def test_shared_deadline_failure_cleans_scratch_and_preserves_data(store, tmp_path, scratch, operation):
    source = tmp_path / "snapshot.sqlite3"
    store.backup(source)
    before = source.read_bytes()
    with pytest.raises(IntegrityError) as error:
        if operation == "backup":
            validation.validate_backup(source, scratch, deadline=0)
        else:
            with validation.validated_snapshot(store.path, scratch, deadline=0):
                pytest.fail("Expired snapshot must never be yielded")
    assert error.value.code == "TIME_BUDGET_EXCEEDED"
    assert source.read_bytes() == before and store.snapshot().revision == 1
    assert list(scratch.iterdir()) == []


def test_arbitrary_disk_directory_is_not_accepted_as_tmpfs(tmp_path):
    with pytest.raises(IntegrityError, match="tmpfs"):
        validation.scratch_space(tmp_path, 1)
    assert list(tmp_path.iterdir()) == []


def test_scratch_failure_is_pre_copy_and_preserves_original(store, tmp_path, scratch, monkeypatch):
    source = tmp_path / "snapshot.sqlite3"
    store.backup(source)
    before = source.read_bytes()

    def unavailable(*args):
        raise IntegrityError("DATABASE_ERROR", "Insufficient validation scratch space")

    monkeypatch.setattr(validation, "scratch_space", unavailable)
    with pytest.raises(IntegrityError) as error:
        validation.validate_backup(source, scratch, deadline=time.monotonic() + 5)
    assert error.value.code == "DATABASE_ERROR" and source.read_bytes() == before
    assert list(scratch.iterdir()) == []


def test_open_backup_sidecars_are_refused_without_copy(store, tmp_path, scratch):
    source = tmp_path / "snapshot.sqlite3"
    store.backup(source)
    sidecar = tmp_path / "snapshot.sqlite3-wal"
    sidecar.write_bytes(b"preserve")
    with pytest.raises(IntegrityError, match="closed"):
        validation.validate_backup(source, scratch, deadline=time.monotonic() + 5)
    assert sidecar.read_bytes() == b"preserve" and list(scratch.iterdir()) == []


def test_memory_allocation_is_bounded_and_insufficient_or_unknown_memory_blocks():
    size = 5_422_329_856
    allocation = receiver.validation_resources(size, 7_665_373_184)
    assert allocation["memory_limit_bytes"] + allocation["host_reserve_bytes"] <= 7_665_373_184
    assert allocation["scratch_bytes"] >= size + validation.HEADROOM
    args = receiver.validation_args(["docker", "run", "--network=none", "image"], allocation)
    assert args[-1] == "image" and args[args.index("--memory-swap") + 1] == str(
        allocation["memory_limit_bytes"]
    )
    assert args[args.index("--cpus") + 1] == "1" and "uid=10001,gid=10001" in args[args.index("--tmpfs") + 1]
    for invalid_size, available in ((None, 10**10), (True, 10**10), (0, 10**10), (size, 10**9), (size, None)):
        with pytest.raises(receiver.DeploymentError):
            receiver.validation_resources(invalid_size, available)
