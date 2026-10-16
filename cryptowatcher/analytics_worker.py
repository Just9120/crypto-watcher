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
    prepared = []
    for _, binding in history.items(prefix, batch):
        if time.monotonic() >= deadline:
            break
        job = history.get(job_prefix + binding[binding_id])
        if job is None:
            continue  # Bounded retention retires the dangling queue index.
        if job["next_attempt_ms"] > now_ms:
            break
        event = history.get(f"event:{job['event_id']}:{job['event_revision']:010d}")
        prepared.append((job, event, binding, calculate(store, job, event)))
    return prepared
