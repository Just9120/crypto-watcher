"""Large schema2 delivery retains full proof, bounded resources and one deadline."""

import hashlib
import json
import sqlite3
import subprocess
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import zstandard

from cryptowatcher import validation
from cryptowatcher.market_storage import enable
from cryptowatcher.outcomes import OutcomeWorker
from cryptowatcher.storage import Store
from scripts import deploy_receiver as receiver
from scripts.deliver_release import delivery_result
from tests.test_outcomes import NOW, future, observe


def environment(tmp_path, monkeypatch, *, state_mode="COMPRESSED_RAM", state_schema=1, schema=1):
    folder = tmp_path / "data/sqlite"
    folder.mkdir(parents=True)
    (folder / "backups").mkdir()
    (folder / ".g1-migration.json").write_text(json.dumps({"legacy_sha256": "a" * 64}))
    monkeypatch.setattr(receiver, "APP", tmp_path)
    monkeypatch.setattr(receiver, "ACTIVE_PATH", tmp_path / "record.json")
    monkeypatch.setattr(receiver, "ACTIVE_RECORD", {"state": "PRE_SWITCH"})
    monkeypatch.setattr(receiver, "OPERATION_DEADLINE", 1000.0)
    now = [100.0]
    monkeypatch.setattr(receiver, "time", SimpleNamespace(monotonic=lambda: now[0], time=lambda: 1000))
    monkeypatch.setattr(receiver, "memory_available", lambda: 8 * 1024**3)
    monkeypatch.setattr(receiver.shutil, "disk_usage", lambda path: SimpleNamespace(free=10 * 1024**3))
    before = {"database": {"schema": schema, "snapshot_bytes": 11_000_119_296}, "configuration_revision": 5}
    proof = {
        "phase": "COMPLETE",
        "validation_mode": "COMPRESSED_RAM",
        "sha256": "b" * 64,
        "archive_encoding": "ZSTD",
        "archive_bytes": 4096,
        "archive_sha256": "c" * 64,
    }
    calls = []

    def run(command, *, timeout=120):
        calls.append((command, timeout))
        if "diagnostics" in command:
            return json.dumps(before)
        assert command[command.index("--memory") + 1] == str(4 * 1024**3)
        assert command[command.index("--memory-swap") + 1] == str(4 * 1024**3)
        assert command[command.index("--cpus") + 1] == "1"
        assert "--compressed-validation" in command and "--validation-directory" in command
        if "state-check" in command:
            # Resource evidence is already durable when the operation begins.
            saved = json.loads((tmp_path / "record.json").read_text())
            assert saved["state_validation_resources"]["memory_limit_bytes"] == 4 * 1024**3
            now[0] += 200
            return json.dumps(
                {"schema": state_schema, "legacy_sha256": "a" * 64, "validation_mode": state_mode}
            )
        assert "backup" in command and "--archive-backup" in command
        assert command[command.index("--destination") + 1].endswith(".sqlite3.zst")
        now[0] += 10
        return json.dumps({"status": "backup_integrity_ok", "backup": proof})

    monkeypatch.setattr(receiver, "run", run)
    return before, proof, calls


def test_legacy_schema1_full_source_and_archived_backup_share_prep_budget_and_fixed_caps(
    tmp_path, monkeypatch
):
    before, proof, calls = environment(tmp_path, monkeypatch)
    name, actual = receiver.full_backup_preflight(["docker", "run", "image"], before, "d" * 40, 900)
    assert actual == proof and name.endswith(".sqlite3.zst")
    operations = [(args, timeout) for args, timeout in calls if "diagnostics" not in args]
    assert [timeout for _, timeout in operations] == [690, 490]
    assert [int(args[args.index("--timeout-seconds") + 1]) for args, _ in operations] == [675, 475]
    assert receiver.ACTIVE_RECORD["state_validation"]["schema"] == 1
    assert receiver.ACTIVE_RECORD["backup_validation"] == proof


