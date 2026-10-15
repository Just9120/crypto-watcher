import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import closed_source_maintenance as adapter


def fresh():
    return {
        "ingestion_at": 101.0,
        "timeframes": ["1m", "3m", "5m", "15m"],
        "subscriptions": {
            name: {"state": "CONNECTED", "requested": 4, "acknowledged": 4, "last_received_ms": 101000}
            for name in ("spot:0", "linear:0")
        },
        "telegram_state": "DELIVERED",
    }


def test_restoration_requires_new_data_both_markets_all_tf_full_ack():
    assert adapter.restored_data(fresh(), 100, 102)


@pytest.mark.parametrize(
    "fault",
    [
        "stale",
        "before_restart",
        "missing_market",
        "missing_tf",
        "partial_ack",
        "not_connected",
        "old_receipt",
        "blocked_delivery",
    ],
)
def test_old_or_incomplete_status_cannot_confirm_restored_alert_runtime(fault):
    value = fresh()
    if fault == "stale":
        value["ingestion_at"] = 101
    elif fault == "before_restart":
        value["ingestion_at"] = 99
    elif fault == "missing_market":
        value["subscriptions"].pop("spot:0")
    elif fault == "missing_tf":
        value["timeframes"].pop()
    elif fault == "partial_ack":
        value["subscriptions"]["spot:0"]["acknowledged"] = 3
    elif fault == "not_connected":
        value["subscriptions"]["spot:0"]["state"] = "RECONNECTING"
    elif fault == "old_receipt":
        value["subscriptions"]["spot:0"]["last_received_ms"] = 99000
    else:
        value["telegram_state"] = "BLOCKED"
    assert not adapter.restored_data(value, 100, 150 if fault == "stale" else 102)


def watchdog(monkeypatch, fault=None, *, one_time=False):
    invocation = "01" * 16
    argv = ["/usr/bin/python3", "-B", "/reviewed/runner.py", "--restore-only"]

    def call(args, **kwargs):
        if "GetUnit" in args:
            return json.dumps({"type": "o", "data": ["/managed/path"]})
        name = args[-1]
        if name == "InvocationID":
            return json.dumps({"type": "ay", "data": [1] * 16 if fault != "invocation" else [2] * 16})
        if name == "RuntimeMaxUSec":
            duration = (3600 if one_time else 900) * 1000000
            return json.dumps({"type": "t", "data": duration if fault != "budget" else 1800000000})
        if name == "ActiveEnterTimestampMonotonic":
            return json.dumps({"type": "t", "data": int(time.monotonic() * 1000000) - 10000000})
        command = [argv[0], argv, False, 0, 0, 0, 0, 0, 0, 0]
        if fault == "echo":
            command = ["/usr/bin/echo", argv, False, 0, 0, 0, 0, 0, 0, 0]
        if fault == "arguments":
            command[1] = argv + ["--unexpected"]
        if fault == "ignore_failure":
            command[2] = True
        commands = [] if fault == "missing" else [command]
        if fault == "duplicate":
            commands.append(command)
        return json.dumps({"type": "a(sasbttttuii)", "data": commands})

    monkeypatch.setattr(adapter, "call", call)
    return invocation, argv


def test_watchdog_must_bind_exact_restore_program_arguments_and_deadline(monkeypatch):
    invocation, argv = watchdog(monkeypatch)
    deadline = adapter.verify_watchdog("unit", invocation, argv)
    assert 878 <= deadline - time.monotonic() <= 881


def test_long_watchdog_cannot_be_used_without_the_explicit_one_time_flag(monkeypatch):
    invocation, argv = watchdog(monkeypatch, one_time=True)
    with pytest.raises(RuntimeError, match="binding"):
        adapter.verify_watchdog("unit", invocation, argv)
    adapter.verify_watchdog("unit", invocation, argv, one_time_maintenance=True)


@pytest.mark.parametrize(
    "fault", ["invocation", "budget", "echo", "arguments", "ignore_failure", "missing", "duplicate"]
)
def test_apparent_recovery_command_cannot_bypass_watchdog_binding(monkeypatch, fault):
    invocation, argv = watchdog(monkeypatch, fault)
    with pytest.raises(RuntimeError):
        adapter.verify_watchdog("unit", invocation, argv)


def test_slow_postcheck_calls_use_one_remaining_budget(monkeypatch):
    now = 0.0
    calls = []

    def clock():
        return now

    def exact(expected, *, seconds):
        nonlocal now
        calls.append(seconds)
        now += 2
        return {"State": {"Running": True, "Health": {"Status": "healthy"}}}

    def private(expected, *, seconds):
        nonlocal now
        calls.append(seconds)
        now += 3
        return {"private_sha256": "preserved"}

    monkeypatch.setattr(adapter.time, "monotonic", clock)
    monkeypatch.setattr(adapter, "exact_unit", exact)
    monkeypatch.setattr(adapter, "private_readback", private)
    monkeypatch.setattr(adapter, "call", lambda *a, **kw: pytest.fail("Deadline exceeded before exec"))
    ops = adapter.DockerOperations(Path("unused"), Path("unused"), "unused", "unused")
    with pytest.raises(RuntimeError, match="budget"):
        ops.postcheck(adapter.Unit("container", "image", "revision"), {"private_sha256": "preserved"}, 5)
    assert calls == [5, 3]


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_watchdog_writes_separate_start_receipt_even_after_cleanup_failure(
    tmp_path, monkeypatch, cleanup_fails
):
    calls = []

    def cleanup():
        calls.append("cleanup")
        if cleanup_fails:
            raise RuntimeError("controlled cleanup failure")

    def restore(expected, seconds):
        calls.append("restore")

    ops = SimpleNamespace(root=tmp_path, terminate_worker=cleanup, restore=restore)
    target = adapter.Unit("container", "image", "revision")
    original = '{"state":"QUALIFYING"}'
    (tmp_path / "operation.json").write_text(original)
    monkeypatch.setattr(adapter, "private_directory", lambda *a, **kw: None)
    monkeypatch.setenv("SERVICE_RESULT", "timeout")
    if cleanup_fails:
        with pytest.raises(RuntimeError):
            adapter.watchdog_restore(ops, target)
    else:
        adapter.watchdog_restore(ops, target)
    assert calls == ["cleanup", "restore"]
    assert (tmp_path / "operation.json").read_text() == original
    receipt = json.loads(next(tmp_path.glob("watchdog-recovery-*.json")).read_text())
    assert receipt["start_acknowledged"] is True
    assert receipt["qualification"] == "NOT_CONFIRMED_BY_RECOVERY"
    assert receipt["health"] == "PENDING"
    assert receipt["service_result"] == "timeout"
