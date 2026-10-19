"""Consistent read-only, paged history export; transport and infrastructure stay private."""

from __future__ import annotations

import csv
import io
import json
import time
from dataclasses import dataclass
from typing import Any, Iterator

from .analytics import cohort_rows
from .catalogue_sql import source
from .events import PREFIX, History
from .history_codec import MARKER
from .storage import Store, encode, json_payload

SCHEMA = "cryptowatcher.export.v1"


@dataclass(frozen=True)
class Selection:
    start_ms: int = 0
    end_ms: int = 9_999_999_999_999_999
    detector: str | None = None
    version: str | None = None
    market: str | None = None

    def validate(self) -> None:
        if (
            type(self.start_ms) is not int
            or type(self.end_ms) is not int
            or not 0 <= self.start_ms < self.end_ms
        ):
            raise ValueError("Invalid export period")
        if self.market not in (None, "spot", "linear"):
            raise ValueError("Invalid export market")


def event_selection(selection: Selection, store: Store | None = None) -> tuple[str, list[Any]]:
    selection.validate()
    terms = [
        "key>=?",
        "key<?",
        "json_extract(cw_record_json(payload_json),'$.market_observation_ms')>=?",
        "json_extract(cw_record_json(payload_json),'$.market_observation_ms')<?",
    ]
    args: list[Any] = [PREFIX + "event-head:", PREFIX + "event-head:~", selection.start_ms, selection.end_ms]
    for name, value in (
        ("detector_id", selection.detector),
        ("detector_version", selection.version),
        ("market_type", selection.market),
    ):
        if value is not None:
            terms.append(f"json_extract(cw_record_json(payload_json),'$.{name}')=?")
            args.append(value)
    table = source(store, "event", head=True) if store is not None else "runtime"
    return "WITH selected AS (SELECT key,payload_json FROM " + table + " WHERE " + " AND ".join(
        terms
    ) + ") ", args


def paged(cursor: Any) -> Iterator[Any]:
    while rows := cursor.fetchmany(100):
        yield from rows


