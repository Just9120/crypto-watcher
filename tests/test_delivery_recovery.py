import json
import sqlite3
import threading
from unittest.mock import Mock

import pytest

from cryptowatcher.config import OwnerSettings
from cryptowatcher.storage import StateError, Store
from scripts import deploy_receiver as receiver


def test_backup_pins_snapshot_while_wal_writer_advances(tmp_path, monkeypatch):
    store = Store(tmp_path / "source.sqlite", 233382575)
    store.initialize(OwnerSettings())
    with store.db:
        store.db.executemany(
            "INSERT INTO runtime VALUES (?,?)", ((str(i), '"' + "x" * 16000 + '"') for i in range(200))
        )
    original_connect = sqlite3.connect
    changed = []

    class Source(sqlite3.Connection):
        def backup(self, destination, **kwargs):
            original_progress = kwargs["progress"]

            def progress(status, remaining, total):
                if not changed:

                    def write():
                        writer = original_connect(store.path)
                        with writer:
                            writer.execute("UPDATE owner_settings SET revision=revision+1")
                        writer.close()
                        changed.append(True)

                    thread = threading.Thread(target=write)
                    thread.start()
                    thread.join(2)
                    assert not thread.is_alive()
                original_progress(status, remaining, total)

            return super().backup(destination, **{**kwargs, "progress": progress})

    def connect(path, **kwargs):
        return (
            original_connect(path, **{**kwargs, "factory": Source})
            if "mode=ro" in str(path)
            else original_connect(path, **kwargs)
        )

    monkeypatch.setattr(sqlite3, "connect", connect)
    destination = tmp_path / "backup.sqlite"
    store.backup(destination)
    restored = Store(destination, 233382575, readonly=True)
    assert changed and store.snapshot().revision == 2 and restored.snapshot().revision == 1
    assert restored.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert (
        restored.db.execute("SELECT key,payload_json FROM runtime ORDER BY key").fetchall()
        == store.db.execute("SELECT key,payload_json FROM runtime ORDER BY key").fetchall()
    )
    restored.close()
    store.close()


def test_expired_backup_preserves_source_and_rejects_reuse(tmp_path):
    store = Store(tmp_path / "source.sqlite", 233382575)
    store.initialize(OwnerSettings())
    destination = tmp_path / "partial.sqlite"
    with pytest.raises(StateError, match="time budget"):
        store.backup(destination, timeout_seconds=0)
    assert destination.exists() and store.snapshot().revision == 1
    with pytest.raises(StateError, match="must be new"):
        store.backup(destination)
    store.close()


def test_failed_stage_is_atomic_identifiable_and_never_contains_exception_body(tmp_path, monkeypatch):
    path = tmp_path / "deployment.json"
    monkeypatch.setattr(receiver, "ACTIVE_PATH", path)
    monkeypatch.setattr(receiver, "ACTIVE_RECORD", {"state": "PRE_SWITCH", "source_sha": "a" * 40})
    receiver.record_phase("CHECK_STATE_INTEGRITY")
    receiver.record_phase("FAILED", failure="TimeoutExpired")
    value = json.loads(path.read_text())
    assert value["state"] == "FAILED_PRE_SWITCH" and value["failure_phase"] == "CHECK_STATE_INTEGRITY"
    assert receiver.database_timeout(3_719_077_888) == 420
    assert receiver.database_timeout(100 * (1 << 30)) == 900
    receiver.ACTIVE_RECORD["switch_started"] = True
    receiver.record_phase("POST_CHECKS")
    receiver.record_phase("FAILED", failure="DeploymentError")
    assert json.loads(path.read_text())["state"] == "POST_CHECK_FAILED"


def test_timeout_cleanup_cannot_stop_application_or_other_project(monkeypatch):
    name = "cw-verify-" + "a" * 12 + "-" + "b" * 16
    monkeypatch.setattr(receiver, "ACTIVE_RECORD", {"candidate_name": name, "image_id": "expected"})
    container = {
        "Name": "/" + name,
        "Image": "expected",
        "Id": "candidate",
        "Config": {"Labels": {"com.docker.compose.project": "crypto_watcher"}},
    }
    monkeypatch.setattr(
        receiver.subprocess, "run", Mock(return_value=Mock(returncode=0, stdout=json.dumps([container])))
    )
    stop = Mock()
    monkeypatch.setattr(receiver, "run", stop)
    with pytest.raises(receiver.DeploymentError, match="offline candidate"):
        receiver.stop_candidate()
    stop.assert_not_called()
    container["Config"]["Labels"] = {}
    receiver.subprocess.run.return_value.stdout = json.dumps([container])
    receiver.stop_candidate()
    stop.assert_called_once_with(["docker", "stop", "--time", "5", "candidate"], timeout=15)
