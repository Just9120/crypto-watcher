"""Large delivery accepts one full shared snapshot proof within unchanged bounds."""

import json
import subprocess

import pytest

from scripts import deploy_receiver as receiver
from scripts.deliver_release import delivery_result
from tests.test_large_delivery_validation import environment


def framed(tmp_path, monkeypatch):
    before, _, calls = environment(tmp_path, monkeypatch, schema=2)
    source = {
        "schema": 2,
        "owner_id": 233382575,
        "configuration_revision": 5,
        "status": "integrity_ok",
        "validation_mode": "FRAMED_DISK",
        "legacy_sha256": "a" * 64,
        "settings_sha256": "e" * 64,
        "snapshot_sha256": "b" * 64,
    }
    proof = {
        "phase": "COMPLETE",
        "validation_mode": "FRAMED_DISK",
        "source_validation": source,
        "sha256": "b" * 64,
        "bytes": 11_000_119_296,
        "archive_encoding": "ZSTD",
        "archive_bytes": 4096,
        "archive_sha256": "c" * 64,
        "archive_limit_bytes": 11_000_119_296,
        "disk_reserve_bytes": 1536 * 1024**2,
        "sqlite_cache_kib": 2 * 1024 * 1024,
        "integrity_basis": "FULL_VALIDATED_CAPTURE_AND_INDEPENDENT_EQUAL_RAW_SHA256",
    }
    response = {"status": "backup_integrity_ok", "backup": proof}

    def run(args, *, timeout=120):
        calls.append((args, timeout))
        assert "backup" in args and "state-check" not in args and "diagnostics" not in args
        assert "--framed-archive-validation" in args and "--compressed-validation" not in args
        assert "--archive-backup" in args
        assert args[args.index("--memory") + 1] == str(4 * 1024**3)
        assert args[args.index("--memory-swap") + 1] == str(4 * 1024**3)
        assert args[args.index("--cpus") + 1] == "1"
        saved = json.loads((tmp_path / "record.json").read_text())
        assert saved["state_validation_method"] == "FRAMED_DISK"
        assert saved["backup_validation_resources"] == saved["state_validation_resources"]
        return json.dumps(response)

    monkeypatch.setattr(receiver, "run", run)
    return before, proof, source, calls, response


def test_one_large_source_capture_and_persisted_proof_keep_fixed_resources_and_reserve(tmp_path, monkeypatch):
    before, proof, source, calls, _ = framed(tmp_path, monkeypatch)
    name, actual = receiver.full_backup_preflight(["docker", "run", "image"], before, "d" * 40, 900)
    assert actual == proof and name.endswith(".sqlite3.zst")
    assert len(calls) == 1 and calls[0][1] == 690
    assert calls[0][0][calls[0][0].index("--timeout-seconds") + 1] == "675"
    assert receiver.ACTIVE_RECORD["state_validation"] == source
    assert receiver.ACTIVE_RECORD["backup_validation"] == proof


@pytest.mark.parametrize(
    "target,key,value",
    [
        ("source", "schema", 1),
        ("source", "schema", True),
        ("source", "configuration_revision", 4),
        ("source", "configuration_revision", None),
        ("source", "owner_id", True),
        ("source", "legacy_sha256", "d" * 64),
        ("source", "settings_sha256", "not-a-hash"),
        ("source", "snapshot_sha256", "d" * 64),
        ("source", "status", "partial"),
        ("source", "validation_mode", "COMPRESSED_RAM"),
        ("proof", "phase", "PENDING"),
        ("proof", "archive_bytes", True),
        ("proof", "archive_limit_bytes", 1),
        ("proof", "disk_reserve_bytes", 512),
        ("proof", "sqlite_cache_kib", 1024 * 1024),
        ("proof", "integrity_basis", "quick_check"),
        ("proof", "archive_encoding", "GZIP"),
        ("proof", "archive_sha256", None),
        ("proof", "source_validation", []),
        ("response", "backup", []),
    ],
)
def test_missing_partial_or_unbound_proof_never_admits_switch(tmp_path, monkeypatch, target, key, value):
    before, proof, source, calls, response = framed(tmp_path, monkeypatch)
    {"source": source, "proof": proof, "response": response}[target][key] = value
    with pytest.raises(receiver.DeploymentError):
        receiver.full_backup_preflight(["docker", "run", "image"], before, "d" * 40, 900)
    assert len(calls) == 1 and "backup_validation" not in receiver.ACTIVE_RECORD


def test_insufficient_prep_budget_rejects_before_any_capture(tmp_path, monkeypatch):
    before, _, _, calls, _ = framed(tmp_path, monkeypatch)
    with pytest.raises(receiver.DeploymentError, match="budget"):
        receiver.full_backup_preflight(["image"], before, "d" * 40, 19)
    assert not calls


def test_actions_accepts_exact_full_framed_proof_without_changing_mode_or_leaking_fields(
    tmp_path, monkeypatch
):
    before, proof, source, _, _ = framed(tmp_path, monkeypatch)
    _, proof = receiver.full_backup_preflight(["docker", "run", "image"], before, "d" * 40, 900)
    source = receiver.ACTIVE_RECORD["state_validation"]
    proof["private-never-export"] = "secret"
    proof["phase_seconds"] = {
        "SOURCE_COPY": 12.0,
        "FULL_INTEGRITY": 2,
        "PIN_SOURCE": "secret",
        "never-export-secret": 17,
    }
    source["private-never-export"] = "secret"
    response = {
        **receiver.ACTIVE_RECORD,
        "state": "PASS",
        "phase": "COMPLETE",
        "source_sha": "d" * 40,
        "bundle_sha256": "f" * 64,
        "build_run": 1,
        "image_id": "sha256:" + "a" * 64,
        "image_config_digest": "sha256:" + "a" * 64,
        "post_checks": "source/image/health/persistence/market PASS",
    }
    completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(response).encode(), stderr=b"")
    result, code = delivery_result(completed, revision="d" * 40, digest="f" * 64, build_run=1)
    assert code == 0 and result["state"] == "PASS"
    assert result["backup_validation"]["validation_mode"] == "FRAMED_DISK"
    assert result["state_validation"]["snapshot_sha256"] == proof["sha256"]
    assert result["backup_validation"]["phase_seconds"] == {"SOURCE_COPY": 12.0, "FULL_INTEGRITY": 2}
    assert "secret" not in json.dumps(result) and "private-never-export" not in json.dumps(result)
    response["state_validation"] = {**source, "snapshot_sha256": "e" * 64}
    completed.stdout = json.dumps(response).encode()
    result, code = delivery_result(completed, revision="d" * 40, digest="f" * 64, build_run=1)
    assert code == 1 and result["runtime_state"] == "UNCONFIRMED"