def ndjson(store: Store, selection: Selection) -> Iterator[str]:
    if not store.readonly or store.db.in_transaction:
        raise ValueError("Export requires a separate read-only connection")
    selection.validate()
    history = History(store)
    store.db.execute("BEGIN")
    try:
        cte, args = event_selection(selection, store)
        # First read pins the WAL snapshot before any output; later writes cannot enter this export.
        total = store.db.execute(cte + "SELECT COUNT(*) FROM selected", args).fetchone()[0]
        from .quality_summary import period_metadata

        period = period_metadata(selection.start_ms, selection.end_ms) if store.bounded else {}
        yield (
            encode(
                {
                    "schema": SCHEMA,
                    "type": "manifest",
                    "snapshot_at_ms": int(time.time() * 1000),
                    "selected_events": total,
                    "filters": selection.__dict__,
                    "period": "[start,end)",
                    "transport_state_included": False,
                    "history_generation": store.get_runtime("history_generation", {"state": "LEGACY"}),
                    "detail_retention_days": store.snapshot().settings.detail_retention_days,
                    "summary_retention_months": 12,
                    "summary_period_resolution": "UTC_DAY" if store.bounded else None,
                    "selection_period": period,
                    "expired_detail": "Unavailable; summaries are not individual event results"
                    if store.bounded
                    else None,
                }
            )
            + "\n"
        )
        counts: dict[str, int] = {}
        relations = {
            "event": "json_extract(cw_record_json(r.payload_json),'$.event_id') IN (SELECT json_extract(cw_record_json(payload_json),'$.event_id') FROM selected)",
            "episode": "json_extract(cw_record_json(r.payload_json),'$.episode_id') IN (SELECT json_extract(cw_record_json(payload_json),'$.episode_id') FROM selected)",
            "outcome": "json_extract(cw_record_json(r.payload_json),'$.event_id') IN (SELECT json_extract(cw_record_json(payload_json),'$.event_id') FROM selected)",
            "comparison": "json_extract(cw_record_json(r.payload_json),'$.event_id') IN (SELECT json_extract(cw_record_json(payload_json),'$.event_id') FROM selected)",
        }
        for kind, relation in relations.items():
            start = PREFIX + kind + ":"
            rows = store.db.execute(
                cte
                + "SELECT r.key,r.payload_json FROM "
                + source(store, kind)
                + " r WHERE r.key>=? AND r.key<? AND "
                + relation
                + " ORDER BY r.key",
                [*args, start, start + "~"],
            )
            for row in paged(rows):
                record = history.resolve(row[0][len(PREFIX) :], json_payload(row[1]))
                counts[kind] = counts.get(kind, 0) + 1
                yield encode({"schema": SCHEMA, "type": kind, "record": record}) + "\n"
        # Historical settings are frozen in every selected revision; export them independently too.
        rows = store.db.execute(
            cte
            + "SELECT DISTINCT json_extract(cw_record_json(r.payload_json),'$.settings_revision'), json_extract(cw_record_json(r.payload_json),'$.settings_snapshot'), "
            + f"json_extract(cw_record_json(r.payload_json),'$.{MARKER}.fields.settings_snapshot') "
            + "FROM "
            + source(store, "event")
            + " r WHERE r.key>=? AND r.key<? AND "
            + relations["event"]
            + " ORDER BY 1,2,3",
            [*args, PREFIX + "event:", PREFIX + "event:~"],
        )
        for row in paged(rows):
            counts["settings"] = counts.get("settings", 0) + 1
            yield (
                encode(
                    {
                        "schema": SCHEMA,
                        "type": "settings",
                        "record": {
                            "revision": row[0],
                            "snapshot": history.codec.field(json.loads(row[2]))
                            if row[2]
                            else json_payload(row[1]),
                        },
                    }
                )
                + "\n"
            )
        for kind in ("outcome-method:", "comparison-method:", "enrichment-method:"):
            start = PREFIX + kind
            for row in paged(
                store.db.execute(
                    "SELECT payload_json FROM runtime WHERE key>=? AND key<? ORDER BY key",
                    (start, start + "~"),
                )
            ):
                counts["methodology"] = counts.get("methodology", 0) + 1
                yield encode({"schema": SCHEMA, "type": "methodology", "record": json_payload(row[0])}) + "\n"
        if store.bounded:
            from .quality_packets import unpack as unpack_quality

            clauses = ["day>=?", "day<?"]
            values: list[Any] = [
                period["archive_start_ms"] // 86400000,
                period["archive_end_ms"] // 86400000,
            ]
            for name, filter_value in (
                ("detector", selection.detector),
                ("version", selection.version),
                ("market", selection.market),
            ):
                if filter_value is not None:
                    clauses.append(f"json_extract(dimensions,'$.{name}')=?")
                    values.append(filter_value)
            for row in store.db.execute(
                "SELECT day,dimensions,provenance,facts FROM market_quality WHERE "
                + " AND ".join(clauses)
                + " ORDER BY day,cohort,chunk",
                values,
            ):
                counts["quality_summary"] = counts.get("quality_summary", 0) + 1
                yield (
                    encode(
                        {
                            "schema": SCHEMA,
                            "type": "quality_summary",
                            "record": {
                                "event_day_utc": row[0],
                                "dimensions": json_payload(row[1]),
                                "provenance": history.codec.unpack(json.loads(row[2]))["quality_provenance"],
                                "facts": unpack_quality(store.db, bytes(row[3])),
                                "detail_available": False,
                            },
                        }
                    )
                    + "\n"
                )
        yield (
            encode({"schema": SCHEMA, "type": "complete", "counts": counts, "selected_events": total}) + "\n"
        )
    finally:
        store.db.rollback()


def csv_summary(store: Store, selection: Selection = Selection()) -> Iterator[str]:
    if not store.readonly or store.db.in_transaction:
        raise ValueError("Summary requires a separate read-only connection")
    store.db.execute("BEGIN")
    try:
        selection.validate()
        from .quality_summary import period_metadata

        period = period_metadata(selection.start_ms, selection.end_ms) if store.bounded else {}
        records = cohort_rows(
            store,
            start_ms=selection.start_ms,
            end_ms=selection.end_ms,
            detector=selection.detector,
            version=selection.version,
            market=selection.market,
        )
        output = io.StringIO(newline="")
        columns = [
            "group",
            "sample_size",
            "return_unit",
            "mean",
            "median",
            "min",
            "max",
            "p10",
            "p90",
            "comparator_sample_size",
            "comparator_mean",
            "classifications",
            "noise_min_pct",
            "noise_max_pct",
            "noise_sample_size",
            *period,
        ]
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        yield output.getvalue()
        for record in records:
            output.seek(0)
            output.truncate()
            writer.writerow(
                {
                    **record,
                    "group": encode(record["group"]),
                    "classifications": encode(record["classifications"]),
                    **period,
                }
            )
            yield output.getvalue()
    finally:
        store.db.rollback()
