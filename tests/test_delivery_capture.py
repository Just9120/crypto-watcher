import hashlib
import json
from subprocess import CompletedProcess
from unittest.mock import Mock

import pytest

from scripts.deliver_release import deliver, delivery_result

REVISION = "a" * 40
DIGEST = "b" * 64
IMAGE = "sha256:" + "c" * 64
RUN = 38133517275


def success(**changes):
    return {
        "state": "PASS",
        "source_sha": REVISION,
        "bundle_sha256": DIGEST,
        "build_run": RUN,
        "image_id": IMAGE,
        "image_config_digest": IMAGE,
        "post_checks": "source/image/health/persistence/market PASS",
        "phase": "COMPLETE",
        "backup_validation": {
            "phase": "COMPLETE",
            "validation_mode": "COMPRESSED_RAM",
            "sha256": "f" * 64,
            "archive_encoding": "ZSTD",
            "archive_bytes": 4096,
            "archive_sha256": "e" * 64,
        },
        **changes,
    }


def test_failed_delivery_artifact_is_nonempty_safe_and_retains_strict_transport(
    tmp_path, monkeypatch, capsys
):
    bundle = b"fixture immutable artifact"
    digest = hashlib.sha256(bundle).hexdigest()
    (tmp_path / "bundle.tar").write_bytes(bundle)
    (tmp_path / "bundle.sha256").write_text(digest)
    (tmp_path / "manifest.json").write_text(json.dumps({"source_sha": REVISION, "build_run": RUN}))

    def invoke(command, **kwargs):
        # Even during the transfer an interrupted job has a non-green, readable artifact.
        assert json.loads((tmp_path / "deployment.json").read_text())["state"] == "UNCONFIRMED"
        assert json.loads((tmp_path / "deployment.json").read_text())["attempted_source_sha"] == REVISION
        assert kwargs["stdin"].read() == bundle
        assert kwargs["capture_output"] and not kwargs["check"]
        assert command[-2:] == [
            "root@167.86.68.98",
            f"cryptowatcher-deploy {REVISION} {digest} {len(bundle)}",
        ]
        for option in ("StrictHostKeyChecking=yes", "IdentitiesOnly=yes", "BatchMode=yes"):
            assert option in command
        assert "UserKnownHostsFile=" + str(tmp_path / "known_hosts") in command
        return CompletedProcess(
            command,
            1,
            stdout=b"",
            stderr=json.dumps(
                {
                    "state": "BLOCKED",
                    "reason": "Operation failed: docker exit 3",
                    "failure_phase": "BACKUP_AND_VALIDATE",
                    "never-export-this-secret": "fixture-secret",
                }
            ).encode(),
        )

    call = Mock(side_effect=invoke)
    monkeypatch.setattr("scripts.deliver_release.subprocess.run", call)
    assert deliver(tmp_path, tmp_path, REVISION, RUN) == 1
    value = json.loads((tmp_path / "deployment.json").read_text())
    assert value["failure_code"] == "TIME_BUDGET_EXCEEDED"
    assert value["failure_phase"] == "BACKUP_AND_VALIDATE"
    assert value["attempted_source_sha"] == REVISION and value["runtime_state"] == "UNCONFIRMED"
    assert value["schema"] == "cryptowatcher.delivery-record.v1" and value["build_run"] == RUN
    assert "fixture-secret" not in (tmp_path / "deployment.json").read_text() + capsys.readouterr().out
    call.assert_called_once()


@pytest.mark.parametrize(
    "changes,exit_status",
    [
        ({}, 0),
        ({"source_sha": "d" * 40}, 0),
        ({"bundle_sha256": "e" * 64}, 0),
        ({"build_run": RUN + 1}, 0),
        ({"post_checks": None}, 0),
        ({"phase": "SWITCH"}, 0),
        ({"backup_validation": {}}, 0),
        ({"backup_validation": {"phase": "COMPLETE", "validation_mode": "DISK", "sha256": "f" * 64}}, 0),
        ({}, 1),
    ],
)
def test_success_requires_exact_source_artifact_checks_and_successful_transport(changes, exit_status):
    payload = success(**changes)
    payload["unrecognized-secret-field"] = "fixture-secret"
    raw = CompletedProcess([], exit_status, stdout=json.dumps(payload).encode(), stderr=b"raw-private-error")
    value, status = delivery_result(raw, revision=REVISION, digest=DIGEST, build_run=RUN)
    assert (status == 0) == (not changes and exit_status == 0)
    assert "fixture-secret" not in json.dumps(value) and "raw-private-error" not in json.dumps(value)


