"""Read-only calculations in a worker; the application remains the only writer."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from .events import History
from .storage import Store


def read_snapshot(path: Any, owner: int, read: Callable[[Store], Any]) -> Any:
    store = Store(path, owner, readonly=True)
    try:
        store.db.execute("PRAGMA query_only=ON")
        store.db.execute("BEGIN")
        return read(store)
    finally:
        store.close()


def prepare(
    store: Store,
    prefix: str,
    job_prefix: str,
    binding_id: str,
    now_ms: int,
    batch: int,
    seconds: float,
    calculate: Callable[[Store, dict[str, Any], dict[str, Any]], dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]]:
    history = History(store)
    deadline = time.monotonic() + seconds
    prepared: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    due: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for _, binding in history.items(prefix, batch):
        if due and time.monotonic() >= deadline:
            break
        job = history.get(job_prefix + binding[binding_id])
        if job is None:
            continue  # Bounded retention retires the dangling queue index.
        if job["next_attempt_ms"] > now_ms:
            break
        # Calculations load their own exact source ranges. Expanding the frozen
        # detector window here duplicates hundreds of verified candle reads per
        # horizon; metadata still validates the stored record/settings identity.
        event = history.metadata(f"event:{job['event_id']}:{job['event_revision']:010d}")
        due.append((job, event, binding))
    if prefix == "comparison-pending:":
        # Calculations are independent and keep their immutable cutoff. Group
        # only this bounded due batch to reuse one population, not a large cache.
        due.sort(
            key=lambda row: (
                row[1]["market_type"],
                row[1]["symbol"],
                row[1]["timeframe"],
                row[1]["reference_time_ms"],
            )
        )
    for job, event, binding in due:
        if prepared and time.monotonic() >= deadline:
            break
        prepared.append((job, event, binding, calculate(store, job, event)))
    return prepared
