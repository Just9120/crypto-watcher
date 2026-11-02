"""Prepared detector transition and one joined subject-matter write boundary.

The pure transition has no SQL handle; callbacks receive the exact committed
revision projection. Transport work remains outside the source transaction.
"""

from __future__ import annotations

import time
from contextlib import nullcontext
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable

from . import events
from .condition_state import MEMBERS
from .domain_repository import STATE_PREFIXES, History
from .events import EventEngine, event_identity, identity
from .storage import StateError
from .writer_memory import transaction


def freeze_episode(value: Any) -> Any:
    return {**value, "confirmations": dict(value["confirmations"]), "conditions": list(value["conditions"])}


@dataclass
class Change:
    operation: str
    key: str
    value: Any = None


class PreparedState:
    """Memory only: unknown reads fail instead of reaching a hidden repository."""

    _condition_batch = None

    def __init__(self, values: Any, allowed: Any, condition_groups: Any = None) -> None:
        self.values, self.allowed = values, allowed
        self.changes: list[Change] = []
        self.condition_groups = condition_groups or {}
        self.store = SimpleNamespace(bounded=False)

    def get(self, key: Any, default: Any = None) -> Any:
        if key not in self.allowed and key not in self.values:
            raise StateError("Unprepared domain read: " + key)
        return self.values.get(key, default)

    def put(self, key: Any, value: Any) -> Any:
        if isinstance(value, dict):
            value = dict(value)
        self.values[key] = value
        self.changes.append(Change("put", key, value))

    def delete(self, key: Any) -> Any:
        self.values.pop(key, None)
        self.changes.append(Change("delete", key))

    def items(self, prefix: Any, limit: Any = None, *, reverse: Any = False) -> Any:
        rows = [(k, v) for k, v in sorted(self.values.items(), reverse=reverse) if k.startswith(prefix)]
        return rows if limit is None else rows[:limit]

    def episode_clock(self, key: Any) -> Any:
        return self.get(key)

    def version(self, kind: Any, value: Any) -> Any:
        saved = freeze_episode(value) if kind == "episode" else dict(value)
        key = f"{kind}:{value[kind + '_id']}:{value['revision']:010d}"
        self.values[key] = saved
        self.values[kind + "-head:" + value[kind + "_id"]] = (
            freeze_episode(saved) if kind == "episode" else dict(saved)
        )
        self.changes.append(Change("version", kind, saved))


class Kernel(EventEngine):
    """Existing detector/episode transition; deliberately no Store or SQL handle."""

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
                candidate["episode_snapshot"] = freeze_episode(episode)
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
                "episode_snapshot": freeze_episode(episode),
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


def prepare(engine: Any, stream: Any, candles: Any, snapshot: Any = None) -> Any:
    history, store = engine.history, engine.store
    if not isinstance(history, History):
        raise StateError("Domain writer needs its explicit repository")
    snapshot = snapshot or store.snapshot()
    instrument = stream.rsplit("/", 1)[0]
    values, allowed = {}, set()
    condition_groups = {}
    streams = {stream}
    for namespace in ("active", "historical-active", "suppressed-active"):
        for direction in ("UP", "DOWN", "NEUTRAL"):
            key = f"{namespace}:{instrument}/{direction}"
            allowed.add(key)
            binding = history.get(key)
            if binding is None:
                continue
            values[key] = binding
            epkey = "episode-head:" + binding["episode_id"]
            episode = history.get(epkey)
            if episode is None:
                raise StateError("Missing domain episode binding")
            values[epkey] = episode
            streams.update(c.split(":")[1] for c in episode["conditions"])
            candidate = episode["pending_notification"]
            if candidate:
                candidate_key = "outbox:telegram:" + candidate
                values[candidate_key] = history.get(candidate_key)
                allowed.add(candidate_key)
            for confirmation in episode["confirmations"].values():
                key = f"event:{confirmation['event_id']}:{confirmation['revision']:010d}"
                # The transition compares original parameters only across a
                # settings revision change; unchanged confirmations need no read.
                if confirmation["settings_revision"] != snapshot.revision:
                    values[key] = history.metadata(key)
    for item in streams:
        group = history.condition_group(item)
        condition_groups[item] = {name: dict(value) for name, value in group.items()}
        for member in MEMBERS:
            key = "condition:" + item + ":" + member
            allowed.add(key)
            if member in group:
                values[key] = group[member]
    for family in snapshot.settings.active_detectors:
        identity = event_identity(
            stream, family, events.VERSIONS[family], snapshot.revision, candles[-1].start
        )
        for key in (
            "event-head:" + identity,
            "decision:telegram:" + identity,
            f"diagnostic:{stream}:{family}",
        ):
            allowed.add(key)
            previous = history.get(key)
            if previous is not None:
                values[key] = previous
    if len(values) > 384:
        raise StateError("Prepared observation state exceeds finite budget")
    return PreparedState(values, allowed, condition_groups), snapshot