def test_backup_and_memory_proof_reject_unknown_secret_fields():
    payload = success()
    payload["backup_validation"]["unknown"] = "fixture-secret"
    payload["state_validation_resources"] = {
        "snapshot_bytes": 4096,
        "host_available_bytes": "fixture-secret",
        "unknown": "fixture-secret",
    }
    raw = CompletedProcess([], 0, stdout=json.dumps(payload).encode(), stderr=b"")
    value, status = delivery_result(raw, revision=REVISION, digest=DIGEST, build_run=RUN)
    assert status == 0 and value["state_validation_resources"] == {"snapshot_bytes": 4096}
    assert "fixture-secret" not in json.dumps(value)


@pytest.mark.parametrize(
    "field,value",
    [("archive_encoding", None), ("archive_bytes", True), ("archive_bytes", 0), ("archive_sha256", "bad")],
)
def test_archive_delivery_requires_durable_representation_proof(field, value):
    payload = success()
    payload["backup_validation"][field] = value
    raw = CompletedProcess([], 0, stdout=json.dumps(payload).encode(), stderr=b"")
    result, status = delivery_result(raw, revision=REVISION, digest=DIGEST, build_run=RUN)
    assert status == 1 and result["state"] != "PASS"


@pytest.mark.parametrize(
    "stderr", [b"raw-private-error", b"{}", b"null", b"x" * 65537], ids=["text", "object", "null", "oversize"]
)
def test_unknown_transport_response_remains_unconfirmed_without_raw_output(stderr):
    raw = CompletedProcess([], 255, stdout=b"", stderr=stderr)
    value, status = delivery_result(raw, revision=REVISION, digest=DIGEST, build_run=RUN)
    assert status == 1 and value["failure_phase"] == "UNSET" and value["runtime_state"] == "UNCONFIRMED"
    assert "raw-private" not in json.dumps(value)


def test_unverified_bundle_or_existing_record_never_invokes_transport(tmp_path, monkeypatch):
    (tmp_path / "bundle.tar").write_bytes(b"changed artifact")
    (tmp_path / "bundle.sha256").write_text(DIGEST)
    call = Mock()
    monkeypatch.setattr("scripts.deliver_release.subprocess.run", call)
    assert deliver(tmp_path, tmp_path, REVISION, RUN) == 1
    before = (tmp_path / "deployment.json").read_bytes()
    assert json.loads(before)["failure_code"] == "DELIVERY_PREPARATION_UNCONFIRMED"
    with pytest.raises(FileExistsError):
        deliver(tmp_path, tmp_path, REVISION, RUN)
    assert (tmp_path / "deployment.json").read_bytes() == before
    call.assert_not_called()


def test_reset_delivery_requires_private_postcheck_and_exact_discard_scope():
    from scripts.reset_delivery import POLICY

    proof = {
        "state": "PASS",
        "integrity": "PASS",
        "private_restore": "PASS",
        "backups_and_experiments": "NOT_TOUCHED",
        "owner_id": 233382575,
        "generation": {"id": "new-period"},
        "private_source_sha256": "1" * 64,
        "private_fresh_sha256": "2" * 64,
        "private_recovery_sha256": "3" * 64,
    }
    value = success(
        backup_validation={},
        delivery_policy=POLICY,
        reset_proof=proof,
        reset_promoted=True,
        reset_old_working_files_removed=2,
        reset_permit_tree="d" * 40,
        reset_private_post_check={"state": "PASS"},
    )

    def checked(v):
        return delivery_result(
            CompletedProcess([], 0, json.dumps(v).encode(), b""),
            revision=REVISION,
            digest=DIGEST,
            build_run=RUN,
        )[1]

    assert checked(value) == 0
    for updates in (
        {"reset_private_post_check": None},
        {"reset_promoted": False},
        {"reset_old_working_files_removed": 5},
        {"reset_proof": {**proof, "owner_id": 1}},
        {"reset_proof": {**proof, "generation": None}},
    ):
        assert checked({**value, **updates}) != 0
