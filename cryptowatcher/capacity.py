"""Read-only capacity evidence. Deep scans are explicit boundary measurements only."""

from __future__ import annotations

import os
import sqlite3
import time
from collections import Counter, defaultdict
from typing import Any

from .events import PREFIX, History
from .history_codec import Codec
from .storage import Store, json_payload
from .storage_meter import measure_indexes, report

DAY_MS = 86_400_000
DETAILS = {
    "event",
    "event-head",
    "episode",
    "episode-head",
    "outcome",
    "outcome-head",
    "comparison",
    "comparison-head",
}
COHORT_FIELDS = (
    "day",
    "detector",
    "version",
    "settings",
    "profile",
    "market",
    "timeframe",
    "instrument",
    "universe",
    "direction",
    "horizon",
    "methodology",
)


def namespace(key: str) -> str:
    return key[len(PREFIX) :].split(":", 1)[0] if key.startswith(PREFIX) else key.split(":", 1)[0]


def lifetime(kind: str, detail_days: int, candle_days: int) -> str:
    if kind in DETAILS or kind in {"decision", "history", "episode-event"}:
        return f"event detail TTL{detail_days}d; linked episodes survive only retained members"
    if kind in {"outcome-job", "comparison-job", "hold"}:
        return "delete on completion; unfinished bounded by parent event detail TTL"
    if kind in {"analytics-gap", "outbox", "agent", "digest-outbox"}:
        return "terminal receipt1d; retry/pending bounded by event deadline/explicit delivery expiry"
    if kind in {"condition", "diagnostic", "oi-attempt"}:
        return f"per-stream state, candle TTL{candle_days}d renewal, not one retained row per observation"
    return (
        "singleton/per-stream index or parent-bound service; inspect lifecycle, no blanket annual multiplier"
    )


def runtime_ledger(store: Store, seconds: float = 50) -> dict[str, Any]:
    """Sequential transient rowid scan; no durable rowid cursor or SQL temp sort."""
    deadline = time.monotonic() + seconds
    counts: dict[str, dict[str, int]] = {}
    cursor = 0
    complete = False
    store.db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    try:
        while time.monotonic() < deadline:
            rows = store.db.execute(
                "SELECT rowid,key,CASE typeof(payload_json) WHEN 'blob' THEN length(payload_json) "
                "ELSE length(CAST(payload_json AS BLOB)) END FROM runtime WHERE rowid>? ORDER BY rowid LIMIT 4096",
                (cursor,),
            ).fetchall()
            for rid, key, size in rows:
                entry = counts.setdefault(namespace(key), {"records": 0, "key_bytes": 0, "payload_bytes": 0})
                entry["records"] += 1
                entry["key_bytes"] += len(key.encode())
                entry["payload_bytes"] += size
                cursor = rid
            if len(rows) < 4096:
                complete = True
                break
    except sqlite3.OperationalError as exc:
        if getattr(exc, "sqlite_errorcode", None) != sqlite3.SQLITE_INTERRUPT:
            raise
    finally:
        store.db.set_progress_handler(None, 0)
    settings = store.snapshot().settings
    return {
        "state": "MEASURED" if complete else "PARTIAL_BUDGET",
        "namespaces": {
            k: {**v, "lifetime": lifetime(k, settings.detail_retention_days, settings.candle_retention_days)}
            for k, v in counts.items()
        },
        "records_scanned": sum(v["records"] for v in counts.values()),
        "scope": "runtime snapshot; encoded values and UTF8 keys only; NOT physical pages/indexes",
        "partial_cursor": None if complete else cursor,
        "revision_multiplicity": {
            kind: {
                "revisions": counts.get(kind, {}).get("records", 0),
                "heads": counts.get(kind + "-head", {}).get("records", 0),
                "revisions_per_head": counts.get(kind, {}).get("records", 0)
                / counts[kind + "-head"]["records"]
                if counts.get(kind + "-head", {}).get("records")
                else None,
            }
            for kind in ("event", "episode", "outcome", "comparison")
        },
    }


