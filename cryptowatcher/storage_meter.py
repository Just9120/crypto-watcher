"""Constant-size counters and bounded storage sampling; no periodic full DB walk."""

from __future__ import annotations

import time
from typing import Any

from .storage import Store

TARGET = 25_000_000_000
BUDGET = 35_000_000_000
QUALITY_CATEGORIES = frozenset(
    {
        "quality",
        "quality_numeric",
        "quality_components",
        "quality_frames",
        "quality_numeric_frames",
        "quality_episodes",
    }
)
SAMPLE_FORMAT = 13


def install_source_usage(db: Any) -> None:
    # Constant two-row ledger, no source scan or per-source index. Existing
    # databases start observation here; an explicit fresh transition starts at0.
    db.execute("""CREATE TABLE IF NOT EXISTS market_source_usage(
        kind TEXT PRIMARY KEY CHECK(kind IN ('candles','context')),
        arrived_bytes INTEGER NOT NULL CHECK(arrived_bytes>=0),
        first_used_bytes INTEGER NOT NULL CHECK(first_used_bytes>=0)) WITHOUT ROWID""")


def record_source(db: Any, kind: str, arrived: int, first_used: int) -> None:
    if arrived or first_used:
        db.execute(
            "INSERT INTO market_source_usage VALUES(?,?,?) ON CONFLICT(kind) DO UPDATE SET "
            "arrived_bytes=arrived_bytes+excluded.arrived_bytes,"
            "first_used_bytes=first_used_bytes+excluded.first_used_bytes",
            (kind, arrived, first_used),
        )


