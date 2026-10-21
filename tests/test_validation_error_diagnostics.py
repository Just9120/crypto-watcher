"""Exact safe failure codes survive CLI/receiver/Actions without raw stderr."""

import json
import subprocess
from unittest.mock import Mock

import pytest

from cryptowatcher.compressed_validation import QualificationError
from cryptowatcher.validation_codes import DETAIL_CODES
from scripts import deliver_release, deploy_receiver
from tests.test_cli import cli


def test_fixed_failure_vocabulary_agrees_across_standalone_tools():
    assert deploy_receiver.VALIDATION_DETAIL_CODES == deliver_release.VALIDATION_DETAIL_CODES == DETAIL_CODES
    assert deploy_receiver.VALIDATION_INNER_PHASES == deliver_release.VALIDATION_INNER_PHASES
    unknown = QualificationError("never-export-secret")
    assert unknown.detail_code is None and "secret" not in str(unknown)


@pytest.mark.parametrize("detail", ["COMPRESSED_MEMORY_LIMIT", "never-export-secret"])
def test_cli_exports_only_known_detail_with_safe_reason(store, infrastructure, monkeypatch, capsys, detail):
    def fail(*args, **kwargs):
        raise QualificationError(detail)

    monkeypatch.setattr("cryptowatcher.disk_validation.validated_disk", fail)
    assert cli(monkeypatch, infrastructure, "state-check") == 7
    result = json.loads(capsys.readouterr().err)
    assert result["failure_code"] == "DATABASE_ERROR" and "secret" not in json.dumps(result)
    assert result.get("failure_detail_code") == (detail if detail in DETAIL_CODES else None)


def test_receiver_primary_record_and_actions_keep_detail_without_secret(tmp_path, monkeypatch):
    record = {"phase": "CHECK_STATE_INTEGRITY"}
    path = tmp_path / "deployment.json"
    monkeypatch.setattr(deploy_receiver, "ACTIVE_RECORD", record)
    monkeypatch.setattr(deploy_receiver, "ACTIVE_PATH", path)
    monkeypatch.setattr(deploy_receiver, "OPERATION_DEADLINE", None)
    body = {
        "status": "blocked",
        "failure_code": "DATABASE_ERROR",
        "failure_detail_code": "COMPRESSED_MEMORY_LIMIT",
        "reason": "never-export-secret",
        "environment": "never-export-secret",
        "backup": {"phase": "FULL_INTEGRITY"},
    }
    completed = subprocess.CompletedProcess([], 7, stdout="never-export-secret", stderr=json.dumps(body))
    monkeypatch.setattr(deploy_receiver.subprocess, "run", Mock(return_value=completed))
    with pytest.raises(deploy_receiver.DeploymentError) as failure:
        deploy_receiver.run(["docker", "run", "image", "python", "-m", "cryptowatcher", "state-check"])
    deploy_receiver.record_phase("FAILED", failure="DeploymentError")
    primary = json.loads(path.read_text())
    assert primary["state"] == "FAILED_PRE_SWITCH" and primary["failure_code"] == "DATABASE_ERROR"
    assert primary["failure_detail_code"] == "COMPRESSED_MEMORY_LIMIT"
    assert primary["failure_validation_phase"] == "FULL_INTEGRITY"
    response = subprocess.CompletedProcess(
        [],
        1,
        stdout=b"",
        stderr=json.dumps(
            {
                "state": "BLOCKED",
                "reason": str(failure.value),
                "failure_phase": primary["failure_phase"],
                "failure_detail_code": primary["failure_detail_code"],
                "failure_validation_phase": primary["failure_validation_phase"],
            }
        ).encode(),
    )
    result, code = deliver_release.delivery_result(response, revision="a" * 40, digest="b" * 64, build_run=1)
    assert code == 1 and result["failure_code"] == "DATABASE_ERROR"
    assert result["failure_detail_code"] == "COMPRESSED_MEMORY_LIMIT"
    assert result["failure_validation_phase"] == "FULL_INTEGRITY"
    assert "secret" not in json.dumps(primary) + json.dumps(result) + str(failure.value)


@pytest.mark.parametrize(
    "body,exit_status",
    [
        (
            {
                "status": "blocked",
                "failure_code": "DATABASE_ERROR",
                "failure_detail_code": "COMPRESSED_MEMORY_LIMIT",
            },
            4,
        ),
        ({"status": "blocked", "failure_code": "DATABASE_ERROR", "failure_detail_code": ["secret"]}, 7),
        ({"status": "blocked", "failure_code": [], "failure_detail_code": "COMPRESSED_MEMORY_LIMIT"}, 7),
        (
            {
                "status": "blocked",
                "failure_code": "DATABASE_ERROR",
                "failure_detail_code": "TIME_BUDGET_EXCEEDED",
            },
            7,
        ),
        ({"status": "blocked", "failure_code": "DATABASE_ERROR", "failure_detail_code": "secret"}, 7),
        (
            {
                "status": "ok",
                "failure_code": "DATABASE_ERROR",
                "failure_detail_code": "COMPRESSED_MEMORY_LIMIT",
            },
            7,
        ),
    ],
)
def test_receiver_ignores_contradictory_or_unknown_failure_fields(monkeypatch, body, exit_status):
    record = {}
    monkeypatch.setattr(deploy_receiver, "ACTIVE_RECORD", record)
    deploy_receiver.capture_validation_failure(
        ["docker", "run", "state-check"],
        subprocess.CompletedProcess([], exit_status, stderr=json.dumps(body)),
    )
    assert "failure_detail_code" not in record


def test_receiver_ignores_oversize_invalid_and_nonvalidation_output(monkeypatch):
    record = {}
    monkeypatch.setattr(deploy_receiver, "ACTIVE_RECORD", record)
    for text in ("{" + "x" * 65536, "not-json", "[]"):
        deploy_receiver.capture_validation_failure(
            ["docker", "run", "state-check"], subprocess.CompletedProcess([], 7, stderr=text)
        )
    body = {
        "status": "blocked",
        "failure_code": "DATABASE_ERROR",
        "failure_detail_code": "COMPRESSED_MEMORY_LIMIT",
    }
    deploy_receiver.capture_validation_failure(
        ["docker", "inspect", "image"], subprocess.CompletedProcess([], 7, stderr=json.dumps(body))
    )
    assert record == {}


def test_cleanup_timeout_does_not_erase_the_first_primary_failure(monkeypatch):
    record = {"failure_code": "DATABASE_ERROR", "failure_detail_code": "COMPRESSED_MEMORY_LIMIT"}
    monkeypatch.setattr(deploy_receiver, "ACTIVE_RECORD", record)
    monkeypatch.setattr(deploy_receiver, "OPERATION_DEADLINE", None)
    monkeypatch.setattr(
        deploy_receiver.subprocess, "run", Mock(side_effect=subprocess.TimeoutExpired(["docker"], 5))
    )
    with pytest.raises(deploy_receiver.DeploymentError):
        deploy_receiver.run(["docker", "stop", "verified-candidate"], timeout=5)
    assert record == {"failure_code": "DATABASE_ERROR", "failure_detail_code": "COMPRESSED_MEMORY_LIMIT"}
