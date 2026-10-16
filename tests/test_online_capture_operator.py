import hashlib
import io
import json
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from scripts import online_capture, receive_online_pages


def test_feed_guard_stops_only_owned_helper_when_feed_stays_unconfirmed(monkeypatch):
    from cryptowatcher.maintenance import Unit

    removed = []
    checked = 0

    def check(expected):
        nonlocal checked
        checked += 1
        if checked > 1:
            raise RuntimeError("fixture stale feed")

    monkeypatch.setattr(online_capture, "require_live_feed", check)
    monkeypatch.setattr(online_capture.subprocess, "run", lambda *args, **kwargs: time.sleep(0.06))
    with pytest.raises(RuntimeError, match="protect radar"):
        online_capture.checked_worker(
            ["owned-helper"],
            Unit("a" * 64, "sha256:" + "b" * 64, "c" * 40),
            SimpleNamespace(remove_owned=lambda *args: removed.append(args)),
            {"worker": "owned-helper", "image": "sha256:" + "b" * 64},
            seconds=1,
            stdout=None,
            interval=0.01,
        )
    assert removed == [("owned-helper", "sha256:" + "b" * 64)]


def test_operator_hash_allows_read_access_time_but_binds_file_identity(tmp_path, monkeypatch):
    path = tmp_path / "baseline"
    path.write_bytes(b"stable input")
    real = type(path).stat

    def stat(p, *args, **kwargs):
        value = real(p, *args, **kwargs)
        return SimpleNamespace(
            **{
                k: getattr(value, k)
                for k in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode", "st_nlink")
            },
            st_atime=time.time(),
        )

    monkeypatch.setattr(type(path), "stat", stat)
    assert online_capture.fingerprint(path, time.monotonic() + 5) == (
        hashlib.sha256(b"stable input").hexdigest(),
        12,
    )


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


def test_initial_rearm_worker_is_refused_before_any_write(tmp_path, monkeypatch):
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

    monkeypatch.setattr(type(tmp_path), "read_text", read)
    monkeypatch.setattr(online_capture_worker.os, "geteuid", lambda: 10001, raising=False)

    def refuse_connect(*args, **kwargs):
        raise AssertionError("Live rearm must not open a write connection")

    monkeypatch.setattr(online_capture_worker.sqlite3, "connect", refuse_connect)
    with pytest.raises(RuntimeError, match="Live journal rearm forbidden"):
        online_capture_worker.run(90, archive_journal=True)
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
