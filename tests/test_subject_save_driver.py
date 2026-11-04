"""Prevent a count-only or unused-path native narrow result from passing."""

from copy import deepcopy

import pytest

from scripts.qualify_subject_save import assess, identity, rewrite_create


def rows():
    values = []
    for repeat in range(3):
        for enabled in (False, True):
            values.append(
                {
                    "repeat": repeat,
                    "enabled": enabled,
                    "oracle_equal": True,
                    "facts": 800,
                    "offered_closed": 192,
                    "due_pending": 0,
                    "completed_outcomes": 8,
                    "future_jobs": 120,
                    "queues": {},
                    "source_blocks": {},
                    "telegram": 32,
                    "agent": 168,
                    "agent_kinds": {"LIVE_EVENT": 96, "HISTORICAL_OUTCOME_UPDATE": 72},
                    "facts_sha": "facts",
                    "oracle_sha": "facts",
                    "outcomes_sha": "numeric",
                    "agent_payloads_sha": "signed-body",
                    "telegram_texts_sha": "text",
                    "subject": {"planned": 100 if enabled else 0},
                    "cpu_seconds": 75 if enabled else 100,
                    "stages": {"save_operation_cpu": 60 if enabled else 100},
                }
            )
    return values


def test_same_counts_with_changed_numeric_or_delivery_bytes_cannot_pass():
    for key in ("outcomes_sha", "agent_payloads_sha", "telegram_texts_sha", "facts_sha"):
        values = rows()
        values[-1][key] = "changed"
        with pytest.raises(ValueError, match="mismatch"):
            assess(values)
    values = rows()
    values[-1]["subject"]["planned"] = 0
    with pytest.raises(ValueError, match="not exercised"):
        assess(values)


def test_empty_due_queue_without_executed_outcomes_is_not_evidence():
    values = rows()
    for row in values:
        row["completed_outcomes"] = 0
    with pytest.raises(ValueError, match="correctness"):
        assess(values)


def test_one_losing_pair_cannot_be_hidden_by_successful_median():
    values = rows()
    values[-1]["stages"]["save_operation_cpu"] = 110
    result = assess(values)
    assert result["reduction_pct"]["save"] >= 25 and result["narrow_gate"] is False
    assert result["full_processing_acceptance"] is False
    assert assess(rows())["narrow_gate"] is True


def test_entrypoint_rewrite_keeps_readonly_cpu_memory_and_owned_report():
    arguments = [
        "create",
        "--name",
        "owned",
        "--network=none",
        "--cpus=1",
        "--memory=4g",
        "--read-only",
        "--entrypoint=python",
        "sha256:owned",
        "-B",
        "scripts/qualify_processing.py",
        "case",
        "--report",
        "/work/result.json",
    ]
    actual = rewrite_create(arguments)
    for option in ("--network=none", "--cpus=1", "--memory=4g", "--read-only", "--env=TMPDIR=/work"):
        assert option in actual
    assert actual[-5:] == ["-m", "scripts.qualify_subject_save", "case", "--report", "/work/result.json"]
    assert arguments[0] == "create" and "scripts/qualify_processing.py" in arguments
    with pytest.raises(ValueError, match="command"):
        rewrite_create(["create", "--entrypoint=sh", "untrusted"])


def test_recovery_never_accepts_other_container_or_changed_resource_limits():
    info = {
        "Id": "owned-id",
        "Image": "sha256:owned",
        "Config": {"Labels": {"io.cryptowatcher.qualifier-owner": "owned"}},
        "HostConfig": {
            "NanoCpus": 1000000000,
            "Memory": 4294967296,
            "MemorySwap": 4294967296,
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "PidsLimit": 64,
        },
    }
    assert identity(info, "owned", "sha256:owned") == "owned-id"
    for field, value in (("NanoCpus", 2000000000), ("ReadonlyRootfs", False), ("NetworkMode", "bridge")):
        bad = deepcopy(info)
        bad["HostConfig"][field] = value
        with pytest.raises(ValueError, match="identity/limits"):
            identity(bad, "owned", "sha256:owned")
