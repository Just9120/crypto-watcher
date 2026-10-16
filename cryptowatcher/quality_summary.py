"""Lossless numeric detector summaries, without event identities or source arrays."""

from __future__ import annotations

import calendar
import hashlib
from datetime import datetime, timezone
from typing import Any

from .market_storage import decoded, encoded
from .storage import Store, encode

DAY = 86400000


def encode_facts(facts: list[Any]) -> bytes:
    """Lossless numeric columns; keep the smaller of typed v3 and legacy zlib."""
    from .record_codec import pack_binary

    typed = pack_binary(encode(facts))
    legacy = encoded(facts)
    return typed if len(typed) < len(legacy) else legacy


def month_boundary(day: int, months: int) -> int:
    value = datetime.fromtimestamp(day * 86400, timezone.utc)
    index = value.year * 12 + value.month - 1 + months
    year, month = divmod(index, 12)
    date = value.replace(
        year=year, month=month + 1, day=min(value.day, calendar.monthrange(year, month + 1)[1])
    )
    return int(date.timestamp()) // 86400


def install(db: Any) -> None:
    db.create_function(
        "cw_quality_json", 1, lambda b: encode(decoded(bytes(b), 1024 * 1024)), deterministic=True
    )


def create(db: Any) -> None:
    db.executescript("""
        CREATE TABLE market_quality(
            day INTEGER NOT NULL, cohort TEXT NOT NULL, chunk INTEGER NOT NULL,
            expires_day INTEGER NOT NULL, dimensions TEXT NOT NULL, provenance TEXT NOT NULL,
            facts BLOB NOT NULL, n INTEGER NOT NULL,
            PRIMARY KEY(day,cohort,chunk)) WITHOUT ROWID;
        CREATE INDEX market_quality_expiry ON market_quality(expires_day);
    """)
    install(db)


def comparator_facts(comparisons: list[dict[str, Any]], horizon: int) -> tuple[list[Any], list[Any]]:
    metrics: list[Any] = []
    sets = []
    for comparison in comparisons:
        values = [
            sample["outcomes"][str(horizon)]["metrics"]
            for sample in comparison.get("samples", [])
            if str(horizon) in sample["outcomes"]
        ]
        sets.append(
            {
                "methodology_version": comparison.get("methodology_version", "UNSET"),
                "offset": len(metrics),
                "count": len(values),
                "state": comparison.get("state", "UNSET"),
                "sample_size": comparison.get("sample_size", len(comparison.get("samples", []))),
                "population_size": comparison.get("population_size"),
                "limitations": comparison.get("limitations", []),
            }
        )
        metrics.extend(values)
    return metrics, sets


