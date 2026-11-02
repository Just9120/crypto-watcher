"""Explicit, fresh-DB domain writer experiment. Never enabled by production.

A prepared in-memory transition cannot access SQLite. One transaction commits
immutable facts, episode member deltas and mutable state before consumers run.
Compatibility readers validate the actual SQL rows on every public read.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from cryptowatcher import events
from cryptowatcher.condition_state import MEMBERS
from cryptowatcher.events import PREFIX, EventEngine, event_identity, identity
from cryptowatcher.storage import RUNTIME_UPSERT, StateError, encode
from tests import sqlite_repository as repository

BASE_HISTORY = repository.History
BASE_INSTALL = repository.install
BASE_MAINTENANCE = repository.maintenance
STATE_PREFIXES = ("active:", "historical-active:", "suppressed-active:", "diagnostic:")
DAY = 86400000


def digest(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def install(store):
    BASE_INSTALL(store)
    store.db.executescript("""
        CREATE TABLE domain_conditions(stream TEXT PRIMARY KEY,payload TEXT NOT NULL,sha TEXT NOT NULL) WITHOUT ROWID;
        CREATE TABLE domain_state(key TEXT PRIMARY KEY,payload TEXT NOT NULL,sha TEXT NOT NULL) WITHOUT ROWID;
        CREATE TABLE domain_members(id TEXT NOT NULL,revision INTEGER NOT NULL,name TEXT NOT NULL,
            payload TEXT NOT NULL,sha TEXT NOT NULL,PRIMARY KEY(id,name,revision)) WITHOUT ROWID;
        CREATE TRIGGER domain_member_immutable BEFORE UPDATE ON domain_members BEGIN
            SELECT RAISE(ABORT,'Immutable domain member'); END;
    """)
    store.db.commit()


class History(BASE_HISTORY):
    def __new__(cls, store):
        return object.__new__(cls) if repository.installed(store) else repository.OriginalHistory(store)

    def condition_group(self, stream):
        row = self.store.db.execute(
            "SELECT payload,sha FROM domain_conditions WHERE stream=?", (stream,)
        ).fetchone()
        if row is None:
            return {}
        if digest(row[0]) != row[1]:
            raise StateError("Corrupt domain conditions")
        return json.loads(row[0])

    def get(self, key, default=None):
        if key.startswith("condition:"):
            _, stream, family, direction = key.split(":")
            return self.condition_group(stream).get(family + ":" + direction, default)
        if key.startswith(STATE_PREFIXES):
            row = self.store.db.execute("SELECT payload,sha FROM domain_state WHERE key=?", (key,)).fetchone()
            if row is None:
                return default
            if digest(row[0]) != row[1]:
                raise StateError("Corrupt domain binding")
            return json.loads(row[0])
        return super().get(key, default)

    def put(self, key, value):
        if key.startswith("condition:"):
            _, stream, family, direction = key.split(":")
            group = self.condition_group(stream)
            group[family + ":" + direction] = value
            self.conditions(stream, group)
        elif key.startswith(STATE_PREFIXES):
            raw = encode(value)
            self.store.db.execute(
                "INSERT INTO domain_state VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload,sha=excluded.sha",
                (key, raw, digest(raw)),
            )
        else:
            super().put(key, value)

    def conditions(self, stream, value):
        if len(value) > len(MEMBERS):
            raise StateError("Domain condition group exceeds fixed detector bound")
        raw = encode(value)
        self.store.db.execute(
            "INSERT INTO domain_conditions VALUES(?,?,?) ON CONFLICT(stream) DO UPDATE SET payload=excluded.payload,sha=excluded.sha",
            (stream, raw, digest(raw)),
        )

    def delete(self, key):
        if key.startswith(STATE_PREFIXES):
            self.store.db.execute("DELETE FROM domain_state WHERE key=?", (key,))
        else:
            super().delete(key)

    def items(self, prefix, limit=None, *, reverse=False):
        if prefix.startswith(STATE_PREFIXES):
            if limit is not None and (type(limit) is not int or limit < 0):
                raise ValueError("Invalid domain scan limit")
            rows = self.store.db.execute(
                "SELECT key FROM domain_state WHERE key>=? AND key<? ORDER BY key "
                + ("DESC" if reverse else "ASC")
                + (" LIMIT ?" if limit is not None else ""),
                (prefix, prefix + "\uffff", *(() if limit is None else (limit,))),
            )
            return [(r[0], self.get(r[0])) for r in rows]
        return super().items(prefix, limit, reverse=reverse)

    def _record(self, key, *, metadata=False):
        point = self.point(key)
        if point is None or point[0] != "episode":
            return super()._record(key, metadata=metadata)
        row = self._row(key)
        if row is None:
            return None
        value, expected = json.loads(row[0]), json.loads(row[1])
        members = self.store.db.execute(
            "SELECT m.name,m.payload,m.sha FROM domain_members m JOIN (SELECT name,MAX(revision) revision FROM domain_members WHERE id=? AND revision<=? GROUP BY name) x ON m.name=x.name AND m.revision=x.revision WHERE m.id=?",
            (row[7], row[8], row[7]),
        ).fetchall()
        actual = {name: sha for name, raw, sha in members}
        if actual != expected or any(digest(raw) != sha for name, raw, sha in members):
            raise StateError("Missing or corrupt pinned episode members")
        value["confirmations"] = {
            name[2:]: json.loads(raw) for name, raw, _ in members if name.startswith("f:")
        }
        value["conditions"] = sorted(json.loads(raw) for name, raw, _ in members if name.startswith("c:"))
        if (
            value.get("episode_id") != row[7]
            or value.get("revision") != row[8]
            or self._head("episode", value, f"episode:{row[7]}:{row[8]:010d}", bounded=True)
            != self._head("episode", json.loads(row[2]), f"episode:{row[7]}:{row[8]:010d}", bounded=True)
        ):
            raise StateError("Conflicting domain episode identity")
        return value

    def version(self, kind, value):
        if kind not in ("event", "episode"):
            return super().version(kind, value)
        identity, revision = value.get(kind + "_id"), value.get("revision")
        if (
            not isinstance(identity, str)
            or re.fullmatch(r"[A-Za-z0-9-]+", identity) is None
            or type(revision) is not int
            or revision < 1
            or "_history_storage" in value
        ):
            raise StateError("Invalid domain revision")
        if self.store.readonly:
            raise StateError("Readonly domain writer")
        if not self.store.db.in_transaction:
            self.store.db.execute("BEGIN IMMEDIATE")
        from cryptowatcher.packet_store import atomic

        with atomic(self.store.db):
            self._publish(kind, identity, revision, value)

    def _publish(self, kind, identity, revision, value):
        key = f"{kind}:{identity}:{revision:010d}"
        db = self.store.db
        head = db.execute(
            f"SELECT r.revision,r.origin,r.reference,r.reference_ms FROM prototype_{kind}_heads h JOIN prototype_{kind}_revisions r ON r.id=h.id AND r.revision=h.revision WHERE h.id=?",
            (identity,),
        ).fetchone()
        if head is not None and head[0] >= revision:
            if self.get(key) != value:
                raise StateError("Conflicting immutable domain revision")
            return
        reference = (
            value.get("origin_event_id"),
            encode(value.get("reference_price")),
            value.get("reference_time_ms"),
        )
        if (
            head is not None
            and tuple(head)[2:] != reference[1:]
            or head is not None
            and kind == "episode"
            and head[1] != reference[0]
        ):
            raise StateError("Domain outcome reference is immutable")
        body, fields = dict(value), {}
        if kind == "episode":
            members = {"f:" + k: v for k, v in body.pop("confirmations").items()}
            members.update({"c:" + c: c for c in body.pop("conditions")})
            prior = (
                {}
                if head is None
                else json.loads(
                    db.execute(
                        "SELECT fields FROM prototype_episode_revisions WHERE id=? AND revision=?",
                        (identity, head[0]),
                    ).fetchone()[0]
                )
            )
            for name, member in members.items():
                raw = encode(member)
                fields[name] = digest(raw)
                if prior.get(name) != fields[name]:
                    db.execute(
                        "INSERT INTO domain_members VALUES(?,?,?,?,?)",
                        (identity, revision, name, raw, fields[name]),
                    )
            if set(prior) - set(fields):
                raise StateError("Episode members cannot disappear")
        else:
            for name in repository.SHARED & body.keys():
                raw = encode(body[name])
                if len(raw.encode()) <= 128:
                    continue
                sha = digest(raw)
                db.execute(
                    "INSERT OR IGNORE INTO prototype_snapshots(digest,payload) VALUES(?,?)", (sha, raw)
                )
                found = db.execute(
                    "SELECT payload FROM prototype_snapshots WHERE digest=?", (sha,)
                ).fetchone()[0]
                if found != raw:
                    raise StateError("Conflicting domain input snapshot")
                db.execute("INSERT INTO prototype_links VALUES(?,?,?)", (key, name, sha))
                fields[name] = sha
                del body[name]
        clock = {
            k: value[k]
            for k in (*events.HEAD_FIELDS[kind], "origin_event_id", "reference_price", "reference_time_ms")
            if k in value
        }
        raw, raw_fields, raw_clock = encode(body), encode(fields), encode(clock)
        checksum = self.checksum(identity, revision, raw, raw_fields, raw_clock, *reference)
        db.execute(
            f"INSERT INTO prototype_{kind}_revisions VALUES(?,?,?,?,?,?,?,?,?,?)",
            (identity, revision, key, raw, raw_fields, raw_clock, *reference, checksum),
        )
        db.execute(
            f"INSERT INTO prototype_{kind}_heads VALUES(?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision",
            (identity, revision),
        )
        db.execute(
            RUNTIME_UPSERT,
            (PREFIX + kind + "-head:" + identity, encode(self._head(kind, value, key, bounded=True))),
        )
        if revision == 1:
            self.put(kind + "-counter", self.get(kind + "-counter", 0) + 1)
        from cryptowatcher.expiry_index import put as put_expiry

        start = value["market_observation_ms"] if kind == "event" else value["started_ms"]
        days = (
            value["settings_snapshot"]["detail_retention_days"]
            if kind == "event"
            else self.store.snapshot().settings.detail_retention_days
        )
        put_expiry(
            db,
            PREFIX + kind + "-head:" + identity,
            start + days * DAY,
            kind + "-head",
            identity if kind == "event" else value.get("origin_event_id"),
        )


def freeze_episode(value):
    return {**value, "confirmations": dict(value["confirmations"]), "conditions": list(value["conditions"])}


@dataclass
class Change:
    operation: str
    key: str
    value: object = None


class PreparedState:
    """Memory only: unknown reads fail instead of reaching a hidden repository."""

    _condition_batch = None

    def __init__(self, values, allowed, condition_groups=None):
        self.values, self.allowed, self.changes = values, allowed, []
        self.condition_groups = condition_groups or {}
        self.store = SimpleNamespace(bounded=False)

    def get(self, key, default=None):
        if key not in self.allowed and key not in self.values:
            raise StateError("Unprepared domain read: " + key)
        return self.values.get(key, default)

    def put(self, key, value):
        if isinstance(value, dict):
            value = dict(value)
        self.values[key] = value
        self.changes.append(Change("put", key, value))

    def delete(self, key):
        self.values.pop(key, None)
        self.changes.append(Change("delete", key))

    def items(self, prefix, limit=None, *, reverse=False):
        rows = [(k, v) for k, v in sorted(self.values.items(), reverse=reverse) if k.startswith(prefix)]
        return rows if limit is None else rows[:limit]

    def episode_clock(self, key):
        return self.get(key)

    def version(self, kind, value):
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


def prepare(engine, stream, candles, snapshot=None):
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


def maintenance(store, now):
    result = BASE_MAINTENANCE(store, now)
    with store.db:
        # Revisions are retained until the existing detail-lifetime policy has
        # removed their parent head. Never discard an active revision's inputs.
        stale = store.db.execute(
            "SELECT DISTINCT id FROM domain_members WHERE NOT EXISTS(SELECT 1 FROM prototype_episode_heads h WHERE h.id=domain_members.id) LIMIT 64"
        ).fetchall()
        for (identity,) in stale:
            store.db.execute("DELETE FROM domain_members WHERE id=?", (identity,))
        store.db.execute(
            "DELETE FROM domain_state WHERE key IN(SELECT key FROM domain_state WHERE key>='diagnostic:' AND key<'diagnostic;' AND CAST(json_extract(payload,'$.at_ms') AS INTEGER)<? LIMIT 64)",
            (now - 30 * DAY,),
        )
        for prefix in STATE_PREFIXES[:3]:
            store.db.execute(
                "DELETE FROM domain_state WHERE key IN(SELECT s.key FROM domain_state s WHERE s.key>=? AND s.key<? AND NOT EXISTS(SELECT 1 FROM prototype_episode_heads h WHERE h.id=json_extract(s.payload,'$.episode_id')) LIMIT 64)",
                (prefix, prefix + "\uffff"),
            )
        store.db.execute(
            "DELETE FROM domain_conditions WHERE stream IN(SELECT stream FROM domain_conditions WHERE NOT EXISTS(SELECT 1 FROM json_each(payload) WHERE CAST(json_extract(value,'$.last_ms') AS INTEGER)>=?) LIMIT 64)",
            (now - 30 * DAY,),
        )
    return result


@contextmanager
def installed(enabled=True):
    stats = {
        "mode": "typed-domain-v1",
        "adapter_revision": "transaction-regression-fix-v1",
        "observations": 0,
        "mutable_buffered": 0,
        "mutable_flushed": 0,
        "condition_read_reuse": 0,
        "optional_agent_failures": 0,
        "cpu_seconds": {"prepare": 0.0, "transition": 0.0, "apply_excluding_consumer": 0.0, "consumer": 0.0},
        "pure_transitions": 0,
        "facts": 0,
        "episode_revisions": 0,
        "condition_groups": 0,
        "failed": 0,
        "max_prepared_keys": 0,
    }
    if not enabled:
        yield stats
        return
    original_observe = EventEngine.observe
    original_tick = EventEngine.tick

    def tick(engine, now):
        from tests.domain_roles import watermark

        return original_tick(engine, watermark(now))

    def observe(engine, stream, candles, liquidity, tick_size, now_ms, **kwargs):
        if not candles:
            return
        store = engine.store
        if not repository.installed(store):
            return original_observe(engine, stream, candles, liquidity, tick_size, now_ms, **kwargs)
        if store.readonly:
            raise StateError("Readonly producer")
        commit = kwargs.pop("commit", True)
        if not commit and not store.db.in_transaction:
            raise StateError("Domain producer requires joined source transaction")
        if candles[-1].end < store.get_runtime("history_generation", {}).get("started_ms", 0):
            return
        snapshot = store.snapshot()
        owner = store.owner_id
        try:
            with store.db if commit else nullcontext():
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
                kernel = object.__new__(Kernel)
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
                original_observe(
                    kernel, stream, candles, liquidity, tick_size, domain_now, commit=True, **kwargs
                )
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
                groups, mutable, published = {}, {}, {}

                def flush_mutable():
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
                stats["cpu_seconds"]["consumer"] += consumer_used
                stats["cpu_seconds"]["apply_excluding_consumer"] += (
                    time.thread_time() - apply_started - consumer_used
                )
            stats["observations"] += 1
        except BaseException:
            stats["failed"] += 1
            raise

    from cryptowatcher.agent_delivery import AgentDelivery
    from cryptowatcher.ingestion import BybitRadar

    original_diagnostic = AgentDelivery._diagnostic

    def agent_diagnostic(agent, state, now):
        if state.startswith("FAILED_"):
            stats["optional_agent_failures"] += 1
        return original_diagnostic(agent, state, now)

    from tests.domain_roles import LOCK, PENDING, receive

    with LOCK:
        if PENDING:
            raise StateError("Previous isolated ingress has not been cleaned")
    with ExitStack() as stack:
        stack.enter_context(patch.object(AgentDelivery, "_diagnostic", agent_diagnostic))
        stack.enter_context(patch.object(EventEngine, "tick", tick))
        stack.enter_context(patch.object(BybitRadar, "_receive_messages", receive))
        stack.enter_context(patch.object(repository, "History", History))
        stack.enter_context(patch.object(repository, "install", install))
        stack.enter_context(patch.object(repository, "maintenance", maintenance))
        stack.enter_context(patch.object(EventEngine, "observe", observe))
        try:
            yield stats
        finally:
            with LOCK:
                stats["unprocessed_ingress_at_cleanup"] = len(PENDING)
                PENDING.clear()
