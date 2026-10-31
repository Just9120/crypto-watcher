"""The capped contour rejects incomplete evidence and a different workload."""

from scripts.qualify_processing import MEMORY, RESERVE, admit, verdict


def test_admission_needs_contiguous_cpu_memory_and_physical_reserve():
    healthy = {"idle_pct": 35, "available_bytes": MEMORY + RESERVE}
    assert admit([healthy] * 6, 7 * 1024**3)
    assert not admit([healthy] * 5, 7 * 1024**3)
    assert not admit([healthy] * 6, 7 * 1024**3 - 1)
    assert not admit([healthy] * 5 + [{**healthy, "idle_pct": 34.9}], 7 * 1024**3)
    assert not admit([healthy] * 5 + [{**healthy, "available_bytes": MEMORY + RESERVE - 1}], 7 * 1024**3)


def test_incomplete_or_changed_input_report_cannot_pass():
    failures = verdict({"native_backend": True, "streams": 1648, "offered_closed": 2472})
    assert "JOINT_ASSERTIONS_FAILED_OR_INCOMPLETE" in failures
    assert "FROZEN_INPUT_IDENTITY_CHANGED" in failures
    assert "NATIVE_REQUIRED" in verdict({})


def test_status_timeout_reobserves_same_container_without_restarting(monkeypatch):
    import json
    import subprocess

    from scripts import qualify_processing as q

    calls = []

    def read(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired("docker inspect", 8)
        return json.dumps([{"Id": "same-container", "State": {"Running": False}}])

    monkeypatch.setattr(q, "docker", read)
    monkeypatch.setattr(q.time, "sleep", lambda _: None)
    assert q.observe("same-container")["State"]["Running"] is False
    assert calls == [("inspect", "same-container"), ("inspect", "same-container")]


def test_hidden_report_artifacts_are_required_and_exclude_fixture_databases():
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    workflow = yaml.load((root / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["processing-qualification"]
    upload = next(step for step in job["steps"] if "actions/upload-artifact@" in step.get("uses", ""))
    settings = upload["with"]
    assert upload["if"] == "always()"
    assert settings["include-hidden-files"] == "true"
    assert settings["if-no-files-found"] == "error"
    patterns = settings["path"].splitlines()
    assert patterns == [".cache-g1/processing-*/*.json", ".cache-g1/processing-*/*.log"]


def test_cpu_profile_covers_measured_phase_and_excludes_seed_and_cleanup(tmp_path):
    import json

    from scripts.qualify_processing import PhaseProfile

    def unprofiled_seed():
        return sum(range(100))

    def measured_work():
        return sum(i * i for i in range(1000))

    def unprofiled_cleanup():
        return sum(range(100))

    capture = PhaseProfile(True)
    unprofiled_seed()
    capture.stage({"stage": "SEED_COMPLETE"})
    measured_work()
    capture.stage({"stage": "STOPPING_FIXTURE_WORKERS"})
    unprofiled_cleanup()
    target = tmp_path / "profile.json"
    capture.save(target)
    report = json.loads(target.read_text())
    names = {v["function"] for v in report["functions"]}
    assert "measured_work" in names
    assert not names.intersection({"unprofiled_seed", "unprofiled_cleanup"})
    assert report["phase_started"] is report["phase_ended"] is True
    assert report["mode"] == "DIAGNOSTIC_PROFILE_NOT_ACCEPTANCE"
