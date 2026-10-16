"""Constant-size counters and bounded storage sampling; no periodic full DB walk."""

from __future__ import annotations

import time
from typing import Any

from .storage import Store

BUDGET = 20_000_000_000


def create(db: Any) -> None:
    # The new layout is empty here. Every later counter update is atomic with its
    # corresponding write, including rollbacks, retries and retention deletes.
    db.execute("ALTER TABLE market_counts ADD COLUMN inserted_bytes INTEGER NOT NULL DEFAULT 0")
    db.execute("ALTER TABLE market_counts ADD COLUMN deleted_bytes INTEGER NOT NULL DEFAULT 0")
    for table, column, category in (
        ("runtime", "payload_json", None),
        ("market_candles", "payload", "candles"),
        ("market_context", "timing", "context"),
        ("market_context_bodies", "payload", "context_bodies"),
        ("market_quality", "facts", "quality"),
        ("market_lifetimes", "key", "service_indexes"),
        ("market_body_links", "body", "reference_indexes"),
        ("market_holds", "key", "pending_holds"),
    ):

        def cat(alias: str) -> str:
            if category:
                return "'" + category + "'"
            return f"CASE WHEN {alias}.key GLOB 'historyblob:*' THEN 'shared' WHEN {alias}.key GLOB 'radar:v1:event*' OR {alias}.key GLOB 'radar:v1:outcome:*' OR {alias}.key GLOB 'radar:v1:outcome-head:*' OR {alias}.key GLOB 'radar:v1:episode:*' OR {alias}.key GLOB 'radar:v1:episode-head:*' OR {alias}.key GLOB 'radar:v1:comparison:*' OR {alias}.key GLOB 'radar:v1:comparison-head:*' THEN 'details' ELSE 'services' END"

        new_size = f"length(CAST(NEW.{column} AS BLOB))"
        old_size = f"length(CAST(OLD.{column} AS BLOB))"
        for event, body in (
            (
                "INSERT",
                f"INSERT INTO market_counts VALUES ({cat('NEW')},1,{new_size},{new_size},0) ON CONFLICT(category) DO UPDATE SET records=records+1,logical_bytes=logical_bytes+excluded.logical_bytes,inserted_bytes=inserted_bytes+excluded.inserted_bytes;",
            ),
            (
                "DELETE",
                f"UPDATE market_counts SET records=records-1,logical_bytes=logical_bytes-{old_size},deleted_bytes=deleted_bytes+{old_size} WHERE category={cat('OLD')};",
            ),
            (
                "UPDATE",
                f"UPDATE market_counts SET logical_bytes=logical_bytes+{new_size}-{old_size},inserted_bytes=inserted_bytes+max(0,{new_size}-{old_size}),deleted_bytes=deleted_bytes+max(0,{old_size}-{new_size}) WHERE category={cat('NEW')};",
            ),
        ):
            db.execute(
                f"CREATE TRIGGER market_count_{table}_{event} AFTER {event} ON {table} BEGIN {body} END"
            )


