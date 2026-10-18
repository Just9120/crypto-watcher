"""Prior no-signal comparators, descriptive cohorts and explicit causal readout."""

from __future__ import annotations

import asyncio
import bisect
import hashlib
import json
import math
import time
from dataclasses import asdict
from typing import Any, Callable

from .config import OwnerSettings
from .detectors import VERSIONS, DetectorUnavailable, atr14, evaluate
from .events import PREFIX, History, identity
from .outcomes import DAY, MINUTE, calculate, pending_key, protect, read_candles
from .source_context import context
from .storage import StateError, Store, encode, json_payload

COMPARATOR_VERSION = "1"
COMPARATOR_DEFINITION = {
    "version": COMPARATOR_VERSION,
    "period": "previous28days; same instrument/market/TF/UTC observation hour",
    "quartiles": "prior complete ATR14/reference_close percent; nearest-rank empirical quartiles, ties lower bin",
    "selection": "frozen detector/settings no-signal; closest prior time first; max20 nonoverlapping full windows",
    "cutoff": "comparison outcome ends strictly before event observation",
}
GROUPS = (
    "detector",
    "version",
    "settings",
    "profile",
    "market",
    "timeframe",
    "instrument",
    "universe",
    "direction",
    "strength",
)


def as_of(history: History, kind: str, key: str, cutoff_ms: int) -> dict[str, Any] | None:
    prefix = PREFIX + f"{kind}:{key}:"
    rows = history.store.db.execute(
        "SELECT key,payload_json FROM runtime WHERE key>=? AND key<? ORDER BY key DESC",
        (prefix, prefix + "~"),
    )
    for row in rows:
        record = history.resolve(row[0][len(PREFIX) :], json_payload(row[1]))
        if record["available_at_ms"] <= cutoff_ms:
            return record
    return None


def decision_context(history: History, event_id: str, cutoff_ms: int) -> dict[str, Any]:
    event = as_of(history, "event", event_id, cutoff_ms)
    return {
        "schema": "cryptowatcher.decision-context.v1",
        "cutoff_ms": cutoff_ms,
        "event": event,
        "episode": as_of(history, "episode", event["episode_id"], cutoff_ms) if event else None,
        "late_analytics_included": False,
        "trading_authorized": False,
        "model_invoked": False,
    }


def prior_population(
    store: Store, native: str, start: int, reference: int, needed: int, step: int
) -> tuple[Any, Any, Any]:
    from .detectors import true_range

    frozen = store.readonly and store.db.in_transaction
    warmup = max(256, needed) if frozen else needed
    key = (native, start, reference, warmup, store.db.execute("PRAGMA data_version").fetchone()[0])
    if frozen and store.comparison_population is not None and store.comparison_population[0] == key:
        return store.comparison_population[1]
    candles = read_candles(store, native, max(0, start - warmup * step), reference)
    ranges = [0.0] + [true_range(b, a) for a, b in zip(candles, candles[1:])]
    points = []
    contiguous = 0
    for i, current in enumerate(candles):
        contiguous = contiguous + 1 if i == 0 or current.start - candles[i - 1].start == current.step else 1
        if contiguous >= 16 and start <= current.end < reference:
            # Same fourteen additions, order and division as atr14; no subtraction.
            value = 100 * (sum(ranges[i - 14 : i]) / 14) / current.close
            if math.isfinite(value):
                points.append((current, i, value))
    frozen_value = (candles, points, sorted(point[2] for point in points))
    if frozen:
        store.comparison_population = (key, frozen_value)
    return frozen_value


