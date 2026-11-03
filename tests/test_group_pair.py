"""The sole comparison keeps source binding, original gates and normal runtime."""

import copy
import json
from pathlib import Path

import pytest
import yaml

from scripts import qualify_group_pair as pair


def sample(failures=()):
    return {
        "state": "CASE_COMPLETE",
        "fixture_cleanup": "COMPLETE",
        "result": {
            "state": "FAIL" if failures else "PASS",
            "failures": list(failures),
            "temporary_database_removed": True,
            "result": {
                "seed_sha256": "seed",
                "offered_sha256": "offered",
                "streams": 1648,
                "offered_closed": 2472,
                "failures_fixture": list(failures),
                "bounded_pool": {
                    "global_actual_sha256": "facts",
                    "global_expected_sha256": "facts",
                    "agent_receipts_by_kind": {"LIVE_EVENT": 1, "HISTORICAL_OUTCOME_UPDATE": 1},
                },
                "delivery_queues_at_endpoint": {},
            },
        },
    }


def test_completed_comparison_never_relabels_failed_candidate_as_acceptance(monkeypatch):
    monkeypatch.setattr(pair.q, "verdict", lambda r: r["failures_fixture"])
    cases = [sample(("late",)), sample(("late",))]
    result = pair.comparison(cases)
    assert result["state"] == "COMPARISON_COMPLETE"
    assert not result["candidate_processing_gates"] and not result["acceptance_qualified"]
    assert result["candidate_failures"] == ["late"]
    for field in ("offered_sha256", "seed_sha256"):
        changed = copy.deepcopy(cases)
        changed[1]["result"]["result"][field] = "other"
        assert pair.comparison(changed)["state"] == "PAIR_INCOMPLETE_IDENTITY"
    changed = copy.deepcopy(cases)
    changed[1]["result"]["result"]["bounded_pool"]["global_actual_sha256"] = "extra"
    assert pair.comparison(changed)["state"] == "PAIR_INCOMPLETE_IDENTITY"


def test_cleanup_gate_and_omitted_failure_cannot_pass_comparison(monkeypatch):
    monkeypatch.setattr(pair.q, "verdict", lambda r: r["failures_fixture"])
    cases = [sample(), sample()]
    cases[1]["fixture_cleanup"] = "UNCONFIRMED"
    assert pair.comparison(cases)["state"] == "PAIR_INCOMPLETE"
    cases[1] = sample()
    cases[1]["result"]["failures"] = ["unrecorded"]
    with pytest.raises(ValueError, match="assertions"):
        pair.comparison(cases)


def test_supervisor_invokes_unmodified_normal_runtime_without_experimental_adapters(tmp_path, monkeypatch):
    from scripts import qualify_framed_backup

    monkeypatch.setattr(qualify_framed_backup, "require_native_limits", lambda **kw: None)
    original = Path
    monkeypatch.setattr(pair, "Path", lambda path: tmp_path if path == "/work" else original(path))
    calls = []

    class Child:
        returncode = 1

        def wait(self, **kw):
            (tmp_path / "controller-ack").write_text("ack")

    monkeypatch.setattr(pair.subprocess, "Popen", lambda command, **kw: calls.append(command) or Child())
    assert pair.supervise(1, 15) == 1
    assert calls[0][3:] == [
        "scripts.qualify_processing",
        "case",
        "--report",
        "/work/result.json",
        "--cpu-limit",
        "1",
    ]
    assert json.loads((tmp_path / "supervisor.json").read_text())["child_exit"] == 1


def test_manual_profile_cannot_deploy_or_replace_required_pr_workflow():
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github/workflows/full-validation.yml").read_text()
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["on"]["workflow_dispatch"]["inputs"]["mode"]["default"] == "full-main"
    group = workflow["jobs"]["group-publication"]
    assert "github.run_attempt == 1" in group["if"]
    assert "codex/interface-acceptance-20261030" in group["if"]
    assert "secrets." not in text and "environment:" not in text
    assert "600s" in text and "qualify_group_pair run" in text
    assert (root / "scripts/qualify_group_pair.py").read_text().count("pair.one(") == 1