def report(store: Store, now_ms: int | None = None) -> dict[str, Any]:
    if not store.bounded:
        return {"state": "LEGACY_UNMETERED", "budget_bytes": BUDGET}
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    page = store.db.execute("PRAGMA page_size").fetchone()[0]
    pages = store.db.execute("PRAGMA page_count").fetchone()[0]
    free = store.db.execute("PRAGMA freelist_count").fetchone()[0]
    files = {}
    for suffix, label in (
        ("", "database"),
        ("-wal", "wal"),
        ("-shm", "shm"),
        ("-journal", "rollback_journal"),
    ):
        from pathlib import Path

        p = Path(str(store.path) + suffix)
        files[label] = p.stat().st_size if p.exists() else 0
    counts = {
        r[0]: {"records": r[1], "logical_bytes": r[2], "inserted_bytes": r[3], "deleted_bytes": r[4]}
        for r in store.db.execute("SELECT * FROM market_counts")
    }
    samples = store.get_runtime("storage_samples", [])
    current = [s for s in samples if s.get("format_version") == 3]
    base = current[0] if current else samples[0] if samples else None
    frequency_base = samples[0] if samples else None
    elapsed = (now_ms - base["at_ms"]) / 1000 if base else 0
    frequency_elapsed = (now_ms - frequency_base["at_ms"]) / 1000 if frequency_base else 0
    growth = (sum(files.values()) - base["working_bytes"]) / elapsed if elapsed > 0 and base else None
    settings = store.snapshot().settings
    event_total = store.get_runtime("radar:v1:event-counter", 0)
    event_rate = (
        max(0, event_total - frequency_base.get("event_total", event_total)) / frequency_elapsed
        if frequency_base and frequency_elapsed > 0
        else None
    )
    repack = store.get_runtime("storage_repack", {})
    saved = repack.get("saved_categories", {})
    forecast = []
    occupancy = {"services", "service_indexes", "pending_holds", "context", "context_bodies"}
    for category, value in counts.items():
        previous = (base or {}).get("categories", {}).get(category, {"inserted_bytes": 0, "deleted_bytes": 0})
        inserted = max(0, value["inserted_bytes"] - previous.get("inserted_bytes", 0))
        deleted = max(0, value["deleted_bytes"] - previous.get("deleted_bytes", 0))
        repack_savings = saved.get(category, 0) - (base or {}).get("repack_saved", {}).get(category, 0)
        rate = inserted / elapsed if elapsed > 0 else None
        net = (inserted - deleted + repack_savings) / elapsed if elapsed > 0 else None
        days = (
            settings.candle_retention_days
            if category in ("candles", "context", "context_bodies")
            else 366
            if category == "quality"
            else settings.detail_retention_days
        )
        projected = None
        if rate is not None and net is not None:
            projected = (
                value["logical_bytes"] + max(0, net) * 86400 * days
                if category in occupancy
                else rate * 86400 * days
            )
        forecast.append(
            {
                "category": category,
                "gross_bytes_per_day": rate * 86400 if rate is not None else None,
                "net_bytes_per_day_excluding_repack": net * 86400 if net is not None else None,
                "retention_days_for_estimate": days,
                "projected_logical_bytes": projected,
                "basis": "CURRENT_OCCUPANCY_PLUS_POSITIVE_NET_GROWTH"
                if category in occupancy
                else "RETAINED_INSERT_FLOW",
            }
        )
    # No annual zero from a not-yet-expired history, and no assumption that the
    # runtime's wide text indexes are the physical layout of numeric quality.
    from .storage_model import quality_scenarios

    scenarios = quality_scenarios()
    quality_model_per_event = scenarios[0]["encoded_bytes_per_fact"] * len(settings.outcome_horizons)
    quality_projection = (
        event_rate * 86400 * 366 * quality_model_per_event if event_rate is not None else None
    )
    quality = next((r for r in forecast if r["category"] == "quality"), None)
    if quality_projection is not None:
        if quality is None:
            quality = {"category": "quality", "gross_bytes_per_day": None, "retention_days_for_estimate": 366}
            forecast.append(quality)
        quality["projected_logical_bytes"] = max(
            quality.get("projected_logical_bytes") or 0, quality_projection
        )
        quality["model_applied"] = True
        quality["basis"] = "ISOLATED_SPARSE_NO_COMPARATOR_BASELINE; SEE_DENSITY_AND_COMPARATOR_SCENARIOS"
    logical = sum(v["logical_bytes"] for v in counts.values())
    # Current measured page utilization calibrates an explicit estimate; it is
    # neither a guaranteed steady-state size nor a replacement for measured files.
    factor = max(1, (pages - free) * page / max(1, logical))
    projected_without_quality = (
        sum(v["projected_logical_bytes"] or 0 for v in forecast if v["category"] != "quality") * factor
        + files["wal"]
        + files["shm"]
    )
    quality_physical = (
        event_rate * 86400 * 366 * scenarios[0]["physical_bytes_per_fact"] * len(settings.outcome_horizons)
        if event_rate is not None
        else 0
    )
    if quality is not None and quality.get("projected_logical_bytes") is not None:
        quality_physical = max(quality_physical, quality["projected_logical_bytes"] or 0)
    projected = (
        max(sum(files.values()), projected_without_quality + quality_physical) if elapsed > 0 else None
    )
    alternatives = (
        [
            {
                **scenario,
                "projected_working_bytes": projected_without_quality
                + event_rate
                * 86400
                * 366
                * scenario["physical_bytes_per_fact"]
                * len(settings.outcome_horizons),
            }
            for scenario in scenarios
        ]
        if elapsed > 0 and event_rate is not None
        else []
    )
    actual = sum(files.values())
    return {
        "schema": "cryptowatcher.storage-report.v1",
        "state": "BUDGET_EXCEEDED"
        if actual > BUDGET
        else "FORECAST_EXCEEDS_BUDGET"
        if projected is not None
        and (projected > BUDGET or any(v["projected_working_bytes"] > BUDGET for v in alternatives))
        else "WARMUP"
        if projected is None
        else "OK",
        "at_ms": now_ms,
        "history_generation": store.get_runtime("history_generation", {}),
        "budget_bytes": BUDGET,
        "working_bytes": actual,
        "files": files,
        "allocated_database_bytes": pages * page,
        "freelist_bytes": free * page,
        "categories": counts,
        "measurement_confidence": "LOW_SHORT_SAMPLE"
        if elapsed < 3600
        else "OBSERVED_WINDOW_WITH_MODEL_LIMITATIONS",
        "category_bytes_kind": "LOGICAL_RECORD_VALUES_OR_PUBLIC_INDEX_KEYS; excludes physical table/index overhead",
        "indexes": store.get_runtime(
            "storage_index_measurement", {"state": "PENDING", "reason": "No bounded exact measurement yet"}
        ),
        "net_bytes_per_second": growth,
        "forecast": forecast,
        "projected_working_bytes": projected,
        "page_overhead_factor": factor,
        "sample_seconds": elapsed,
        "observed_events_total": event_total,
        "observed_events_per_day_extrapolated": event_rate * 86400 if event_rate is not None else None,
        "quality_forecast_model": {
            "kind": "ISOLATED_CURRENT_CODEC_AND_SQLITE_LAYOUT_EXTRAPOLATION",
            "encoded_bytes_per_event": quality_model_per_event,
            "scenarios": list(scenarios),
            "point_scenario": "SPARSE_NO_COMPARATOR_BASELINE",
            "not_a_bound": True,
            "actual_production_measurement": False,
        },
        "forecast_scenarios": alternatives,
        "repack": repack,
        "forecast_kind": "RETAINED_FLOW_AND_NET_QUEUE_OCCUPANCY_PLUS_EXPLICIT_QUALITY_SCENARIOS",
        "limitations": [
            "Bootstrap/bursts/short samples distort forecast",
            "Quality density/comparator scenarios are synthetic measurements; neither a proven population mix nor upper bounds",
            "Aggregate service occupancy extrapolates positive net growth conservatively; terminal1d and stream-scoped state are not multiplied by gross turnover",
            "Additional retained methodology/comparator versions can increase cost beyond the single-method fixture",
            "Retained used inputs may exceed raw TTL; forecast is a baseline, not an upper bound",
            "20GB target includes indexes and working journals; no silent scope/retention reduction",
        ],
        "options": [
            "Review largest category and repeated values",
            "Measure representative event/outcome/summary mix",
            "Discuss retention/schema alternatives explicitly if forecast persists",
        ],
        "backups_and_experiments": {
            "included_in_working_budget": False,
            "state": "SEPARATE_OPERATOR_INVENTORY",
            "auto_deleted": False,
        },
    }