def comparison_dataset(store: Store, event: dict[str, Any]) -> dict[str, Any]:
    reference = event["reference_time_ms"]
    start = max(0, reference - 28 * DAY)
    settings = OwnerSettings.from_payload(event["settings_snapshot"])
    horizons = settings.outcome_horizons
    max_horizon = max(horizons) * MINUTE
    step = int(event["timeframe"][:-1]) * MINUTE
    native = f"{event['market_type']}/{event['symbol']}/{event['timeframe']}"
    parameters = event["effective_parameters"]
    counts = {
        "price": 2,
        "turnover": int(parameters["turnover_lookback"]) + 1,
        "composite": int(parameters["turnover_lookback"]) + 1,
        "breakout": int(parameters["breakout_lookback"]) + 1,
        "volatility": int(parameters["volatility_lookback"]) + 2,
    }
    needed = max(16, counts[event["detector_id"]])
    result: dict[str, Any] = {
        "schema": "cryptowatcher.comparison.v1",
        "comparison_id": identity(f"comparison/{event['event_id']}/{COMPARATOR_VERSION}"),
        "event_id": event["event_id"],
        "event_revision": event["revision"],
        "methodology_version": COMPARATOR_VERSION,
        "state": "COMPLETE_LIMITED",
        "settings_sha256": hashlib.sha256(encode(event["settings_snapshot"]).encode()).hexdigest(),
        "period_start_ms": start,
        "cutoff_ms": reference,
        "sample_size": 0,
        "population_size": 0,
        "quartile_boundaries": [],
        "event_quartile": None,
        "samples": [],
        "limitations": [],
    }
    if VERSIONS.get(event["detector_id"]) != event["detector_version"]:
        result["limitations"] = ["FROZEN_DETECTOR_VERSION_UNAVAILABLE"]
        return result
    reference_atr = event.get("reference_atr14")
    if reference_atr is None or not math.isfinite(reference_atr):
        result["limitations"] = ["REFERENCE_ATR_UNAVAILABLE"]
        return result
    candles, points, population = prior_population(store, native, start, reference, needed, step)
    result["population_size"] = len(population)
    if not population:
        result["limitations"] = ["INSUFFICIENT_PRIOR_HISTORY"]
        return result
    boundaries = [population[max(0, math.ceil(len(population) * q / 4) - 1)] for q in (1, 2, 3)]
    quartile = bisect.bisect_left(boundaries, 100 * reference_atr / event["reference_price"]) + 1
    result.update(quartile_boundaries=boundaries, event_quartile=quartile)
    occupied: list[tuple[int, int]] = []
    source_errors = 0
    missing_liquidity = 0
    for current, index, value in reversed(points):
        if (
            current.end + max_horizon >= reference
            or current.end // 3_600_000 % 24 != reference // 3_600_000 % 24
            or bisect.bisect_left(boundaries, value) + 1 != quartile
            or any(current.end < end and current.end + max_horizon > begin for begin, end in occupied)
        ):
            continue
        window = candles[max(0, index - needed + 1) : index + 1]
        try:
            historical = context(store, native.rsplit("/", 1)[0], current.end)
            ticker = historical["ticker"]
            liquidity = 0.0
            if event["detector_id"] == "composite":
                if ticker is None or current.end - ticker["market_observation_ms"] > 5 * MINUTE:
                    missing_liquidity += 1
                    continue
                liquidity = float(ticker["values"]["turnover24h"])
                if not math.isfinite(liquidity) or liquidity < 0:
                    raise DetectorUnavailable("invalid_historical_liquidity")
            observation = evaluate(
                event["detector_id"],
                window,
                settings,
                native.rsplit("/", 1)[0],
                liquidity,
            )
        except DetectorUnavailable:
            source_errors += 1
            continue
        if observation.signal:
            continue
        future = read_candles(store, native.rsplit("/", 1)[0] + "/1m", current.end, current.end + max_horizon)
        metadata = historical["instrument"]
        noise = None
        if metadata is not None:
            tick = float(metadata["tick_size"])
            noise = max(2 * tick, 0.1 * atr14(window)) / current.close * 100
            if not math.isfinite(noise) or tick <= 0:
                noise = None
        comparator_event = {
            **event,
            "reference_time_ms": current.end,
            "reference_price": current.close,
            "continuation_threshold_pct": noise,
        }
        outcomes = {
            str(h): calculate(comparator_event, h, [c for c in future if c.end <= current.end + h * MINUTE])
            for h in horizons
        }
        if any(v["state"] != "COMPLETE" for v in outcomes.values()):
            source_errors += 1
            continue
        for outcome in outcomes.values():
            outcome.pop("source_inputs")
        result["samples"].append(
            {
                "reference_time_ms": current.end,
                "reference_price": current.close,
                "atr_pct": value,
                "quartile": quartile,
                "no_signal_metrics": observation.metrics,
                "source_context": historical,
                "source_inputs": [asdict(c) for c in window],
                "future_inputs": [asdict(c) for c in future],
                "outcomes": outcomes,
            }
        )
        occupied.append((current.end, current.end + max_horizon))
        if len(occupied) == 20:
            break
    result["sample_size"] = len(occupied)
    if len(occupied) < 20:
        result["limitations"].append("FEWER_THAN20_MATCHING_COMPLETE_NONOVERLAPPING_WINDOWS")
    if not candles or candles[0].end > start:
        result["limitations"].append("PARTIAL28DAY_SOURCE_HISTORY")
    if source_errors:
        result["limitations"].append("INCOMPLETE_OR_UNCALCULABLE_CANDIDATES")
    if missing_liquidity:
        result["limitations"].append("HISTORICAL_LIQUIDITY_UNAVAILABLE")
    result["state"] = "COMPLETE_LIMITED" if result["limitations"] else "COMPLETE"
    return result


