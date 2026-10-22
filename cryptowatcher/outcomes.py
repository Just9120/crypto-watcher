"""Immutable full-window outcomes and restartable, bounded analytics admission."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import asdict
from typing import Any, Callable

from .config import OwnerSettings
from .events import PREFIX, History, identity
from .sources import Candle, Sources
from .storage import StateError, Store, encode, json_payload

MINUTE = 60_000
DAY = 86_400_000
METHOD = "1"
DEFINITION = {
    "version": METHOD,
    "reference": "immutable event observation time and reference close",
    "window": "complete subsequent closed 1m candles; exact horizon end",
    "return": "100*(target_close/reference_price-1)",
    "directed_return": "return*direction_sign; neutral has no directed labels",
    "excursion": "MFE>=0/MAE<=0 from full-window high/low; neutral max_rise/max_fall/range",
    "classification": "directed return beyond frozen event noise threshold; missing threshold UNCLASSIFIED",
}


def read_candles(store: Store, stream: str, start: int, end: int) -> list[Candle]:
    frozen = store.bounded and store.readonly and store.db.in_transaction
    key = (stream, start, end, store.db.execute("PRAGMA data_version").fetchone()[0]) if frozen else None
    if key is not None and key in store.analytics_ranges:
        cached = store.analytics_ranges.pop(key)
        store.analytics_ranges[key] = cached
        return list(cached[0])
    if store.bounded:
        from .market_storage import read

        result = [Candle(**v) for v in read(store.db, stream, start, end, cache=store.cached_bytes)]
        if key is not None and len(result) <= 4096:
            import sys

            size = (
                len(stream.encode())
                + 256
                + sys.getsizeof(tuple(result))
                + sum(
                    sys.getsizeof(c)
                    + sys.getsizeof(vars(c))
                    + sum(sys.getsizeof(v) for v in vars(c).values())
                    for c in result
                )
            )
            if size <= 4 * 1024 * 1024:
                while store.analytics_ranges and store.analytics_range_bytes + size > 4 * 1024 * 1024:
                    removed = store.analytics_ranges.pop(next(iter(store.analytics_ranges)))
                    store.analytics_range_bytes -= removed[1]
                store.analytics_ranges[key] = (tuple(result), size)
                store.analytics_range_bytes += size
        return result
    prefix = Sources.prefix(stream)
    rows = store.db.execute(
        "SELECT payload_json FROM runtime WHERE key>=? AND key<? ORDER BY key",
        (prefix + f"{start:016d}", prefix + f"{end:016d}"),
    )
    result = []
    for row in rows:
        candle = Candle(**json_payload(row[0]))
        if candle != Candle.parse(
            candle.row(), stream.rsplit("/", 1)[1], candle.source, candle.received_ms, candle.server_ms
        ):
            raise StateError("Invalid analytics source")
        result.append(candle)
    return result


def missing_ranges(candles: list[Candle], start: int, end: int) -> list[tuple[int, int]]:
    gaps = []
    cursor = start
    for candle in sorted(candles, key=lambda c: c.start):
        if candle.step != MINUTE or candle.start < cursor or candle.end > end:
            raise ValueError("Invalid outcome window")
        if candle.start > cursor:
            gaps.append((cursor, candle.start))
        cursor = candle.end
    if cursor < end:
        gaps.append((cursor, end))
    return gaps


def calculate(
    event: dict[str, Any], horizon: int, candles: list[Candle], *, include_source_inputs: bool = True
) -> dict[str, Any]:
    if type(horizon) is not int or horizon <= 0:
        raise ValueError("Invalid outcome horizon")
    start = event["reference_time_ms"]
    reference = event["reference_price"]
    if (
        type(start) is not int
        or start < 0
        or not isinstance(reference, (int, float))
        or isinstance(reference, bool)
        or not math.isfinite(reference)
        or reference <= 0
        or event["direction"] not in ("UP", "DOWN", "NEUTRAL")
    ):
        raise ValueError("Invalid immutable outcome reference")
    end = start + horizon * MINUTE
    candles = sorted(candles, key=lambda c: c.start)
    gaps = missing_ranges(candles, start, end)
    base: dict[str, Any] = {
        "state": "INCOMPLETE" if gaps else "COMPLETE",
        "expected_candles": horizon,
        "observed_candles": len(candles),
        "missing_ranges": gaps,
        "window_start_ms": start,
        "window_end_ms": end,
        "reference_price": event["reference_price"],
        "reference_time_ms": start,
        "metrics": {},
        "units": {},
        "classification": "UNCLASSIFIED",
        "classification_reason": "INCOMPLETE_WINDOW" if gaps else "NEUTRAL_EVENT",
        "noise_threshold_pct": event.get("continuation_threshold_pct"),
        "source_inputs": [],
    }
    if gaps:
        return base
    price_return = 100 * (candles[-1].close / reference - 1)
    rise = 100 * (max(c.high for c in candles) / reference - 1)
    fall = 100 * (min(c.low for c in candles) / reference - 1)
    metrics = {"price_return_pct": price_return}
    if event["direction"] == "NEUTRAL":
        metrics.update(max_rise_pct=max(0, rise), max_fall_pct=min(0, fall), range_pct=rise - fall)
    else:
        sign = 1 if event["direction"] == "UP" else -1
        directed = price_return * sign
        metrics.update(
            directed_return_pct=directed,
            mfe_pct=max(0, rise if sign == 1 else -fall),
            mae_pct=min(0, fall if sign == 1 else -rise),
        )
        noise = event.get("continuation_threshold_pct")
        if isinstance(noise, (int, float)) and math.isfinite(noise) and noise >= 0:
            base["classification"] = (
                "CONTINUATION"
                if directed > noise
                else "REVERSAL"
                if directed < -noise
                else "NEUTRAL_MOVEMENT"
            )
            base["classification_reason"] = "FROZEN_EVENT_THRESHOLD"
        else:
            base["classification_reason"] = "MISSING_REFERENCE_CONTEXT"
    if not all(math.isfinite(v) for v in metrics.values()):
        raise ValueError("Non-finite outcome metric")
    base.update(
        metrics=metrics,
        units={key: "%" for key in metrics},
        source_inputs=[asdict(c) for c in candles] if include_source_inputs else [],
    )
    return base


def protect(history: History, stream: str, candles: list[dict[str, Any]]) -> None:
    if history.store.bounded:
        # Normalized windows lease only the exact used revisions; no per-candle
        # permanent runtime markers are written by the bounded layout.
        return
    for candle in candles:
        history.store.db.execute(
            "INSERT OR IGNORE INTO runtime VALUES (?,?)",
            (
                PREFIX + "protected:" + Sources.prefix(stream) + f"{candle['start']:016d}",
                encode({"retained": True}),
            ),
        )


def pending_key(due: int, outcome_id: str) -> str:
    # Numeric ordering also holds for a valid, exceptionally long owner horizon.
    value = str(due)
    return f"outcome-pending:{len(value):04d}:{value}:{outcome_id}"


class OutcomeWorker:
    def __init__(
        self,
        store: Store,
        request_gap: Callable[[str, int, int], str | None] | None = None,
        *,
        on_outcome: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.store = store
        self.history = History(store)
        self.request_gap = request_gap
        self.on_outcome = on_outcome
        with store.db:
            previous = self.history.get("outcome-method:" + METHOD)
            if previous is not None and previous != DEFINITION:
                raise StateError("Outcome methodology changed without a new version")
            if previous is None:
                self.history.put("outcome-method:" + METHOD, DEFINITION)

    def admit(self, event: dict[str, Any]) -> None:
        """Called inside the event producer's or initial-admission transaction."""
        settings = OwnerSettings.from_payload(event["settings_snapshot"])
        native = f"{event['market_type']}/{event['symbol']}/{event['timeframe']}"
        stream = native.rsplit("/", 1)[0] + "/1m"
        protect(self.history, native, event["source_inputs"])
        for horizon in settings.outcome_horizons:
            outcome_id = identity(f"outcome/{event['event_id']}/{horizon}/{METHOD}")
            key = "outcome-job:" + outcome_id
            if self.history.get(key) is not None or (
                self.store.bounded
                and self.history.get("outcome-head:" + outcome_id, {}).get("state") == "COMPLETE"
            ):
                continue
            due = event["reference_time_ms"] + horizon * MINUTE
            pending = pending_key(due, outcome_id)
            hold = f"hold:{stream}:{outcome_id}"
            job = {
                "outcome_id": outcome_id,
                "event_id": event["event_id"],
                "event_revision": event["revision"],
                "horizon_min": horizon,
                "methodology_version": METHOD,
                "stream": stream,
                "state": "PENDING",
                "next_attempt_ms": due,
                "due_ms": due,
                "pending_key": pending,
                "hold_key": hold,
            }
            self.history.put(key, job)
            from .queue_health import arrival

            arrival(self.store, "outcome")
            self.history.put(pending, {"outcome_id": outcome_id})
            self.history.put(hold, {"start": event["reference_time_ms"], "end": due})
        comparison = "comparison-job:" + event["event_id"]
        from .analytics import COMPARATOR_VERSION

        comparison_id = identity(f"comparison/{event['event_id']}/{COMPARATOR_VERSION}")
        if self.history.get(comparison) is None and not (
            self.store.bounded
            and self.history.get("comparison-head:" + comparison_id, {}).get("state")
            in ("COMPLETE", "COMPLETE_LIMITED")
        ):
            comparison_pending = pending_key(event["available_at_ms"], event["event_id"]).replace(
                "outcome-pending:", "comparison-pending:", 1
            )
            self.history.put(
                comparison,
                {
                    "event_id": event["event_id"],
                    "event_revision": event["revision"],
                    "state": "PENDING",
                    "methodology_version": COMPARATOR_VERSION,
                    "pending_key": comparison_pending,
                    "next_attempt_ms": event["available_at_ms"],
                    "due_ms": event["available_at_ms"],
                },
            )
            self.history.put(comparison_pending, {"event_id": event["event_id"]})
            from .queue_health import arrival

            arrival(self.store, "comparison")
            lookback = max(
                16,
                int(max(v for k, v in event["effective_parameters"].items() if k.endswith("lookback"))) + 2,
            )
            for held_stream in (native, stream):
                self.history.put(
                    f"hold:{held_stream}:comparison:{event['event_id']}",
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

    def bootstrap(self, batch: int = 20) -> None:
        admission = self.history.get("outcome-admission", {"cursor": "", "complete": False})
        if admission["complete"]:
            return
        from .packet_store import atomic

        if self.store.db.in_transaction:
            with atomic(self.store.db):
                self._bootstrap(batch)
        else:
            with self.store.db:
                self.store.db.execute("BEGIN IMMEDIATE")
                with atomic(self.store.db):
                    self._bootstrap(batch)

    def _bootstrap(self, batch: int) -> None:
        # Read cursor, frozen revisions and admit jobs in the same writer scope.
        # A caller's transaction is neither committed nor rolled back here.
        admission = self.history.get("outcome-admission", {"cursor": "", "complete": False})
        if admission["complete"]:
            return
        prefix = PREFIX + "history:"
        rows = list(
            self.store.scan_runtime(
                prefix, after=admission["cursor"] or prefix, before=prefix + "~", limit=batch
            )
        )
        for row in rows:
            event_id = row[1]["event_id"]
            self.admit(self.history.get("event:" + event_id + ":0000000001"))
            admission["cursor"] = row[0]
        admission["complete"] = len(rows) < batch
        self.history.put("outcome-admission", admission)

    def work(self, now_ms: int, batch: int = 20) -> None:
        self.bootstrap(batch)
        for pending, binding in self.history.items("outcome-pending:", batch):
            job = self.history.get("outcome-job:" + binding["outcome_id"])
            if job is None:
                with self.store.db:
                    self.history.delete(pending)
                continue
            if job["next_attempt_ms"] > now_ms:
                break
            event = self.history.get(f"event:{job['event_id']}:{job['event_revision']:010d}")
            self.apply(job, event, binding, self.calculate(self.store, job, event), now_ms)

    @staticmethod
    def calculate(store: Store, job: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
        if job.get("methodology_version", "1") != METHOD:
            return {
                "state": "INVALID_SOURCE",
                "metrics": {},
                "source_inputs": [],
                "reason": "OUTCOME_METHODOLOGY_UNAVAILABLE",
            }
        start = event["reference_time_ms"]
        end = start + job["horizon_min"] * MINUTE
        try:
            return calculate(event, job["horizon_min"], read_candles(store, job["stream"], start, end))
        except (ValueError, TypeError, KeyError, OverflowError, StateError) as exc:
            return {
                "state": "INVALID_SOURCE",
                "metrics": {},
                "source_inputs": [],
                "reason": type(exc).__name__,
            }

    async def async_work(self, clock: Callable[[], int], batch: int = 256, seconds: float = 2) -> int:
        from .analytics_worker import BatchTiming, prepare, read_snapshot

        self.bootstrap(20)
        now_ms = clock()
        started = time.monotonic()
        timing = BatchTiming()
        prepared = await asyncio.to_thread(
            read_snapshot,
            self.store.path,
            self.store.owner_id,
            lambda store: timing.read(
                lambda: prepare(
                    store,
                    "outcome-pending:",
                    "outcome-job:",
                    "outcome_id",
                    now_ms,
                    batch,
                    seconds,
                    self.calculate,
                )
            ),
        )
        read_seconds = time.monotonic() - started
        applied = 0
        completed_lags = []
        for job, event, binding, result in prepared:
            # Owner operations or a restarted worker cannot be overwritten by an old read.
            if self.history.get("outcome-job:" + job["outcome_id"]) == job:
                with timing.applying():
                    self.apply(job, event, binding, result, clock())
                applied += 1
                if result["state"] == "COMPLETE":
                    completed_lags.append(
                        max(
                            0,
                            clock()
                            - job.get("due_ms", event["reference_time_ms"] + job["horizon_min"] * MINUTE),
                        )
                    )
            await asyncio.sleep(0)
        from .queue_health import completed_batch

        completed_batch(
            self.store,
            "outcome",
            clock(),
            completed_lags,
            time.monotonic() - started,
            read_seconds=read_seconds,
            timing=timing,
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
        reason = None
        if result["state"] == "INCOMPLETE" and self.request_gap is not None:
            for missing_start, missing_end in result["missing_ranges"]:
                reason = self.request_gap(job["stream"], missing_start, missing_end)
        if reason:
            result["recovery_reason"] = reason
        result.update(
            schema="cryptowatcher.outcome.v1",
            outcome_id=job["outcome_id"],
            event_id=job["event_id"],
            event_revision=job["event_revision"],
            horizon_min=job["horizon_min"],
            methodology_version=job.get("methodology_version", "1"),
        )
        previous = self.history.get("outcome-head:" + job["outcome_id"])
        changed = previous is None or encode(result) != encode(
            {k: v for k, v in previous.items() if k not in ("revision", "available_at_ms")}
        )
        with self.store.db:
            if changed:
                result.update(
                    revision=1 if previous is None else previous["revision"] + 1, available_at_ms=now_ms
                )
                self.history.version("outcome", result)
                if self.on_outcome is not None:
                    self.on_outcome(result)
            self.history.delete(job["pending_key"])
            if result["state"] == "COMPLETE":
                protect(self.history, job["stream"], result["source_inputs"])
                self.history.delete(job["hold_key"])
                job.update(state="COMPLETE", completed_ms=now_ms)
            else:
                job.update(state=result["state"], next_attempt_ms=now_ms + MINUTE)
                job["pending_key"] = pending_key(job["next_attempt_ms"], job["outcome_id"])
                self.history.put(job["pending_key"], binding)
            if self.store.bounded and result["state"] == "COMPLETE":
                self.history.delete("outcome-job:" + job["outcome_id"])
            else:
                self.history.put("outcome-job:" + job["outcome_id"], job)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "methodology_version": METHOD,
            "pending": self.history.count("outcome-pending:"),
            "comparison_pending": self.history.count("comparison-pending:"),
            "admission": self.history.get("outcome-admission", {"complete": False}),
        }


class Retention:
    def __init__(self, store: Store) -> None:
        self.store, self.history = store, History(store)

    def work(self, now_ms: int, batch: int = 500) -> int:
        if self.store.bounded:
            from .storage_lifecycle import BoundedRetention

            return BoundedRetention(self.store).work(now_ms, min(batch, 200))
        if not self.history.get("outcome-admission", {}).get("complete"):
            return 0
        state = self.history.get("retention", {"cursor": ""})
        prefix = "source:v1:candle:"
        rows = self.store.db.execute(
            "SELECT key,payload_json FROM runtime WHERE key>? AND key<? ORDER BY key LIMIT ?",
            (state["cursor"] or prefix, prefix + "~", batch),
        ).fetchall()
        removed = 0
        holds: dict[str, list[dict[str, int]]] = {}
        with self.store.db:
            for row in rows:
                state["cursor"] = row[0]
                candle = Candle(**json_payload(row[1]))
                if candle.end >= now_ms - 90 * DAY or self.history.get("protected:" + row[0]):
                    continue
                stream = row[0][len(prefix) :].rsplit(":", 1)[0]
                if stream not in holds:
                    holds[stream] = [v for _, v in self.history.items("hold:" + stream + ":")]
                if any(candle.start < h["end"] and candle.end > h["start"] for h in holds[stream]):
                    continue
                self.store.db.execute("DELETE FROM runtime WHERE key=?", (row[0],))
                removed += 1
            if len(rows) < batch:
                state["cursor"] = ""
            state.update(last_checked_ms=now_ms, last_removed=removed)
            self.history.put("retention", state)
        return removed
