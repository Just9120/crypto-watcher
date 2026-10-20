"""Bounded read-only diagnostics; expensive queue counts cannot gate liveness."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from typing import Any

from .agent_delivery import AgentDelivery
from .events import History
from .queue_health import snapshot as queue_snapshot
from .storage import Store


def snapshot(store: Store, seconds: float = 3) -> dict[str, Any]:
    history = History(store)
    settings = store.snapshot()
    deadline = time.monotonic() + seconds

    def bounded(read: Callable[[], Any]) -> Any:
        query_deadline = min(deadline, time.monotonic() + 0.5)
        store.db.set_progress_handler(lambda: int(time.monotonic() > query_deadline), 1000)
        try:
            return read()
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", None) != sqlite3.SQLITE_INTERRUPT:
                raise
            return {"state": "PENDING", "reason": "DIAGNOSTIC_QUERY_BUDGET"}
        finally:
            store.db.set_progress_handler(None, 0)

    ingestion = store.get_runtime("ingestion", {"state": "PENDING"})
    age = time.time() - ingestion.get("at", 0)
    ingestion.update(
        diagnostic_age_seconds=round(age, 1), diagnostic_state="FRESH" if 0 <= age <= 30 else "STALE"
    )
    from .storage_meter import report

    return {
        "at_ms": int(time.time() * 1000),
        "storage": bounded(lambda: report(store)),
        "history_generation": store.get_runtime("history_generation", {"state": "LEGACY"}),
        "retention_worker": store.get_runtime("retention_worker", {"state": "PENDING"}),
        "database": {
            "schema": store.db.execute("PRAGMA user_version").fetchone()[0],
            "snapshot_bytes": store.db.execute("PRAGMA page_count").fetchone()[0]
            * store.db.execute("PRAGMA page_size").fetchone()[0],
        },
        "configuration_revision": settings.revision,
        "heartbeat": store.get_runtime("heartbeat", {}),
        "last_poll": store.get_runtime("last_poll_status", {"state": "PENDING"}),
        "telegram_delivery": store.get_runtime("telegram_delivery", {"state": "PENDING"}),
        "ingestion": ingestion,
        "radar": bounded(history.diagnostics),
        "agent_delivery": bounded(AgentDelivery(store, None).diagnostics),
        "outcomes": {
            "pending": bounded(lambda: history.count("outcome-pending:")),
            "comparison_pending": bounded(lambda: history.count("comparison-pending:")),
            "admission": history.get("outcome-admission", {}),
        },
        "analytics": store.get_runtime("analytics", {"state": "PENDING"}),
        "analytics_queues": bounded(lambda: queue_snapshot(store, int(time.time() * 1000))),
        "analytics_recovery": {"pending": bounded(lambda: history.count("analytics-gap-pending:"))},
        "retired_pending": history.get("orphaned-pending-summary", {}),
        "enrichment": {
            "enabled": settings.settings.enrichment_enabled,
            "ticker_cadence_ms": 60_000,
            "oi_cadence_ms": 300_000,
            "worker": store.get_runtime("enrichment", {"state": "PENDING"}),
            "mapping_count": bounded(lambda: history.count("mapping-head:")),
            "optional": True,
        },
        "digest": {
            "preferences": history.get("digest-preferences", {"enabled": False}),
            "pending": bounded(lambda: history.count("digest-pending:")),
            "target": history.get("target:telegram", {"state": "READY"}),
        },
    }
