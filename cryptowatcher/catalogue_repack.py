"""Explicit repack integration; finite initial work and independent live inbox."""

from __future__ import annotations

import time
from typing import Any

from . import catalogue_lifecycle, catalogue_queue, catalogue_transfer, packet_store, record_catalog
from .storage import StateError, Store

FLAGS = ("record_catalogue_runtime", "catalogue_queue_runtime")
IMMUTABLE_PHASES = (
    "source_transfer_complete",
    "numeric_transfer_complete",
    "records_complete",
    "decisions_complete",
)
PREREQUISITES = (
    *IMMUTABLE_PHASES,
    "expiry_legacy_empty",
    "quality_complete",
    "quality_legacy_empty",
    "namespace_meter_complete",
    "reference_legacy_empty",
    "quality_meter_complete",
    "key_registry_complete",
    "cohort_meter_complete",
    "numeric_search_complete",
)


def validate(store: Store, state: dict[str, Any], version: int) -> None:
    """Reject unknown durable state before installing any extension."""
    catalogue_transfer.validate(state.get("catalogue_transfer", {}))
    catalogue_queue.validate(state.get("catalogue_queue", {}))
    for name in (
        "catalogue_complete",
        "initial_reclaim_complete",
        "quality_frame_initial_reclaim_complete",
        "context_tile_initial_reclaim_complete",
        "parent_affinity_initial_reclaim_complete",
        "numeric_frame_initial_reclaim_complete",
    ):
        if name in state and type(state[name]) is not bool:
            raise StateError("Invalid catalogue repack completion")
    upper = state.get("catalogue_queue_upper")
    if upper is not None and (type(upper) is not int or not 0 <= upper < 2**63):
        raise StateError("Invalid initial queue boundary")
    if state.get("catalogue_complete") and (
        upper is None
        or state.get("catalogue_transfer", {}).get("phase") != "complete"
        or not all(state.get(key) is True for key in IMMUTABLE_PHASES)
    ):
        raise StateError("Missing completed catalogue boundary")
    markers = dict(
        store.db.execute(
            "SELECT key,value FROM market_meta WHERE key IN (?,?,?,?)",
            (*FLAGS, "record_catalog", "catalogue_queue"),
        )
    )
    if (
        any(markers.get(flag, "1") != "1" for flag in FLAGS)
        or markers.get("record_catalog", "3") != "3"
        or markers.get("catalogue_queue", "1") != "1"
    ):
        raise StateError("Unknown catalogue repack layout; data preserved")
    if state.get("version") in (13, 14, 15, 16, 17, 18, 19, 20, 21, version):
        if markers.get(FLAGS[0]) != "1" or not catalogue_lifecycle.active(store.db):
            raise StateError("Missing catalogue runtime checkpoint")
        catalogue_queue.header(store.db)
        if "catalogue_transfer" in state and markers.get(FLAGS[1]) != "1":
            raise StateError("Missing catalogue queue writer checkpoint")
        if state.get("catalogue_complete"):
            assert upper is not None
            if not catalogue_queue.through(store.db, upper):
                raise StateError("Unfinished initial catalogue queue boundary")


def install(store: Store) -> None:
    record_catalog.install(store.db, store.cached_text, store.cached_bytes)
    catalogue_queue.install(store.db)


def activate(store: Store) -> None:
    store.db.execute(
        "INSERT INTO market_meta VALUES('record_catalogue_runtime','1') ON CONFLICT(key) DO NOTHING"
    )
    if not catalogue_lifecycle.active(store.db):
        raise StateError("Missing complete catalogue runtime")


def initial(store: Store, state: dict[str, Any], batch: int, deadline: float) -> None:
    if state.get("catalogue_complete") or not all(state.get(key) for key in PREREQUISITES):
        return
    catalogue_queue.activate(store.db)
    if catalogue_transfer.advance(
        store, state.setdefault("catalogue_transfer", {}), min(batch, 256), deadline
    ):
        state.setdefault("catalogue_queue_upper", catalogue_queue.ceiling(store.db))


def maintenance(store: Store, state: dict[str, Any], now_ms: int, batch: int, deadline: float) -> None:
    if "catalogue_transfer" not in state:
        return
    progress = state.setdefault("catalogue_queue", {})
    before = progress.get("queue_rows_examined", 0)
    catalogue_queue.advance(store, progress, min(batch, 256), deadline)
    if time.monotonic() < deadline:
        packet_store.collect(store.db, min(batch, 32))
    if "catalogue_queue_upper" in state and catalogue_queue.through(store.db, state["catalogue_queue_upper"]):
        state["catalogue_complete"] = True
    value = status(store, now_ms)
    if progress.get("queue_rows_examined", 0) > before:
        value["last_progress_ms"] = now_ms
    else:
        previous = state.get("catalogue_maintenance", {}).get("last_progress_ms")
        if previous is not None:
            value["last_progress_ms"] = previous
    state["catalogue_maintenance"] = value


def status(store: Store, now_ms: int) -> dict[str, Any]:
    from . import packet_bundles

    pending = catalogue_queue.header(store.db)
    bundles = packet_bundles.header(store.db) if packet_bundles.installed(store.db) else None
    if bundles is not None:
        stamp = bundles["oldest_ms"]
        bundles["oldest_age_ms"] = max(0, now_ms - stamp) if stamp is not None else None
    oldest = pending["oldest_ms"]
    gc = bool(
        store.db.execute("SELECT 1 FROM market_packets WHERE refs=0 LIMIT 1").fetchone()
        or store.db.execute("SELECT 1 FROM market_packet_arenas WHERE live_packets=0 LIMIT 1").fetchone()
    )
    free_pages = store.db.execute("PRAGMA freelist_count").fetchone()[0]
    return {
        **pending,
        "oldest_age_ms": max(0, now_ms - oldest) if oldest is not None else None,
        "free_pages": free_pages,
        "vacuum_pending": free_pages > 256,
        "gc_pending": gc,
        "bundles": bundles,
        "pending_work": bool(
            pending["count"]
            or gc
            or free_pages > 256
            or bundles
            and (bundles["count"] or bundles["unknown"] or bundles["sparse"])
        ),
        "at_ms": now_ms,
    }


def busy(state: dict[str, Any]) -> bool:
    return state.get("state") not in ("COMPLETE", "N/A_LEGACY") or bool(
        state.get("catalogue_maintenance", {}).get("pending_work")
        or state.get("quality_frame_maintenance", {}).get("pending_work")
        or state.get("context_tile_maintenance", {}).get("pending_work")
        or state.get("numeric_frame_maintenance", {}).get("pending_work")
    )