def create(db: Any) -> None:
    # The new layout is empty here. Every later counter update is atomic with its
    # corresponding write, including rollbacks, retries and retention deletes.
    install_source_usage(db)
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
        return {"state": "LEGACY_UNMETERED", "target_bytes": TARGET, "budget_bytes": BUDGET}
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
    settings = store.snapshot().settings
    from dataclasses import asdict
    from hashlib import sha256

    from .storage import encode

    scope_key = sha256(
        encode([store.get_runtime("history_generation", {}), asdict(settings)]).encode()
    ).hexdigest()
    samples = store.get_runtime("storage_samples", [])
    same_scope = [
        s for s in samples if s.get("format_version") == SAMPLE_FORMAT and s.get("scope_key") == scope_key
    ]
    repack = store.get_runtime("storage_repack", {})
    completed_repack = repack.get("version") if repack.get("state") == "COMPLETE" else None
    from .shared_record_work import status as shared_status

    shared_layout = shared_status(store.db, now_ms)
    completed_shared = shared_layout.get("version") if shared_layout.get("initial_reclaim_complete") else None
    current = [
        s
        for s in same_scope
        if s.get("format_version") == SAMPLE_FORMAT
        and s.get("completed_repack_version") == completed_repack
        and s.get("completed_shared_layout") == completed_shared
    ]
    base = current[0] if current else None
    frequency_base = same_scope[0] if same_scope else None
    elapsed = (now_ms - base["at_ms"]) / 1000 if base else 0
    frequency_elapsed = (now_ms - frequency_base["at_ms"]) / 1000 if frequency_base else 0
    growth = (sum(files.values()) - base["working_bytes"]) / elapsed if elapsed > 0 and base else None
    event_total = store.get_runtime("radar:v1:event-counter", 0)
    event_rate = (
        max(0, event_total - frequency_base.get("event_total", event_total)) / frequency_elapsed
        if frequency_base and frequency_elapsed > 0
        else None
    )
    episode_total = store.get_runtime("radar:v1:episode-counter", 0)
    episode_rate = (
        max(0, episode_total - frequency_base.get("episode_total", episode_total)) / frequency_elapsed
        if frequency_base and frequency_elapsed > 0
        else None
    )
    usage = (
        {
            row[0]: {"arrived_bytes": row[1], "first_used_bytes": row[2]}
            for row in store.db.execute("SELECT kind,arrived_bytes,first_used_bytes FROM market_source_usage")
        }
        if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='market_source_usage'").fetchone()
        else {}
    )
    used_shares = {}
    for kind in ("candles", "context"):
        before = (base or {}).get("source_usage", {}).get(kind, {})
        arrived = usage.get(kind, {}).get("arrived_bytes", 0) - before.get("arrived_bytes", 0)
        used = usage.get(kind, {}).get("first_used_bytes", 0) - before.get("first_used_bytes", 0)
        qualified = base is not None and arrived > 0 and 0 <= used <= arrived
        used_shares[kind] = {
            "arrived_canonical_bytes": arrived,
            "first_used_canonical_bytes": used,
            "share": used / arrived if qualified else 1.0,
            "basis": "OBSERVED_FIRST_LEASE_BYTES_OVER_UNIQUE_ARRIVALS"
            if qualified
            else "CONSERVATIVE_ALL_INPUTS_USED",
            "not_a_bound": True,
        }
    saved = repack.get("saved_categories", {})
    forecast = []
    from .config import TIMEFRAMES
    from .sources import WINDOW_SIZE

    # Only actual used inputs retain detail ownership. Current native lookback
    # fits72h; N20's former28-day prior population has no admission/lease here.
    native_days = (WINDOW_SIZE * max(int(tf[:-1]) for tf in TIMEFRAMES) + 1439) // 1440
    candle_dependency_days = max(settings.candle_retention_days, settings.detail_retention_days + native_days)
    context_dependency_days = max(settings.candle_retention_days, settings.detail_retention_days)
    effective_candle_days = settings.candle_retention_days + used_shares["candles"]["share"] * (
        candle_dependency_days - settings.candle_retention_days
    )
    effective_context_days = settings.candle_retention_days + used_shares["context"]["share"] * (
        context_dependency_days - settings.candle_retention_days
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
        "numeric_frame_pending",
        "shared_record_groups",
        "shared_record_members",
        "shared_record_queue",
        "shared_record_pending",
        "shared_record_reclaim",
    }
    if shared_layout.get("installed"):
        # Ordinary DB2 admissions become transient staging plus DS2 pointers;
        # shared groups own those same retained bodies. Gross DB2 insertion
        # flow would count the same immutable values a second time.
        occupancy.add("detail_packets")
    if store.db.execute(
        "SELECT 1 FROM market_meta WHERE key='record_catalogue_runtime' AND value='1'"
    ).fetchone():
        # Hot details become transient staging. Their admissions are represented
        # again by retained catalogue packets/coordinates after transfer; annual
        # gross hot insertion flow would count those same records twice.
        occupancy.add("details")
    from . import quality_frames

    frame_layout = quality_frames.status(store.db, now_ms)
    frame_layout["writer"] = "IF1_OR_QC1_MIN" if quality_frames.column_writing(store.db) else "IF1_COMPAT"
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
            effective_candle_days
            if category in ("candles", "source_blocks", "source_block_indexes")
            else effective_context_days
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
    # One current-production model. Annual results use created episodes;
    # distinct meaningful events conservatively bound archived memberships.
    # Prequalified exact-writer coefficients include every summary index and
    # full unshared provenance; runtime never builds a synthetic population.
    from .storage_model import (
        EPISODE_MEMBER_LOGICAL,
        EPISODE_MEMBER_PHYSICAL,
        EPISODE_MODEL_VERSION,
        EPISODE_RESULT_LOGICAL,
        EPISODE_RESULT_PHYSICAL,
    )

    quality_flow = sum(
        item["projected_logical_bytes"] or 0 for item in forecast if item["category"] in QUALITY_CATEGORIES
    )
    quality_projection = (
        (
            episode_rate * len(settings.outcome_horizons) * EPISODE_RESULT_LOGICAL
            + (event_rate or 0) * EPISODE_MEMBER_LOGICAL
        )
        * 86400
        * 366
        if episode_rate is not None
        else None
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
            "ONE_EPISODE_RESULT_SET_AND_MEANINGFUL_MEMBERSHIPS; ALL_SUMMARY_TABLES_REPLACED_ONCE"
        )
        for item in forecast:
            if item["category"] in QUALITY_CATEGORIES - {"quality"}:
                item["allocation_excluded"] = "ALREADY_INCLUDED_IN_WHOLE_ARCHIVE_MODEL"
    logical = sum(v["logical_bytes"] for v in counts.values())
    factor = max(1, (pages - free) * page / max(1, logical))
    sidecars = files["wal"] + files["shm"] + files["rollback_journal"]
    observed_sidecars = max(
        (sum(s.get("files", {}).get(k, 0) for k in ("wal", "shm", "rollback_journal")) for s in same_scope),
        default=0,
    )
    checkpoint_frames = store.db.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
    checkpoint_scenario = checkpoint_frames * (page + 24) + 32 + 32768 if checkpoint_frames else 0
    journal_reserve = max(sidecars, observed_sidecars, checkpoint_scenario)
    projected_without_quality = (
        sum(v["projected_logical_bytes"] or 0 for v in forecast if v["category"] not in QUALITY_CATEGORIES)
        * factor
    )
    quality_physical = (
        (
            episode_rate * len(settings.outcome_horizons) * EPISODE_RESULT_PHYSICAL
            + (event_rate or 0) * EPISODE_MEMBER_PHYSICAL
        )
        * 86400
        * 366
        if episode_rate is not None
        else 0
    )
    quality_physical = max(quality_physical, quality_flow * factor)
    projected = (
        max(sum(files.values()), projected_without_quality + quality_physical + free * page + journal_reserve)
        if elapsed > 0
        else None
    )
    alternatives: list[dict[str, Any]] = []
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
        from .source_column_work import status as source_column_status

        source_layout["columns"] = source_column_status(store.db)
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
        from . import numeric_frame_work

        numeric_layout["frames"] = numeric_frame_work.status(store.db, now_ms)
        if numeric_frame_work.mode(store.db) == "1":
            source_tables.add("market_numeric_frame_pending")
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
    if shared_layout["installed"]:
        source_tables |= {"market_shared_records", "market_shared_members", "market_shared_tails"}
        if shared_layout.get("work_installed"):
            source_tables |= {
                "market_shared_queue",
                "market_shared_pending",
                "market_shared_scan",
                "market_shared_staging",
                "market_shared_reclaim",
            }
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
        "target_bytes": TARGET,
        "target_exceeded": actual > TARGET,
        "catalogue_layout": catalogue_layout,
        "quality_frame_layout": frame_layout,
        "context_tile_layout": context_tile_layout,
        "parent_affinity_layout": affinity_layout,
        "shared_record_layout": shared_layout,
        "candle_dependency_retention_estimate": {
            "configured_minimum_days": settings.candle_retention_days,
            "detail_days": settings.detail_retention_days,
            "comparator_prior_days": 0,
            "native_input_window": WINDOW_SIZE,
            "days_for_estimate": candle_dependency_days,
            "effective_days_for_estimate": effective_candle_days,
            "used_inputs": used_shares["candles"],
            "basis": "GENERAL_72H_PLUS_OBSERVED_USED_INPUT_DETAIL_LEASES; NO_N20",
            "limitations": [
                "Historical configurations outside current input bounds require observed lease qualification"
            ],
        },
        "context_dependency_retention_estimate": {
            "configured_minimum_days": settings.candle_retention_days,
            "detail_days": settings.detail_retention_days,
            "comparator_prior_days": 0,
            "days_for_estimate": context_dependency_days,
            "effective_days_for_estimate": effective_context_days,
            "used_inputs": used_shares["context"],
            "basis": "GENERAL_72H_PLUS_OBSERVED_USED_CONTEXT_DETAIL_LEASES; NO_N20",
            "limitations": [
                "An older selector predecessor per stream/kind and supplied historical dependencies may exceed this span",
                "Actual frozen leases, population churn and native allocation still require qualification",
            ],
        },
        "working_bytes": actual,
        "files": files,
        "journal_reserve": {
            "bytes": journal_reserve,
            "observed_peak_bytes": observed_sidecars,
            "auto_checkpoint_scenario_bytes": checkpoint_scenario,
            "basis": "MAX_CURRENT_OBSERVED_PEAK_AND_CHECKPOINT_SCENARIO",
            "not_a_bound": True,
            "limitation": "Long readers can hold WAL beyond auto-checkpoint trigger; qualify real peaks and backup/temp reserve separately",
        },
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
        "scope_key": scope_key,
        "source_usage": usage,
        "observed_episodes_total": episode_total,
        "observed_episodes_per_day_extrapolated": episode_rate * 86400 if episode_rate is not None else None,
        "observed_events_total": event_total,
        "observed_events_per_day_extrapolated": event_rate * 86400 if event_rate is not None else None,
        "quality_forecast_model": {
            "kind": EPISODE_MODEL_VERSION,
            "physical_bytes_per_result_with_provenance": EPISODE_RESULT_PHYSICAL,
            "physical_bytes_per_membership_with_provenance": EPISODE_MEMBER_PHYSICAL,
            "assumption": "UNSHARED_PROVENANCE; DISTINCT_MEANINGFUL_EVENTS_BOUND_MEMBERSHIPS",
            "horizons_per_episode": len(settings.outcome_horizons),
            "not_a_bound": True,
            "actual_production_measurement": False,
            "legacy_n20_model_applied": False,
        },
        "forecast_scenarios": alternatives,
        "repack": repack,
        "forecast_kind": "OBSERVED_INPUT_USE_AND_EPISODE_RESULT_MEMBERSHIP_MODEL",
        "limitations": [
            "Bootstrap/bursts/short samples distort forecast",
            "Episode coefficients use isolated current SQLite writer with full unshared provenance; production allocation remains to qualify",
            "First-use ratios are byte-weighted canonical values, not physical allocation fractions; delayed leases and population churn can change them",
            "Source transfers and renewals do not add arrivals or first leases; insufficient or inconsistent usage window assumes all inputs used",
            "Aggregate service occupancy extrapolates positive net growth conservatively; terminal1d and stream-scoped state are not multiplied by gross turnover",
            "Historical methodology versions and old leases outside current admission rules need separate transition/qualification",
            "Retained used inputs may exceed raw TTL; forecast is a baseline, not an upper bound",
            "25GB desired target and35GB hard limit include indexes/free pages/working journals; backup/temp/physical reserve are separate",
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
            "files": value["files"],
            "format_version": SAMPLE_FORMAT,
            "completed_shared_layout": value.get("shared_record_layout", {}).get("version")
            if value.get("shared_record_layout", {}).get("initial_reclaim_complete")
            else None,
            "completed_repack_version": value["repack"].get("version")
            if value["repack"].get("state") == "COMPLETE"
            else None,
            "repack_saved": store.get_runtime("storage_repack", {}).get("saved_categories", {}),
            "categories": value["categories"],
            "event_total": value["observed_events_total"],
            "episode_total": value["observed_episodes_total"],
            "scope_key": value["scope_key"],
            "source_usage": value["source_usage"],
        }
    )
    # 24-hour minute samples are bounded. No full database/index scan here.
    if (
        len(samples) > 2
        and samples[-1]["at_ms"] // 3600000 == samples[-2]["at_ms"] // 3600000
        and any(
            s.get("format_version") == SAMPLE_FORMAT
            and s.get("scope_key") == samples[-1]["scope_key"]
            and s.get("completed_repack_version") == samples[-1]["completed_repack_version"]
            and s.get("completed_shared_layout") == samples[-1]["completed_shared_layout"]
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
