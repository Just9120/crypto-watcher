import hashlib
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cryptowatcher.config import OwnerSettings
from cryptowatcher.reset_history import prepare as candidate
from cryptowatcher.reset_history import private_state, signature
from cryptowatcher.storage import Store
from scripts import deploy_receiver as receiver
from scripts import reset_delivery as reset


def require(value, message):
    if not value:
        raise RuntimeError(message)


@pytest.fixture
def cutover(tmp_path, monkeypatch):
    app = tmp_path / "cw"
    root = app / "data/sqlite"
    root.mkdir(parents=True)
    source = root / "cryptowatcher.sqlite3"
    old = Store(source, 233382575)
    old.initialize(OwnerSettings())
    private = private_state(old)
    old.close()
    work = root / "history-reset-proof"
    work.mkdir(mode=0o700)
    proof = candidate(
        source.resolve(), (work / "fresh.sqlite3").resolve(), (work / "private.json").resolve(), 233382575
    )
    (app / ".env").write_bytes(b"private config placeholder")
    record = {
        "reset_directory": str(work),
        "reset_directory_inode": [work.stat().st_dev, work.stat().st_ino],
        "reset_candidate_sha256": hashlib.sha256((work / "fresh.sqlite3").read_bytes()).hexdigest(),
        "reset_proof": proof,
    }
    permit = {
        "source_inode": source.stat().st_ino,
        "env_sha256": hashlib.sha256((app / ".env").read_bytes()).hexdigest(),
    }
    actual = Path.stat

    class Metadata:
        def __init__(self, value):
            self.value = value

        @property
        def st_uid(self):
            return 10001

        @property
        def st_mode(self):
            # Unit fixtures model the production Unix metadata on Windows too.
            return stat.S_IFDIR | 0o700 if stat.S_ISDIR(self.value.st_mode) else self.value.st_mode

        def __getattr__(self, name):
            return getattr(self.value, name)

    monkeypatch.setattr(
        Path,
        "stat",
        lambda p, *args, **kwargs: (
            Metadata(actual(p, *args, **kwargs))
            if p == work or p.parent == work or p.parent == root and p.name.startswith(source.name)
            else actual(p, *args, **kwargs)
        ),
    )
    monkeypatch.setattr(reset, "source_handles", lambda path: [])
    monkeypatch.setattr(reset, "store_permit", lambda value: None)
    if os.name == "nt":
        from contextlib import nullcontext

        monkeypatch.setattr(reset, "_directory_sync", lambda p: nullcontext())

    def phase(name):
        record["phase"] = name

    return SimpleNamespace(
        app=app,
        root=root,
        source=source,
        work=work,
        record=record,
        permit=permit,
        private=private,
        phase=phase,
    )


def test_reset_promotion_and_exact_old_working_file_cleanup_preserve_settings_and_existing_backups(cutover):
    v = cutover
    backups = v.root / "backups"
    backups.mkdir()
    keep = backups / "keep.sqlite3"
    keep.write_bytes(b"untouched existing backup")
    reset.promote(v.app, v.permit, require, v.phase, v.record)
    assert v.record["reset_promoted"] is True
    fresh = Store(v.source, 233382575)
    try:
        assert signature(private_state(fresh)) == v.record["reset_proof"]["private_fresh_sha256"]
        assert fresh.bounded
    finally:
        fresh.close()
    diagnostics = {"database": {"schema": 2}, "history_generation": v.record["reset_proof"]["generation"]}
    reset.finish(v.app, v.permit, diagnostics, require, v.phase, v.record)
    assert 1 <= v.record["reset_old_working_files_removed"] <= 4
    assert keep.read_bytes() == b"untouched existing backup"
    assert (v.work / "private.json").is_file()
    assert (v.work / "fresh.sqlite3.private-restore").is_file()
    assert not (v.work / "discarded-market.sqlite3").exists()


