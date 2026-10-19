"""Atomic versioned event history, episode lifecycle and independent delivery decisions."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import asdict
from typing import Any, Callable, Iterator

from .detector_config import FAMILIES, effective_parameters
from .detectors import FORMULAS, UNITS, VERSIONS, Detection, DetectorUnavailable, atr14, evaluate, freshness
from .history_codec import Codec
from .sources import Candle
from .storage import RUNTIME_UPSERT, StateError, Store, encode

PREFIX = "radar:v1:"
HEAD_SCHEMA = "cryptowatcher.history-head.v1"
DECISION_HEAD = "_cw_decision_revision"
# SQL summaries use these small fields; public readers dereference the immutable record.
HEAD_FIELDS = {
    "event": (
        "event_id",
        "revision",
        "episode_id",
        "market_observation_ms",
        "available_at_ms",
        "market_type",
        "detector_id",
        "detector_version",
        "symbol",
        "timeframe",
        "direction",
        "strength",
    ),
    "outcome": (
        "outcome_id",
        "revision",
        "event_id",
        "event_revision",
        "available_at_ms",
        "state",
        "horizon_min",
        "methodology_version",
        "classification",
        "noise_threshold_pct",
        "metrics",
    ),
    "episode": (
        "episode_id",
        "revision",
        "instrument",
        "direction",
        "state",
        "started_ms",
        "last_observation_ms",
        "longest_tf_ms",
        "strength",
        "available_at_ms",
    ),
    "comparison": ("comparison_id", "revision", "event_id", "available_at_ms", "state"),
}


def identity(value: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "cryptowatcher/" + value))


def event_identity(stream: str, family: str, version: str, settings_revision: int, candle_start: int) -> str:
    return identity(f"{stream}/{family}/{version}/{settings_revision}/{candle_start}")


class History:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.codec = Codec(store.db, binary_cache=store.cached_bytes)

    def get(self, key: str, default: Any = None) -> Any:
        return self.resolve(key, self.store.get_runtime(PREFIX + key, default))

    def metadata(self, key: str) -> Any:
        """Stored results/provenance only; does not assert raw-window availability."""
        return self.resolve(
            key,
            self.store.get_runtime(PREFIX + key),
            omit_fields=frozenset({"source_inputs", "future_inputs", "source_context", "enrichment"}),
        )

    def resolve(self, key: str, value: Any, *, omit_fields: frozenset[str] = frozenset()) -> Any:
        """Read both legacy full heads and identity-checked compact heads."""
        if isinstance(value, dict) and DECISION_HEAD in value:
            revision = value[DECISION_HEAD]
            if (
                set(value) != {DECISION_HEAD}
                or type(revision) is not int
                or revision < 1
                or not re.fullmatch(r"decision:telegram:[0-9a-f-]{36}", key)
            ):
                raise StateError("Invalid decision head reference")
            value = self.store.get_runtime(f"{PREFIX}{key}:{revision:010d}")
            if (
                not isinstance(value, dict)
                or value.get("revision") != revision
                or value.get("event_id") != key.rsplit(":", 1)[1]
                or DECISION_HEAD in value
            ):
                raise StateError("Missing or conflicting decision revision")
        value = self.codec.unpack(value, omit_fields=omit_fields)
        if self.store.bounded:
            value = self._expand_services(value)
        if not isinstance(value, dict) or value.get("schema") != HEAD_SCHEMA:
            return value
        kind, separator, record_id = key.partition("-head:")
        revision = value.get("revision")
        if (
            not separator
            or kind not in HEAD_FIELDS
            or type(revision) is not int
            or revision < 1
            or value.get(f"{kind}_id") != record_id
            or value.get("revision_key") != f"{kind}:{record_id}:{revision:010d}"
        ):
            raise StateError("Invalid history head reference")
        record = self.codec.unpack(
            self.store.get_runtime(PREFIX + value["revision_key"]), omit_fields=omit_fields
        )
        if (
            not isinstance(record, dict)
            or record.get("schema") == HEAD_SCHEMA
            or record.get(f"{kind}_id") != record_id
            or record.get("revision") != revision
            or encode(self._head(kind, record, value["revision_key"], bounded=self.store.bounded))
            != encode(value)
        ):
            raise StateError("Missing or conflicting history head revision")
        return record

    @staticmethod
    def _head(
        kind: str, value: dict[str, Any], revision_key: str, *, bounded: bool = False
    ) -> dict[str, Any]:
        if kind not in HEAD_FIELDS:
            return value
        return {
            "schema": HEAD_SCHEMA,
            "revision_key": revision_key,
            **{
                field: value[field]
                for field in HEAD_FIELDS[kind]
                if field in value
                and not (
                    bounded
                    and kind == "outcome"
                    and field in ("metrics", "classification", "noise_threshold_pct")
                )
            },
        }

    def _freeze_services(self, value: Any, depth: int = 0) -> Any:
        if depth > 24:
            raise StateError("Service snapshot nesting exceeds bound")
        if isinstance(value, list):
            return [self._freeze_services(v, depth + 1) for v in value]
        if not isinstance(value, dict):
            return value
        for kind in ("event", "outcome", "episode", "comparison"):
            if (
                value.get("schema") == f"cryptowatcher.{kind}.v1"
                and f"{kind}_id" in value
                and type(value.get("revision")) is int
            ):
                key = f"{kind}:{value[f'{kind}_id']}:{value['revision']:010d}"
                saved = self.get(key)
                if saved is not None and encode(saved) == encode(value):
                    return {"_cw_revision": key, "sha256": hashlib.sha256(encode(value).encode()).hexdigest()}
        return {k: self._freeze_services(v, depth + 1) for k, v in value.items()}

    def _expand_services(self, value: Any, depth: int = 0) -> Any:
        if depth > 24:
            raise StateError("Service snapshot nesting exceeds bound")
        if isinstance(value, list):
            return [self._expand_services(v, depth + 1) for v in value]
        if not isinstance(value, dict):
            return value
        if "_cw_revision" in value:
            if (
                set(value) != {"_cw_revision", "sha256"}
                or not isinstance(value["_cw_revision"], str)
                or not re.fullmatch(
                    r"(?:event|outcome|episode|comparison):[a-zA-Z0-9-]+:[0-9]{10}", value["_cw_revision"]
                )
            ):
                raise StateError("Invalid service revision reference")
            raw = self.store.get_runtime(PREFIX + value["_cw_revision"])
            if raw is None:
                raise StateError("Missing retained service revision")
            restored = self.codec.unpack(raw)
            if hashlib.sha256(encode(restored).encode()).hexdigest() != value["sha256"]:
                raise StateError("Service revision hash mismatch")
            return self._expand_services(restored, depth + 1)
        return {k: self._expand_services(v, depth + 1) for k, v in value.items()}

    def put(self, key: str, value: Any) -> None:
        from .storage_lifecycle import TERMINAL_DELIVERY

        if (
            self.store.bounded
            and isinstance(value, dict)
            and key.startswith(("agent:outbox:", "outbox:telegram:", "digest-outbox:"))
            and value.get("state") in TERMINAL_DELIVERY
        ):
            parent = (
                value.get("event_id")
                or value.get("intent", {}).get("event_id")
                or next(iter(value.get("event_ids", [])), None)
            )
            value = {
                k: v
                for k, v in value.items()
                if k not in ("intent", "body", "episode_snapshot", "snapshot", "text")
            }
            if parent:
                value["event_id"] = parent
        if self.store.bounded and key.startswith(("agent:", "outbox:")):
            value = self._freeze_services(value)
        # Transaction is owned by the engine/delivery operation, never commit nested writes.
        plain = encode(value)
        payload: str | bytes = plain
        if self.store.bounded:
            from .record_codec import pack_binary

            compact = pack_binary(plain)
            if len(compact) < len(plain.encode()) or '"source-cells"' in plain:
                payload = compact
        from .catalogue_write import mutation

        with mutation(self.store, PREFIX + key, value) as write:
            if not write:
                return
            self.store.db.execute(RUNTIME_UPSERT, (PREFIX + key, payload))
            if self.store.bounded:
                from .storage_lifecycle import track

                track(self.store, PREFIX + key, value)
                if key.startswith(("outcome-job:", "comparison-job:")):
                    from .queue_health import sync_job

                    sync_job(self, key, value)

    def delete(self, key: str) -> None:
        from .catalogue_write import deletion

        with deletion(self.store, PREFIX + key):
            if self.store.bounded:
                from .storage_lifecycle import forget

                forget(self.store, PREFIX + key)
                if key.startswith(("outcome-job:", "comparison-job:")):
                    from .queue_health import sync_job

                    sync_job(self, key, None)
            self.store.db.execute("DELETE FROM runtime WHERE key=?", (PREFIX + key,))

    def items(self, prefix: str, limit: int | None = None, *, reverse: bool = False) -> list[tuple[str, Any]]:
        return [
            (key[len(PREFIX) :], self.resolve(key[len(PREFIX) :], value))
            for key, value in self.store.scan_runtime(PREFIX + prefix, limit, reverse=reverse)
        ]

    def scan(self, prefix: str) -> Iterator[tuple[str, Any]]:
        for key, value in self.store.scan_runtime(PREFIX + prefix):
            yield key[len(PREFIX) :], self.resolve(key[len(PREFIX) :], value)

    def count(self, prefix: str) -> int:
        return self.store.count_runtime(PREFIX + prefix)

    def version(self, kind: str, value: dict[str, Any]) -> None:
        from .catalogue_write import scope

        with scope(self.store):
            self._version(kind, value)

    def _version(self, kind: str, value: dict[str, Any]) -> None:
        key = value[f"{kind}_id"]
        version_key = f"{kind}:{key}:{value['revision']:010d}"
        existing = self.get(version_key)
        if existing is not None and encode(existing) != encode(value):
            raise StateError("Conflicting immutable history revision")
        head = self.metadata(f"{kind}-head:{key}")
        if (
            kind == "event"
            and head
            and any(value[k] != head[k] for k in ("reference_price", "reference_time_ms"))
        ):
            raise StateError("Event outcome reference is immutable")
        if existing is None:
            if self.store.bounded and kind == "event" and value["revision"] == 1:
                self.put("event-counter", self.get("event-counter", 0) + 1)
            if self.store.bounded and kind in ("event", "outcome", "comparison"):
                event = (
                    value
                    if kind == "event"
                    else self.metadata(f"event:{value['event_id']}:{value['event_revision']:010d}")
                )
                self.codec.source_stream = f"{event['market_type']}/{event['symbol']}/" + (
                    "1m" if kind == "outcome" else event["timeframe"]
                )
                self.codec.source_expiry = event["market_observation_ms"]
            try:
                self.put(version_key, self.codec.pack(value))
            finally:
                self.codec.source_stream = None
        if head is None or value["revision"] > head["revision"]:
            self.put(f"{kind}-head:{key}", self._head(kind, value, version_key, bounded=self.store.bounded))

    def prune_pending(self, limit: int = 64) -> int:
        """Caller owns the transaction. A missing job can never be retried."""
        if not self.store.bounded:
            return 0
        counts = self.get("orphaned-pending-summary", {})
        removed = 0
        for pending, parent, field in (
            ("outcome-pending:", "outcome-job:", "outcome_id"),
            ("comparison-pending:", "comparison-job:", "event_id"),
            ("analytics-gap-pending:", "analytics-gap:", "id"),
            ("pending:telegram:", "outbox:telegram:", "delivery_id"),
            ("agent:send-due:", "agent:outbox:", "delivery_id"),
            ("digest-pending:", "digest-outbox:", "delivery_id"),
        ):
            for key, binding in self.items(pending, limit):
                if (
                    self.store.db.execute(
                        "SELECT 1 FROM runtime WHERE key=?", (PREFIX + parent + binding[field],)
                    ).fetchone()
                    is None
                ):
                    self.delete(key)
                    counts[pending + "EXPIRED_MISSING_PARENT"] = (
                        counts.get(pending + "EXPIRED_MISSING_PARENT", 0) + 1
                    )
                    removed += 1
        if removed:
            self.put("orphaned-pending-summary", counts)
        return removed

    def events(self, limit: int = 20) -> list[dict[str, Any]]:
        return [
            self.get(f"event-head:{v['event_id']}") for _, v in self.items("history:", limit, reverse=True)
        ]

    def diagnostics(self) -> dict[str, Any]:
        return {
            "active_episodes": self.count("active:"),
            "telegram_pending": self.count("pending:telegram:"),
            "detector_errors": {key: value for key, value in self.items("diagnostic:", 50)},
            "telegram_target": self.get("target:telegram", {"state": "READY"}),
            "schema": "cryptowatcher.radar.v1",
            "detector_metadata": [v for _, v in self.items("detector-metadata:")],
        }


class EventEngine:
    def __init__(
        self,
        store: Store,
        detector: Callable[..., Detection] = evaluate,
        *,
        source_revision: str = "UNSET",
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_episode: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}|UNSET", source_revision):
            raise ValueError("Invalid detector source revision")
        self.store = store
        self.history = History(store)
        self.detector = detector
        self.source_revision = source_revision
        self.on_event = on_event
        self.on_episode = on_episode
        with store.db:
            for family in FAMILIES:
                key = f"detector-metadata:{family}:{VERSIONS[family]}"
                definition = {
                    "detector_id": family,
                    "version": VERSIONS[family],
                    "formula": FORMULAS[family],
                    "baseline": "previous closed candles only",
                    "atr14": "simple mean previous14 true ranges",
                    "strength_methodology": "threshold-normalized; composite minimum price/turnover",
                }
                previous = self.history.get(key)
                if previous is not None and previous != definition:
                    raise StateError("Detector definition changed without a new version")
                if previous is None:
                    self.history.put(key, definition)

    def decision(self, event_id: str, value: dict[str, Any]) -> None:
        key = "decision:telegram:" + event_id
        previous = self.history.get(key, {})
        value = {**value, "revision": previous.get("revision", 0) + 1}
        self.history.put(f"{key}:{value['revision']:010d}", value)
        compact = self.store.bounded and re.fullmatch(r"[0-9a-f-]{36}", event_id)
        self.history.put(key, {DECISION_HEAD: value["revision"]} if compact else value)

    def _close_due(self, now_ms: int, instrument: str | None = None) -> None:
        prefix = "active:" if instrument is None else f"active:{instrument}/"
        for key, binding in self.history.items(prefix):
            episode = self.history.get("episode-head:" + binding["episode_id"])
            max_life = now_ms - episode["started_ms"] >= 3_600_000
            inactive = now_ms - episode["last_observation_ms"] >= 2 * episode["longest_tf_ms"]
            if not (max_life or inactive):
                continue
            episode.update(
                state="CLOSED", closed_ms=now_ms, close_reason="max_lifetime" if max_life else "inactivity"
            )
            episode["revision"] += 1
            episode["available_at_ms"] = now_ms
            self.history.version("episode", episode)
            if self.on_episode is not None:
                self.on_episode(episode)
            self.history.delete(key)
            for condition in episode["conditions"]:
                runtime = self.history.get(condition, {})
                if runtime.get("non_signal_bars", 0) < 2:
                    runtime["blocked"] = True
                    self.history.put(condition, runtime)

    def tick(self, now_ms: int) -> None:
        with self.store.db:
            self._close_due(now_ms)

    def observe(
        self,
        stream: str,
        candles: list[Candle],
        liquidity: float,
        tick_size: float,
        now_ms: int,
        *,
        live: bool,
        source_context: dict[str, Any] | None = None,
        enrichment: dict[str, Any] | None = None,
    ) -> None:
        if not candles:
            return
        snapshot = self.store.snapshot()
        current = candles[-1]
        if self.store.bounded and current.end < self.store.get_runtime("history_generation", {}).get(
            "started_ms", 0
        ):
            return
        market, symbol, tf = stream.split("/")
        instrument = f"{market}/{symbol}"
        outputs: list[tuple[str, Detection | Exception]] = []
        for family in FAMILIES:
            try:
                outputs.append(
                    (family, self.detector(family, candles, snapshot.settings, instrument, liquidity))
                )
            except Exception as exc:
                outputs.append((family, exc))  # One family never hides another family's observation.
        with self.store.db:
            self._close_due(now_ms, instrument)
            for family, result in outputs:
                diagnostic = f"diagnostic:{stream}:{family}"
                if isinstance(result, Exception):
                    self.history.put(
                        diagnostic,
                        {
                            "reason": str(result)
                            if isinstance(result, DetectorUnavailable)
                            else type(result).__name__,
                            "at_ms": now_ms,
                        },
                    )
                    continue
                self.history.delete(diagnostic)
                directions = ("NEUTRAL",) if family in ("turnover", "volatility") else ("UP", "DOWN")
                for direction in directions:
                    key = f"condition:{stream}:{family}:{direction}"
                    runtime = self.history.get(key, {"non_signal_bars": 2, "blocked": False, "last_ms": 0})
                    ordered = current.end > runtime["last_ms"]
                    signal = result.signal and result.direction == direction
                    recovered_rearm = False
                    if (
                        runtime["blocked"]
                        and ordered
                        and runtime["last_ms"] < current.start
                        and len(candles) >= 3
                    ):
                        # Recovery can establish two complete non-signal bars without replaying alerts.
                        try:
                            prior = [
                                self.detector(family, candles[:-n], snapshot.settings, instrument, liquidity)
                                for n in (2, 1)
                            ]
                            recovered_rearm = all(not p.signal or p.direction != direction for p in prior)
                        except Exception:
                            recovered_rearm = False
                        if recovered_rearm:
                            runtime.update(blocked=False, non_signal_bars=2, last_ms=current.start)
                            self.history.put(key, runtime)
                    timely = 0 <= now_ms - current.end <= (2 if family == "breakout" else 1) * current.step
                    if ordered and timely:
                        consecutive = runtime["last_ms"] == current.start
                        runtime["non_signal_bars"] = (
                            0 if signal else (runtime["non_signal_bars"] + 1 if consecutive else 1)
                        )
                        runtime["last_ms"] = current.end
                        if runtime["non_signal_bars"] >= 2:
                            runtime["blocked"] = False
                        self.history.put(key, runtime)
                    if not signal:
                        continue
                    event_id = event_identity(
                        stream, family, VERSIONS[family], snapshot.revision, current.start
                    )
                    if self.history.get("event-head:" + event_id):
                        continue
                    is_live = live and ordered and timely
                    active_key = f"active:{instrument}/{direction}"
                    suppressed = is_live and runtime["blocked"]
                    binding = self.history.get(active_key) if is_live and not suppressed else None
                    episode = self.history.get("episode-head:" + binding["episode_id"]) if binding else None
                    if suppressed:
                        reason = "awaiting_rearm"
                    elif not is_live:
                        reason = "historical_observation"
                    else:
                        reason = "live_observation"
                    if episode is None:
                        episode_id = identity(f"episode/{event_id}")
                        episode = {
                            "episode_id": episode_id,
                            "revision": 0,
                            "instrument": instrument,
                            "direction": direction,
                            "state": "ACTIVE"
                            if is_live and not suppressed
                            else "SUPPRESSED"
                            if suppressed
                            else "HISTORICAL",
                            "started_ms": current.end,
                            "last_observation_ms": current.end,
                            "longest_tf_ms": current.step,
                            "strength": 0.0,
                            "confirmations": {},
                            "conditions": [],
                            "last_notified": None,
                            "pending_notification": None,
                            "notification_sequence": 0,
                        }
                        if episode["state"] == "ACTIVE":
                            self.history.put(active_key, {"episode_id": episode_id})
                    episode["revision"] += 1
                    episode["available_at_ms"] = now_ms
                    episode["last_observation_ms"] = max(episode["last_observation_ms"], current.end)
                    episode["longest_tf_ms"] = max(episode["longest_tf_ms"], current.step)
                    episode["strength"] = max(episode["strength"], result.strength)
                    episode["conditions"] = sorted(set((*episode["conditions"], key)))
                    episode["confirmations"][f"{family}/{tf}"] = {
                        "event_id": event_id,
                        "revision": 1,
                        "detector": family,
                        "timeframe": tf,
                        "strength": result.strength,
                        "market_observation_ms": current.end,
                        "detector_version": VERSIONS[family],
                        "settings_revision": snapshot.revision,
                        "fresh_until_ms": current.end + (2 if family == "breakout" else 1) * current.step,
                        "expires_at_ms": current.end + (4 if family == "breakout" else 2) * current.step,
                    }
                    multiplier = 2 if family == "breakout" else 1
                    try:
                        atr = atr14(candles)
                    except ValueError:
                        atr = None
                    noise = max(2 * tick_size, 0.1 * atr) / current.close * 100 if atr is not None else None
                    if noise is not None and not math.isfinite(noise):
                        noise = None
                    inputs = tuple(
                        candles[
                            -max(
                                len(result.inputs) + (2 if recovered_rearm else 0),
                                16 if atr is not None else 2,
                            ) :
                        ]
                    )
                    event = {
                        "schema": "cryptowatcher.event.v1",
                        "event_id": event_id,
                        "revision": 1,
                        "episode_id": episode["episode_id"],
                        "episode_revision": episode["revision"],
                        "venue": "BYBIT",
                        "symbol": symbol,
                        "market_type": market,
                        "timeframe": tf,
                        "detector_id": family,
                        "detector_version": VERSIONS[family],
                        "settings_revision": snapshot.revision,
                        "settings_snapshot": snapshot.settings.payload(),
                        "effective_parameters": effective_parameters(snapshot.settings, instrument),
                        "profile": snapshot.settings.profile,
                        "direction": direction,
                        "strength": result.strength,
                        "metrics": result.metrics,
                        "units": {k: UNITS[k] for k in result.metrics},
                        "market_observation_ms": current.end,
                        "detection_ms": now_ms,
                        "receipt_ms": current.received_ms,
                        "available_at_ms": now_ms,
                        "reference_price": current.close,
                        "reference_time_ms": current.end,
                        "freshness_policy_version": "1",
                        "fresh_until_ms": current.end + multiplier * current.step,
                        "expires_at_ms": current.end + 2 * multiplier * current.step,
                        "completeness": "COMPLETE",
                        "tick_size": tick_size,
                        "reference_atr14": atr,
                        "continuation_threshold_pct": noise,
                        "source_window_id": hashlib.sha256(
                            encode([asdict(c) for c in inputs]).encode()
                        ).hexdigest(),
                        "source_inputs": [asdict(c) for c in inputs],
                        "source_context": source_context or {"ticker": None, "instrument": None},
                        "enrichment": enrichment or {"state": "UNAVAILABLE", "detector_input": False},
                        "producer_source_revision": self.source_revision,
                        "outcome_context_completeness": "COMPLETE"
                        if noise is not None
                        else "ATR_UNAVAILABLE"
                        if atr is None
                        else "NONFINITE_REFERENCE_THRESHOLD",
                        "live": is_live and not suppressed,
                        "observation_reason": reason,
                        "rearm_from_complete_sources": recovered_rearm,
                        "trading_authorized": False,
                        "model_invoked": False,
                    }
                    self.history.version("event", event)
                    self.history.put(f"history:{current.end:016d}:{event_id}", {"event_id": event_id})
                    self.history.put(
                        f"episode-event:{episode['episode_id']}:{current.end:016d}:{event_id}",
                        {"event_id": event_id},
                    )
                    if (
                        event["live"]
                        and freshness(event, now_ms) == "FRESH"
                        and family in snapshot.settings.alert_detectors
                    ):
                        self._notify(episode, event, now_ms)
                    else:
                        self.decision(
                            event_id,
                            {
                                "event_id": event_id,
                                "reason": reason
                                if not event["live"]
                                else "detector_silent"
                                if family not in snapshot.settings.alert_detectors
                                else "not_fresh",
                                "delivery_id": None,
                                "at_ms": now_ms,
                            },
                        )
                    self.history.version("episode", episode)
                    if self.on_event is not None:
                        self.on_event(
                            event
                        )  # Source, episode and all consumer intents share the transaction.

    def _notify(self, episode: dict[str, Any], event: dict[str, Any], now_ms: int) -> None:
        previous = episode["last_notified"]
        if previous and previous.get("settings_revision") != event["settings_revision"]:
            previous = None
        confirmations = [
            c
            for c in episode["confirmations"].values()
            if c["settings_revision"] == event["settings_revision"]
        ]
        families = sorted({c["detector"] for c in confirmations})
        timeframes = sorted({c["timeframe"] for c in confirmations})
        strength = max(c["strength"] for c in confirmations)
        notification_state = {
            "families": families,
            "timeframes": timeframes,
            "strength": strength,
            "settings_revision": event["settings_revision"],
        }
        reason = (
            "first_detection"
            if previous is None
            else "new_detector"
            if set(families) - set(previous["families"])
            else "new_timeframe"
            if set(timeframes) - set(previous["timeframes"])
            else "strengthening"
            if strength >= previous["strength"] * 1.5
            else "unchanged_episode"
        )
        decision_id = event["event_id"]
        candidate_id = episode["pending_notification"]
        candidate = self.history.get("outbox:telegram:" + candidate_id) if candidate_id else None
        if (
            candidate
            and candidate["settings_revision"] != event["settings_revision"]
            and candidate["state"] in ("PENDING", "RETRY")
        ):
            candidate.update(state="OBSOLETE_CONFIGURATION", completed_ms=now_ms)
            self.history.put("outbox:telegram:" + candidate_id, candidate)
            self.history.delete(candidate["pending_key"])
            episode["pending_notification"] = None
            candidate_id = None
            candidate = None
        if not self.store.enabled():
            reason = "alerts_disabled"
        elif self.store.muted(episode["instrument"], now_ms / 1000) or (
            event["market_type"] == self.store.snapshot().settings.bybit_category
            and self.store.muted(event["symbol"], now_ms / 1000)
        ):
            reason = "muted"
        elif candidate and candidate["state"] in ("PENDING", "RETRY", "IN_FLIGHT"):
            reason = (
                "coalesced"
                if candidate["state"] == "PENDING" and now_ms <= candidate["created_ms"] + 2000
                else "delivery_pending"
            )
            if reason == "coalesced":
                candidate["episode_snapshot"] = json.loads(encode(episode))
                candidate["event_ids"] = sorted(set((*candidate["event_ids"], event["event_id"])))
                candidate["event_revisions"][event["event_id"]] = event["revision"]
                candidate["notification_state"] = notification_state
                self.history.put("outbox:telegram:" + candidate_id, candidate)
        elif reason != "unchanged_episode":
            episode["notification_sequence"] += 1
            candidate_id = identity(f"telegram/{episode['episode_id']}/{episode['notification_sequence']}")
            episode["pending_notification"] = candidate_id
            candidate = {
                "delivery_id": candidate_id,
                "consumer": "telegram",
                "episode_id": episode["episode_id"],
                "episode_snapshot": json.loads(encode(episode)),
                "settings_revision": event["settings_revision"],
                "event_ids": [event["event_id"]],
                "event_revisions": {event["event_id"]: event["revision"]},
                "notification_state": notification_state,
                "reason": reason,
                "state": "PENDING",
                "created_ms": now_ms,
                "next_attempt_ms": now_ms + 2000,
                "attempts": [],
            }
            candidate["pending_key"] = f"pending:telegram:{candidate['next_attempt_ms']:016d}:{candidate_id}"
            self.history.put("outbox:telegram:" + candidate_id, candidate)
            self.history.put(candidate["pending_key"], {"delivery_id": candidate_id})
        self.decision(
            decision_id,
            {"event_id": decision_id, "reason": reason, "delivery_id": candidate_id, "at_ms": now_ms},
        )

    def reconcile_notifications(self, episode_id: str, now_ms: int) -> None:
        # Observations arriving during network I/O remain eligible after the older ack.
        episode = self.history.get("episode-head:" + episode_id)
        if episode["state"] != "ACTIVE" or episode["pending_notification"]:
            return
        notified = episode["last_notified"]
        cutoff = notified.get("available_cutoff_ms", 0) if notified else 0
        settings = self.store.snapshot()
        events = [
            self.history.get(f"event:{value['event_id']}:{value['revision']:010d}")
            for value in episode["confirmations"].values()
        ]
        eligible = [
            e
            for e in events
            if e["detection_ms"] > cutoff
            and e["live"]
            and e["settings_revision"] == settings.revision
            and e["detector_id"] in settings.settings.alert_detectors
            and freshness(e, now_ms) == "FRESH"
        ]
        if eligible:
            with self.store.db:
                self._notify(episode, max(eligible, key=lambda e: e["detection_ms"]), now_ms)
                episode["revision"] += 1
                episode["available_at_ms"] = now_ms
                self.history.version("episode", episode)