def comparator_sample(store: Store, limit: int = 128) -> dict[str, Any]:
    history = History(store)
    prefix = PREFIX + "comparison:"
    keys = store.db.execute(
        "SELECT key FROM runtime WHERE key>=? AND key<? ORDER BY key LIMIT ?",
        (prefix, prefix + "~", limit),
    ).fetchall()
    sizes: Counter[int] = Counter()
    nonempty: Counter[str] = Counter()
    for (key,) in keys:
        value = history.metadata(key[len(PREFIX) :])
        samples = value.get("samples", [])
        sizes[len(samples)] += 1
        if samples:
            # Hash exact numerical samples, not envelope/event IDs or the empty set.
            import hashlib

            from .storage import encode

            nonempty[hashlib.sha256(encode(samples).encode()).hexdigest()] += 1
    return {
        "n": len(keys),
        "sample_sizes": dict(sizes),
        "distinct_nonempty_sets": len(nonempty),
        "repeated_nonempty_observations": sum(n - 1 for n in nonempty.values()),
        "kind": "BOUNDED_LEXICOGRAPHIC_NOT_RANDOM; public metadata, not encoded-byte attribution",
        "empty_sets_are_not_dedup_evidence": True,
    }


def prospective_cohorts(store: Store, seconds: float = 60) -> dict[str, Any]:
    """Measure frozen event grouping, explicitly distinct from actually archived facts."""
    deadline = time.monotonic() + seconds
    codec = Codec(store.db)
    counts: Counter[tuple[Any, ...]] = Counter()
    revisions: Counter[int] = Counter()
    marginal: dict[str, set[Any]] = defaultdict(set)
    strengths: set[float] = set()
    complete = False
    published = [
        r[0]
        for r in store.db.execute(
            "SELECT key FROM runtime WHERE key>=? AND key<?",
            (PREFIX + "outcome-method:", PREFIX + "outcome-method:~"),
        )
    ]
    # Multiple historical methods need outcome-linked counting, not a guessed
    # Cartesian multiplication of every event by every published methodology.
    if published != [PREFIX + "outcome-method:1"]:
        return {"state": "UNSET_MULTIPLE_OR_UNKNOWN_METHODS", "published_methods": published}
    store.db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    try:
        for key, payload in store.db.execute(
            "SELECT key,payload_json FROM runtime NOT INDEXED WHERE key>=? AND key<? ORDER BY rowid",
            (PREFIX + "event:", PREFIX + "event:~"),
        ):
            if time.monotonic() >= deadline:
                break
            event = codec.unpack(
                json_payload(payload),
                omit_fields=frozenset({"source_inputs", "future_inputs", "source_context", "enrichment"}),
            )
            settings = event["settings_snapshot"]
            revisions[event["revision"]] += 1
            strengths.add(event["strength"])
            for horizon in settings["outcome_horizons"]:
                counts[
                    (
                        event["market_observation_ms"] // DAY_MS,
                        event["detector_id"],
                        event["detector_version"],
                        event["settings_revision"],
                        settings.get("profile", "custom"),
                        event["market_type"],
                        event["timeframe"],
                        event["symbol"],
                        settings["alert_universe_mode"],
                        event["direction"],
                        horizon,
                        "1",
                    )
                ] += 1
        else:
            complete = True
    except sqlite3.OperationalError as exc:
        if getattr(exc, "sqlite_errorcode", None) != sqlite3.SQLITE_INTERRUPT:
            raise
    finally:
        store.db.set_progress_handler(None, 0)
    for group in counts:
        for field, value in zip(COHORT_FIELDS, group):
            marginal[field].add(value)
    density = sorted(counts.values())
    return {
        "state": "MEASURED_PROSPECTIVE" if complete else "PARTIAL_BUDGET",
        "event_revisions": dict(revisions),
        "event_records": sum(revisions.values()),
        "facts_if_archived": sum(density),
        "cohort_days_if_archived": len(counts),
        "density_histogram": dict(Counter(density)),
        "density_mean": sum(density) / max(1, len(density)),
        "density_median": density[len(density) // 2] if density else None,
        "dimension_cardinality": {k: len(v) for k, v in marginal.items()},
        "distinct_without_dimension": {
            name: len({key[:i] + key[i + 1 :] for key in counts}) for i, name in enumerate(COHORT_FIELDS)
        },
        "distinct_strengths": len(strengths),
        "limitations": [
            "Frozen event horizons and published outcome methodology1; not archived production summaries",
            "One calendar-day density may be partial; event revisions are counted explicitly",
            "Strength is a numeric fact, not a stored cohort dimension; ablation is explanatory, not a scope change",
            "Future comparator availability and additional settings/methods are not observed by this calculation",
        ],
    }


def stable_reason(store: Store) -> list[str]:
    ingestion = store.get_runtime("ingestion", {})
    reasons = []
    if time.time() - ingestion.get("at", 0) > 30:
        reasons.append("INGESTION_STALE")
    for key in (
        "bootstrap_pending_streams",
        "blocked_streams",
        "recovery_pending",
        "recovery_working",
        "historical_gaps_pending",
    ):
        if ingestion.get(key, 1) != 0:
            reasons.append(key.upper())
    if ingestion.get("market_errors"):
        reasons.append("MARKET_ERRORS")
    connections = ingestion.get("subscriptions", {})
    if not connections or any(
        c.get("state") != "CONNECTED" or c.get("requested") != c.get("acknowledged")
        for c in connections.values()
    ):
        reasons.append("SUBSCRIPTION_ACK")
    if not store.get_runtime("radar:v1:outcome-admission", {}).get("complete"):
        reasons.append("OUTCOME_BOOTSTRAP")
    # Referenced analytics backfill has a different queue from live ingestion
    # recovery. A populated queue must not be mistaken for a settled baseline.
    prefix = PREFIX + "analytics-gap-pending:"
    if store.db.execute(
        "SELECT 1 FROM runtime WHERE key>=? AND key<? LIMIT 1", (prefix, prefix + "~")
    ).fetchone():
        reasons.append("ANALYTICS_RECOVERY_PENDING")
    if store.get_runtime("storage_repack", {}).get("state") != "COMPLETE":
        reasons.append("REPACK")
    analytics = store.get_runtime("analytics", {})
    if analytics.get("state") != "OK" or time.time() * 1000 - analytics.get("at_ms", 0) > 60000:
        reasons.append("ANALYTICS_NOT_CURRENT")
    return reasons


def explain_forecast(value: dict[str, Any], ledger: dict[str, Any]) -> dict[str, Any]:
    """Reconcile the existing point estimate; do not call an allocation a measured flow."""
    namespaces = ledger.get("namespaces", {})
    detail_bytes = sum(v["payload_bytes"] for k, v in namespaces.items() if k in DETAILS)
    factor = value.get("page_overhead_factor", 1)
    details: dict[str, Any] = next((x for x in value.get("forecast", []) if x["category"] == "details"), {})
    projected_details = details.get("projected_logical_bytes")
    allocated = {
        k: {
            **v,
            "retention_days": details.get("retention_days_for_estimate"),
            "allocated_logical_forecast_bytes": projected_details * v["payload_bytes"] / detail_bytes
            if projected_details is not None and detail_bytes
            else None,
        }
        for k, v in namespaces.items()
        if k in DETAILS
    }
    return {
        "kind": "EXPLANATION_OF_EXISTING_FLOW_MODEL_NOT_A_NEW_MEASUREMENT",
        "runtime_namespace_allocation": allocated,
        "allocation_formula": "details projected_logical_bytes * namespace current payload / current details payload",
        "physical_formula": "SUM(non-quality projected logical)*measured occupied-page/logical factor + quality scenario physical + WAL + SHM",
        "factor": factor,
        "categories": value.get("forecast"),
        "quality_scenarios": value.get("forecast_scenarios"),
        "point_bytes": value.get("projected_working_bytes"),
        "limitations": [
            "Allocation is current mix, NOT observed per-namespace daily insert flow",
            "Physical indexes already reside in page factor; NEVER add them a second time",
            "Annual quality uses synthetic numeric facts; cardinality/rehearsal/24h data are separate evidence",
            "Services use occupancy plus positive net; short post-restart or repack samples are not steady state",
        ],
    }


def snapshot(store: Store, *, deep: bool = False, cohorts: bool = False) -> dict[str, Any]:
    if not store.readonly:
        raise ValueError("Capacity probe requires a dedicated readonly Store")
    if store.db.in_transaction:
        raise ValueError("Capacity probe owns its short read snapshot")
    if not store.bounded:
        return {
            "schema": "cryptowatcher.capacity-evidence.v1",
            "state": "N/A_LEGACY_LAYOUT",
            "storage": report(store),
            "stability_blockers": ["UNMETERED_LEGACY_LAYOUT"],
        }
    started = time.monotonic()
    store.db.execute("BEGIN")
    try:
        storage = report(store)
        quality: dict[str, Any] = {
            "blocks": storage.get("categories", {}).get("quality", {}).get("records", 0),
            "facts": None,
            "encoded_fact_bytes": storage.get("categories", {}).get("quality", {}).get("logical_bytes", 0),
            "state": "COUNTERS_ONLY",
        }
        if deep:
            deadline = time.monotonic() + 5
            store.db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            try:
                row = store.db.execute("SELECT COUNT(*),COALESCE(SUM(n),0) FROM market_quality").fetchone()
                quality.update(blocks=row[0], facts=row[1], state="MEASURED")
            except sqlite3.OperationalError as exc:
                if getattr(exc, "sqlite_errorcode", None) != sqlite3.SQLITE_INTERRUPT:
                    raise
                quality["state"] = "PARTIAL_BUDGET"
            finally:
                store.db.set_progress_handler(None, 0)
        value = {
            "schema": "cryptowatcher.capacity-evidence.v1",
            "at_ms": int(time.time() * 1000),
            "source_revision": os.getenv("SOURCE_REVISION", "UNSET"),
            "history_generation": store.get_runtime("history_generation"),
            "configuration_revision": store.snapshot().revision,
            "storage": storage,
            "stability_blockers": stable_reason(store),
            "ingestion": store.get_runtime("ingestion"),
            "repack": store.get_runtime("storage_repack"),
            "analytics": store.get_runtime("analytics"),
            "retention": store.get_runtime("market_retention"),
            "retention_totals": store.get_runtime("market_retention_totals", {}),
            "quality_actual": quality,
            "comparator_sample": comparator_sample(store) if deep else {"state": "NOT_REQUESTED"},
            "runtime_ledger": runtime_ledger(store) if deep else {"state": "NOT_REQUESTED"},
            "prospective_cohorts": prospective_cohorts(store) if cohorts else {"state": "NOT_REQUESTED"},
            "physical_snapshot": measure_indexes(store, seconds=10) if deep else {"state": "NOT_REQUESTED"},
            "backups_and_experiments": "EXCLUDED_FROM_WORKING_TOTAL; separate operator inventory",
            "capacity_verdict": "UNPROVEN; tests/CD success does not establish20GB fit",
        }
        value["forecast_explanation"] = explain_forecast(storage, value["runtime_ledger"])
        value["elapsed_seconds"] = round(time.monotonic() - started, 3)
        return value
    finally:
        store.db.set_progress_handler(None, 0)
        store.db.rollback()


def interval(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    seconds = (after["at_ms"] - before["at_ms"]) / 1000
    if seconds <= 0:
        raise ValueError("Positive measurement interval required")
    compatible = before.get("history_generation") == after.get("history_generation")
    same_config = before.get("configuration_revision") == after.get("configuration_revision")
    same_source = before.get("source_revision") == after.get("source_revision")
    stable = not before.get("stability_blockers") and not after.get("stability_blockers")
    full_day = seconds >= 86400 and compatible and stable and same_config and same_source
    b, a = before["storage"], after["storage"]
    result: dict[str, Any] = {
        "state": "OBSERVED_24H" if full_day else "PARTIAL_OR_UNSTABLE",
        "seconds": seconds,
        "generation_unchanged": compatible,
        "configuration_unchanged": same_config,
        "source_unchanged": same_source,
        "working_net_bytes": a["working_bytes"] - b["working_bytes"],
        "file_deltas": {k: a["files"].get(k, 0) - b["files"].get(k, 0) for k in a["files"]},
        "daily_extrapolation_bytes": (a["working_bytes"] - b["working_bytes"]) / seconds * 86400
        if full_day
        else None,
        "namespace_changes": {},
        "namespace_measurement_state": "UNSET_PARTIAL_OR_MISSING_BOUNDARY_LEDGER",
        "limitations": [
            "Boundary stability alone cannot prove stability between samples; inspect observation log",
            "WAL size is checkpoint-sensitive; DB and journals measured separately",
            "A measured day is not proof of annual population/method/comparator mix",
        ],
    }
    bp, ap = before.get("physical_snapshot", {}), after.get("physical_snapshot", {})
    if bp.get("state") == ap.get("state") == "PASS":
        result["physical_page_changes"] = {
            "index_bytes": ap["total_index_bytes"] - bp["total_index_bytes"],
            "tables": {
                key: ap["tables"].get(key, 0) - bp["tables"].get(key, 0)
                for key in ap["tables"].keys() | bp["tables"].keys()
            },
            "indexes_are_part_of_database_delta": True,
        }
    result["cleanup_changes"] = {
        k: after.get("retention_totals", {}).get(k, 0) - before.get("retention_totals", {}).get(k, 0)
        for k in after.get("retention_totals", {})
        if k not in ("at_ms", "last_transaction_work_seconds")
    }
    if (
        before.get("runtime_ledger", {}).get("state")
        == after.get("runtime_ledger", {}).get("state")
        == "MEASURED"
    ):
        result["namespace_measurement_state"] = "MEASURED_BOUNDARY_DELTA"
        bns, ans = before["runtime_ledger"]["namespaces"], after["runtime_ledger"]["namespaces"]
        result["namespace_changes"] = {
            k: {
                field: ans.get(k, {}).get(field, 0) - bns.get(k, {}).get(field, 0)
                for field in ("records", "key_bytes", "payload_bytes")
            }
            for k in bns.keys() | ans.keys()
        }
    return result
