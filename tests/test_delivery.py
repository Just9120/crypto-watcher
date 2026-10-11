import io
import json
import tarfile
from unittest.mock import Mock

import pytest
import yaml

from scripts import deploy_receiver as receiver
from scripts.impact import code_required


@pytest.mark.parametrize(
    "paths,expected",
    [
        (["README.md", "docs/state_contract.md"], False),
        (["unknown/file"], True),
        (["docs/state_contract.md", "cryptowatcher/bot.py"], True),
        ([], True),
        ([".github/workflows/ci.yml"], True),
        (["requirements.txt"], True),
    ],
)
def test_unknown_and_mixed_impact_require_code_checks(paths, expected):
    assert code_required(paths) is expected


@pytest.mark.parametrize(
    "command",
    [
        "bash",
        "cryptowatcher-deploy abc",
        "cryptowatcher-deploy " + "a" * 40 + " " + "b" * 64 + " 2147483648",
        "cryptowatcher-deploy " + "a" * 40 + " " + "b" * 64 + " 123; whoami",
    ],
)
def test_forced_command_rejects_other_operations(command):
    with pytest.raises(receiver.DeploymentError):
        receiver.command_arguments(command)


def bundle(tmp_path, *, extra=None, migration="NONE", tamper=False):
    image = b"disposable-image-placeholder"
    compose = b"services: {crypto_watcher: {image: fixed}}"
    manifest = {
        "schema": "cryptowatcher.release.v1",
        "database_schema": 1,
        "migration_class": migration,
        "image_id": "sha256:" + "c" * 64,
        "image_archive_sha256": "wrong" if tamper else __import__("hashlib").sha256(image).hexdigest(),
        "compose_sha256": __import__("hashlib").sha256(compose).hexdigest(),
    }
    entries = {
        "image.tar.gz": image,
        "docker-compose.yml": compose,
        "manifest.json": json.dumps(manifest).encode(),
    }
    if extra:
        entries[extra] = b"unexpected"
    path = tmp_path / "bundle.tar"
    with tarfile.open(path, "w") as archive:
        for name, data in entries.items():
            item = tarfile.TarInfo(name)
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
    output = tmp_path / "output"
    output.mkdir()
    return path, output


@pytest.mark.parametrize(
    "changes", [{"extra": "../outside"}, {"migration": "EXPLICITLY_GATED"}, {"tamper": True}]
)
def test_archive_integrity_and_stateful_gate(tmp_path, changes):
    path, output = bundle(tmp_path, **changes)
    with pytest.raises(receiver.DeploymentError):
        receiver.extract_bundle(path, output)
    assert not (tmp_path / "outside").exists()


def test_valid_bundle_and_ssh_identity(tmp_path):
    path, output = bundle(tmp_path)
    assert receiver.extract_bundle(path, output)["database_schema"] == 1
    assert receiver.command_arguments("cryptowatcher-deploy " + "a" * 40 + " " + "b" * 64 + " 123") == (
        "a" * 40,
        "b" * 64,
        123,
    )


@pytest.mark.parametrize(
    "poll,expected",
    [
        ({"state": "OK", "at": 99, "available": 100, "requested": 100}, False),
        ({"state": "DEGRADED", "at": 101, "reason": "rate_limited"}, False),
        ({"state": "OK", "at": 101, "available": 0, "requested": 0}, True),
        ({"state": "DEGRADED", "at": 101, "available": 99, "requested": 100}, True),
    ],
)
def test_post_check_requires_new_poll_and_usable_data(poll, expected):
    assert receiver.usable_poll(poll, 100) is expected


def test_queued_stale_candidate_rejected_after_target_lock(monkeypatch, tmp_path):
    import sys

    flock = Mock()
    monkeypatch.setitem(sys.modules, "fcntl", Mock(flock=flock, LOCK_EX=2))
    monkeypatch.setattr(receiver.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(receiver, "APP", tmp_path)
    monkeypatch.setattr(receiver, "target_config", lambda: {})
    monkeypatch.setattr(receiver, "main_revision", lambda: "b" * 40)
    docker = Mock()
    monkeypatch.setattr(receiver, "run", docker)
    with pytest.raises(receiver.DeploymentError, match="no longer current"):
        receiver.deploy("a" * 40, "c" * 64, 123)
    flock.assert_called_once()
    docker.assert_not_called()
    assert not list((tmp_path / "releases").iterdir())


def test_failed_command_does_not_echo_secret_stderr(monkeypatch):
    monkeypatch.setattr(
        receiver.subprocess, "run", Mock(return_value=Mock(returncode=1, stderr="SECRET", stdout=""))
    )
    with pytest.raises(receiver.DeploymentError, match="Operation failed") as error:
        receiver.run(["docker", "inspect"])
    assert "SECRET" not in str(error.value)


def test_workflow_trust_boundaries_and_pinned_actions():
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for path in (root / ".github/workflows").glob("*.yml"):
        workflow = yaml.safe_load(path.read_text())
        assert workflow["permissions"] == {"contents": "read"}
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                if "uses" in step:
                    assert re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", step["uses"])
    ci = (root / ".github/workflows/ci.yml").read_text()
    assert "pull_request_target" not in ci and "secrets." not in ci
    assert "if: always()" in ci and 'test "$VALIDATION" = success' in ci
    release = (root / ".github/workflows/release.yml").read_text()
    assert "cancel-in-progress: false" in release
    assert (
        'python scripts/deliver_release.py "$REVISION" "$RUN_ID" --ssh-directory "$RUNNER_TEMP/cw-ssh"'
        in release
    )
    client = (root / "scripts/deliver_release.py").read_text()
    assert "StrictHostKeyChecking=yes" in client and "UserKnownHostsFile=" in client
    assert "IdentitiesOnly=yes" in client and "BatchMode=yes" in client
    assert "environment: production" in release