def observe(
    engine: Any,
    stream: Any,
    candles: Any,
    liquidity: Any,
    tick_size: Any,
    now_ms: Any,
    *,
    transition: Callable[..., Any],
    **kwargs: Any,
) -> Any:
    if not candles:
        return
    store = engine.store
    if not isinstance(engine.history, History):
        return transition(engine, stream, candles, liquidity, tick_size, now_ms, **kwargs)
    if store.readonly:
        raise StateError("Readonly producer")
    commit = kwargs.pop("commit", True)
    if not commit and not store.db.in_transaction:
        raise StateError("Domain producer requires joined source transaction")
    if candles[-1].end < store.get_runtime("history_generation", {}).get("started_ms", 0):
        return
    snapshot = store.snapshot()
    owner = store.owner_id
    stats = engine.writer_stats
    try:
        with store.db if commit else nullcontext():
            if not store.db.in_transaction:
                store.db.execute("BEGIN IMMEDIATE")
            with transaction(engine.history):
                binding_query = "SELECT revision,settings_json FROM owner_settings WHERE owner_id=?"
                expected_binding = store.db.execute(binding_query, (owner,)).fetchone()

                def ensure_binding() -> None:
                    current = store.db.execute(binding_query, (owner,)).fetchone()
                    if (
                        owner != store.owner_id
                        or current is None
                        or tuple(current) != tuple(expected_binding or ())
                        or current[0] != snapshot.revision
                    ):
                        raise StateError("Domain owner/settings revision changed during publication")

                ensure_binding()
                began = time.thread_time()
                prepared, snapshot = prepare(engine, stream, candles, snapshot)
                stats["cpu_seconds"]["prepare"] += time.thread_time() - began
                stats["max_prepared_keys"] = max(stats["max_prepared_keys"], len(prepared.values))
                # External context is captured only for a meaningful fact and
                # outside the pure transition; silent candles do not query it.
                provider = kwargs.pop("context_provider", None)
                muted = {
                    instrument: store.muted(instrument, now_ms / 1000)
                    for instrument in (stream.rsplit("/", 1)[0], stream.split("/")[1])
                }
                kernel: Any = object.__new__(Kernel)
                kernel.store = SimpleNamespace(
                    bounded=False,
                    db=nullcontext(),
                    snapshot=lambda: snapshot,
                    muted=lambda instrument, at: muted.get(instrument, False),
                )
                # Capture enabled before computation; no closure can reach Store.
                enabled_value = store.enabled()
                kernel.store.enabled = lambda: enabled_value
                kernel.history = prepared
                kernel.detector, kernel.source_revision = engine.detector, engine.source_revision
                kernel.on_event = kernel._snapshot_event_callback = lambda event: None
                kernel.on_event_snapshot = lambda event, episode: prepared.changes.append(
                    Change("consumer", "event", (event, freeze_episode(episode)))
                )
                kernel.on_episode = lambda episode: prepared.changes.append(
                    Change("consumer", "episode", freeze_episode(episode))
                )
                # Delayed computation does not turn an accepted live arrival into
                # a historical signal. Availability/detection remain actual wall
                # times; network freshness is checked again at send time.
                domain_now = (
                    min(now_ms, candles[-1].received_ms)
                    if kwargs.get("live") and candles[-1].source == "websocket"
                    else now_ms
                )
                began = time.thread_time()
                transition(kernel, stream, candles, liquidity, tick_size, domain_now, commit=True, **kwargs)
                stats["cpu_seconds"]["transition"] += time.thread_time() - began
                stats["pure_transitions"] += 1
                if provider is not None and any(
                    c.operation == "version" and c.key == "event" for c in prepared.changes
                ):
                    source_context, enrichment = provider()
                    source_context = source_context or {"ticker": None, "instrument": None}
                    enrichment = enrichment or {"state": "UNAVAILABLE", "detector_input": False}
                    for change in prepared.changes:
                        if change.operation == "version" and change.key == "event":
                            change.value["source_context"], change.value["enrichment"] = (
                                source_context,
                                enrichment,
                            )
                        elif change.operation == "consumer" and change.key == "event":
                            change.value[0]["source_context"], change.value[0]["enrichment"] = (
                                source_context,
                                enrichment,
                            )
                if owner != store.owner_id or snapshot.revision != store.snapshot().revision:
                    raise StateError("Domain owner/settings revision changed")
                groups: dict[Any, Any] = {}
                mutable: dict[Any, Any] = {}
                published: dict[Any, Any] = {}

                def flush_mutable() -> Any:
                    for key, (operation, value) in mutable.items():
                        if operation == "delete":
                            engine.history.delete(key)
                        else:
                            engine.history.put(key, value)
                        stats["mutable_flushed"] += 1
                    mutable.clear()
                    for item, group in groups.items():
                        engine.history.conditions(item, group)
                        stats["condition_groups"] += 1
                    groups.clear()

                apply_started, consumer_used = time.thread_time(), 0.0
                for change in prepared.changes:
                    if change.operation == "consumer":
                        ensure_binding()
                        consumer_started = time.thread_time()
                        if change.key == "event":
                            event, episode = change.value
                            event = dict(published[("event", event["event_id"], event["revision"])])
                            episode = freeze_episode(
                                published[("episode", episode["episode_id"], episode["revision"])]
                            )
                            if (
                                engine.on_event is engine._snapshot_event_callback
                                and engine.on_event_snapshot is not None
                            ):
                                engine.on_event_snapshot(event, episode)
                            elif engine.on_event is not None:
                                flush_mutable()
                                engine.on_event(event)
                        elif engine.on_episode is not None:
                            episode = change.value
                            # Use exactly the normalized revision just persisted,
                            # never the pure ingress-time copy or a later head.
                            engine.on_episode(
                                freeze_episode(
                                    published[("episode", episode["episode_id"], episode["revision"])]
                                )
                            )
                        consumer_used += time.thread_time() - consumer_started
                    elif change.operation == "version":
                        value = change.value
                        value["available_at_ms"] = now_ms
                        if change.key == "event":
                            value["detection_ms"] = now_ms
                            stats["facts"] += 1
                        else:
                            stats["episode_revisions"] += 1
                        engine.history.version(change.key, value)
                        published[(change.key, value[change.key + "_id"], value["revision"])] = value
                    elif change.key.startswith("condition:"):
                        _, item, family, direction = change.key.split(":")
                        if item not in groups:
                            groups[item] = dict(prepared.condition_groups[item])
                            stats["condition_read_reuse"] += 1
                        groups[item][family + ":" + direction] = change.value
                    elif change.key.startswith(STATE_PREFIXES):
                        mutable[change.key] = (change.operation, change.value)
                        stats["mutable_buffered"] += 1
                    elif change.operation == "delete":
                        engine.history.delete(change.key)
                    else:
                        value = change.value
                        if isinstance(value, dict) and "episode_snapshot" in value:
                            episode = value["episode_snapshot"]
                            point = ("episode", episode["episode_id"], episode["revision"])
                            # A Telegram candidate may precede publication of its
                            # revision. Normalize the same availability once here;
                            # consumer callbacks use the persisted projection above.
                            snapshot_value = published.get(point, {**episode, "available_at_ms": now_ms})
                            value = {**value, "episode_snapshot": freeze_episode(snapshot_value)}
                        engine.history.put(change.key, value)
                flush_mutable()
                ensure_binding()
                stats["cpu_seconds"]["consumer"] += consumer_used
                stats["cpu_seconds"]["apply_excluding_consumer"] += (
                    time.thread_time() - apply_started - consumer_used
                )
        stats["observations"] += 1
    except BaseException:
        stats["failed"] += 1
        raise
