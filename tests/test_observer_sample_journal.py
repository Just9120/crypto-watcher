import json
import subprocess
from unittest.mock import patch

import pytest

from scripts import observe_capacity as observer

SOURCE = "f" * 40
PENDING = {"at_ms": 301000, "stability_blockers": ["ANALYTICS_RECOVERY_PENDING"]}
CLEARED = {"at_ms": 311000, "stability_blockers": [], "evidence": "later actual snapshot"}


@pytest.mark.parametrize(
    "error,extra",
    [
        (subprocess.TimeoutExpired("private-command", 15), {"timeout_seconds": 15}),
        (subprocess.CalledProcessError(7, "private-command", output="private-output"), {"returncode": 7}),
        (ValueError("private-message"), {}),
    ],
    ids=("timeout", "nonzero", "invalid-revision"),
)
def test_failed_settling_probe_preserves_each_previous_snapshot_and_sanitized_error(tmp_path, error, extra):
    state = {"state": "WAITING_STABLE"}
    with patch.object(observer, "probe", side_effect=[PENDING, error]), patch.object(observer.time, "sleep"):
        with pytest.raises(type(error)):
            observer.sample_probe(tmp_path, state, SOURCE)
    path = tmp_path / state["active_sample_attempt"]
    raw = path.read_text(encoding="utf-8")
    attempt = json.loads(raw)
    assert attempt["decision"] == "ERROR" and attempt["error_class"] == type(error).__name__
    assert [value["decision"] for value in attempt["probes"]] == ["CAPTURED", "ERROR"]
    assert attempt["probes"][0]["snapshot"] == PENDING
    assert attempt["probes"][1]["error_class"] == type(error).__name__
    assert all(attempt["probes"][1][key] == value for key, value in extra.items())
    assert "private-" not in raw
    assert all("started_at" in value and "finished_at" in value for value in attempt["probes"])


def test_successful_settling_keeps_both_actual_probes_without_mutating_source_values(tmp_path):
    state = {"state": "WAITING_STABLE"}
    with (
        patch.object(observer, "probe", side_effect=[PENDING, CLEARED]),
        patch.object(observer.time, "sleep"),
    ):
        value = observer.sample_probe(tmp_path, state, SOURCE)
    attempt = json.loads((tmp_path / state["active_sample_attempt"]).read_text(encoding="utf-8"))
    assert attempt["decision"] == "CAPTURED" and attempt["snapshot"] == value
    assert [item["snapshot"] for item in attempt["probes"]] == [PENDING, CLEARED]
    assert all(item["decision"] == "CAPTURED" for item in attempt["probes"])
    assert value["baseline_settling_attempts"] == [
        {"at_ms": 301000, "blockers": PENDING["stability_blockers"]}
    ]
    assert "baseline_settling_attempts" not in PENDING and "baseline_settling_attempts" not in CLEARED


def test_restart_closes_inflight_subprobe_and_preserves_captured_snapshot(tmp_path):
    state = {"state": "WAITING_STABLE"}
    with (
        patch.object(observer, "probe", side_effect=[PENDING, KeyboardInterrupt]),
        patch.object(observer.time, "sleep"),
        pytest.raises(KeyboardInterrupt),
    ):
        observer.sample_probe(tmp_path, state, SOURCE)
    path = tmp_path / state["active_sample_attempt"]
    before = json.loads(path.read_text(encoding="utf-8"))
    assert before["decision"] == "PENDING"
    assert [item["decision"] for item in before["probes"]] == ["CAPTURED", "PENDING"]
    observer.recover_sample(tmp_path, state)
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["decision"] == "INTERRUPTED"
    assert after["probes"][0] == before["probes"][0]
    assert after["probes"][1]["decision"] == "INTERRUPTED"
    assert after["probes"][1]["error_class"] == "ObserverRestart"
    assert state["stable_samples"] == 0 and state.get("interrupted_samples", 0) == 0
