"""CPU exceptions stay explicit; pair budgets and all result gates are preserved."""

import copy
import json
import subprocess
from pathlib import Path

import pytest

from scripts import cpu_pair_budget as b
from scripts import qualify_processing as q


def test_shared_budget_charges_both_cases_controller_and_final_tail():
    state = {"wall": 0.0, "cpu": 0.0}
    budget = b.Budget(lambda: state["wall"], lambda: state["cpu"])
    budget.completed_cpu_upper = b.tail_upper(100, 150, 151, 1)
    state.update(wall=180, cpu=2)
    assert budget.used(120) == pytest.approx(223.2)
    assert budget.admit_second()
    budget.completed_cpu_upper = 300
    assert not budget.admit_second()
    assert budget.reason(118) == "STOP_PAIR_CPU_RESERVE"
    state["wall"] = 510
    assert budget.reason() == "STOP_PAIR_WALL_BUDGET"
    with pytest.raises(ValueError):
        b.tail_upper(1, 2, 1, 1.5)
    with pytest.raises(ValueError):
        b.tail_upper(1, 1, 2, 2.5)


def test_pair_admission_preserves_reserve_for_the_larger_cpu_ceiling():
    sample = {"idle_pct": 52.5, "available_bytes": q.MEMORY + q.RESERVE}
    assert b.admission([sample] * 6, 7 * 1024**3, 4)
    assert not b.admission([{**sample, "idle_pct": 52.4}] * 6, 7 * 1024**3, 4)
    assert not b.admission([sample] * 5, 7 * 1024**3, 4)
    assert not b.admission([sample] * 6, 7 * 1024**3 - 1, 4)
    assert not b.admission([{**sample, "available_bytes": 0}] * 6, 7 * 1024**3, 4)


def test_native_cpu_exception_is_explicit_and_other_guards_are_unchanged(monkeypatch):
    import sys
    from types import SimpleNamespace

    from scripts import qualify_framed_backup as f

    original = Path.read_text
    values = {"cpu.max": "150000 100000", "memory.max": str(q.MEMORY), "memory.swap.max": "0"}
    monkeypatch.setattr(f.sys, "platform", "linux")
    monkeypatch.setattr(f.frozen_codec_native, "available", lambda: True)
    monkeypatch.setitem(
        sys.modules, "resource", SimpleNamespace(RLIMIT_CPU=0, getrlimit=lambda _: (300, 300))
    )
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda p, *a, **k: (
            values[p.name] if p.as_posix().startswith("/sys/fs/cgroup/") else original(p, *a, **k)
        ),
    )
    with pytest.raises(f.StateError, match="at most1CPU"):
        f.require_native_limits()
    f.require_native_limits(cpu_limit=1.5)
    values["memory.swap.max"] = "1"
    with pytest.raises(f.StateError):
        f.require_native_limits(cpu_limit=1.5)
    values["memory.swap.max"] = "0"
    values["memory.max"] = str(q.MEMORY + 1)
    with pytest.raises(f.StateError):
        f.require_native_limits(cpu_limit=1.5)
    values["memory.max"] = str(q.MEMORY)
    values["cpu.max"] = "200000 100000"
    f.require_native_limits(cpu_limit=2)
    with pytest.raises(f.StateError):
        f.require_native_limits(cpu_limit=1.5)
    with pytest.raises(f.StateError, match="Unsupported"):
        f.require_native_limits(cpu_limit=2.5)


def test_pair_recomputes_gates_and_never_promotes_incomplete_or_coverage_failures(monkeypatch):
    assert not b.processing_ok({"source": b.RUNTIME, "failures": [], "temporary_database_removed": True})
    envelope = {
        "source": b.RUNTIME,
        "failures": ["FULL_POOL_PRESSURE_UNPROVEN", "MATURE_CAPACITY_UNPROVEN"],
        "temporary_database_removed": True,
        "result": {
            "bounded_pool": {"agent_receipts_by_kind": {"LIVE_EVENT": 1, "HISTORICAL_OUTCOME_UPDATE": 1}},
            "phase_cpu_seconds": 90,
            "phase_seconds": 130,
        },
    }
    monkeypatch.setattr(
        q, "verdict", lambda *a, **k: ["FULL_POOL_PRESSURE_UNPROVEN", "MATURE_CAPACITY_UNPROVEN"]
    )
    assert b.processing_ok(envelope)
    bad = copy.deepcopy(envelope)
    bad["failures"] = []
    assert not b.processing_ok(bad)
    bad = copy.deepcopy(envelope)
    bad["result"]["bounded_pool"]["agent_receipts_by_kind"]["LIVE_EVENT"] = 0
    assert not b.processing_ok(bad)
    case = {
        "result": envelope,
        "samples": [
            {"cpu_seconds": 0, "sampled_at": 0},
            {"cpu_seconds": 1, "sampled_at": 2, "nr_throttled": 2},
        ],
    }
    assert b.interpretation([case], False) == "INCOMPLETE"
    assert b.interpretation([case, case], True) == "PROCESSING_GATES_MET_CPU_CAUSE_UNPROVEN"
    monkeypatch.setattr(q, "verdict", lambda *a, **k: ["SOURCE_DEADLINE_EXCEEDED"])
    assert b.interpretation([case, case], True) == "CPU_BUDGET_NOT_SUFFICIENT"


