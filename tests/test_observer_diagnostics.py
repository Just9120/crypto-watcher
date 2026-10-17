import json
import subprocess
from unittest.mock import patch

import pytest

from scripts import observe_capacity as observer

SOURCE = "f" * 40


def state():
    return {"state": "READY_FOR_BASELINE", "stable_samples": 2, "last_sample_blockers": []}


def test_rejected_deep_boundary_preserves_real_reason_and_full_snapshot(tmp_path):
    value = {
        "at_ms": 1000,
        "stability_blockers": ["ANALYTICS_RECOVERY_PENDING"],
        "runtime_ledger": {"state": "PARTIAL_BUDGET", "records": 12},
    }
    current = state()
    with patch.object(observer, "probe", return_value=value) as probe:
        observer.baseline_boundary(tmp_path, current, SOURCE)
    probe.assert_called_once_with(SOURCE, deep=True)
    assert current["state"] == "WAITING_STABLE" and current["stable_samples"] == 0
    assert current["last_blockers"] == value["stability_blockers"]
    assert current["last_sample_blockers"] == []
    assert current["last_baseline_rejection"]["file"] == "baseline-attempt-0001.json"
    attempt = json.loads((tmp_path / "baseline-attempt-0001.json").read_text())
    assert attempt["snapshot"] == value and attempt["decision"] == "REJECTED"
    assert not (tmp_path / "baseline.json").exists()
    # A later successful boundary does not erase or overwrite the rejected evidence.
    rejected = (tmp_path / "baseline-attempt-0001.json").read_bytes()
    current["state"] = "READY_FOR_BASELINE"
    clean = {"at_ms": 2000, "stability_blockers": []}
    with patch.object(observer, "probe", return_value=clean):
        observer.baseline_boundary(tmp_path, current, SOURCE)
    assert current["state"] == "OBSERVING" and current["baseline_ms"] == 2000
    assert json.loads((tmp_path / "baseline.json").read_text()) == clean
    assert (tmp_path / "baseline-attempt-0001.json").read_bytes() == rejected
    assert current["last_baseline_rejection"]["blockers"] == value["stability_blockers"]


@pytest.mark.parametrize(
    "error",
    [
        ValueError("private body"),
        subprocess.TimeoutExpired("secret command", 145),
        subprocess.CalledProcessError(7, "secret command", stderr="private stderr"),
    ],
)
def test_boundary_failures_record_phase_without_exception_bodies(tmp_path, error):
    current = state()
    with patch.object(observer, "probe", side_effect=error), pytest.raises(type(error)):
        observer.boundary_probe(tmp_path, current, SOURCE, "baseline")
    path = tmp_path / "baseline-attempt-0001.json"
    raw = path.read_text()
    value = json.loads(raw)
    assert value["decision"] == "ERROR" and value["error_class"] == type(error).__name__
    assert "secret" not in raw and "private" not in raw
    assert current["last_error_phase"] == "BASELINE"
    assert current["last_error_attempt"] == path.name
    persisted = json.loads((tmp_path / "status.json").read_text())
    assert persisted["phase"] == "BASELINE" and persisted["active_attempt"] == path.name


def test_restart_of_inflight_boundary_retains_interrupted_attempt(tmp_path):
    current = state()
    # Simulate a process terminating while its subprocess is still being awaited.
    with patch.object(observer, "probe", side_effect=KeyboardInterrupt), pytest.raises(KeyboardInterrupt):
        observer.boundary_probe(tmp_path, current, SOURCE, "baseline")
    assert json.loads((tmp_path / "baseline-attempt-0001.json").read_text())["decision"] == "PENDING"
    observer.recover_boundary(tmp_path, current)
    assert current["state"] == "WAITING_STABLE" and current["stable_samples"] == 0
    assert json.loads((tmp_path / "baseline-attempt-0001.json").read_text())["decision"] == "INTERRUPTED"
    with patch.object(observer, "probe", return_value={"at_ms": 2000, "stability_blockers": []}):
        observer.baseline_boundary(tmp_path, current, SOURCE)
    assert (tmp_path / "baseline-attempt-0002.json").exists()


def test_final_blockers_are_captured_without_hiding_interruption(tmp_path):
    current = {"state": "READY_FOR_FINAL", "interrupted_samples": 3}
    value = {"at_ms": 86400000, "stability_blockers": ["INGESTION_STALE"]}
    with patch.object(observer, "probe", return_value=value):
        assert observer.boundary_probe(tmp_path, current, SOURCE, "final") == value
    attempt = json.loads((tmp_path / "final-attempt-0001.json").read_text())
    assert attempt["blockers"] == ["INGESTION_STALE"] and attempt["snapshot"] == value
    assert current["interrupted_samples"] == 3


def test_orphaned_attempt_is_never_overwritten_when_status_update_was_lost(tmp_path):
    path = tmp_path / "baseline-attempt-0001.json"
    path.write_text('{"decision":"PENDING","evidence":"preserved"}')
    original = path.read_bytes()
    current = state()
    with patch.object(observer, "probe", return_value={"at_ms": 2000, "stability_blockers": []}):
        observer.baseline_boundary(tmp_path, current, SOURCE)
    assert path.read_bytes() == original
    assert current["last_baseline_attempt"]["file"] == "baseline-attempt-0002.json"
