"""Bounded, durable selection for episode outcomes."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from .events import PREFIX, History, identity
from .outcomes import METHOD, OutcomeWorker
from .storage import encode
from .storage_lifecycle import DAY


class EpisodePool:
    def __init__(self, worker: OutcomeWorker, *, capacity: int = 64) -> None:
        self.worker = worker
        self.store = worker.store
        self.history = worker.history
        self.capacity = capacity
        self.store.db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS episode_pool(
                episode_id TEXT PRIMARY KEY, event_id TEXT NOT NULL,
                instrument TEXT NOT NULL, priority INTEGER NOT NULL,
                strength REAL NOT NULL, reference_ms INTEGER NOT NULL,
                fresh_ms INTEGER NOT NULL, expiry_ms INTEGER NOT NULL,
                state TEXT NOT NULL, selected_ms INTEGER, jobs TEXT
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS episode_pool_rank
              ON episode_pool(state,priority DESC,strength DESC,reference_ms,episode_id);
            CREATE INDEX IF NOT EXISTS episode_pool_expiry ON episode_pool(expiry_ms);
            COMMIT;
        """)

    def offer(self, event: dict[str, Any]) -> None:
        if not event["live"] or event.get("outcome_unit") != "EPISODE":
            return
        episode_id = event["episode_id"]
        reference = event["reference_time_ms"]
        if reference != event["market_observation_ms"]:
            self.store.db.execute(
                "UPDATE episode_pool SET strength=MAX(strength,?),fresh_ms=MAX(fresh_ms,?) "
                "WHERE episode_id=? AND state='WAITING'",
                (event["strength"], event["fresh_until_ms"], episode_id),
            )
            return
        episode = self.history.metadata("episode-head:" + episode_id)
        if episode is None or episode["origin_event_id"] != event["event_id"]:
            return
        instrument = f"{event['market_type']}/{event['symbol']}"
        priority = int(instrument in self.store.snapshot().settings.instruments())
        rank = (priority, event["strength"], -reference)
        waiting = self.store.db.execute(
            "SELECT episode_id,priority,strength,reference_ms FROM episode_pool WHERE state='WAITING' "
            "ORDER BY priority DESC,strength DESC,reference_ms,episode_id LIMIT 1 OFFSET 127"
        ).fetchone()
        if waiting:
            if rank <= (waiting[1], waiting[2], -waiting[3]):
                return
            self.store.db.execute("DELETE FROM episode_pool WHERE episode_id=?", (waiting[0],))
        self.store.db.execute(
            "INSERT OR IGNORE INTO episode_pool VALUES(?,?,?,?,?,?,?,?,'WAITING',NULL,NULL)",
            (
                episode_id,
                event["event_id"],
                instrument,
                priority,
                event["strength"],
                reference,
                event["fresh_until_ms"],
                reference + event["settings_snapshot"]["detail_retention_days"] * DAY,
            ),
        )

    def selected(self, episode_id: str) -> bool:
        return (
            self.store.db.execute(
                "SELECT 1 FROM episode_pool WHERE episode_id=? AND state='SELECTED'", (episode_id,)
            ).fetchone()
            is not None
        )

    def follows(self, instrument: str) -> bool:
        return (
            self.store.db.execute(
                "SELECT 1 FROM episode_pool WHERE state='SELECTED' AND instrument=? LIMIT 1", (instrument,)
            ).fetchone()
            is not None
        )

    def instruments(self) -> set[str]:
        return {
            row[0]
            for row in self.store.db.execute(
                "SELECT DISTINCT instrument FROM episode_pool WHERE state='SELECTED'"
            )
        }

    def advance(self, now: int) -> None:
        db = self.store.db
        with db:
            db.execute("BEGIN IMMEDIATE")
            expired = db.execute(
                "SELECT episode_id FROM episode_pool WHERE expiry_ms<=? ORDER BY expiry_ms LIMIT 128", (now,)
            ).fetchall()
            for (episode_id,) in expired:
                if self.history.metadata("episode-head:" + episode_id) is None:
                    db.execute("DELETE FROM episode_pool WHERE episode_id=?", (episode_id,))
            db.execute("DELETE FROM episode_pool WHERE state='WAITING' AND fresh_ms<?", (now,))
            slots = db.execute(
                "SELECT episode_id,instrument,priority,jobs,expiry_ms FROM episode_pool WHERE state='SELECTED'"
            ).fetchall()
            active: Counter[str] = Counter()
            manual = 0
            for episode_id, instrument, priority, jobs, expiry in slots:
                episode = self.history.metadata("episode-head:" + episode_id)
                finished = (episode is None or episode["state"] != "ACTIVE") and all(
                    (self.history.metadata("outcome-head:" + job) or {}).get("state") == "COMPLETE"
                    for job in json.loads(jobs)
                )
                if expiry <= now or finished:
                    db.execute("UPDATE episode_pool SET state='FINISHED' WHERE episode_id=?", (episode_id,))
                    continue
                active[instrument.split("/", 1)[0]] += 1
                manual += priority
            available = self.capacity - sum(active.values())
            if available <= 0:
                return
            # Pre-cutover work consumes the same horizon budget until it drains.
            prefix = PREFIX + "outcome-pending:"
            queued = db.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM runtime WHERE key>=? AND key<? LIMIT 256)",
                (prefix, prefix + "~"),
            ).fetchone()[0]
            admitted = 0
            for episode_id, event_id, instrument, priority in db.execute(
                "SELECT episode_id,event_id,instrument,priority FROM episode_pool WHERE state='WAITING' "
                "ORDER BY priority DESC,strength DESC,reference_ms,episode_id"
            ).fetchall():
                if not available or queued >= 256 or admitted >= 8:
                    break
                market = instrument.split("/", 1)[0]
                if active[market] >= (self.capacity + 1) // 2 or (
                    priority and manual >= min(16, self.capacity)
                ):
                    continue
                episode = self.history.metadata("episode-head:" + episode_id)
                if episode is None or episode["state"] != "ACTIVE":
                    db.execute("DELETE FROM episode_pool WHERE episode_id=?", (episode_id,))
                    continue
                event = self.history.get("event-head:" + event_id)
                if event is None:
                    db.execute("DELETE FROM episode_pool WHERE episode_id=?", (episode_id,))
                    continue
                jobs = [
                    identity(f"outcome/{episode_id}/{horizon}/{METHOD}")
                    for horizon in event["settings_snapshot"]["outcome_horizons"]
                ]
                if queued + len(jobs) > 256:
                    continue
                self.worker.admit(event)
                db.execute(
                    "UPDATE episode_pool SET state='SELECTED',selected_ms=?,jobs=? WHERE episode_id=?",
                    (now, encode(jobs), episode_id),
                )
                active[market] += 1
                manual += priority
                queued += len(jobs)
                available -= 1
                admitted += 1


