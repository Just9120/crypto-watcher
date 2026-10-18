"""Bounded queue age reads and durable completion telemetry; future work is separate."""

from __future__ import annotations

from typing import Any

from .events import PREFIX, History
from .storage import Store

SLO_MS = 60_000


def snapshot(store: Store, now_ms: int) -> dict[str, Any]:
    result = {}
    history = History(store)
    for name, jobs, field in (
        ("outcome", "outcome-job:", "outcome_id"),
        ("comparison", "comparison-job:", "event_id"),
        ("analytics-gap", "analytics-gap:", "id"),
    ):
        prefix = PREFIX + name + "-pending:"
        row = store.db.execute(
            "SELECT key,payload_json FROM runtime WHERE key>=? AND key<? ORDER BY key LIMIT 1",
            (prefix, prefix + "~"),
        ).fetchone()
        if row is None:
            result[name] = {"state": "EMPTY", "due_lag_ms": 0}
            continue
        from .storage import json_payload

        binding = json_payload(row[1])
        job = history.get(jobs + str(binding.get(field, "")))
        if not isinstance(job, dict) or type(job.get("next_attempt_ms")) is not int:
            result[name] = {"state": "ORPHAN_OR_UNKNOWN", "due_lag_ms": None}
            continue
        lag = max(0, now_ms - job["next_attempt_ms"])
        state = "FUTURE" if job["next_attempt_ms"] > now_ms else "CURRENT" if lag <= SLO_MS else "OVERDUE"
        if name == "analytics-gap" and not (
            job.get("state") == "PENDING"
            and job.get("attempts") == 0
            and type(job.get("end")) is int
            and 0 <= now_ms - job["end"] <= SLO_MS
        ):
            state = "REFERENCED_RECOVERY"
        result[name] = {
            "state": state,
            "due_lag_ms": lag,
            "next_attempt_ms": job["next_attempt_ms"],
            "job_state": job.get("state"),
            "reason": job.get("reason"),
        }
    return result


def completed_batch(store: Store, name: str, now_ms: int, lags: list[int], elapsed: float) -> None:
    key = "analytics_progress:" + name
    previous = store.get_runtime(key, {})
    value = {
        "schema": "cryptowatcher.analytics-progress.v1",
        "at_ms": now_ms,
        "completed_total": previous.get("completed_total", 0) + len(lags),
        "batch_completed": len(lags),
        "elapsed_seconds": elapsed,
        "last_slo_violation_ms": now_ms
        if any(v > SLO_MS for v in lags)
        else previous.get("last_slo_violation_ms", 0),
        "max_completed_due_lag_ms": max([previous.get("max_completed_due_lag_ms", 0), *lags]),
    }
    store.put_runtime(key, value)