def archive_event(store: Store, event: dict[str, Any]) -> None:
    """Called in the same transaction as detail deletion. Replay rolls back both."""
    from .events import PREFIX, History
    from .storage import StateError

    history = History(store)
    # Owner/event index is bounded to this event; never walk all method heads.
    owned = store.db.execute(
        "SELECT key,category FROM market_lifetimes WHERE event_id=? AND category IN ('outcome-head','outcome-job','comparison-head') ORDER BY key",
        (event["event_id"],),
    ).fetchall()
    outcomes: dict[str, dict[str, Any]] = {}
    jobs = []
    comparisons = []
    for row in owned:
        value = history.metadata(row[0][len(PREFIX) :])
        if row[1] == "outcome-head":
            outcomes[value["outcome_id"]] = value
        elif row[1] == "outcome-job":
            jobs.append(value)
        else:
            comparisons.append(value)
    for job in jobs:
        outcomes.setdefault(
            job["outcome_id"],
            {
                "state": job["state"],
                "metrics": {},
                "classification": "UNCLASSIFIED",
                "methodology_version": job["methodology_version"],
                "event_revision": job["event_revision"],
                "horizon_min": job["horizon_min"],
            },
        )
    for horizon in event["settings_snapshot"].get("outcome_horizons", [5, 15, 30, 60]):
        if not any(o["horizon_min"] == horizon for o in outcomes.values()):
            # No head/job means the original methodology cannot be established.
            # A newly deployed default must never be attributed to this old event.
            outcomes[f"missing:{horizon}"] = {
                "state": "EXPIRED_INCOMPLETE",
                "metrics": {},
                "classification": "UNCLASSIFIED",
                "methodology_version": "UNSET",
                "event_revision": event["revision"],
                "horizon_min": horizon,
            }
    versions = sorted({c.get("methodology_version", "UNSET") for c in comparisons})
    comparison_definitions = {v: history.get("comparison-method:" + v) for v in versions}
    revisions = {event["revision"]: event}
    for outcome in outcomes.values():
        horizon = outcome["horizon_min"]
        revision = outcome["event_revision"]
        if revision not in revisions:
            revisions[revision] = history.metadata(f"event:{event['event_id']}:{revision:010d}")
        source = revisions[revision]
        if source is None:
            raise StateError("Missing frozen event revision during aggregation")
        day = source["market_observation_ms"] // DAY
        group = {
            "detector": source["detector_id"],
            "version": source["detector_version"],
            "settings": source["settings_revision"],
            "profile": source["settings_snapshot"].get("profile", "custom"),
            "market": source["market_type"],
            "timeframe": source["timeframe"],
            "instrument": source["symbol"],
            "universe": source["settings_snapshot"]["alert_universe_mode"],
            "direction": source["direction"],
            "horizon_min": horizon,
            "methodology_version": outcome["methodology_version"],
        }
        provenance = {
            "settings_snapshot": source["settings_snapshot"],
            "settings_sha256": hashlib.sha256(encode(source["settings_snapshot"]).encode()).hexdigest(),
            "detector_definition": history.get(
                f"detector-metadata:{source['detector_id']}:{source['detector_version']}"
            ),
            "outcome_definition": history.get("outcome-method:" + outcome["methodology_version"]),
            "comparison_version": versions[0] if len(versions) == 1 else "UNSET",
            "comparison_definition": comparison_definitions.get(versions[0]) if len(versions) == 1 else None,
            "comparison_versions": versions,
            "comparison_definitions": comparison_definitions,
        }
        # Retain every numeric metric and comparator outcome. No event IDs, times,
        # episodes, candles or decision context are retained in numeric summaries.
        comparator_metrics, comparator_sets = comparator_facts(comparisons, horizon)
        fact = {
            "strength": source["strength"],
            "state": outcome["state"] if outcome["state"] == "COMPLETE" else "EXPIRED_RETENTION_INCOMPLETE",
            "previous_state": outcome["state"],
            "metrics": outcome["metrics"],
            "classification": outcome.get("classification", "UNCLASSIFIED"),
            "noise": outcome.get("noise_threshold_pct"),
            "comparator_metrics": comparator_metrics,
            "comparator_sets": comparator_sets,
            "limitations": list(
                dict.fromkeys(reason for c in comparisons for reason in c.get("limitations", []))
            )
            if comparisons
            else ["COMPARISON_UNAVAILABLE"],
            "completeness": {
                k: outcome[k] for k in ("expected_candles", "observed_candles", "reason") if k in outcome
            },
        }
        cohort = hashlib.sha256(encode(group).encode()).hexdigest()
        packed = history.codec.pack({"quality_provenance": provenance})
        old = store.db.execute(
            "SELECT chunk,facts,n,provenance FROM market_quality WHERE day=? AND cohort=? ORDER BY chunk DESC LIMIT 1",
            (day, cohort),
        ).fetchone()
        chunk = old[0] if old else 0
        append = old and old[2] < 128 and old[3] == encode(packed)
        facts = decoded(bytes(old[1]), 1024 * 1024) if append else []
        if old and not append:
            chunk += 1
        facts.append(fact)
        # Shared settings/definitions outlive detail via the same ref-counted body pool.
        from .quality_layout import put

        put(
            store.db,
            (
                day,
                cohort,
                chunk,
                month_boundary(day, 12),
                encode(group),
                encode(packed),
                encode_facts(facts),
                len(facts),
            ),
        )
        from .storage_lifecycle import body_links

        body_links(store, f"quality:{day}:{cohort}:{chunk}", packed)


def union_sql(
    groups: tuple[str, ...],
    start_ms: int,
    end_ms: int,
    detector: str | None,
    version: str | None,
    market: str | None,
) -> tuple[str, list[Any]]:
    fields: list[str] = []
    for group in groups:
        fields.extend(
            (
                "'" + group + "'",
                "json_extract(s.value,'$.strength')"
                if group == "strength"
                else f"json_extract(q.dimensions,'$.{group}')",
            )
        )
    fields.extend(
        (
            "'horizon_min'",
            "json_extract(q.dimensions,'$.horizon_min')",
            "'methodology_version'",
            "json_extract(q.dimensions,'$.methodology_version')",
        )
    )
    clauses = ["q.day>=?", "q.day<?", "json_extract(s.value,'$.state')='COMPLETE'"]
    args: list[Any] = [(start_ms + DAY - 1) // DAY, (end_ms + DAY - 1) // DAY]
    for key, value in (("detector", detector), ("version", version), ("market", market)):
        if value is not None:
            clauses.append(f"json_extract(q.dimensions,'$.{key}')=?")
            args.append(value)
    # Composite comparator samples join below, using a synthetic summary coordinate,
    # never an old event identity.
    return (
        "SELECT json_object("
        + ",".join(fields)
        + ") AS group_key, COALESCE(json_extract(s.value,'$.metrics.directed_return_pct'),json_extract(s.value,'$.metrics.price_return_pct')) AS value, json_extract(s.value,'$.classification') AS class, json_extract(s.value,'$.noise') AS noise, 'summary:'||q.day||':'||q.cohort||':'||q.chunk||':'||s.key AS event_id, json_extract(q.dimensions,'$.horizon_min') AS horizon FROM market_quality q JOIN json_each(cw_quality_json(q.facts)) s WHERE "
        + " AND ".join(clauses),
        args,
    )
