import pytest

from cryptowatcher.maintenance import Unit, qualify_closed
from cryptowatcher.storage import StateError

TARGET = Unit("reviewed-container", "reviewed-image", "reviewed-source")


class FakeOperations:
    def __init__(self, failure=None):
        self.now = 0.0
        self.calls = []
        self.records = []
        self.failure = failure
        self.worker_budget = None

    def step(self, name, cost):
        self.calls.append(name)
        self.now += cost
        if self.failure == name:
            raise RuntimeError("controlled failure")

    def verify(self, expected):
        assert expected == TARGET
        self.step("verify", 2)
        return {"settings_hash": "preserved"}

    def record(self, value):
        self.records.append(dict(value))

    def stop(self, expected, seconds):
        assert seconds <= 60
        self.step("stop", 10)

    def checkpoint(self, expected, seconds):
        assert seconds <= 30
        self.step("checkpoint", 5)

    def qualify(self, expected, seconds):
        self.worker_budget = seconds
        self.step("qualify", {"overrun": 800, "hard_overrun": 1000}.get(self.failure, 400))
        return {"state": "QUALIFIED_SNAPSHOT_CANDIDATE"}

    def terminate_worker(self):
        self.step("terminate", 1)

    def restore(self, expected, seconds):
        assert expected == TARGET and seconds <= 60
        self.step("restore", 10)

    def postcheck(self, expected, baseline, seconds):
        assert baseline == {"settings_hash": "preserved"}
        self.step("postcheck", 20)
        return {"state": "PASS"}


def run(ops):
    return qualify_closed(ops, TARGET, clock=lambda: ops.now)


def test_whole_deadline_includes_stop_checkpoint_qualification_restore_and_checks():
    ops = FakeOperations()
    proof = run(ops)
    assert proof["state"] == "QUALIFIED_RUNTIME_RESTORED"
    assert proof["production_replacement"] is False
    assert ops.worker_budget == 900 - 150 - 17
    assert proof["elapsed_seconds"] == 448
    assert ops.calls == ["verify", "stop", "checkpoint", "qualify", "terminate", "restore", "postcheck"]


@pytest.mark.parametrize("failure", ["stop", "checkpoint", "qualify", "terminate", "overrun"])
def test_failure_or_ambiguous_stop_ack_still_restores_exact_original_unit(failure):
    ops = FakeOperations(failure)
    proof = run(ops)
    assert "restore" in ops.calls and "postcheck" in ops.calls
    assert proof["state"] == "UNQUALIFIED_RUNTIME_RESTORED"
    assert ops.records[-1]["state"] == proof["state"]


@pytest.mark.parametrize("failure", ["restore", "postcheck"])
def test_failed_restore_is_never_reported_as_success(failure):
    proof = run(FakeOperations(failure))
    assert proof["state"] == "RESTORATION_UNCONFIRMED"


def test_preflight_failure_never_changes_production():
    ops = FakeOperations("verify")
    with pytest.raises(RuntimeError):
        run(ops)
    assert ops.calls == ["verify"]


def test_adapter_overrun_still_attempts_start_without_claiming_a_budget_pass():
    ops = FakeOperations("hard_overrun")
    proof = run(ops)
    assert "restore" in ops.calls
    assert proof["elapsed_seconds"] > 900
    assert proof["state"] == "RESTORATION_UNCONFIRMED"


@pytest.mark.parametrize("seconds,reserve", [(901, 150), (900, 0), (900, 900), (0, 150)])
def test_budget_extension_or_missing_restore_reserve_is_rejected(seconds, reserve):
    ops = FakeOperations()
    with pytest.raises(StateError):
        qualify_closed(ops, TARGET, seconds=seconds, restore_reserve=reserve)
    assert not ops.calls


def test_explicit_one_time_budget_keeps_single_deadline_and_restore_reserve():
    ops = FakeOperations()
    proof = qualify_closed(ops, TARGET, seconds=3600, one_time_maintenance=True, clock=lambda: ops.now)
    assert proof["state"] == "QUALIFIED_RUNTIME_RESTORED"
    assert proof["one_time_maintenance"] is True
    assert proof["whole_budget_seconds"] == 3600
    assert ops.worker_budget == 3600 - 150 - 17
    assert ops.calls[-3:] == ["terminate", "restore", "postcheck"]


def test_one_time_mode_also_has_a_hard_upper_bound():
    ops = FakeOperations()
    with pytest.raises(StateError):
        qualify_closed(ops, TARGET, seconds=3601, one_time_maintenance=True)
    assert not ops.calls
