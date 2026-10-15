"""Bound one closed-source qualification and always restore the original unit.

The operator supplies an exact target, a managed watchdog and approved adapters.
This controller never promotes a candidate or resets the whole-operation budget.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol

from .storage import StateError

ONE_TIME_SECONDS = 3600


@dataclass(frozen=True)
class Unit:
    container: str
    image: str
    revision: str


class CommonOperations(Protocol):
    def verify(self, expected: Unit) -> dict[str, Any]: ...
    def stop(self, expected: Unit, seconds: float) -> None: ...
    def checkpoint(self, expected: Unit, seconds: float) -> None: ...
    def terminate_worker(self) -> None: ...
    def restore(self, expected: Unit, seconds: float) -> None: ...
    def postcheck(self, expected: Unit, baseline: dict[str, Any], seconds: float) -> dict[str, Any]: ...
    def record(self, value: dict[str, Any]) -> None: ...


class Operations(CommonOperations, Protocol):
    def qualify(self, expected: Unit, seconds: float) -> dict[str, Any]: ...


class CaptureOperations(CommonOperations, Protocol):
    def capture(self, expected: Unit, seconds: float) -> dict[str, Any]: ...


def qualify_closed(
    operations: Operations,
    expected: Unit,
    *,
    seconds: float = 900,
    restore_reserve: float = 150,
    one_time_maintenance: bool = False,
    absolute_deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Execute only with prior authorization and an independently armed watchdog."""
    return _run_closed(
        operations,
        expected,
        work=operations.qualify,
        mode="QUALIFICATION",
        seconds=seconds,
        restore_reserve=restore_reserve,
        one_time_maintenance=one_time_maintenance,
        absolute_deadline=absolute_deadline,
        clock=clock,
    )


def capture_closed(
    operations: CaptureOperations,
    expected: Unit,
    *,
    seconds: float = 900,
    restore_reserve: float = 150,
    absolute_deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Capture bytes and restore runtime; never claim SQLite/history qualification."""
    return _run_closed(
        operations,
        expected,
        work=operations.capture,
        mode="CAPTURE",
        seconds=seconds,
        restore_reserve=restore_reserve,
        one_time_maintenance=False,
        absolute_deadline=absolute_deadline,
        clock=clock,
    )


def _run_closed(
    operations: CommonOperations,
    expected: Unit,
    *,
    work: Callable[[Unit, float], dict[str, Any]],
    mode: Literal["QUALIFICATION", "CAPTURE"],
    seconds: float,
    restore_reserve: float,
    one_time_maintenance: bool,
    absolute_deadline: float | None,
    clock: Callable[[], float],
) -> dict[str, Any]:
    limit = ONE_TIME_SECONDS if one_time_maintenance else 900
    if not 0 < seconds <= limit or not 30 <= restore_reserve < seconds:
        raise StateError("Invalid maintenance deadline or restore reserve")
    started = clock()
    deadline = started + seconds
    if absolute_deadline is not None:
        if not math.isfinite(absolute_deadline):
            raise StateError("Invalid managed deadline")
        deadline = min(deadline, absolute_deadline)

    def remaining(reserve: float = 0) -> float:
        value = deadline - clock() - reserve
        if value <= 0:
            raise StateError("Maintenance time budget exhausted")
        return value

    remaining(restore_reserve)
    baseline = operations.verify(expected)
    report: dict[str, Any] = {
        "state": "PREFLIGHT_PASS",
        "container": expected.container,
        "image": expected.image,
        "source": expected.revision,
        "production_replacement": False,
        "whole_budget_seconds": seconds,
        "one_time_maintenance": one_time_maintenance,
        "baseline": baseline,
        "mode": mode,
    }
    operations.record(report)
    # Set this before requesting stop: a lost acknowledgement must restore too.
    stop_requested = False
    failure: Exception | None = None
    restoration_failure: Exception | None = None
    try:
        stop_requested = True
        operations.record({**report, "state": "STOP_REQUESTED"})
        operations.stop(expected, min(60, remaining(restore_reserve)))
        operations.checkpoint(expected, min(30, remaining(restore_reserve)))
        operations.record({**report, "state": "QUALIFYING" if mode == "QUALIFICATION" else "CAPTURING"})
        result = work(expected, remaining(restore_reserve))
        report["qualification" if mode == "QUALIFICATION" else "capture"] = result
        wanted = "QUALIFIED_SNAPSHOT_CANDIDATE" if mode == "QUALIFICATION" else "CAPTURED_BYTES_NOT_QUALIFIED"
        if result.get("state") != wanted:
            raise StateError(
                "Candidate not qualified" if mode == "QUALIFICATION" else "Closed capture incomplete"
            )
        remaining(restore_reserve)
    except Exception as error:
        failure = error
        report["failure_type"] = type(error).__name__
    finally:
        if stop_requested:
            # Worker shutdown cannot prevent restoring production. A managed
            # ExecStopPost additionally covers SIGKILL/loss of the Python host.
            try:
                operations.terminate_worker()
            except Exception as error:
                failure = failure or error
                report["worker_cleanup_failure"] = type(error).__name__
            try:
                # An adapter overrun is a failed budget, but must not leave the
                # bot stopped. Emergency recovery gets one bounded start attempt;
                # the result cannot qualify outside its approved whole budget.
                operations.restore(expected, max(1, min(60, deadline - clock())))
                report["post_checks"] = operations.postcheck(expected, baseline, remaining())
                if report["post_checks"].get("state") != "PASS":
                    raise StateError("Restoration post-check incomplete")
            except Exception as error:
                restoration_failure = error
                report["restoration_failure"] = type(error).__name__
        elapsed = clock() - started
        report.update(
            elapsed_seconds=round(elapsed, 3),
            state=(
                "RESTORATION_UNCONFIRMED"
                if restoration_failure is not None
                else (
                    "UNQUALIFIED_RUNTIME_RESTORED"
                    if mode == "QUALIFICATION"
                    else "UNCAPTURED_RUNTIME_RESTORED"
                )
                if failure is not None or clock() > deadline
                else (
                    "QUALIFIED_RUNTIME_RESTORED" if mode == "QUALIFICATION" else "CAPTURED_RUNTIME_RESTORED"
                )
            ),
        )
        operations.record(report)
    return report