def selection(history: History, event: dict[str, Any]) -> dict[str, Any] | None:
    """Project current selection without changing immutable event or v2 bytes."""
    db = history.store.db
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='episode_pool'").fetchone() is None:
        return None
    row = db.execute(
        "SELECT state,selected_ms,priority FROM episode_pool WHERE episode_id=?", (event["episode_id"],)
    ).fetchone()
    if row:
        state, selected_ms, priority = row
        return {
            "state": state,
            "selected_ms": selected_ms,
            "reason": "AWAITING_CAPACITY"
            if state == "WAITING"
            else "WATCHLIST_PRIORITY"
            if priority
            else "RANK",
        }
    return {
        "state": "NOT_SELECTED" if event.get("outcome_selection_policy") == "EPISODE_POOL_V1" else "LEGACY"
    }


def outcome_scope_note(history: History, event: dict[str, Any]) -> str:
    current = selection(history, event)
    if current is None:
        return ""
    label = {
        "WAITING": "кандидат на сопровождение",
        "SELECTED": "выбран для сопровождения",
        "FINISHED": "сопровождение завершено",
        "NOT_SELECTED": "вне пула сопровождения",
        "LEGACY": "история до запуска пула",
    }[current["state"]]
    return "\nОтбор для outcomes: " + label