def test_cleanup_rejects_existing_backup_as_a_discard_target(cutover):
    v = cutover
    reset.promote(v.app, v.permit, require, v.phase, v.record)
    keep = v.root / "keep.sqlite3"
    keep.write_bytes(b"preserve")
    v.record["reset_discard_scope"][0]["path"] = str(keep)
    with pytest.raises(RuntimeError, match="Unexpected discard target"):
        reset.finish(
            v.app,
            v.permit,
            {"database": {"schema": 2}, "history_generation": v.record["reset_proof"]["generation"]},
            require,
            v.phase,
            v.record,
        )
    assert keep.read_bytes() == b"preserve"
    assert (v.work / "discarded-market.sqlite3").is_file()


def test_unknown_source_handle_refuses_promotion_before_any_rename(cutover, monkeypatch):
    v = cutover
    monkeypatch.setattr(reset, "source_handles", lambda path: [999])
    with pytest.raises(RuntimeError, match="handles"):
        reset.promote(v.app, v.permit, require, v.phase, v.record)
    assert v.source.is_file() and (v.work / "fresh.sqlite3").is_file()


def test_recovery_never_starts_legacy_container_after_partial_promotion(cutover):
    v = cutover
    v.record.update(reset_stopped_container="old", reset_promotion_started=True)
    run = Mock()
    reset.recover_pre_switch(v.app, run, require, v.record)
    assert not run.called
    assert v.record["reset_recovery"] == "INSPECT_PROMOTION_RECORD"


def test_disk_backup_keeps_source_and_destination_checks_hashes_and_memory_cap(tmp_path, monkeypatch):
    app = tmp_path / "app"
    data = app / "data/sqlite"
    data.mkdir(parents=True)
    (data / ".g1-migration.json").write_text(json.dumps({"legacy_sha256": "a" * 64}))
    monkeypatch.setattr(receiver, "APP", app)
    monkeypatch.setattr(receiver, "ACTIVE_RECORD", {})
    monkeypatch.setattr(receiver, "OPERATION_DEADLINE", None)
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        assert "--memory" in args and args[args.index("--memory") + 1] == "4g"
        assert "--compressed-validation" not in args
        if "state-check" in args:
            return json.dumps(
                {"schema": 2, "status": "integrity_ok", "legacy_sha256": "a" * 64, "validation_mode": "DISK"}
            )
        return json.dumps(
            {
                "status": "backup_integrity_ok",
                "backup": {
                    "phase": "COMPLETE",
                    "validation_mode": "DISK_STREAMING",
                    "sha256": "b" * 64,
                    "bytes": 1234,
                },
            }
        )

    monkeypatch.setattr(receiver, "run", run)
    monkeypatch.setattr(receiver.shutil, "disk_usage", lambda p: SimpleNamespace(free=10 * 1024**3))
    receiver.disk_backup_preflight(
        ["docker", "run", "image"], {"database": {"schema": 2, "snapshot_bytes": 1234}}, "f" * 40, 900
    )
    assert (
        len(calls) == 2 and calls[0][1]["timeout"] <= 900 and calls[1][1]["timeout"] <= calls[0][1]["timeout"]
    )
    assert receiver.ACTIVE_RECORD["state_validation"]["status"] == "integrity_ok"


def test_optional_delivery_deadline_cannot_inject_commands_or_change_identity():
    command = "cryptowatcher-deploy " + "a" * 40 + " " + "b" * 64 + " 123"
    assert receiver.command_arguments(command + " 1792149999") == receiver.command_arguments(command)
    for suffix in (" 0", " 1792149999;whoami", " 1792149999 extra"):
        with pytest.raises(receiver.DeploymentError):
            receiver.command_arguments(command + suffix)


@pytest.mark.skipif(os.name == "nt", reason="Linux procfs handle inventory")
def test_source_handle_inventory_identifies_actual_open_file(tmp_path):
    p = tmp_path / "source.sqlite3"
    p.write_bytes(b"fixture")
    with p.open("rb"):
        assert os.getpid() in reset.source_handles(p)


def test_private_post_start_readback_allows_only_digest_cursor(cutover):
    from cryptowatcher.reset_history import verify_private

    v = cutover
    fresh = v.work / "fresh.sqlite3"
    assert verify_private(fresh, v.work / "private.json", 233382575)["state"] == "PASS"
    with __import__("contextlib").closing(Store(fresh, 233382575)) as store:
        store.db.execute("UPDATE delivery_preferences SET enabled=0")
        store.db.commit()
    with pytest.raises(Exception, match="configuration mismatch"):
        verify_private(fresh, v.work / "private.json", 233382575)


