"""Offline adoption binds source/private state and never resets retained data."""

import json
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from cryptowatcher import shared_activation as activation
from cryptowatcher import shared_record_work
from cryptowatcher.lease import RuntimeLease
from cryptowatcher.reset_history import private_state, signature
from cryptowatcher.storage import StateError, Store
from scripts import qualify_numeric_boundary as numeric

SOURCE = "a" * 40
GENERATION = "1" * 32


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    path = tmp_path / "owned.sqlite3"
    numeric.materialize(path, 1)
    with closing(Store(path, 233382575)) as store:
        store.put_runtime("history_generation", {"id": GENERATION, "started_ms": 1_800_000_000_000})
        store.put_runtime("radar:v1:agent:preference", {"enabled": False})
        store.db.commit()
        options = dict(
            owner=store.owner_id,
            expected_source=SOURCE,
            expected_generation=GENERATION,
            expected_revision=store.snapshot().revision,
            expected_private=signature(private_state(store)),
            timeout_seconds=20,
        )
    monkeypatch.setenv("SOURCE_REVISION", SOURCE)
    return path, tmp_path / "backup.sqlite3", options


def rows(path: Path):
    with closing(Store(path, 233382575, readonly=True)) as store:
        return {
            name: [tuple(row) for row in store.db.execute('SELECT * FROM "' + name + '"')]
            for (name,) in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }


def test_explicit_backup_upgrade_reopen_preserves_every_original_row_and_repeat_is_readonly(prepared):
    path, backup, options = prepared
    before = rows(path)
    result = activation.activate(path, backup, **options)
    assert result["phase"] == "COMPLETE" and result["writer_enabled"]
    assert result["backup"]["phase"] == "COMPLETE"
    assert result["history_generation"]["id"] == GENERATION
    assert result["configuration_revision"] == options["expected_revision"]
    assert result["private_sha256"] == options["expected_private"]
    assert rows(backup) == before
    after = rows(path)
    for name, original in before.items():
        if name in ("market_meta", "market_counts"):
            assert set(original).issubset(set(after[name]))
        else:
            assert after[name] == original
    with closing(Store(path, options["owner"])) as store:
        shared_record_work.validate(store.db)
        store.db.execute("UPDATE market_meta SET value='0' WHERE key='shared_record_writer'")
        store.db.commit()
    repeat_before = rows(path)
    repeat = activation.activate(path, backup, **options)
    assert repeat["phase"] == "ALREADY_ENABLED" and not repeat["writer_enabled"]
    assert rows(path) == repeat_before and rows(backup) == before


@pytest.mark.parametrize(
    "change",
    [
        {"expected_source": "b" * 40},
        {"expected_generation": "2" * 32},
        {"expected_revision": 2},
        {"expected_private": "0" * 64},
        {"owner": 233382576},
        {"timeout_seconds": 0},
        {"timeout_seconds": 901},
    ],
)
def test_wrong_identity_or_budget_creates_no_backup_or_layout(prepared, change):
    path, backup, options = prepared
    before = rows(path)
    with pytest.raises(StateError):
        activation.activate(path, backup, **(options | change))
    assert not backup.exists() and rows(path) == before


def test_active_runtime_lease_refuses_the_second_writer(prepared):
    path, backup, options = prepared
    before = rows(path)
    with RuntimeLease(path), pytest.raises(StateError, match="Another CryptoWatcher writer"):
        activation.activate(path, backup, **options)
    assert not backup.exists() and rows(path) == before


def test_existing_backup_sidecar_is_preserved_before_any_install(prepared):
    path, backup, options = prepared
    sidecar = backup.with_name(backup.name + "-wal")
    sidecar.write_bytes(b"unknown retained file")
    before = rows(path)
    with pytest.raises(StateError, match="already exists"):
        activation.activate(path, backup, **options)
    assert not backup.exists() and sidecar.read_bytes() == b"unknown retained file"
    assert rows(path) == before


def test_unknown_packet_guard_fails_before_backup_creation(prepared):
    path, backup, options = prepared
    with closing(sqlite3.connect(path)) as db:
        db.execute("DROP TRIGGER market_packet_move")
    before = rows(path)
    with pytest.raises(StateError, match="Unknown source packet"):
        activation.activate(path, backup, **options)
    assert not backup.exists() and rows(path) == before


def test_backup_failure_never_installs_and_retains_its_file(prepared, monkeypatch):
    path, backup, options = prepared
    before = rows(path)

    def fail(self, destination, **kwargs):
        destination.write_bytes(b"incomplete backup retained for diagnosis")
        raise StateError("Injected backup failure")

    monkeypatch.setattr(Store, "backup", fail)
    with pytest.raises(StateError, match="backup failure"):
        activation.activate(path, backup, **options)
    assert rows(path) == before and backup.read_bytes() == b"incomplete backup retained for diagnosis"


