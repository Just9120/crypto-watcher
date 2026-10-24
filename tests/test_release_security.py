"""Exercise deployment trust boundaries without Docker, credentials or live services."""

import gzip
import hashlib
import io
import json
import sys
import tarfile
import types
import zipfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import deploy_receiver as receiver

SOURCE = "a" * 40
DIGEST = "b" * 64
RUN = 321
SIZE = 1234


def provenance():
    binding = {
        "id": RUN,
        "repository_id": 1210326462,
        "head_repository_id": 1210326462,
        "head_sha": SOURCE,
        "head_branch": "main",
    }
    run = {
        "id": RUN,
        "workflow_id": 88,
        "path": ".github/workflows/release.yml",
        "event": "push",
        "head_sha": SOURCE,
        "head_branch": "main",
        "repository": {"id": 1210326462},
        "head_repository": {"id": 1210326462},
    }
    artifact = {
        "id": 111,
        "name": f"cryptowatcher-{SOURCE}-{RUN}",
        "expired": False,
        "digest": "sha256:" + DIGEST,
        "size_in_bytes": SIZE,
        "workflow_run": binding,
    }
    return {
        "actions/workflows/release.yml": {"id": 88, "path": ".github/workflows/release.yml"},
        f"actions/workflows/88/runs?head_sha={SOURCE}&per_page=10": {"workflow_runs": [run]},
        f"actions/runs/{RUN}/artifacts?per_page=100": {"total_count": 1, "artifacts": [artifact]},
        f"actions/runs/{RUN}/jobs?per_page=100": {
            "total_count": 1,
            "jobs": [{"name": "build", "status": "completed", "conclusion": "success"}],
        },
    }


def test_exact_public_release_artifact_has_independent_identity(monkeypatch):
    records = provenance()
    monkeypatch.setattr(receiver, "github_record", records.__getitem__)
    assert receiver.trusted_artifact(SOURCE, DIGEST, SIZE) == {
        "build_run": RUN,
        "artifact_id": 111,
        "artifact_sha256": DIGEST,
    }


@pytest.mark.parametrize(
    "variation",
    [
        "fork",
        "workflow",
        "ref",
        "sha",
        "event",
        "digest",
        "size",
        "binding",
        "expired",
        "failed_build",
        "missing_build",
    ],
)
def test_self_consistent_but_untrusted_release_is_rejected(monkeypatch, variation):
    records = provenance()
    run = records[f"actions/workflows/88/runs?head_sha={SOURCE}&per_page=10"]["workflow_runs"][0]
    artifact = records[f"actions/runs/{RUN}/artifacts?per_page=100"]["artifacts"][0]
    if variation == "fork":
        run["head_repository"]["id"] = 999
    elif variation == "workflow":
        run["path"] = ".github/workflows/untrusted.yml"
    elif variation == "ref":
        run["head_branch"] = "untrusted"
    elif variation == "sha":
        run["head_sha"] = "c" * 40
    elif variation == "event":
        run["event"] = "pull_request"
    elif variation in ("digest", "size", "expired"):
        artifact.update(
            {"digest": "sha256:" + "f" * 64}
            if variation == "digest"
            else {"size_in_bytes": SIZE + 1}
            if variation == "size"
            else {"expired": True}
        )
    elif variation == "binding":
        artifact["workflow_run"]["head_sha"] = "d" * 40
    else:
        jobs = records[f"actions/runs/{RUN}/jobs?per_page=100"]
        if variation == "failed_build":
            jobs["jobs"][0]["conclusion"] = "failure"
        else:
            jobs["jobs"] = []
    monkeypatch.setattr(receiver, "github_record", records.__getitem__)
    with pytest.raises(receiver.DeploymentError):
        receiver.trusted_artifact(SOURCE, DIGEST, SIZE)


def deploy_until_rejection(tmp_path, monkeypatch, payload, *, trusted):
    monkeypatch.setitem(sys.modules, "fcntl", types.SimpleNamespace(flock=lambda *a: None, LOCK_EX=2))
    monkeypatch.setattr(receiver.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(receiver, "APP", tmp_path)
    monkeypatch.setattr(receiver, "ACTIVE_RECORD", None)
    monkeypatch.setattr(receiver, "ACTIVE_PATH", None)
    monkeypatch.setattr(receiver, "OPERATION_DEADLINE", None)
    monkeypatch.setattr(receiver, "HARD_DEADLINE", None)
    monkeypatch.setattr(receiver.signal, "setitimer", lambda *a: None, raising=False)
    monkeypatch.setattr(receiver.signal, "ITIMER_REAL", 0, raising=False)
    monkeypatch.setattr(receiver.signal, "SIGALRM", 0, raising=False)
    monkeypatch.setattr(receiver.signal, "signal", lambda *a: None)
    monkeypatch.setattr(receiver, "target_config", lambda: {"architecture": "amd64"})
    monkeypatch.setattr(receiver, "main_revision", lambda: SOURCE)
    monkeypatch.setattr(receiver.shutil, "disk_usage", lambda p: types.SimpleNamespace(free=10 * 1024**3))
    monkeypatch.setattr(receiver.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(payload)))
    docker = Mock(side_effect=AssertionError("Docker authority reached before trust checks"))
    monkeypatch.setattr(receiver, "run", docker)
    trust = (
        Mock(return_value={"build_run": RUN, "artifact_id": 111})
        if trusted
        else Mock(side_effect=receiver.DeploymentError("Artifact has no trusted release provenance"))
    )
    monkeypatch.setattr(receiver, "trusted_artifact", trust)
    with pytest.raises(receiver.DeploymentError):
        receiver.deploy(SOURCE, hashlib.sha256(payload).hexdigest(), len(payload))
    docker.assert_not_called()
    return trust