def test_pair_image_overlay_cannot_replace_frozen_application_or_scenario():
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "tests/cpu_pair.Dockerfile").read_text()
    copies = [s for s in dockerfile.splitlines() if s.startswith("COPY")]
    assert len(copies) == 1
    assert "cryptowatcher" not in copies[0] and "tests/" not in copies[0]
    assert all(
        name in copies[0]
        for name in (
            "cpu_pair_budget.py",
            "qualify_cpu_pair.py",
            "qualify_processing.py",
            "qualify_framed_backup.py",
        )
    )
    assert "io.cryptowatcher.measurement-revision" in dockerfile


def test_pair_checks_actual_container_limits_and_identity():
    info = {
        "Id": "id",
        "Config": {"Labels": {"io.cryptowatcher.cpu-pair": "owned"}},
        "HostConfig": {
            "NanoCpus": 1500000000,
            "Memory": q.MEMORY,
            "MemorySwap": q.MEMORY,
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "PidsLimit": 64,
        },
    }
    b.verify_container(info, "owned", "id", 1.5)
    for key, value in (("NanoCpus", 2000000000), ("MemorySwap", 2 * q.MEMORY), ("NetworkMode", "default")):
        wrong = copy.deepcopy(info)
        wrong["HostConfig"][key] = value
        with pytest.raises(ValueError):
            b.verify_container(wrong, "owned", "id", 1.5)
    with pytest.raises(ValueError):
        b.verify_container(info, "foreign", "id", 1.5)


@pytest.mark.parametrize("fault", ["none", "sample", "create-timeout"])
def test_pair_lifecycle_accounts_final_cpu_and_cleans_same_object_without_restarting(
    tmp_path, monkeypatch, fault
):
    from types import SimpleNamespace

    from scripts import qualify_cpu_pair as pair

    state = {"wall": 0.0, "running": False}
    calls = []
    owner = "cw-cpu-pair-fixed"
    cid = "same-owned-container"
    group = tmp_path / "group"
    group.mkdir()
    monkeypatch.setattr(pair.uuid, "uuid4", lambda: SimpleNamespace(hex="fixed"))
    monkeypatch.setattr(pair.os, "chown", lambda *a: None, raising=False)
    monkeypatch.setattr(pair.time, "monotonic", lambda: state["wall"])
    monkeypatch.setattr(pair.shutil, "disk_usage", lambda _: SimpleNamespace(free=8 * 1024**3))
    info = {
        "Id": cid,
        "Config": {"Labels": {"io.cryptowatcher.cpu-pair": owner}},
        "HostConfig": {
            "NanoCpus": 1500000000,
            "Memory": q.MEMORY,
            "MemorySwap": q.MEMORY,
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "PidsLimit": 64,
        },
    }

    def inspect(_):
        return {**info, "State": {"Running": state["running"], "Pid": 1}}

    def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == "create" and fault == "create-timeout":
            raise subprocess.TimeoutExpired("create", 10)
        if args[0] == "kill":
            state["running"] = False
        return cid

    def interval(seconds):
        state["wall"] += seconds
        return {"idle_pct": 80, "available_bytes": q.MEMORY + q.RESERVE}

    def sample(_):
        if fault == "sample":
            raise ValueError("sample unavailable")
        return {
            "sampled_at": state["wall"],
            "cpu_seconds": 20,
            "cpu_period": 100000,
            "cpu_quota": 150000,
            "nr_throttled": 0,
            "memory_peak_bytes": 1,
            "io_stat": "",
        }

    class Child:
        def __init__(self, command, **kwargs):
            calls.append(tuple(command))
            state["running"] = True
            work = tmp_path / "cpu1_5-work"
            (work / "result.json").write_text(json.dumps({"source": b.RUNTIME, "state": "FAIL"}))
            (work / "supervisor.json").write_text(json.dumps({"child_exit": 1, "timed_out": False}))

        def poll(self):
            return None if state["running"] else 1

        def wait(self, **kwargs):
            state["wall"] += 0.1
            state["running"] = False
            return 1

    monkeypatch.setattr(q, "docker", docker)
    monkeypatch.setattr(q, "observe", inspect)
    monkeypatch.setattr(q, "interval", interval)
    monkeypatch.setattr(b, "group_path", lambda _: group)
    monkeypatch.setattr(b, "group_sample", sample)
    monkeypatch.setattr(pair.subprocess, "Popen", Child)
    budget = b.Budget(lambda: state["wall"], lambda: 0)
    budget.completed_cpu_upper = 80
    proof = pair.one("frozen-image", "m" * 40, tmp_path, "cpu1_5", 1.5, budget)
    assert proof["fixture_cleanup"] == "COMPLETE" and not (tmp_path / "cpu1_5-work").exists()
    assert len([c for c in calls if c[:2] == ("docker", "--host")]) == (0 if fault == "create-timeout" else 1)
    assert len([c for c in calls if c[0] == "create"]) == 1
    assert ("rm", cid) in calls
    assert "--ulimit=cpu=340:340" in next(c for c in calls if c[0] == "create")
    assert budget.completed_cpu_upper > 80
    saved = json.loads((tmp_path / "cpu1_5-controller.json").read_text())
    assert saved["state"] == proof["state"]
    if fault != "none":
        assert proof["state"] == "CONTROLLER_FAILED"
        if fault == "sample":
            assert ("kill", cid) in calls
    else:
        assert proof["state"] == "CASE_COMPLETE"
        assert proof["cpu_seconds_upper"] > 20


