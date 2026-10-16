"""Indexed, bounded expiry. Detail deletion and numeric aggregation are atomic."""

from __future__ import annotations

import re
import time
from typing import Any

from .storage import Store, encode, json_payload

DAY = 86400000
PREFIX = "radar:v1:"
FIXED = ("detector-metadata:", "outcome-method:", "comparison-method:", "enrichment-method:")
PRIVATE = {"agent:preference", "digest-preferences"}
TERMINAL_DELIVERY = frozenset(
    (
        "DELIVERED",
        "DELIVERED_PRIOR_SETTINGS",
        "DELIVERED_PRIOR_PREFERENCE",
        "EXPIRED",
        "EXPIRED_LIVE",
        "CANCELLED",
        "OBSOLETE_CONFIGURATION",
        "SUPERSEDED",
        "MUTED",
        "ALERTS_DISABLED",
        "DIGEST_DISABLED",
        "DIGEST_PREFERENCE_CHANGED",
        "SUPERSEDED_PERIOD",
        "CANCELED_AFTER_FAILED_ATTEMPT",
    )
)


def body_links(store: Store, owner: str, value: Any) -> None:
    keys: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, list):
            for v in item:
                visit(v)
        elif isinstance(item, dict):
            for reference in item.get("_history_storage", {}).get("fields", {}).values():
                if reference.get("mode") == "value2":
                    keys.add(reference["id"])
                elif reference.get("mode") in ("value", "window"):
                    keys.update(reference["ids"])
            for name, v in item.items():
                if name != "_history_storage":
                    visit(v)

    visit(value)
    store.db.execute("DELETE FROM market_body_links WHERE owner=?", (owner,))
    store.db.executemany("INSERT INTO market_body_links VALUES (?,?)", ((owner, k) for k in keys))


