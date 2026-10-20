"""Constant-size counters and bounded storage sampling; no periodic full DB walk."""

from __future__ import annotations

import time
from typing import Any

from .storage import Store

BUDGET = 25_000_000_000
QUALITY_CATEGORIES = frozenset(
    {"quality", "quality_numeric", "quality_components", "quality_frames", "quality_numeric_frames"}
)
SAMPLE_FORMAT = 9


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
    repack = store.get_runtime("storage_repack", {})
    completed_repack = repack.get("version") if repack.get("state") == "COMPLETE" else None
    current = [
        s
        for s in samples
        if s.get("format_version") == SAMPLE_FORMAT and s.get("completed_repack_version") == completed_repack
    ]
    base = current[0] if current else None
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
    saved = repack.get("saved_categories", {})
    forecast = []
    from .analytics import COMPARATOR_LOOKBACK_DAYS
    from .config import TIMEFRAMES
    from .sources import WINDOW_SIZE

    # A used comparator input can precede its event by28days plus the frozen
    # native detector window. Its existing lease then lasts detail_retention.
    # This estimates current admitted bounds; it does not change any expiry.
    native_days = (WINDOW_SIZE * max(int(tf[:-1]) for tf in TIMEFRAMES) + 1439) // 1440
    candle_dependency_days = max(
        settings.candle_retention_days,
        settings.detail_retention_days + COMPARATOR_LOOKBACK_DAYS + native_days,
    )
    context_dependency_days = max(
        settings.candle_retention_days,
        settings.detail_retention_days + COMPARATOR_LOOKBACK_DAYS,
    )
    occupancy = {
        "services",
        "service_indexes",
        "pending_holds",
        "context",
        "context_bodies",
        "context_integrity",
        "context_tiles",
        "context_tile_pending",
        "source_windows",
        "prospective_indexes",
        "source_blocks",
        "source_block_indexes",
        "catalogue_overlap",
        "record_pending",
        "bundle_pending",
        "quality_frame_pending",
    }
    if store.db.execute(
        "SELECT 1 FROM market_meta WHERE key='record_catalogue_runtime' AND value='1'"
    ).fetchone():
        # Hot details become transient staging. Their admissions are represented
        # again by retained catalogue packets/coordinates after transfer; annual
        # gross hot insertion flow would count those same records twice.
        occupancy.add("details")
    from . import quality_frames

    frame_layout = quality_frames.status(store.db, now_ms)
    if frame_layout["installed"]:
        # CQ5 becomes staging plus permanent13byte coordinates. Gross CQ5
        # admissions are the same facts subsequently owned by QF1 containers.
        occupancy.add("quality")
    for category, value in counts.items():
        previous = (base or {}).get("categories", {}).get(category, {"inserted_bytes": 0, "deleted_bytes": 0})
        inserted = max(0, value["inserted_bytes"] - previous.get("inserted_bytes", 0))
        deleted = max(0, value["deleted_bytes"] - previous.get("deleted_bytes", 0))
        repack_savings = saved.get(category, 0) - (base or {}).get("repack_saved", {}).get(category, 0)
        rate = inserted / elapsed if elapsed > 0 else None
        net = (inserted - deleted + repack_savings) / elapsed if elapsed > 0 else None
        days = (
            candle_dependency_days
            if category in ("candles", "source_blocks", "source_block_indexes")
            else context_dependency_days
            if category in ("context", "context_bodies", "context_integrity", "context_tiles")
            else 366
            if category in QUALITY_CATEGORIES
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
    quality_flow = sum(
        item["projected_logical_bytes"] or 0 for item in forecast if item["category"] in QUALITY_CATEGORIES
    )
    quality_model_per_event = scenarios[0]["encoded_bytes_per_fact"] * len(settings.outcome_horizons)
    quality_projection = (
        event_rate * 86400 * 366 * quality_model_per_event if event_rate is not None else None
    )
    quality = next((r for r in forecast if r["category"] == "quality"), None)
    if quality_projection is not None:
        if quality is None:
            quality = {"category": "quality", "gross_bytes_per_day": None, "retention_days_for_estimate": 366}
            forecast.append(quality)
        quality["flow_projected_logical_bytes"] = quality.get("projected_logical_bytes")
        quality["flow_basis"] = quality.get("basis")
        quality["projected_logical_bytes"] = max(quality_flow, quality_projection)
        quality["model_applied"] = True
        quality["basis"] = (
            "WHOLE_ARCHIVE_MODEL_REPLACES_ALL_QUALITY_CATEGORIES; SPARSE_NO_COMPARATOR_SCENARIO"
        )
        for item in forecast:
            if item["category"] in QUALITY_CATEGORIES - {"quality"}:
                item["allocation_excluded"] = "ALREADY_INCLUDED_IN_WHOLE_ARCHIVE_MODEL"
    logical = sum(v["logical_bytes"] for v in counts.values())
    # Current measured page utilization calibrates an explicit estimate; it is
    # neither a guaranteed steady-state size nor a replacement for measured files.
    factor = max(1, (pages - free) * page / max(1, logical))
    projected_without_quality = (
        sum(v["projected_logical_bytes"] or 0 for v in forecast if v["category"] not in QUALITY_CATEGORIES)
        * factor
        + files["wal"]
        + files["shm"]
    )
    quality_physical = (
        event_rate * 86400 * 366 * scenarios[0]["physical_bytes_per_fact"] * len(settings.outcome_horizons)
        if event_rate is not None
        else 0
    )
    quality_physical = max(quality_physical, quality_flow * factor)
    projected = (
        max(sum(files.values()), projected_without_quality + quality_physical) if elapsed > 0 else None
    )
    alternatives = (
        [
            {
                **scenario,
                "projected_working_bytes": projected_without_quality
                + max(
                    quality_flow * factor,
                    event_rate
                    * 86400
                    * 366
                    * scenario["physical_bytes_per_fact"]
                    * len(settings.outcome_horizons),
                ),
            }
            for scenario in scenarios
        ]
        if elapsed > 0 and event_rate is not None
        else []
    )
    actual = sum(files.values())
    source_layout: dict[str, Any] = {
        "state": "LEGACY_NORMALIZED",
        "legacy_versions": counts.get("candles", {}).get("records", 0),
    }
    from .candle_access import installed as source_tiles_installed

    source_tables: set[str] = set()
    if source_tiles_installed(store.db):
        from .candle_blocks import summary as source_summary

        source_layout.update(state="MIXED_SOURCE_TILES", **source_summary(store.db))
        source_layout["total_versions"] = source_layout["legacy_versions"] + source_layout["versions"]
        source_tables = {
            "market_candle_blocks",
            "market_candle_block_totals",
            "market_candle_fallbacks",
            "market_candle_gc",
        }
    from .numeric_blocks import installed as numeric_blocks_installed

    numeric_layout: dict[str, Any] = {"state": "LEGACY_METRIC_ROWS"}
    if numeric_blocks_installed(store.db):
        from .numeric_blocks import summary as numeric_summary

        numeric_layout.update(state="MIXED_NUMERIC_BLOCKS", **numeric_summary(store.db))
        from .numeric_refs import installed as numeric_refs_installed
        from .numeric_refs import writing as numeric_refs_writing

        numeric_layout["lease_format"] = (
            "NR2"
            if numeric_refs_writing(store.db)
            else "NR1_COMPAT"
            if numeric_refs_installed(store.db)
            else "NR1"
        )
        source_tables |= {
            "market_numeric_blocks",
            "market_numeric_block_refs",
            "market_numeric_block_lookup",
            "market_numeric_totals",
            "market_numeric_transfer",
            "market_numeric_gc",
        }
        from . import numeric_frames

        numeric_layout["frame_mode"] = numeric_frames.mode(store.db)
        if numeric_layout["frame_mode"] is not None:
            numeric_frames.header(store.db)
        if numeric_frames.writing(store.db):
            source_tables.add("market_numeric_frames")
    catalogue_layout: dict[str, Any] = {"state": "LEGACY_DOMAIN_ROWS"}
    catalogue = store.db.execute("SELECT value FROM market_meta WHERE key='record_catalog'").fetchone()
    if catalogue is not None:
        from . import catalogue_reader, record_catalog, record_masks

        version = record_catalog.layout(store.db)
        catalogue_layout = {
            "state": "MIXED_DOMAIN_CATALOGUE"
            if catalogue_reader.reading(store.db)
            else "INACTIVE_DOMAIN_CATALOGUE",
            "version": version,
        }
        source_tables |= {
            "market_packet_arenas",
            "market_packet_space",
            "market_packet_empty",
            "market_packets",
            "market_record_parents",
            "market_records",
            "market_record_members",
            "market_record_bodies",
            "market_catalogue_counts",
            "market_cold_cohort_scan",
            "market_cold_cohorts",
            "market_cold_cohort_revisions",
            "market_cold_cohort_strengths",
        }
        if version == 3:
            from .catalogue_overlap import header as overlap_header

            catalogue_layout["overlaps"] = overlap_header(store.db)
            catalogue_layout["counts"] = record_masks.tallies(store.db, "record_parents")
            record_masks.tallies(store.db, "record_owners")
            record_masks.tallies(store.db, "record_aliases")
            source_tables |= {
                "market_record_owners",
                "market_catalogue_index_counts",
                "market_catalogue_overlap",
            }
    if store.db.execute("SELECT 1 FROM market_meta WHERE key='catalogue_queue'").fetchone():
        from .catalogue_queue import header as queue_header

        pending = queue_header(store.db)
        oldest = pending["oldest_ms"]
        catalogue_layout["pending"] = {
            **pending,
            "oldest_age_ms": max(0, now_ms - oldest) if oldest is not None else None,
        }
        source_tables.add("market_record_pending")
    if store.db.execute("SELECT 1 FROM market_meta WHERE key='packet_bundles'").fetchone():
        from .packet_bundles import INDEXES as bundle_indexes
        from .packet_bundles import header as bundle_header

        bundles = bundle_header(store.db)
        oldest = bundles["oldest_ms"]
        catalogue_layout["bundles"] = {
            **bundles,
            "oldest_age_ms": max(0, now_ms - oldest) if oldest is not None else None,
        }
        source_tables.update(bundle_indexes)
    indexes = store.get_runtime(
        "storage_index_measurement", {"state": "PENDING", "reason": "No bounded exact measurement yet"}
    )
    if frame_layout["installed"]:
        source_tables |= {"market_quality_frames", "market_quality_frame_pending"}
    from .context_tile_maintenance import status as context_tile_status

    context_tile_layout = context_tile_status(store.db, now_ms)
    if context_tile_layout["installed"]:
        source_tables |= {"market_context_tiles", "market_context_tile_pending"}
    from .parent_affinity import header as parent_affinity_header

    affinity_layout: dict[str, Any] = {"installed": parent_affinity_header(store.db)}
    if affinity_layout["installed"]:
        source_tables.add("market_parent_affinity")
        affinity_layout.update(counts["parent_affinity"])
    if (
        indexes.get("state") == "PASS"
        and store.db.execute("SELECT 1 FROM sqlite_master WHERE name='market_expiry'").fetchone()
        and not (
            {
                "market_expiry",
                "market_quality_blocks",
                "market_source_windows",
                "market_numeric",
                "market_namespaces",
            }
            | source_tables
        ).issubset(indexes.get("tables", {}))
    ):
        indexes = {**indexes, "state": "STALE", "reason": "STORAGE_EXTENSION_AFTER_SNAPSHOT"}
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
        "catalogue_layout": catalogue_layout,
        "quality_frame_layout": frame_layout,
        "context_tile_layout": context_tile_layout,
        "parent_affinity_layout": affinity_layout,
        "candle_dependency_retention_estimate": {
            "configured_minimum_days": settings.candle_retention_days,
            "detail_days": settings.detail_retention_days,
            "comparator_prior_days": COMPARATOR_LOOKBACK_DAYS,
            "native_input_window": WINDOW_SIZE,
            "days_for_estimate": candle_dependency_days,
            "basis": "CURRENT_ADMITTED_INPUT_BOUND_AND_EXISTING_DETAIL_LEASES; NOT_A_POLICY_CHANGE",
            "limitations": [
                "Historical configurations outside current input bounds require observed lease qualification"
            ],
        },
        "context_dependency_retention_estimate": {
            "configured_minimum_days": settings.candle_retention_days,
            "detail_days": settings.detail_retention_days,
            "comparator_prior_days": COMPARATOR_LOOKBACK_DAYS,
            "days_for_estimate": context_dependency_days,
            "basis": "CURRENT_COMPARATOR_CUTOFF_AND_PARENT_DETAIL_LEASES; NOT_A_POLICY_CHANGE",
            "limitations": [
                "An older selector predecessor per stream/kind and supplied historical dependencies may exceed this span",
                "Actual frozen leases, population churn and native allocation still require qualification",
            ],
        },
        "working_bytes": actual,
        "files": files,
        "allocated_database_bytes": pages * page,
        "freelist_bytes": free * page,
        "categories": counts,
        "source_layout": source_layout,
        "numeric_layout": numeric_layout,
        "measurement_confidence": "LOW_SHORT_SAMPLE"
        if elapsed < 3600
        else "OBSERVED_WINDOW_WITH_MODEL_LIMITATIONS",
        "category_bytes_kind": "LOGICAL_RECORD_VALUES_OR_PUBLIC_INDEX_KEYS; excludes physical table/index overhead",
        "indexes": indexes,
        "net_bytes_per_second": growth,
        "forecast": forecast,
        "projected_working_bytes": projected,
        "page_overhead_factor": factor,
        "quality_physical_allocation_bytes": quality_physical,
        "quality_replaced_categories": sorted(QUALITY_CATEGORIES),
        "sample_seconds": elapsed,
        "observed_events_total": event_total,
        "observed_events_per_day_extrapolated": event_rate * 86400 if event_rate is not None else None,
        "quality_forecast_model": {
            "kind": "ISOLATED_CQ5_ALL_QUALITY_POOLS_AND_INDEXES_SCENARIOS",
            "encoded_bytes_per_event": quality_model_per_event,
            "scenarios": list(scenarios),
            "point_scenario": "SPARSE_NO_COMPARATOR_BASELINE",
            "not_a_bound": True,
            "actual_production_measurement": False,
            "shared_frame_mature_model_qualified": False,
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
            "25GB target includes indexes and working journals; no silent scope/retention reduction",
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
            "format_version": SAMPLE_FORMAT,
            "completed_repack_version": value["repack"].get("version")
            if value["repack"].get("state") == "COMPLETE"
            else None,
            "repack_saved": store.get_runtime("storage_repack", {}).get("saved_categories", {}),
            "categories": value["categories"],
            "event_total": value["observed_events_total"],
        }
    )
    # 24-hour minute samples are bounded. No full database/index scan here.
    if (
        len(samples) > 2
        and samples[-1]["at_ms"] // 3600000 == samples[-2]["at_ms"] // 3600000
        and any(
            s.get("format_version") == SAMPLE_FORMAT
            and s.get("completed_repack_version") == samples[-1]["completed_repack_version"]
            for s in samples[:-2]
        )
    ):
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
        rows = store.db.execute("SELECT name,pgsize FROM dbstat WHERE aggregate=TRUE").fetchall()
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