def sample(store: Store, now_ms: int) -> None:
    if not store.bounded:
        return
    last = store.get_runtime("storage_sample_at", 0)
    if 0 <= now_ms - last < 60000:
        return
    # One initial small-file measurement; subsequent control never scans DB pages.
    if store.get_runtime("storage_index_measurement") is None:
        if store.path.stat().st_size <= 8 * 1024 * 1024:
            store.put_runtime("storage_index_measurement", measure_indexes(store))
        else:
            store.put_runtime(
                "storage_index_measurement",
                {"state": "PENDING", "reason": "Explicit bounded measurement required above8MiB"},
            )
    value = report(store, now_ms)
    samples = store.get_runtime("storage_samples", [])
    samples.append(
        {
            "at_ms": now_ms,
            "working_bytes": value["working_bytes"],
            "format_version": 3,
            "repack_saved": store.get_runtime("storage_repack", {}).get("saved_categories", {}),
            "categories": value["categories"],
            "event_total": value["observed_events_total"],
        }
    )
    # 24-hour minute samples are bounded. No full database/index scan here.
    if len(samples) > 2 and samples[-1]["at_ms"] // 3600000 == samples[-2]["at_ms"] // 3600000:
        samples.pop(-2)
    samples = samples[-26:]
    with store.db:
        store.put_runtime("storage_samples", samples)
        store.put_runtime("storage_sample_at", now_ms)
        store.put_runtime("storage_report", value)


def measure_indexes(store: Store, seconds: float = 0.25) -> dict[str, Any]:
    import sqlite3

    deadline = time.monotonic() + seconds
    store.db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    try:
        rows = store.db.execute("SELECT name,SUM(pgsize) FROM dbstat GROUP BY name").fetchall()
        schema = {r[0]: r[1] for r in store.db.execute("SELECT name,type FROM sqlite_master")}
        return {
            "state": "PASS",
            "at_ms": int(time.time() * 1000),
            "kind": "MEASURED_BOUNDED_DBSTAT_SNAPSHOT",
            "tables": {r[0]: r[1] for r in rows if schema.get(r[0]) != "index"},
            "indexes": {r[0]: r[1] for r in rows if schema.get(r[0]) == "index"},
            "total_index_bytes": sum(r[1] for r in rows if schema.get(r[0]) == "index"),
            "limitations": [
                "Snapshot sizes; do not imply later unchanged indexes",
                "WITHOUT ROWID primary keys are integrated in table pages",
            ],
        }
    except sqlite3.OperationalError as exc:
        if getattr(exc, "sqlite_errorcode", None) != sqlite3.SQLITE_INTERRUPT and "dbstat" not in str(exc):
            raise
        return {"state": "UNAVAILABLE", "reason": "BOUNDED_INDEX_MEASUREMENT_BUDGET_OR_DBSTAT_UNAVAILABLE"}
    finally:
        store.db.set_progress_handler(None, 0)
