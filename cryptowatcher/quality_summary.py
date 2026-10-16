"""Lossless numeric detector summaries, without event identities or source arrays."""

from __future__ import annotations

import calendar
import hashlib
from datetime import datetime, timezone
from typing import Any

from .market_storage import decoded, encoded
from .storage import Store, encode

DAY = 86400000


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


def archive_event(store: Store, event: dict[str, Any]) -> None:
    """Called in the same transaction as detail deletion. Replay rolls back both."""
    from .analytics import COMPARATOR_VERSION
    from .events import History, identity
    from .outcomes import METHOD

    history = History(store)
    day = event["market_observation_ms"] // DAY
    comparison = history.get(
        "comparison-head:" + identity(f"comparison/{event['event_id']}/{COMPARATOR_VERSION}")
    )
    for horizon in event["settings_snapshot"].get("outcome_horizons", [5, 15, 30, 60]):
        oid = identity(f"outcome/{event['event_id']}/{horizon}/{METHOD}")
        outcome = history.get("outcome-head:" + oid)
        # Legacy/unfinished horizons are explicit coverage facts, never zero returns.
        if outcome is None:
            outcome = {
                "state": "EXPIRED_INCOMPLETE",
                "metrics": {},
                "classification": "UNCLASSIFIED",
                "methodology_version": METHOD,
            }
        group = {
            "detector": event["detector_id"],
            "version": event["detector_version"],
            "settings": event["settings_revision"],
            "profile": event["settings_snapshot"].get("profile", "custom"),
            "market": event["market_type"],
            "timeframe": event["timeframe"],
            "instrument": event["symbol"],
            "universe": event["settings_snapshot"]["alert_universe_mode"],
            "direction": event["direction"],
            "horizon_min": horizon,
            "methodology_version": outcome["methodology_version"],
        }
        provenance = {
            "settings_snapshot": event["settings_snapshot"],
            "settings_sha256": hashlib.sha256(encode(event["settings_snapshot"]).encode()).hexdigest(),
            "detector_definition": history.get(
                f"detector-metadata:{event['detector_id']}:{event['detector_version']}"
            ),
            "outcome_definition": history.get("outcome-method:" + outcome["methodology_version"]),
            "comparison_version": COMPARATOR_VERSION,
            "comparison_definition": history.get("comparison-method:" + COMPARATOR_VERSION),
        }
        # Retain every numeric metric and comparator outcome. No event IDs, times,
        # episodes, candles or decision context are retained in numeric summaries.
        fact = {
            "strength": event["strength"],
            "state": outcome["state"] if outcome["state"] == "COMPLETE" else "EXPIRED_RETENTION_INCOMPLETE",
            "previous_state": outcome["state"],
            "metrics": outcome["metrics"],
            "classification": outcome.get("classification", "UNCLASSIFIED"),
            "noise": outcome.get("noise_threshold_pct"),
            "comparator_metrics": [
                v["outcomes"][str(horizon)]["metrics"]
                for v in (comparison or {}).get("samples", [])
                if str(horizon) in v["outcomes"]
            ],
            "limitations": (comparison or {}).get("limitations", ["COMPARISON_UNAVAILABLE"]),
            "completeness": {
                k: outcome[k] for k in ("expected_candles", "observed_candles", "reason") if k in outcome
            },
        }
        cohort = hashlib.sha256(encode(group).encode()).hexdigest()
        old = store.db.execute(
            "SELECT chunk,facts,n FROM market_quality WHERE day=? AND cohort=? ORDER BY chunk DESC LIMIT 1",
            (day, cohort),
        ).fetchone()
        chunk = old[0] if old else 0
        facts = decoded(bytes(old[1]), 1024 * 1024) if old and old[2] < 128 else []
        if old and old[2] >= 128:
            chunk += 1
        facts.append(fact)
        # Shared settings/definitions outlive detail via the same ref-counted body pool.
        packed = history.codec.pack({"quality_provenance": provenance})
        store.db.execute(
            "INSERT INTO market_quality VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(day,cohort,chunk) DO UPDATE SET facts=excluded.facts,n=excluded.n",
            (
                day,
                cohort,
                chunk,
                month_boundary(day, 12),
                encode(group),
                encode(packed),
                encoded(facts),
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