@pytest.mark.parametrize("size", [4096, 3 * 1024**3])
def test_small_schema2_retains_existing_disk_validation(tmp_path, monkeypatch, size):
    before, _, _ = environment(tmp_path, monkeypatch, schema=2)
    before["database"]["snapshot_bytes"] = size
    disk = Mock(return_value=("disk.sqlite3", {"phase": "COMPLETE"}))
    monkeypatch.setattr(receiver, "disk_backup_preflight", disk)
    assert receiver.full_backup_preflight(["image"], before, "d" * 40, 900)[0] == "disk.sqlite3"
    disk.assert_called_once()


@pytest.mark.parametrize("mode,schema", [("DISK", 1), ("COMPRESSED_RAM", 2)])
def test_wrong_full_source_proof_cannot_admit_backup_or_fallback(tmp_path, monkeypatch, mode, schema):
    before, _, calls = environment(tmp_path, monkeypatch, state_mode=mode, state_schema=schema)
    with pytest.raises(receiver.DeploymentError):
        receiver.full_backup_preflight(["image"], before, "d" * 40, 900)
    assert len(calls) == 1 and "state-check" in calls[0][0]


def test_expired_shared_prep_budget_cannot_start_second_stage(tmp_path, monkeypatch):
    before, _, calls = environment(tmp_path, monkeypatch)
    with pytest.raises(receiver.DeploymentError, match="budget"):
        receiver.full_backup_preflight(["image"], before, "d" * 40, 100)
    assert not any("backup" in args for args, _ in calls)


def test_process_timeout_has_safe_primary_code_and_actions_classification(monkeypatch):
    record = {"state": "PRE_SWITCH", "phase": "CHECK_STATE_INTEGRITY"}
    monkeypatch.setattr(receiver, "ACTIVE_RECORD", record)
    monkeypatch.setattr(receiver, "OPERATION_DEADLINE", None)
    monkeypatch.setattr(
        receiver.subprocess,
        "run",
        Mock(side_effect=subprocess.TimeoutExpired(["docker", "secret-never-export"], 5)),
    )
    with pytest.raises(receiver.DeploymentError) as error:
        receiver.run(["docker", "run", "image"], timeout=5)
    assert "secret" not in str(error.value) and record["failure_code"] == "TIME_BUDGET_EXCEEDED"
    output = subprocess.CompletedProcess(
        [],
        1,
        stdout=b"",
        stderr=json.dumps(
            {"state": "BLOCKED", "reason": str(error.value), "failure_phase": record["phase"]}
        ).encode(),
    )
    result, status = delivery_result(output, revision="a" * 40, digest="b" * 64, build_run=123)
    assert status == 1 and result["failure_code"] == "TIME_BUDGET_EXCEEDED"
    assert result["runtime_state"] == "UNCONFIRMED" and "secret" not in json.dumps(result)


def test_real_schema2_archived_backup_restores_every_table_and_domain_value(store, tmp_path, monkeypatch):
    enable(store.db)
    store.bounded = True
    store.clock_ms = lambda: NOW
    worker = OutcomeWorker(store)
    observe(store, worker)
    from cryptowatcher.sources import Sources

    Sources(store).put("spot/BTCUSDT/1m", future())
    worker.work(NOW + 6 * 60_000)
    original = list(store.db.iterdump())
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(validation, "scratch_space", lambda *args: None)
    archive = tmp_path / "schema2.sqlite3.zst"
    proof = store.backup(archive, validation_directory=scratch, compressed_validation=True, archive=True)
    assert proof["phase"] == "COMPLETE" and proof["validation_mode"] == "COMPRESSED_RAM"
    assert proof["archive_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    restored = tmp_path / "restored.sqlite3"
    with archive.open("rb") as source, restored.open("xb") as target:
        zstandard.ZstdDecompressor().copy_stream(source, target)
    assert hashlib.sha256(restored.read_bytes()).hexdigest() == proof["sha256"]
    with closing(Store(restored, store.owner_id, readonly=True)) as reader:
        assert reader.db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert list(reader.db.iterdump()) == original
        assert reader.snapshot() == store.snapshot()
    with closing(sqlite3.connect(restored)) as reader:
        assert reader.execute("PRAGMA foreign_key_check").fetchall() == []
    assert list(scratch.iterdir()) == [] and list(store.db.iterdump()) == original
