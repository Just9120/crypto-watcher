"""The bounded service trial cannot change the frozen scenario or acceptance."""

import copy
import json
from pathlib import Path

import pytest
import yaml

from scripts import qualify_group_pair as original
from scripts import qualify_service_pair as trial
from tests.test_group_pair import sample


def test_trial_uses_original_comparison_and_retains_failed_timing(monkeypatch):
    assert trial.comparison is original.comparison
    monkeypatch.setattr(original.q, "verdict", lambda result: result["failures_fixture"])
    cases = [sample(("SOURCE_LATENCY",)), sample(("SOURCE_LATENCY",))]
    result = trial.comparison(cases)
    assert result["state"] == "COMPARISON_COMPLETE"
    assert not result["candidate_processing_gates"]
    assert not result["acceptance_qualified"]
    altered = copy.deepcopy(cases)
    altered[1]["result"]["result"]["offered_sha256"] = "different"
    assert trial.comparison(altered)["state"] == "PAIR_INCOMPLETE_IDENTITY"


def test_trial_admission_is_original_one_cpu_and_budgets():
    from scripts import qualify_processing as q

    healthy = {"idle_pct": 35, "available_bytes": q.MEMORY + q.RESERVE}
    assert trial.admission([healthy] * 6, 7 * 1024**3, cpu_limit=1)
    assert not trial.admission([healthy] * 5, 7 * 1024**3, cpu_limit=1)
    with pytest.raises(ValueError):
        trial.admission([healthy] * 6, 7 * 1024**3, cpu_limit=2)


def test_supervisor_invokes_one_explicit_case_same_container(tmp_path, monkeypatch):
    from scripts import qualify_framed_backup

    monkeypatch.setattr(qualify_framed_backup, "require_native_limits", lambda **kw: None)
    original_path = Path
    monkeypatch.setattr(trial, "Path", lambda path: tmp_path if path == "/work" else original_path(path))
    monkeypatch.setenv("CW_SUBJECT_SERVICES", "1")
    calls = []

    class Child:
        returncode = 1

        def wait(self, **kwargs):
            (tmp_path / "controller-ack").write_text("ack")

    monkeypatch.setattr(trial.subprocess, "Popen", lambda command, **kw: calls.append(command) or Child())
    assert trial.supervise(1, 15) == 1
    assert calls[0][3:] == [
        "scripts.qualify_service_pair",
        "case",
        "--report",
        "/work/result.json",
        "--cpu-limit",
        "1",
    ]
    assert json.loads((tmp_path / "supervisor.json").read_text())["child_exit"] == 1
    assert len(calls) == 1


def test_manual_trial_never_touches_pr94_or_deployment():
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.load((root / ".github/workflows/full-validation.yml").read_text(), Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["subject-services"]
    assert "codex/typed-services-trial-20261104" in job["if"] and "github.run_attempt == 1" in job["if"]
    text = json.dumps(job)
    assert "secrets." not in text and "environment" not in job
    assert "600s" in text and "qualify_service_pair run" in text
    assert trial.BASELINE == "19e0051b3ed009c90d6badd93a75b14a6d097e6a"
