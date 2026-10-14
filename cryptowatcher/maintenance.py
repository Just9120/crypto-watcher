"""Bound one closed-source qualification and always restore the original unit.

The operator supplies an exact target, a managed watchdog and approved adapters.
This controller never promotes a candidate or resets the whole-operation budget.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .storage import StateError


@dataclass(frozen=True)
class Unit:
    container: str
    image: str
    revision: str


class Operations(Protocol):
    def verify(self, expected: Unit) -> dict[str, Any]: ...
    def stop(self, expected: Unit, seconds: float) -> None: ...
    def checkpoint(self, expected: Unit, seconds: float) -> None: ...
    def qualify(self, expected: Unit, seconds: float) -> dict[str, Any]: ...
    def terminate_worker(self) -> None: ...
    def restore(self, expected: Unit, seconds: float) -> None: ...
    def postcheck(self, expected: Unit, baseline: dict[str, Any], seconds: float) -> dict[str, Any]: ...
    def record(self, value: dict[str, Any]) -> None: ...


def qualify_closed(
    operations: Operations,
    expected: Unit,
    *,
    seconds: float = 900,
    restore_reserve: float = 150,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Execute only with prior authorization and an independently armed watchdog."""
    if not 0 < seconds <= 900 or not 30 <= restore_reserve < seconds:
        raise StateError("Invalid maintenance deadline or restore reserve")
    started = clock()
    deadline = started + seconds

    def remaining(reserve: float = 0) -> float:
        value = deadline - clock() - reserve
        if value <= 0:
            raise StateError("Maintenance time budget exhausted")
        return value

    baseline = operations.verify(expected)
    report: dict[str, Any] = {
        "state": "PREFLIGHT_PASS",
        "container": expected.container,
        "image": expected.image,
        "source": expected.revision,
        "production_replacement": False,
        "whole_budget_seconds": seconds,
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
        operations.record({**report, "state": "QUALIFYING"})
        report["qualification"] = operations.qualify(expected, remaining(restore_reserve))
        if report["qualification"].get("state") != "QUALIFIED_SNAPSHOT_CANDIDATE":
            raise StateError("Candidate not qualified")
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
                # the result still cannot be a successful <=900s qualification.
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
                else "UNQUALIFIED_RUNTIME_RESTORED"
                if failure is not None or elapsed > seconds
                else "QUALIFIED_RUNTIME_RESTORED"
            ),
        )
        operations.record(report)
    return report
