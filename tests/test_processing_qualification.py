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