def test_late_backup_stops_before_install(prepared, monkeypatch):
    path, backup, options = prepared
    before = rows(path)

    def late(self, destination, **kwargs):
        at = time.monotonic()
        monkeypatch.setattr(activation, "time", SimpleNamespace(monotonic=lambda: at + 100))
        return {"phase": "COMPLETE"}

    monkeypatch.setattr(Store, "backup", late)
    with pytest.raises(StateError, match="time budget exceeded"):
        activation.activate(path, backup, **options)
    assert not backup.exists() and rows(path) == before


def test_private_change_during_backup_rejects_stale_approval_before_ddl(prepared, monkeypatch):
    path, backup, options = prepared

    def changed(self, destination, **kwargs):
        self.db.execute("UPDATE owner_settings SET revision=revision+1")
        self.db.commit()
        return {"phase": "COMPLETE"}

    monkeypatch.setattr(Store, "backup", changed)
    with pytest.raises(StateError, match="identity changed"):
        activation.activate(path, backup, **options)
    with closing(Store(path, options["owner"])) as reopened:
        assert reopened.snapshot().revision == 2
        assert not reopened.db.execute("SELECT 1 FROM market_meta WHERE key='shared_records'").fetchone()


def test_known_partial_layout_can_resume_with_a_new_backup_without_replacing_data(prepared, monkeypatch):
    path, backup, options = prepared
    before = rows(path)
    install = shared_record_work.install

    def fail(*args, **kwargs):
        raise RuntimeError("Injected after atomic layout2")

    monkeypatch.setattr(shared_record_work, "install", fail)
    with pytest.raises(RuntimeError, match="atomic layout2"):
        activation.activate(path, backup, **options)
    assert rows(backup) == before
    with closing(Store(path, options["owner"], readonly=True)) as reader:
        assert (
            reader.db.execute("SELECT value FROM market_meta WHERE key='shared_records'").fetchone()[0] == "2"
        )
        assert signature(private_state(reader)) == options["expected_private"]
    monkeypatch.setattr(shared_record_work, "install", install)
    second_backup = path.with_name("partial-backup.sqlite3")
    result = activation.activate(path, second_backup, **options)
    assert result["phase"] == "COMPLETE" and rows(backup) == before
    assert second_backup.exists()


def test_cli_requires_every_identity_and_does_not_ignore_backup_mode_flags(prepared, monkeypatch, capsys):
    from cryptowatcher.__main__ import main

    path, backup, options = prepared
    prefix = ["cryptowatcher", "enable-shared-storage", "--database", str(path), "--destination", str(backup)]
    monkeypatch.setattr(sys, "argv", prefix)
    assert main() == 2 and "requires" in json.loads(capsys.readouterr().err)["reason"]
    flags = [
        "--owner-id",
        str(options["owner"]),
        "--expected-source",
        SOURCE,
        "--expected-generation",
        GENERATION,
        "--expected-configuration-revision",
        "1",
        "--expected-private-sha256",
        options["expected_private"],
        "--archive-backup",
    ]
    monkeypatch.setattr(sys, "argv", prefix + flags)
    assert main() == 2 and "archive-backup requires" in json.loads(capsys.readouterr().err)["reason"]
    assert not backup.exists()


def test_readonly_preflight_token_drives_explicit_cli_without_implicit_install(prepared, monkeypatch, capsys):
    from cryptowatcher.__main__ import main

    path, backup, options = prepared
    before = rows(path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cryptowatcher",
            "shared-storage-status",
            "--database",
            str(path),
            "--owner-id",
            str(options["owner"]),
        ],
    )
    assert main() == 0
    token = json.loads(capsys.readouterr().out)
    assert token["phase"] == "READ_ONLY_SNAPSHOT" and token["layout"] is None
    assert token["source_revision"] == SOURCE and token["private_sha256"] == options["expected_private"]
    assert rows(path) == before and not backup.exists()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cryptowatcher",
            "enable-shared-storage",
            "--database",
            str(path),
            "--destination",
            str(backup),
            "--owner-id",
            str(options["owner"]),
            "--expected-source",
            token["source_revision"],
            "--expected-generation",
            token["history_generation"]["id"],
            "--expected-configuration-revision",
            str(token["configuration_revision"]),
            "--expected-private-sha256",
            token["private_sha256"],
        ],
    )
    assert main() == 0 and json.loads(capsys.readouterr().out)["phase"] == "COMPLETE"
    assert rows(backup) == before


def test_repeated_request_refuses_an_unknown_installed_guard_without_repair(prepared):
    path, backup, options = prepared
    activation.activate(path, backup, **options)
    with closing(sqlite3.connect(path)) as db:
        db.execute("DROP TRIGGER market_shared_pending_keep")
    before = rows(path)
    second = path.with_name("not-created.sqlite3")
    with pytest.raises(ValueError, match="guard"):
        activation.activate(path, second, **options)
    assert rows(path) == before and not second.exists()