@pytest.mark.parametrize(
    "single_cpu,expected,missing_unit", [(None, [1, 1.5], False), (2, [2], False), (2, [2], True)]
)
def test_complete_pair_runs_each_ceiling_once_and_keeps_qualification_failure(
    tmp_path, monkeypatch, single_cpu, expected, missing_unit
):
    from types import SimpleNamespace

    from scripts import qualify_cpu_pair as pair

    measurement = "a" * 40
    calls = []
    image = [
        {
            "Id": "frozen-image",
            "Config": {
                "Labels": {
                    "org.opencontainers.image.revision": b.RUNTIME,
                    "io.cryptowatcher.measurement-revision": measurement,
                }
            },
        }
    ]
    if missing_unit:
        image[0]["Config"]["Labels"]["io.cryptowatcher.publication-unit"] = "one-pass-v1"
    monkeypatch.setattr(pair.sys, "platform", "linux")
    monkeypatch.setattr(pair.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(q, "docker", lambda *a, **k: json.dumps(image))
    monkeypatch.setattr(
        pair,
        "signal",
        SimpleNamespace(SIGALRM=1, SIGINT=2, SIGTERM=3, signal=lambda *a: None, alarm=lambda *a: None),
    )
    monkeypatch.setattr(
        b,
        "Budget",
        lambda: SimpleNamespace(
            reason=lambda: None, admit_second=lambda: True, used=lambda: 250, elapsed=lambda: 380
        ),
    )
    monkeypatch.setattr(
        b, "interpretation", lambda cases, complete: "CPU_HYPOTHESIS_SUPPORTED_IN_FINITE_CASE"
    )

    def case(image, revision, directory, label, cpu, budget):
        calls.append((image, revision, cpu))
        return {
            "state": "CASE_COMPLETE",
            "fixture_cleanup": "COMPLETE",
            "result": {"state": "FAIL", "failures": sorted(b.COVERAGE)},
        }

    monkeypatch.setattr(pair, "one", case)
    output = tmp_path / "pair"
    monkeypatch.setattr(b, "processing_ok", lambda _: False)
    assert pair.run("tag", measurement, output, single_cpu=single_cpu) == 1
    result = json.loads((output / "pair.json").read_text())
    assert [c[2] for c in calls] == expected
    assert result["limits"]["cpu_order"] == expected
    if single_cpu:
        assert result["mode"] == "SINGLE_CPU"
        assert result["cpu_hypothesis"] == (
            "INCOMPLETE" if missing_unit else "SINGLE_CASE_PROCESSING_GATES_FAILED"
        )
    assert len({c[0] for c in calls}) == 1
    assert result["comparison_complete"] is (not missing_unit)
    assert result["state"] == ("PAIR_INCOMPLETE" if missing_unit else "QUALIFICATION_FAIL")
    if missing_unit:
        assert result["cases"][0]["error"] == "PUBLICATION_UNIT_EVIDENCE_MISSING"
    assert result["cpu_seconds_upper_including_controller"] == 250
    assert result["limits"]["cpu_seconds_pair_total"] == 450


def test_two_cpu_admission_and_tail_keep_host_reserve_and_explicit_ceiling():
    sample = {"idle_pct": 65, "available_bytes": q.MEMORY + q.RESERVE}
    assert b.admission([sample] * 6, 7 * 1024**3, 4, cpu_limit=2)
    assert not b.admission([{**sample, "idle_pct": 64.9}] * 6, 7 * 1024**3, 4, cpu_limit=2)
    assert b.tail_upper(10, 5, 6, 2) == pytest.approx(12.4)