def parent_event(store: Store, value: Any, key: str) -> str | None:
    if isinstance(value, dict):
        if isinstance(value.get("event_id"), str):
            return value["event_id"]
        for field, prefixes in (
            ("outcome_id", ("outcome-job:",)),
            ("delivery_id", ("outbox:telegram:", "agent:outbox:")),
            ("key", ("",)),
        ):
            if not isinstance(value.get(field), str):
                continue
            for prefix in prefixes:
                row = store.db.execute(
                    "SELECT payload_json FROM runtime WHERE key=?", (PREFIX + prefix + value[field],)
                ).fetchone()
                if row:
                    raw = json_payload(row[0])
                    event_id = raw.get("event_id") or raw.get("intent", {}).get("event_id")
                    if event_id:
                        return event_id
                    ids = raw.get("event_ids", [])
                    if ids:
                        return ids[0]
        for child in ("intent", "event", "episode_snapshot"):
            result = parent_event(store, value.get(child), key)
            if result:
                return result
    for candidate in re.findall(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", key):
        if store.db.execute(
            "SELECT 1 FROM runtime WHERE key=?", (PREFIX + "event-head:" + candidate,)
        ).fetchone():
            return candidate
        for prefix in ("outcome-job:", "outbox:telegram:", "agent:outbox:"):
            parent = store.get_runtime(PREFIX + prefix + candidate)
            if isinstance(parent, dict):
                result = (
                    parent.get("event_id")
                    or parent.get("intent", {}).get("event_id")
                    or next(iter(parent.get("event_ids", [])), None)
                )
                if result:
                    return result
    return None


def track(store: Store, key: str, value: Any) -> None:
    if not store.bounded or not key.startswith(PREFIX):
        return
    name = key[len(PREFIX) :]
    if (
        name in PRIVATE
        or name in ("target:telegram", "agent:target")
        or name.startswith(FIXED)
        or ":" not in name
    ):
        return
    now = store.clock_ms()
    event_id = parent_event(store, value, name)
    days = store.snapshot().settings.detail_retention_days
    previous = store.db.execute("SELECT expires FROM market_lifetimes WHERE key=?", (key,)).fetchone()
    expiry = previous[0] if previous else now + days * DAY
    if isinstance(value, dict):
        anchor = value.get(
            "market_observation_ms",
            value.get("reference_time_ms", value.get("started_ms", value.get("created_ms"))),
        )
        if type(anchor) is int:
            expiry = anchor + days * DAY
    if event_id:
        parent = store.db.execute(
            "SELECT expires FROM market_lifetimes WHERE key=?", (PREFIX + "event-head:" + event_id,)
        ).fetchone()
        if parent:
            expiry = parent[0]
    if isinstance(value, dict) and type(value.get("expires_ms")) is int:
        expiry = min(expiry, value["expires_ms"])
    category = name.split(":", 1)[0]
    # Active per-stream state may renew; immutable/per-event/service state cannot.
    if category == "active":
        expiry = now + days * DAY
    elif category in ("condition", "diagnostic", "oi-attempt"):
        expiry = now + store.snapshot().settings.candle_retention_days * DAY
    store.db.execute(
        "INSERT INTO market_lifetimes VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET expires=excluded.expires,event_id=excluded.event_id",
        (key, expiry, category, event_id),
    )
    if (
        isinstance(value, dict)
        and value.get("state") in TERMINAL_DELIVERY | {"COMPLETE", "EXPIRED_RETENTION_INCOMPLETE"}
        and name.startswith(
            ("agent:attempt:", "agent:outbox:", "outbox:telegram:", "analytics-gap:", "digest-outbox:")
        )
    ):
        finished = value.get("completed_ms", value.get("at_ms", now))
        if type(finished) is not int:
            finished = now
        store.db.execute(
            "UPDATE market_lifetimes SET expires=min(expires,?) WHERE key=?", (finished + DAY, key)
        )
    body_links(store, key, value)
    if name.startswith("hold:"):
        from .market_storage import stream_id

        stream = name.split(":")[1]
        sid = stream_id(store.db, stream, create=True)
        store.db.execute(
            "INSERT INTO market_holds VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET start=excluded.start,end=excluded.end,expires=excluded.expires",
            (key, sid, value["start"], value["end"], expiry),
        )


def forget(store: Store, key: str) -> None:
    store.db.execute("DELETE FROM market_body_links WHERE owner=?", (key,))
    store.db.execute("DELETE FROM market_lifetimes WHERE key=?", (key,))
    store.db.execute("DELETE FROM market_holds WHERE key=?", (key,))


def remove(store: Store, key: str) -> None:
    forget(store, key)
    store.db.execute("DELETE FROM runtime WHERE key=?", (key,))


class BoundedRetention:
    def __init__(self, store: Store) -> None:
        self.store = store

    def work(self, now_ms: int, batch: int = 200) -> int:
        from .events import History
        from .history_codec import BLOB_PREFIX
        from .quality_summary import archive_event

        store = self.store
        history = History(store)
        removed = 0
        # Chronological history index, no scan over the current candle universe.
        settings = store.snapshot().settings
        cutoff = now_ms - settings.detail_retention_days * DAY
        prefix = PREFIX + "history:"
        due = store.db.execute(
            "SELECT key,payload_json FROM runtime WHERE key>=? AND key<? ORDER BY key LIMIT ?",
            (prefix, prefix + f"{max(0, cutoff):016d}", batch),
        ).fetchall()
        deadline = time.monotonic() + 0.05
        processed = 0
        with store.db:
            # Repair only these two singleton scheduling indexes from initial v2.
            # A permanent target failure is retained until an owner re-enables it.
            store.db.execute(
                "DELETE FROM market_lifetimes WHERE key IN (?,?)",
                (PREFIX + "target:telegram", PREFIX + "agent:target"),
            )
            for row in due:
                if time.monotonic() >= deadline:
                    break
                eid = json_payload(row[1])["event_id"]
                event = history.metadata("event-head:" + eid)
                if event is not None:
                    archive_event(store, event)
                owned = store.db.execute(
                    "SELECT key FROM market_lifetimes WHERE event_id=?", (eid,)
                ).fetchall()
                for item in owned:
                    raw = store.get_runtime(item[0], {})
                    state = raw.get("state") if isinstance(raw, dict) else None
                    if state in ("PENDING", "RETRY", "IN_FLIGHT", "BLOCKED", "INVALID_SOURCE", "INCOMPLETE"):
                        self._terminal(item[0][len(PREFIX) :].split(":", 1)[0], state)
                    remove(store, item[0])
                    removed += 1
                remove(store, row[0])
                processed += 1
            # Service receipts expire first. They do not keep detail alive indefinitely.
            service = store.db.execute(
                "SELECT key,category FROM market_lifetimes WHERE expires<=? AND category NOT IN ('event','event-head','outcome','outcome-head','comparison','comparison-head','history') ORDER BY expires,key LIMIT ?",
                (now_ms, batch),
            ).fetchall()
            service_removed = 0
            for row in service:
                if row[1] in ("episode", "episode-head"):
                    episode_id = row[0].split(":")[3]
                    prefix = PREFIX + "episode-event:" + episode_id + ":"
                    keep = store.db.execute(
                        "SELECT MIN(l.expires) FROM market_lifetimes l WHERE l.key>=? AND l.key<?",
                        (prefix, prefix + "~"),
                    ).fetchone()[0]
                    if keep is not None and keep > now_ms:
                        store.db.execute("UPDATE market_lifetimes SET expires=? WHERE key=?", (keep, row[0]))
                        continue
                # Store a bounded terminal-status counter, instead of retaining whole jobs.
                raw = store.get_runtime(row[0], {})
                if (
                    row[1] in ("outcome-job", "comparison-job")
                    and isinstance(raw, dict)
                    and raw.get("event_id")
                ):
                    # Preserve original pending methodology/revision until its
                    # parent is aggregated, even across a split expiry batch.
                    if store.db.execute(
                        "SELECT 1 FROM runtime WHERE key=?", (PREFIX + "event-head:" + raw["event_id"],)
                    ).fetchone():
                        continue
                state = raw.get("state") if isinstance(raw, dict) else None
                if state in ("PENDING", "RETRY", "IN_FLIGHT", "BLOCKED", "INVALID_SOURCE", "INCOMPLETE"):
                    self._terminal(row[1], state)
                remove(store, row[0])
                removed += 1
                service_removed += 1
            removed += history.prune_pending()
            # A source revision survives the ordinary TTL only while a retained detail
            # references it. Old global `protected` records are not a v2 mechanism.
            candidates = store.db.execute(
                "SELECT stream,start,revision,payload,needed_until FROM market_candles WHERE purge_after<=? ORDER BY purge_after LIMIT ?",
                (now_ms, batch * 4),
            ).fetchall()
            candles = []
            for candidate in candidates:
                from .market_storage import public

                value = public(bytes(candidate[3]))
                real_expiry = max(
                    value["start"] + value["step"] + settings.candle_retention_days * DAY,
                    candidate[4] + settings.detail_retention_days * DAY,
                )
                held = store.db.execute(
                    "SELECT MAX(expires) FROM market_holds WHERE stream=? AND start<=? AND end>? AND expires>?",
                    (candidate[0], candidate[1], candidate[1], now_ms),
                ).fetchone()[0]
                if held:
                    real_expiry = max(real_expiry, min(held, now_ms + 3600000))
                if real_expiry > now_ms:
                    store.db.execute(
                        "UPDATE market_candles SET purge_after=? WHERE stream=? AND start=? AND revision=?",
                        (real_expiry, *candidate[:3]),
                    )
                else:
                    candles.append(candidate[:3])
            store.db.executemany(
                "DELETE FROM market_candles WHERE stream=? AND start=? AND revision=?", candles
            )
            context_candidates = store.db.execute(
                "SELECT stream,kind,at,needed_anchor FROM market_context WHERE purge_after<=? ORDER BY purge_after LIMIT ?",
                (now_ms, batch),
            ).fetchall()
            contexts = []
            for candidate in context_candidates:
                real_expiry = max(
                    candidate[2] + settings.candle_retention_days * DAY,
                    candidate[3] + settings.detail_retention_days * DAY,
                )
                if real_expiry > now_ms:
                    store.db.execute(
                        "UPDATE market_context SET purge_after=? WHERE stream=? AND kind=? AND at=?",
                        (real_expiry, *candidate[:3]),
                    )
                else:
                    contexts.append(candidate[:3])
            store.db.executemany("DELETE FROM market_context WHERE stream=? AND kind=? AND at=?", contexts)
            context_garbage = store.db.execute(
                "SELECT id FROM market_context_bodies WHERE refs=0 LIMIT ?",
                (batch,),
            ).fetchall()
            store.db.executemany(
                "DELETE FROM market_context_bodies WHERE id=?", (tuple(r) for r in context_garbage)
            )
            quality = store.db.execute(
                "SELECT day,cohort,chunk FROM market_quality WHERE expires_day<=? LIMIT ?",
                (now_ms // DAY, batch),
            ).fetchall()
            for day, cohort, chunk in quality:
                store.db.execute(
                    "DELETE FROM market_body_links WHERE owner=?", (f"quality:{day}:{cohort}:{chunk}",)
                )
                store.db.execute(
                    "DELETE FROM market_quality WHERE day=? AND cohort=? AND chunk=?", (day, cohort, chunk)
                )
            garbage = store.db.execute(
                "SELECT body FROM market_body_counts WHERE refs=0 LIMIT ?", (batch,)
            ).fetchall()
            for row in garbage:
                store.db.execute("DELETE FROM runtime WHERE key=?", (BLOB_PREFIX + row[0],))
                store.db.execute("DELETE FROM market_body_counts WHERE body=?", (row[0],))
            store.put_runtime(
                "market_retention",
                {
                    "at_ms": now_ms,
                    "expired_events": processed,
                    "remaining_due_batch": len(due) - processed,
                    "removed_services": service_removed,
                    "removed_candles": len(candles),
                    "removed_context": len(contexts),
                    "removed_summaries": len(quality),
                    "removed_bodies": len(garbage),
                    "batch": batch,
                    "state": "OK",
                    "terminal_status": "EXPIRED_RETENTION_INCOMPLETE",
                },
            )
        if removed and store.db.execute("PRAGMA freelist_count").fetchone()[0] > 256:
            store.db.execute("PRAGMA incremental_vacuum(32)").fetchall()
        return removed + len(candles) + len(contexts) + len(quality) + len(garbage)

    def _terminal(self, category: str, state: str) -> None:
        row = self.store.db.execute("SELECT value FROM market_meta WHERE key='expired_work'").fetchone()
        counts = json_payload(row[0]) if row else {}
        key = category + ":" + state + "->EXPIRED_RETENTION_INCOMPLETE"
        counts[key] = counts.get(key, 0) + 1
        self.store.db.execute(
            "INSERT INTO market_meta VALUES ('expired_work',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (encode(counts),),
        )
