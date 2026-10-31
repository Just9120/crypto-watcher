"""Bounded queue age reads and durable completion telemetry; future work is separate."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .analytics_worker import BatchTiming

from .events import PREFIX, History
from .storage import RUNTIME_UPSERT, Store, encode

SLO_MS = 60_000


def sync_job(history: History, key: str, value: Any) -> None:
    """A small failed-work index cannot be hidden behind future retry dates."""
    kind, separator, identity = key.partition("-job:")
    if not separator or kind not in ("outcome", "comparison"):
        return
    warning = "analytics-blocked:" + kind + ":" + identity
    if isinstance(value, dict) and value.get("state") not in ("PENDING", "COMPLETE", "COMPLETE_LIMITED"):
        history.put(
            warning,
            {
                "event_id": value.get("event_id"),
                "job_key": key,
                "state": value.get("state"),
                "reason": value.get("reason"),
            },
        )
    else:
        history.delete(warning)


def arrival(store: Store, name: str) -> None:
    key = "analytics_progress:" + name
    previous = store.get_runtime(key, {})
    previous["admitted_total"] = previous.get("admitted_total", 0) + 1
    # Admission is already inside the event transaction. Never commit its parent.
    store.db.execute(RUNTIME_UPSERT, (key, encode(previous)))


def snapshot(store: Store, now_ms: int) -> dict[str, Any]:
    result = {}
    history = History(store)
    for name, jobs, field in (
        ("outcome", "outcome-job:", "outcome_id"),
        ("comparison", "comparison-job:", "event_id"),
        ("analytics-gap", "analytics-gap:", "id"),
    ):
        warning = history.items("analytics-blocked:" + name + ":", 1)
        prefix = PREFIX + name + "-pending:"
        row = store.db.execute(
            "SELECT key,payload_json FROM runtime WHERE key>=? AND key<? ORDER BY key LIMIT 1",
            (prefix, prefix + "~"),
        ).fetchone()
        pending: dict[str, Any]
        if row is None:
            pending = {"state": "EMPTY", "due_lag_ms": 0}
        else:
            from .storage import json_payload

            binding = json_payload(row[1])
            job = history.get(jobs + str(binding.get(field, "")))
            if not isinstance(job, dict) or type(job.get("next_attempt_ms")) is not int:
                pending = {"state": "ORPHAN_OR_UNKNOWN", "due_lag_ms": None}
            else:
                lag = max(0, now_ms - job["next_attempt_ms"])
                state = (
                    "FUTURE" if job["next_attempt_ms"] > now_ms else "CURRENT" if lag <= SLO_MS else "OVERDUE"
                )
                if name == "analytics-gap" and not (
                    job.get("state") == "PENDING"
                    and job.get("attempts") == 0
                    and type(job.get("end")) is int
                    and 0 <= now_ms - job["end"] <= SLO_MS
                ):
                    state = "REFERENCED_RECOVERY"
                due = job.get("due_ms")
                pending = {
                    "state": state,
                    "due_lag_ms": lag,
                    "next_attempt_ms": job["next_attempt_ms"],
                    "job_state": job.get("state"),
                    "reason": job.get("reason"),
                    "original_due_ms": due,
                    "original_due_lag_ms": max(0, now_ms - due) if type(due) is int else None,
                }
        if warning:
            _, item = warning[0]
            # A failed last attempt does not establish today's input readiness.
            # Preserve the fail-closed public state, but never hide a separately
            # overdue pending queue behind that historical failure or retry date.
            result[name] = {
                "state": "SOURCE_LIMITED_OR_UNKNOWN",
                "due_lag_ms": None,
                "job_state": item.get("state"),
                "reason": item.get("reason"),
                "input_readiness": "NOT_RECHECKED",
                "status_basis": "LAST_FAILED_ATTEMPT_NOT_CURRENT_INPUTS",
                "pending_queue": pending,
                "scope": "FIRST_PENDING_AND_ONE_FAILED_SAMPLE_NOT_ALL_JOBS",
            }
        else:
            result[name] = pending
    return result


def completed_batch(
    store: Store,
    name: str,
    now_ms: int,
    lags: list[int],
    elapsed: float,
    *,
    read_seconds: float | None = None,
    timing: BatchTiming | None = None,
) -> None:
    key = "analytics_progress:" + name
    previous = store.get_runtime(key, {})
    value = {
        **previous,
        "schema": "cryptowatcher.analytics-progress.v1",
        "at_ms": now_ms,
        "completed_total": previous.get("completed_total", 0) + len(lags),
        "batch_completed": len(lags),
        "elapsed_seconds": elapsed,
        "work_seconds_total": previous.get("work_seconds_total", 0.0) + elapsed,
        "read_seconds_total": previous.get("read_seconds_total", 0.0) + (read_seconds or 0.0),
        "apply_seconds_total": previous.get("apply_seconds_total", 0.0)
        + max(0.0, elapsed - (read_seconds or 0.0)),
        "read_calculate_seconds": read_seconds,
        "apply_seconds": max(0, elapsed - read_seconds) if read_seconds is not None else None,
        "last_slo_violation_ms": now_ms
        if any(v > SLO_MS for v in lags)
        else previous.get("last_slo_violation_ms", 0),
        "max_completed_due_lag_ms": max([previous.get("max_completed_due_lag_ms", 0), *lags]),
    }
    if timing is not None and timing.read_cpu_seconds is not None:
        measured = {
            "read_cpu_seconds": timing.read_cpu_seconds,
            "apply_active_seconds": timing.apply_active_seconds,
            "apply_cpu_seconds": timing.apply_cpu_seconds,
            "apply_calls": timing.apply_calls,
            "interleaved_seconds": max(0.0, elapsed - (read_seconds or 0.0) - timing.apply_active_seconds),
        }
        value.update(
            timing_basis="prepare callback thread CPU excludes connection setup; apply active wall/CPU includes writer unit CAS/commit and excludes yields; apply_calls counts actual result applications; interleaved wall includes preliminary checks/other tasks",
            timed_batches_total=previous.get("timed_batches_total", 0) + 1,
            **measured,
            **{key + "_total": previous.get(key + "_total", 0) + count for key, count in measured.items()},
        )
    else:
        # Legacy/recovery callers do not measure CPU. Never repeat the previous
        # batch's CPU sample under a new timestamp or pretend it was zero work.
        value.update(
            **dict.fromkeys(
                (
                    "read_cpu_seconds",
                    "apply_active_seconds",
                    "apply_cpu_seconds",
                    "apply_calls",
                    "interleaved_seconds",
                )
            ),
            timing_basis="UNMEASURED; legacy wall durations include yields",
        )
    if store.db.in_transaction:
        store.db.execute(RUNTIME_UPSERT, (key, encode(value)))
    else:
        store.put_runtime(key, value)
