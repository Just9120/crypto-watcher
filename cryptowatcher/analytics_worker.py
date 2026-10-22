"""Read-only calculations in a worker; the application remains the only writer."""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from .events import History, event_identity
from .storage import Store


@dataclass
class BatchTiming:
    """Observe work on its actual thread, without charging asyncio turns to it."""

    read_cpu_seconds: float | None = None
    apply_active_seconds: float = 0.0
    apply_cpu_seconds: float = 0.0
    apply_calls: int = 0

    def read(self, calculate: Callable[[], Any]) -> Any:
        began = time.thread_time()
        try:
            return calculate()
        finally:
            self.read_cpu_seconds = max(0.0, time.thread_time() - began)

    @contextmanager
    def applying(self) -> Iterator[None]:
        # Use the same clock as the enclosing batch. Mixing perf_counter with
        # a coarser monotonic clock can report active work above total elapsed.
        began, cpu = time.monotonic(), time.thread_time()
        try:
            yield
        finally:
            self.apply_active_seconds += max(0.0, time.monotonic() - began)
            self.apply_cpu_seconds += max(0.0, time.thread_time() - cpu)
            self.apply_calls += 1


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
    selected: set[str] = set()

    def admit(binding: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
        identifier = binding[binding_id]
        if identifier in selected or len(due) >= batch:
            return None
        job = history.get(job_prefix + identifier)
        if job is None or job["next_attempt_ms"] > now_ms:
            return None
        event = history.metadata(f"event:{job['event_id']}:{job['event_revision']:010d}")
        if event is None:
            raise ValueError("Missing frozen analytics event")
        due.append((job, event, binding))
        selected.add(identifier)
        return job, event

    for _, binding in history.items(prefix, batch):
        if len(due) >= batch or (due and time.monotonic() >= deadline):
            break
        job = history.get(job_prefix + binding[binding_id])
        if job is None:
            continue  # Bounded retention retires the dangling queue index.
        if job["next_attempt_ms"] > now_ms:
            break
        # Calculations load their own exact source ranges. Expanding the frozen
        # detector window here duplicates hundreds of verified candle reads per
        # horizon; metadata still validates the stored record/settings identity.
        admitted = admit(binding)
        if admitted is not None and prefix == "comparison-pending:":
            from .detectors import FAMILIES, VERSIONS

            _, event = admitted
            if not {"market_observation_ms", "settings_revision", "detector_version"}.issubset(event):
                continue  # Older minimal records retain the original admission path.
            stream = f"{event['market_type']}/{event['symbol']}/{event['timeframe']}"
            start = event["market_observation_ms"] - int(event["timeframe"][:-1]) * 60000
            # Probe at most ten canonical siblings. This finds the same closed
            # candle's families beyond a UUID-ordered batch, without a new queue
            # or a large population cache. Their own cutoff/settings stay frozen.
            for family in FAMILIES:
                for version in {VERSIONS[family], event["detector_version"]}:
                    if len(due) >= batch or time.monotonic() >= deadline:
                        break
                    peer = event_identity(stream, family, version, event["settings_revision"], start)
                    if peer in selected:
                        continue
                    candidate = history.get(job_prefix + peer)
                    if candidate is None or candidate.get("pending_key") is None:
                        continue
                    peer_binding = history.get(candidate["pending_key"])
                    if isinstance(peer_binding, dict) and peer_binding.get(binding_id) == peer:
                        admit(peer_binding)
    if prefix == "comparison-pending:" and due:
        # Calculations are independent and keep their immutable cutoff. Group
        # only this bounded due batch to reuse one population, not a large cache.
        # The earliest due job must run first even when one calculation consumes
        # the remaining budget. Otherwise lexical grouping can starve its stream.
        first = due.pop(0)
        scope = tuple(first[1][field] for field in ("market_type", "symbol", "timeframe"))
        due.sort(
            key=lambda row: (
                tuple(row[1][field] for field in ("market_type", "symbol", "timeframe")) != scope,
                row[1]["market_type"],
                row[1]["symbol"],
                row[1]["timeframe"],
                row[1]["reference_time_ms"],
            )
        )
        due.insert(0, first)
    for job, event, binding in due:
        if prepared and time.monotonic() >= deadline:
            break
        prepared.append((job, event, binding, calculate(store, job, event)))
    return prepared