class ComparisonWorker:
    def __init__(self, store: Store) -> None:
        self.store, self.history = store, History(store)
        with store.db:
            previous = self.history.get("comparison-method:" + COMPARATOR_VERSION)
            if previous is not None and previous != COMPARATOR_DEFINITION:
                raise StateError("Comparator methodology changed without a new version")
            if previous is None:
                self.history.put("comparison-method:" + COMPARATOR_VERSION, COMPARATOR_DEFINITION)

    def requeue(self, event_id: str, now_ms: int) -> str:
        """Explicit owner action; preserve the original revision and prior analytics."""
        job = self.history.get("comparison-job:" + event_id)
        if job is None and self.store.bounded:
            event = self.history.get(f"event:{event_id}:0000000001")
            if event is not None:
                job = {"event_id": event_id, "event_revision": event["revision"], "pending_key": ""}
        if job is None:
            raise ValueError("No admitted event")
        if self.history.get(job["pending_key"]) is not None:
            return "ALREADY_PENDING"
        event = self.history.get(f"event:{event_id}:{job['event_revision']:010d}")
        if event is None:
            raise StateError("Missing frozen comparison event")
        native = f"{event['market_type']}/{event['symbol']}/{event['timeframe']}"
        lookback = max(
            16, int(max(v for k, v in event["effective_parameters"].items() if k.endswith("lookback"))) + 2
        )
        with self.store.db:
            job.update(
                state="PENDING",
                next_attempt_ms=now_ms,
                recalculation_requested_ms=now_ms,
                due_ms=now_ms,
                methodology_version=COMPARATOR_VERSION,
            )
            job["pending_key"] = pending_key(now_ms, event_id).replace(
                "outcome-pending:", "comparison-pending:", 1
            )
            self.history.put("comparison-job:" + event_id, job)
            self.history.put(job["pending_key"], {"event_id": event_id})
            for stream in (native, native.rsplit("/", 1)[0] + "/1m"):
                self.history.put(
                    f"hold:{stream}:comparison:{event_id}",
                    {
                        "start": max(
                            0,
                            event["reference_time_ms"]
                            - 28 * DAY
                            - lookback * int(event["timeframe"][:-1]) * MINUTE,
                        ),
                        "end": event["reference_time_ms"],
                    },
                )
        return "PENDING"

    def work(self, now_ms: int) -> None:
        for pending, binding in self.history.items("comparison-pending:", 1):
            job = self.history.get("comparison-job:" + binding["event_id"])
            if job is None:
                with self.store.db:
                    self.history.delete(pending)
                continue
            if job["next_attempt_ms"] > now_ms:
                return
            event = self.history.get(f"event:{job['event_id']}:{job['event_revision']:010d}")
            self.apply(job, event, binding, self.calculate(self.store, job, event), now_ms)

    @staticmethod
    def calculate(store: Store, job: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
        if job.get("methodology_version", "1") != COMPARATOR_VERSION:
            version = job.get("methodology_version", "1")
            return {
                "schema": "cryptowatcher.comparison.v1",
                "comparison_id": identity(f"comparison/{event['event_id']}/{version}"),
                "event_id": event["event_id"],
                "event_revision": event["revision"],
                "methodology_version": version,
                "state": "INVALID_SOURCE",
                "sample_size": 0,
                "samples": [],
                "limitations": ["FROZEN_COMPARISON_VERSION_UNAVAILABLE"],
            }
        try:
            return comparison_dataset(store, event)
        except (ValueError, TypeError, KeyError, OverflowError, StateError) as exc:
            return {
                "schema": "cryptowatcher.comparison.v1",
                "comparison_id": identity(f"comparison/{event['event_id']}/{COMPARATOR_VERSION}"),
                "event_id": event["event_id"],
                "event_revision": event["revision"],
                "methodology_version": COMPARATOR_VERSION,
                "state": "INVALID_SOURCE",
                "sample_size": 0,
                "samples": [],
                "limitations": [type(exc).__name__],
            }

    async def async_work(self, clock: Callable[[], int], batch: int = 64, seconds: float = 2) -> int:
        from .analytics_worker import prepare, read_snapshot

        now_ms = clock()
        started = time.monotonic()
        prepared = await asyncio.to_thread(
            read_snapshot,
            self.store.path,
            self.store.owner_id,
            lambda store: prepare(
                store,
                "comparison-pending:",
                "comparison-job:",
                "event_id",
                now_ms,
                batch,
                seconds,
                self.calculate,
            ),
        )
        read_seconds = time.monotonic() - started
        applied = 0
        completed_lags = []
        for job, event, binding, result in prepared:
            if self.history.get("comparison-job:" + event["event_id"]) == job:
                self.apply(job, event, binding, result, clock())
                applied += 1
                if result["state"] in ("COMPLETE", "COMPLETE_LIMITED"):
                    completed_lags.append(max(0, clock() - job.get("due_ms", job["next_attempt_ms"])))
            await asyncio.sleep(0)
        from .queue_health import completed_batch

        completed_batch(
            self.store,
            "comparison",
            clock(),
            completed_lags,
            time.monotonic() - started,
            read_seconds=read_seconds,
        )
        return applied

    def apply(
        self,
        job: dict[str, Any],
        event: dict[str, Any],
        binding: dict[str, Any],
        result: dict[str, Any],
        now_ms: int,
    ) -> None:
        if "recalculation_requested_ms" in job:
            result["recalculation_requested_ms"] = job["recalculation_requested_ms"]
        head = self.history.get("comparison-head:" + result["comparison_id"])
        changed = head is None or encode(result) != encode(
            {k: v for k, v in head.items() if k not in ("revision", "available_at_ms")}
        )
        with self.store.db:
            if changed:
                result.update(revision=1 if head is None else head["revision"] + 1, available_at_ms=now_ms)
                self.history.version("comparison", result)
            self.history.delete(job["pending_key"])
            if result["state"] == "INVALID_SOURCE":
                job.update(state="INVALID_SOURCE", next_attempt_ms=now_ms + MINUTE)
                job["pending_key"] = pending_key(job["next_attempt_ms"], event["event_id"]).replace(
                    "outcome-pending:", "comparison-pending:", 1
                )
                self.history.put(job["pending_key"], binding)
            else:
                native = f"{event['market_type']}/{event['symbol']}/{event['timeframe']}"
                stream = native.rsplit("/", 1)[0] + "/1m"
                for sample in result["samples"]:
                    protect(self.history, native, sample["source_inputs"])
                    protect(self.history, stream, sample["future_inputs"])
                for held in (native, stream):
                    self.history.delete(f"hold:{held}:comparison:{event['event_id']}")
                job.update(state=result["state"], completed_ms=now_ms)
            if self.store.bounded and result["state"] in ("COMPLETE", "COMPLETE_LIMITED"):
                self.history.delete("comparison-job:" + event["event_id"])
            else:
                self.history.put("comparison-job:" + event["event_id"], job)


def dimensions(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "detector": event["detector_id"],
        "version": event["detector_version"],
        "settings": hashlib.sha256(encode(event["settings_snapshot"]).encode()).hexdigest(),
        "profile": event["settings_snapshot"].get("profile", "custom"),
        "market": event["market_type"],
        "timeframe": event["timeframe"],
        "instrument": event["symbol"],
        "universe": event["settings_snapshot"]["alert_universe_mode"],
        "direction": event["direction"],
        "strength": event["strength"],
    }


def cohort_rows(
    store: Store,
    groups: tuple[str, ...] = (
        "detector",
        "profile",
        "version",
        "settings",
        "market",
        "timeframe",
        "direction",
    ),
    *,
    cutoff_ms: int | None = None,
    start_ms: int = 0,
    end_ms: int = 9_999_999_999_999_999,
    detector: str | None = None,
    version: str | None = None,
    market: str | None = None,
) -> Any:
    """SQLite sorts/aggregates on disk; the exporter never collects raw returns in memory."""
    if not groups or len(set(groups)) != len(groups) or any(g not in GROUPS for g in groups):
        raise ValueError("Invalid analytics grouping")
    args: list[Any] = []
    definitions = []
    for kind, name in (("outcome", "o"), ("comparison", "c")):
        if cutoff_ms is None:
            prefix = PREFIX + kind + "-head:"
            if kind in ("comparison", "outcome"):
                definitions.append(
                    f"{name} AS (SELECT COALESCE(v.payload_json,h.payload_json) AS payload_json "
                    "FROM runtime h LEFT JOIN runtime v ON v.key='"
                    + PREFIX
                    + "' || json_extract(cw_record_json(h.payload_json),'$.revision_key') "
                    "WHERE h.key>=? AND h.key<?)"
                )
            else:
                definitions.append(f"{name} AS (SELECT payload_json FROM runtime WHERE key>=? AND key<?)")
            args += [prefix, prefix + "~"]
        else:
            prefix = PREFIX + kind + ":"
            definitions += [
                f"{name}v AS (SELECT payload_json, ROW_NUMBER() OVER (PARTITION BY json_extract(cw_record_json(payload_json),'$.{kind}_id') ORDER BY json_extract(cw_record_json(payload_json),'$.revision') DESC) AS pos FROM runtime WHERE key>=? AND key<? AND json_extract(cw_record_json(payload_json),'$.available_at_ms')<=?)",
                f"{name} AS (SELECT payload_json FROM {name}v WHERE pos=1)",
            ]
            args += [prefix, prefix + "~", cutoff_ms]
    paths = {
        "detector": "detector_id",
        "version": "detector_version",
        "market": "market_type",
        "timeframe": "timeframe",
        "instrument": "symbol",
        "direction": "direction",
        "strength": "strength",
        "universe": "settings_snapshot.alert_universe_mode",
        "profile": "settings_snapshot.profile",
    }
    fields = []
    for group in groups:
        expression = (
            "json_extract(cw_record_json(e.payload_json),'$.settings_revision')"
            if group == "settings"
            else f"json_extract(cw_record_json(e.payload_json),'$.{paths[group]}')"
        )
        # Settings revision uniquely identifies the immutable owner snapshot in this database.
        fields += [f"'{group}'", expression]
    fields += [
        "'horizon_min'",
        "json_extract(cw_record_json(o.payload_json),'$.horizon_min')",
        "'methodology_version'",
        "json_extract(cw_record_json(o.payload_json),'$.methodology_version')",
    ]
    terms = [
        "json_extract(cw_record_json(o.payload_json),'$.state')='COMPLETE'",
        "json_extract(cw_record_json(e.payload_json),'$.market_observation_ms')>=?",
        "json_extract(cw_record_json(e.payload_json),'$.market_observation_ms')<?",
    ]
    args += [start_ms, end_ms]
    if cutoff_ms is not None:
        terms.append("json_extract(cw_record_json(e.payload_json),'$.available_at_ms')<=?")
        args.append(cutoff_ms)
    for field, value in (("detector_id", detector), ("detector_version", version), ("market_type", market)):
        if value is not None:
            terms.append(f"json_extract(cw_record_json(e.payload_json),'$.{field}')=?")
            args.append(value)
    definitions += [
        "base AS (SELECT json_object(" + ",".join(fields) + ") AS group_key, "
        "COALESCE(json_extract(cw_record_json(o.payload_json),'$.metrics.directed_return_pct'),json_extract(cw_record_json(o.payload_json),'$.metrics.price_return_pct')) AS value, "
        "json_extract(cw_record_json(o.payload_json),'$.classification') AS class, json_extract(cw_record_json(o.payload_json),'$.noise_threshold_pct') AS noise, "
        "json_extract(cw_record_json(o.payload_json),'$.event_id') AS event_id, json_extract(cw_record_json(o.payload_json),'$.horizon_min') AS horizon "
        "FROM o JOIN runtime e ON e.key='"
        + PREFIX
        + "event:' || json_extract(cw_record_json(o.payload_json),'$.event_id') || ':' || printf('%010d',json_extract(cw_record_json(o.payload_json),'$.event_revision')) WHERE "
        + " AND ".join(terms)
        + ")",
        "ordered AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY group_key ORDER BY value) AS rn, COUNT(*) OVER (PARTITION BY group_key) AS n FROM base)",
        "stats AS (SELECT group_key,COUNT(*) AS n,MIN(noise) AS noise_min,MAX(noise) AS noise_max,COUNT(noise) AS noise_n,AVG(value) AS mean,MIN(value) AS minimum,MAX(value) AS maximum,AVG(CASE WHEN rn IN ((n+1)/2,(n+2)/2) THEN value END) AS median,MAX(CASE WHEN rn=(n+9)/10 THEN value END) AS p10,MAX(CASE WHEN rn=(n*9+9)/10 THEN value END) AS p90 FROM ordered GROUP BY group_key)",
        "classes AS (SELECT group_key,class,COUNT(*) AS n FROM base GROUP BY group_key,class)",
        "class_json AS (SELECT group_key,json_group_object(class,n) AS counts FROM classes GROUP BY group_key)",
        "comparators AS (SELECT b.group_key,COUNT(*) AS n,AVG(COALESCE(json_extract(s.value,'$.outcomes.' || json_quote(CAST(b.horizon AS TEXT)) || '.metrics.directed_return_pct'),json_extract(s.value,'$.outcomes.' || json_quote(CAST(b.horizon AS TEXT)) || '.metrics.price_return_pct'))) AS mean FROM base b JOIN c ON json_extract(cw_record_json(c.payload_json),'$.event_id')=b.event_id JOIN json_each(cw_record_json(c.payload_json),'$.samples') s GROUP BY b.group_key)",
    ]
    if store.bounded and cutoff_ms is None:
        from .quality_summary import union_sql

        summary_sql, summary_args = union_sql(groups, start_ms, end_ms, detector, version, market)
        for index, definition in enumerate(definitions):
            if definition.startswith("base AS ("):
                definitions[index] = definition[:-1] + " UNION ALL " + summary_sql + ")"
            elif definition.startswith("c AS ("):
                definitions[index] = (
                    definition[:-1]
                    + " UNION ALL "
                    + (
                        "SELECT json_object('event_id','summary:'||q.day||':'||q.cohort||':'||q.chunk||':'||s.key,"
                        "'samples',json((SELECT json_group_array(json_object('outcomes',json_object(CAST(json_extract(q.dimensions,'$.horizon_min') AS TEXT),json_object('metrics',json(m.value))))) FROM json_each(s.value,'$.comparator_metrics') m))) AS payload_json "
                        "FROM market_quality q JOIN json_each(cw_quality_json(q.facts)) s)"
                    )
                )
        args.extend(summary_args)
    query = (
        "WITH "
        + ",".join(definitions)
        + " SELECT stats.*,class_json.counts,comparators.n AS cn,comparators.mean AS cm FROM stats JOIN class_json USING(group_key) LEFT JOIN comparators USING(group_key) ORDER BY group_key"
    )
    for row in store.db.execute(query, args):
        yield {
            "group": json.loads(row["group_key"]),
            "sample_size": row["n"],
            "return_unit": "%",
            "mean": row["mean"],
            "median": row["median"],
            "min": row["minimum"],
            "max": row["maximum"],
            "p10": row["p10"],
            "p90": row["p90"],
            "comparator_sample_size": row["cn"] or 0,
            "comparator_mean": row["cm"],
            "classifications": json.loads(row["counts"]),
            "noise_min_pct": row["noise_min"],
            "noise_max_pct": row["noise_max"],
            "noise_sample_size": row["noise_n"],
        }


def cohorts(
    store: Store,
    groups: tuple[str, ...] = (
        "detector",
        "profile",
        "version",
        "settings",
        "market",
        "timeframe",
        "direction",
    ),
    *,
    cutoff_ms: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    if limit < 1:
        raise ValueError("Invalid cohort limit")
    from .quality_summary import period_metadata

    records: list[dict[str, Any]] = []
    truncated = False
    for row in cohort_rows(store, groups, cutoff_ms=cutoff_ms):
        if len(records) == limit:
            truncated = True
            break
        records.append(row)
    # Summary counts need only projected state; never expand every candle window.
    if cutoff_ms is None:
        incomplete = store.db.execute(
            "SELECT COUNT(*) FROM runtime WHERE key>=? AND key<? AND json_extract(cw_record_json(payload_json),'$.state')!='COMPLETE'",
            (PREFIX + "outcome-head:", PREFIX + "outcome-head:~"),
        ).fetchone()[0]
    else:
        incomplete = store.db.execute(
            "WITH revisions AS (SELECT json_extract(cw_record_json(payload_json),'$.state') AS state, "
            "ROW_NUMBER() OVER (PARTITION BY json_extract(cw_record_json(payload_json),'$.outcome_id') "
            "ORDER BY json_extract(cw_record_json(payload_json),'$.revision') DESC) AS pos FROM runtime "
            "WHERE key>=? AND key<? AND json_extract(cw_record_json(payload_json),'$.available_at_ms')<=?) "
            "SELECT COUNT(*) FROM revisions WHERE pos=1 AND state!='COMPLETE'",
            (PREFIX + "outcome:", PREFIX + "outcome:~", cutoff_ms),
        ).fetchone()[0]
    if store.bounded and cutoff_ms is None:
        incomplete += store.db.execute(
            "SELECT COUNT(*) FROM runtime j WHERE j.key>=? AND j.key<? AND NOT EXISTS (SELECT 1 FROM runtime h WHERE h.key=?||json_extract(cw_record_json(j.payload_json),'$.outcome_id'))",
            (PREFIX + "outcome-job:", PREFIX + "outcome-job:~", PREFIX + "outcome-head:"),
        ).fetchone()[0]
        incomplete += store.db.execute(
            "SELECT COUNT(*) FROM market_quality q JOIN json_each(cw_quality_json(q.facts)) s WHERE json_extract(s.value,'$.state')!='COMPLETE'"
        ).fetchone()[0]
    return {
        "schema": "cryptowatcher.analytics.v1",
        "selection_period": period_metadata(0, 9_999_999_999_999_999) if store.bounded else {},
        "groups": list(groups),
        "cohorts": records,
        "incomplete_outcomes": incomplete,
        "cohort_limit": limit,
        "truncated": truncated,
        "limitations": [
            "Descriptive detector informativeness; not Strategy profitability",
            "Repeated events and cross-event comparator overlap may correlate samples",
            "Small N and incomplete prior source history constrain interpretation",
        ],
        "cutoff_ms": cutoff_ms,
        "history_period": store.get_runtime("history_generation", {"state": "LEGACY"}),
        "detail_retention_days": store.snapshot().settings.detail_retention_days,
        "summary_retention_months": 12,
        "old_detail_available": False if store.bounded else None,
        "summary_period_resolution": "UTC_DAY" if store.bounded else None,
        "historical_cutoff_limit": "Expired detail cannot reconstruct old decision cutoff"
        if store.bounded and cutoff_ms is not None
        else None,
    }
