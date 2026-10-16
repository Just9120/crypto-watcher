import hashlib
import io
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import online_capture, receive_online_pages


def test_root_launcher_imports_with_no_installed_third_party_dependencies():
    result = subprocess.run(
        [sys.executable, "-S", "-c", "from scripts import online_capture"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_pc_receiver_binds_primary_snapshot_to_same_operation_and_journal(tmp_path, monkeypatch):
    stream = b"complete fixture stream"
    descriptor = {
        "state": "CAPTURED_SNAPSHOT_NOT_QUALIFIED",
        "data_sha256": hashlib.sha256(stream).hexdigest(),
        "data_bytes": len(stream),
        "raw_bytes": 4096,
        "journal_epoch": "d" * 32,
        "journal_watermark": 12,
    }
    producer = {
        "journal_epoch": "d" * 32,
        "revision": "a" * 40,
        "image": "sha256:" + "b" * 64,
        "container": "c" * 64,
        "code_revision": "e" * 40,
    }
    operation = {
        "state": "ONLINE_CAPTURE_FINISHED_PC_READBACK_REQUIRED",
        "snapshot": descriptor,
        "runtime_unchanged": True,
    }

    class Process:
        stdout = io.BytesIO(stream)

        def wait(self, **kw):
            return 0

        def poll(self):
            return 0

        def kill(self):
            raise AssertionError("Completed receiver must not be killed")

    monkeypatch.setattr(receive_online_pages, "connection", lambda cfg: ["reviewed-ssh"])
    monkeypatch.setattr(receive_online_pages.subprocess, "Popen", lambda *a, **kw: Process())
    monkeypatch.setattr(
        receive_online_pages.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            stdout="\n".join(json.dumps(x) for x in (descriptor, producer, operation)).encode()
        ),
    )
    result = receive_online_pages.receive(
        {}, "/opt/crypto_watcher/rehearsals/online-snapshot-fixture", (tmp_path / "private").resolve()
    )
    assert result["state"] == "PC_STREAM_FSYNCED_IDENTITY_PASS"
    assert result["full_source_integrity"] == "PENDING"
    assert json.loads((tmp_path / "private/producer.json").read_text())["operation"] == operation


def test_initial_rearm_worker_preserves_expiry_and_application_tables(tmp_path, monkeypatch):
    import sqlite3
    import time

    from scripts import online_capture_worker

    plan = {
        "schema": "cryptowatcher.online-capture-plan.v1",
        "journal_epoch": "e" * 32,
        "initial_snapshot": {"prior_epoch": "d" * 32, "expires": int(time.time()) + 3600},
    }
    original_read = type(tmp_path).read_text

    def read(path, *args, **kwargs):
        return {
            "/sys/fs/cgroup/cpu.max": "100000 100000",
            "/sys/fs/cgroup/memory.max": str(4 * 1024**3),
            "/sys/fs/cgroup/memory.swap.max": "0",
            "/input/capture-plan.json": json.dumps(plan),
        }.get(path.as_posix(), "")

    seen = []
    monkeypatch.setattr(type(tmp_path), "read_text", read)
    monkeypatch.setattr(online_capture_worker.os, "geteuid", lambda: 10001, raising=False)
    connect = sqlite3.connect
    monkeypatch.setattr(online_capture_worker.sqlite3, "connect", lambda *a, **kw: connect(":memory:"))

    def rearm(db, **kw):
        seen.append(kw)
        return {
            "state": "JOURNAL_REARMED_BEFORE_INITIAL_SNAPSHOT",
            "epoch": kw["epoch"],
            "application_rows_deleted": 0,
            "prior_snapshot_reused": False,
        }

    monkeypatch.setattr(online_capture_worker, "rearm_before_initial_snapshot", rearm)
    monkeypatch.setattr(online_capture_worker, "_json", lambda p, v: None)
    assert online_capture_worker.run(90, archive_journal=True)["application_rows_deleted"] == 0
    assert seen[0]["expires"] == plan["initial_snapshot"]["expires"] and seen[0]["no_prior_capture"] is True
    monkeypatch.setattr(type(tmp_path), "read_text", original_read)


@pytest.mark.parametrize("journal", [False, True])
def test_only_metadata_archive_role_has_writeable_source_and_resource_caps_stay_fixed(tmp_path, journal):
    value = {"worker": "cw-online-snapshot-" + "a" * 32, "image": "sha256:" + "b" * 64}
    command = online_capture.worker_command(tmp_path, value, 100, archive_journal=journal)
    assert command[command.index("--memory") + 1] == command[command.index("--memory-swap") + 1] == "4g"
    assert command[command.index("--cpus") + 1] == "1" and command[command.index("--network") + 1] == "none"
    mounts = [command[i + 1] for i, item in enumerate(command) if item == "--mount"]
    assert any(x.endswith(",dst=/history" + ("" if journal else ",readonly")) for x in mounts)
    assert any(x.endswith(",dst=/input,readonly") for x in mounts)
    assert ("--journal-archive" in command) == journal
    assert all(x not in command for x in ("--privileged", "--env-file", "stop", "restart"))


def test_managed_online_launcher_has_one900s_deadline_cleanup_and_binary_pipe(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    seen = []
    monkeypatch.setattr(online_capture, "preflight", lambda *args: {"safe": "baseline"})
    monkeypatch.setattr(
        online_capture.subprocess,
        "run",
        lambda command, **kw: seen.append(command) or SimpleNamespace(returncode=0),
    )
    assert online_capture.launch(root, {"worker": "cw-online-snapshot-" + "c" * 32}) == 0
    assert len(seen) == 1 and "--pipe" in seen[0] and "--wait" in seen[0]
    assert "--property=RuntimeMaxSec=900" in seen[0]
    assert any(x.startswith("--property=ExecStopPost=") and x.endswith("--cleanup") for x in seen[0])
    assert not any("3600" in x or "restore-only" in x for x in seen[0])
    assert json.loads((root / "dispatch.json").read_text())["application_stopped"] is False


@pytest.mark.parametrize("fault", ["config", "proxy", "shell"])
def test_receiver_refuses_unfrozen_ssh_options_before_starting_a_process(fault):
    args = ["-F", "none", "-o", "ProxyCommand=none"]
    if fault == "config":
        args[1] = "untrusted-config"
    elif fault == "proxy":
        args[-1] = "ProxyCommand=unexpected-helper"
    else:
        args += ["-o", "RemoteCommand=unexpected-command"]
    with pytest.raises(RuntimeError):
        receive_online_pages.connection(
            {"Destination": "codex-ops@167.86.68.98", "SshArguments": args, "SshExecutable": "ssh.exe"}
        )


def test_failed_binary_producer_is_not_promoted_to_a_pc_snapshot(tmp_path, monkeypatch):
    class Process:
        stdout = io.BytesIO(b"partial binary")

        def wait(self, **kw):
            return 3

        def poll(self):
            return 3

        def kill(self):
            raise AssertionError("Terminal process must not be killed again")

    monkeypatch.setattr(receive_online_pages, "connection", lambda cfg: ["reviewed-ssh"])
    monkeypatch.setattr(receive_online_pages.subprocess, "Popen", lambda *a, **kw: Process())
    with pytest.raises(RuntimeError, match="did not finish"):
        receive_online_pages.receive(
            {},
            "/opt/crypto_watcher/rehearsals/online-snapshot-fixture",
            (tmp_path / "private").resolve(),
            seconds=20,
        )
    assert not (tmp_path / "private/snapshot.json").exists()
    assert (tmp_path / "private/pages.delta").read_bytes() == b"partial binary"
