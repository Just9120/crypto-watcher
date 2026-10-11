import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import update_cd_receiver as update


def setup(tmp_path, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    (root / "rehearsals").mkdir()
    (root / ".deploy.lock").write_text("")
    script = tmp_path / "receiver.py"
    script.write_bytes(b"old = True\n")
    original_stat = Path.stat

    def stat(path, *args, **kwargs):
        return (
            SimpleNamespace(st_uid=0, st_mode=0o100644)
            if path == script
            else original_stat(path, *args, **kwargs)
        )

    monkeypatch.setattr(Path, "stat", stat)
    target = tmp_path / "target.json"
    target.write_text("{}")
    monkeypatch.setattr(update, "APP", root)
    monkeypatch.setattr(update, "RECEIVER", script)
    monkeypatch.setattr(update, "TARGET", target)
    monkeypatch.setattr(update, "OLD_HASH", update.digest(script.read_bytes()))
    monkeypatch.setattr(update, "TARGET_HASH", update.digest(target.read_bytes()))
    monkeypatch.setattr(update.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(update.socket, "gethostname", lambda: "vmi3224957")
    monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace(flock=Mock(), LOCK_EX=1, LOCK_NB=2))
    monkeypatch.setattr(update, "gate", Mock())
    candidate = b"new = True\n"
    monkeypatch.setattr(update, "read_url", lambda url: candidate)
    live = {
        "Id": "original",
        "State": {"Running": True, "Health": {"Status": "healthy"}},
        "Config": {
            "Labels": {
                "com.docker.compose.project": "crypto_watcher",
                "com.docker.compose.service": "crypto_watcher",
                "org.opencontainers.image.revision": "afd10770aa3a330e6c5a28e3ea85a676060e6c29",
            }
        },
    }
    docker = Mock(return_value=Mock(stdout=json.dumps([live])))
    monkeypatch.setattr(update.subprocess, "run", docker)
    return script, target, candidate, docker


def test_operator_preflight_and_exact_update_preserve_runtime_target_and_original(tmp_path, monkeypatch):
    script, target, candidate, docker = setup(tmp_path, monkeypatch)
    original = script.read_bytes()
    report = update.install("a" * 40, "b" * 40, 29, update.digest(candidate))
    assert report["state"] == "PREFLIGHT_PASS" and script.read_bytes() == original
    report = update.install("a" * 40, "b" * 40, 29, update.digest(candidate), execute=True)
    assert report["state"] == "PASS" and script.read_bytes() == candidate
    assert target.read_text() == "{}"
    assert not report["runtime_restarted"] and not report["database_changed"] and not report["ssh_changed"]
    assert Path(report["backup_path"]).read_bytes() == original
    assert all(call.args[0] == ["docker", "inspect", "crypto-watcher"] for call in docker.call_args_list)
    with pytest.raises(RuntimeError, match="Receiver changed"):
        update.install("a" * 40, "b" * 40, 29, update.digest(candidate), execute=True)


def test_unknown_receiver_or_failed_gate_prevents_update(tmp_path, monkeypatch):
    script, _, candidate, _ = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(update, "gate", Mock(side_effect=RuntimeError("Main changed")))
    with pytest.raises(RuntimeError, match="Main changed"):
        update.install("a" * 40, "b" * 40, 29, update.digest(candidate), execute=True)
    assert script.read_bytes() == b"old = True\n"
    monkeypatch.setattr(update, "gate", Mock())
    with pytest.raises(RuntimeError, match="hash mismatch"):
        update.install("a" * 40, "b" * 40, 29, "0" * 64, execute=True)
    assert script.read_bytes() == b"old = True\n"


def test_check_gate_uses_latest_trusted_revision_and_rejects_newer_failure(monkeypatch):
    base, head = "a" * 40, "b" * 40
    pr = {
        "state": "open",
        "head": {"sha": head, "repo": {"full_name": update.REPO}},
        "base": {"sha": base, "repo": {"full_name": update.REPO}},
    }
    checks = {
        "total_count": 1,
        "check_runs": [
            {
                "id": 1,
                "name": "ci-required",
                "app": {"id": 15368},
                "status": "completed",
                "conclusion": "success",
            }
        ],
    }

    def response(url):
        return json.dumps(
            pr if "/pulls/" in url else checks if "/check-runs?" in url else {"object": {"sha": base}}
        ).encode()

    monkeypatch.setattr(update, "read_url", response)
    update.gate(base, head, 29)
    checks["check_runs"].append({**checks["check_runs"][0], "id": 2, "conclusion": "failure"})
    checks["total_count"] = 2
    with pytest.raises(RuntimeError, match="CI not passed"):
        update.gate(base, head, 29)
    checks["check_runs"].pop()
    checks["check_runs"][0]["app"]["id"] = 123
    with pytest.raises(RuntimeError, match="check missing"):
        update.gate(base, head, 29)