def test_reset_installer_preflight_binds_ci_tree_private_config_and_script_hashes(cutover, monkeypatch):
    import sys

    from scripts import install_history_reset as installer

    v = cutover
    deploy_root = v.app / "root-owned"
    deploy_root.mkdir()
    installed = deploy_root / "receiver.py"
    installed.write_bytes(b"old reviewed receiver")
    target = deploy_root / "target.json"
    target.write_bytes(b"fixed target")
    helper = deploy_root / "reset_delivery.py"
    receiver_bytes = b"pass # reviewed receiver"
    helper_bytes = b"pass # reviewed helper"
    monkeypatch.setattr(installer, "APP", v.app)
    monkeypatch.setattr(installer, "RECEIVER", installed)
    monkeypatch.setattr(installer, "HELPER", helper)
    monkeypatch.setattr(installer, "TARGET", target)
    monkeypatch.setattr(installer, "PERMIT", deploy_root / "permit.json")
    monkeypatch.setattr(installer, "OLD_RECEIVER", installer.sha(installed.read_bytes()))
    monkeypatch.setattr(installer, "TARGET_HASH", installer.sha(target.read_bytes()))
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(installer.socket, "gethostname", lambda: "vmi3224957")
    if os.name == "nt":
        monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace())
    meta = Path.stat

    def metadata(path, *args, **kwargs):
        value = meta(path, *args, **kwargs)
        if path in (installed, v.app / ".env"):
            return SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o600)
        return value

    monkeypatch.setattr(Path, "stat", metadata)
    gate = Mock()
    monkeypatch.setattr(installer, "gate", gate)
    monkeypatch.setattr(
        installer,
        "live",
        lambda: {
            "Id": "container",
            "Image": "image",
            "State": {"Running": True},
            "Config": {
                "Labels": {
                    "org.opencontainers.image.revision": installer.BASELINE,
                    "com.docker.compose.project": "crypto_watcher",
                    "com.docker.compose.service": "crypto_watcher",
                }
            },
        },
    )

    def fetch(url):
        if url.endswith("/deploy_receiver.py"):
            return receiver_bytes
        if url.endswith("/reset_delivery.py"):
            return helper_bytes
        return json.dumps({"tree": {"sha": "a" * 40}}).encode()

    monkeypatch.setattr(installer, "read_url", fetch)
    result = installer.install(
        installer.BASELINE, "b" * 40, 37, installer.sha(receiver_bytes), installer.sha(helper_bytes)
    )
    assert result["state"] == "PREFLIGHT_PASS" and not helper.exists()
    assert result["private_sha256"] == signature(v.private)
    assert installed.read_bytes() == b"old reviewed receiver"
    gate.assert_called_once()
    with pytest.raises(RuntimeError, match="hash mismatch"):
        installer.install(installer.BASELINE, "b" * 40, 37, "c" * 64, installer.sha(helper_bytes))


def test_inventory_separates_hardlinked_backups_experiments_and_working_database(tmp_path):
    from scripts.storage_inventory import inventory

    app = tmp_path / "app"
    backups = app / "data/sqlite/backups"
    backups.mkdir(parents=True)
    (backups / "saved").write_bytes(b"archive")
    trials = app / "rehearsals"
    trials.mkdir()
    try:
        os.link(backups / "saved", trials / "copy")
    except OSError:
        pytest.skip("Hardlinks unavailable")
    (app / "data/sqlite/cryptowatcher.sqlite3").write_bytes(b"working excluded")
    value = inventory(app)
    assert value["categories"]["backups"]["logical_bytes"] == 7
    assert value["categories"]["experiments"]["logical_bytes"] == 7
    assert value["categories"]["experiments"]["unique_allocated_bytes"] == 0
    assert value["categories"]["reset_private_and_staging"]["files"] == 0
    assert not value["files_changed"]