def test_untrusted_bytes_fail_before_zip_tar_or_docker(tmp_path, monkeypatch):
    parse = Mock(side_effect=AssertionError("Untrusted archive parsed"))
    monkeypatch.setattr(receiver, "extract_trusted_artifact", parse)
    deploy_until_rejection(tmp_path, monkeypatch, b"attacker archive", trusted=False).assert_called_once()
    parse.assert_not_called()


@pytest.mark.parametrize(
    "override",
    [
        "user: '0'",
        "privileged: true",
        "pid: host",
        "devices: [/dev/sda]",
        "entrypoint: [sh]",
        "cap_add: [SYS_ADMIN]",
        "security_opt: []",
        "extends: {file: /etc/attacker.yml, service: root}",
        "profiles: [hidden]",
        "deploy: {resources: {reservations: {devices: [{capabilities: [gpu]}]}}}",
    ],
)
def test_compose_authority_changes_fail_before_first_docker_operation(tmp_path, monkeypatch, override):
    compose = ("services:\n  crypto_watcher:\n    " + override + "\n").encode()
    image = b"image never loaded or parsed"
    manifest = {
        "schema": "cryptowatcher.release.v1",
        "database_schema": 1,
        "migration_class": "NONE",
        "image_id": "sha256:" + "c" * 64,
        "source_sha": SOURCE,
        "build_run": RUN,
        "architecture": "amd64",
        "compose_sha256": hashlib.sha256(compose).hexdigest(),
        "image_archive_sha256": hashlib.sha256(image).hexdigest(),
    }
    inner = io.BytesIO()
    with tarfile.open(fileobj=inner, mode="w") as archive:
        for name, data in {
            "manifest.json": json.dumps(manifest).encode(),
            "image.tar.gz": image,
            "docker-compose.yml": compose,
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    bundle = inner.getvalue()
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as archive:
        archive.writestr("bundle.tar", bundle)
        archive.writestr("bundle.sha256", hashlib.sha256(bundle).hexdigest())
        archive.writestr("manifest.json", json.dumps(manifest))
    deploy_until_rejection(tmp_path, monkeypatch, outer.getvalue(), trusted=True)


def test_reviewed_compose_bytes_match_root_policy():
    path = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    assert (
        hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        == receiver.COMPOSE_POLICY_SHA256
    )


@pytest.mark.parametrize("tamper", [False, True])
def test_runner_retains_exact_github_zip_and_checks_digest(tmp_path, monkeypatch, tamper):
    from scripts import download_release

    bundle = b"immutable trusted inner bundle"
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("bundle.tar", bundle)
        archive.writestr("bundle.sha256", hashlib.sha256(bundle).hexdigest())
        archive.writestr("manifest.json", json.dumps({"source_sha": SOURCE, "build_run": RUN}))
    data = content.getvalue()
    artifact = {
        "id": 111,
        "name": f"cryptowatcher-{SOURCE}-{RUN}",
        "expired": False,
        "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
        "size_in_bytes": len(data),
    }
    monkeypatch.setattr(
        download_release.subprocess,
        "check_output",
        lambda *a, **k: json.dumps({"total_count": 1, "artifacts": [artifact]}).encode(),
    )

    def download(args, **kwargs):
        assert args[-1] == "repos/Just9120/crypto-watcher/actions/artifacts/111/zip"
        kwargs["stdout"].write(data + (b"tampering" if tamper else b""))

    monkeypatch.setattr(download_release.subprocess, "run", download)
    destination = tmp_path / "release"
    if tamper:
        with pytest.raises(ValueError, match="differs from GitHub digest"):
            download_release.download(SOURCE, RUN, destination)
        assert not (destination / "bundle.tar").exists()
    else:
        download_release.download(SOURCE, RUN, destination)
        assert (destination / "artifact.zip").read_bytes() == data
        assert (destination / "bundle.tar").read_bytes() == bundle
        assert json.loads((destination / "manifest.json").read_text())["build_run"] == RUN


@pytest.mark.parametrize("kind", ["many_headers", "large_pax", "large_declared_layer", "trailer"])
def test_image_archive_resource_bombs_are_rejected_in_worker(tmp_path, kind):
    path = tmp_path / "bomb.tar.gz"
    info = tarfile.TarInfo("layer")
    if kind == "large_pax":
        info.type, info.size = tarfile.XHDTYPE, 1024**3
    elif kind == "large_declared_layer":
        info.size = 4 * 1024**3
    elif kind == "many_headers":
        info.size = 0
    raw = info.tobuf() * (4097 if kind == "many_headers" else 1)
    if kind == "trailer":
        raw = bytes(1024) + b"hidden nonzero archive"
    path.write_bytes(gzip.compress(raw))
    with pytest.raises(receiver.DeploymentError):
        receiver.archive_image_identity(
            path, {"source_sha": SOURCE, "build_run": RUN, "architecture": "amd64"}
        )
