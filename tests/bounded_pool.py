"""Isolated durable admission prototype; production never imports this module.

Only OutcomeWorker.admit is gated. Detection, immutable signal records and both
independent deliveries remain the real application paths for every instrument.
"""

from __future__ import annotations

import json
import math
from collections import Counter

from cryptowatcher.events import identity
from cryptowatcher.storage import StateError, encode

MINUTE = 60_000
DAY = 86_400_000
LIMITS = {
    "obligations": 64,
    "warming": 4,
    "manual": 16,
    "admissions_per_minute": 8,
    "waitlist": 128,
    "waitlist_bytes": 128 * 1024,
    "delivery_pending": 16384,
    "delivery_pending_bytes": 64 * 1024**2,
}


class Pool:
    def __init__(self, worker, *, manual=()):
        self.worker, self.store, self.history = worker, worker.store, worker.history
        self.manual = tuple(sorted(manual))
        self.limits = LIMITS
        if len(self.manual) > LIMITS["manual"]:
            raise ValueError("Manual pins exceed the isolated pool reservation")
        self.original_admit = worker.admit
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS prototype_pool_policy(id INTEGER PRIMARY KEY CHECK(id=1),body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS prototype_pool_choices(
                id TEXT PRIMARY KEY,event_id TEXT NOT NULL,market TEXT NOT NULL,
                reference_ms INTEGER NOT NULL,reference_price TEXT NOT NULL,
                status TEXT NOT NULL,reason TEXT NOT NULL,rank TEXT NOT NULL,
                fresh_ms INTEGER NOT NULL,admitted_ms INTEGER,manual INTEGER NOT NULL,
                seen_ms INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS prototype_pool_wait ON prototype_pool_choices(status,fresh_ms);
            CREATE TABLE IF NOT EXISTS prototype_pool_slots(
                id TEXT PRIMARY KEY REFERENCES prototype_pool_choices(id) ON DELETE CASCADE,
                state TEXT NOT NULL CHECK(state IN ('WARMING','ACTIVE','DRAINING')));
            CREATE TABLE IF NOT EXISTS prototype_pool_rate(
                market TEXT PRIMARY KEY,tokens REAL NOT NULL CHECK(tokens>=0 AND tokens<=4),at_ms INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS prototype_pool_legacy(id TEXT PRIMARY KEY);
        """)
        policy = encode({"limits": LIMITS, "manual": self.manual, "method": "bounded-pool-v1"})
        previous = self.store.db.execute("SELECT body FROM prototype_pool_policy WHERE id=1").fetchone()
        if previous and previous[0] != policy:
            raise StateError("Pool policy cannot change within the measurement")
        self.store.db.execute("INSERT OR IGNORE INTO prototype_pool_policy VALUES(1,?)", (policy,))
        known = set()
        for (event_id,) in self.store.db.execute(
            "SELECT event_id FROM prototype_pool_choices WHERE admitted_ms IS NOT NULL"
        ):
            event = self.history.get("event-head:" + event_id)
            known.update(
                identity(f"outcome/{event['episode_id']}/{h}/2")
                for h in event["settings_snapshot"]["outcome_horizons"]
            )
        for _, job in self.history.items("outcome-job:"):
            if job["outcome_id"] not in known and job["state"] != "COMPLETE":
                self.store.db.execute(
                    "INSERT OR IGNORE INTO prototype_pool_legacy VALUES(?)", (job["outcome_id"],)
                )
        self.store.db.commit()
        self.audit = Counter()
        self.peaks = Counter()
        worker.admit = self.offer

    def offer(self, event, *, episode=None):
        if event.get("outcome_unit") != "EPISODE":
            return self.original_admit(event, episode=episode)
        episode = episode or self.history.get("episode-head:" + event["episode_id"])
        if not episode or not episode.get("origin_event_id"):
            raise StateError("Missing immutable episode origin at selection")
        if event["event_id"] != episode["origin_event_id"]:
            self.store.db.execute(
                "UPDATE prototype_pool_choices SET rank=?,fresh_ms=MAX(fresh_ms,?) WHERE id=? AND status='WAITING'",
                (
                    encode(
                        [
                            int(f"{event['market_type']}/{event['symbol']}" in self.manual),
                            len(episode["confirmations"]),
                            episode["strength"],
                            event["episode_id"],
                        ]
                    ),
                    event["fresh_until_ms"],
                    event["episode_id"],
                ),
            )
            return
        now = self.store.clock_ms()
        point = (event["reference_time_ms"], encode(event["reference_price"]))
        previous = self.store.db.execute(
            "SELECT reference_ms,reference_price FROM prototype_pool_choices WHERE id=?",
            (event["episode_id"],),
        ).fetchone()
        if previous:
            if tuple(previous) != point:
                raise StateError("Selection cannot reset the episode reference")
            return
        strength = event["strength"]
        if not math.isfinite(strength):
            raise ValueError("Nonfinite selection strength")
        manual = f"{event['market_type']}/{event['symbol']}" in self.manual
        rank = [int(manual), len(episode["confirmations"]), strength, event["episode_id"]]
        self.store.db.execute(
            "INSERT INTO prototype_pool_choices VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event["episode_id"],
                event["event_id"],
                event["market_type"],
                *point,
                "WAITING",
                "AWAITING_CAPACITY",
                encode(rank),
                event["fresh_until_ms"],
                None,
                int(manual),
                now,
            ),
        )
        self.audit["offers"] += 1
        waiting = list(
            self.store.db.execute("SELECT id,rank FROM prototype_pool_choices WHERE status='WAITING'")
        )
        waiting.sort(key=lambda row: json.loads(row[1]), reverse=True)
        used = 0
        for index, row in enumerate(waiting):
            used += len(row[1].encode()) + len(row[0]) + 128
            if index >= LIMITS["waitlist"] or used > LIMITS["waitlist_bytes"]:
                self.store.db.execute(
                    "UPDATE prototype_pool_choices SET status='NOT_SELECTED',reason='WAITLIST_BUDGET' WHERE id=?",
                    (row[0],),
                )
                self.audit["waitlist_rejected"] += 1
        self._peaks()

    def _peaks(self):
        counts = dict(self.store.db.execute("SELECT state,COUNT(*) FROM prototype_pool_slots GROUP BY state"))
        for name, value in {"obligations": sum(counts.values()), "warming": counts.get("WARMING", 0)}.items():
            self.peaks[name] = max(self.peaks[name], value)
            if value > LIMITS[name]:
                raise StateError("Pool budget exceeded: " + name)
        waiting = self.store.db.execute(
            "SELECT COUNT(*) FROM prototype_pool_choices WHERE status='WAITING'"
        ).fetchone()[0]
        self.peaks["waitlist"] = max(self.peaks["waitlist"], waiting)
        if waiting > LIMITS["waitlist"]:
            raise StateError("Waitlist budget exceeded")

    def tick(self, now):
        with self.store.db:
            self._tick(now)

    def _tick(self, now):
        # Bounded reads: at most K obligations, at most four configured horizons.
        for row in list(self.store.db.execute("SELECT id FROM prototype_pool_slots")):
            reference = self.store.db.execute(
                "SELECT reference_ms FROM prototype_pool_choices WHERE id=?", (row[0],)
            ).fetchone()[0]
            if now >= reference + 30 * DAY:
                self.store.db.execute("DELETE FROM prototype_pool_slots WHERE id=?", (row[0],))
                self.store.db.execute(
                    "UPDATE prototype_pool_choices SET status='EXPIRED',reason='DETAIL_TTL' WHERE id=?",
                    (row[0],),
                )
                self.audit["detail_expiries"] += 1
                continue
            episode = self.history.episode_clock("episode-head:" + row[0])
            if not episode:
                raise StateError("Selected episode disappeared before terminal release")
            if episode["state"] == "ACTIVE":
                continue
            self.store.db.execute("UPDATE prototype_pool_slots SET state='DRAINING' WHERE id=?", (row[0],))
            origin = self.history.get("event-head:" + episode["origin_event_id"])
            jobs = [
                identity(f"outcome/{row[0]}/{h}/2") for h in origin["settings_snapshot"]["outcome_horizons"]
            ]
            if any(
                self.history.get("outcome-job:" + job, {}).get("state", "COMPLETE") != "COMPLETE"
                for job in jobs
            ):
                continue
            if any(self.history.metadata("outcome-head:" + job) is None for job in jobs):
                raise StateError("Outcome obligation lost without terminal result")
            self.store.db.execute("DELETE FROM prototype_pool_slots WHERE id=?", (row[0],))
            self.store.db.execute(
                "UPDATE prototype_pool_choices SET status='FINISHED',reason='TERMINAL' WHERE id=?", (row[0],)
            )
            self.audit["releases"] += 1
        expired = self.store.db.execute(
            "UPDATE prototype_pool_choices SET status='NOT_SELECTED',reason='EXPIRED_WAIT' WHERE status='WAITING' AND fresh_ms<?",
            (now,),
        ).rowcount
        self.audit["expired_wait"] += expired
        self.store.db.execute(
            "DELETE FROM prototype_pool_choices WHERE reference_ms<? AND id NOT IN (SELECT id FROM prototype_pool_slots)",
            (now - 30 * DAY,),
        )
        candidates = list(
            self.store.db.execute(
                "SELECT id,market,rank,manual,event_id FROM prototype_pool_choices WHERE status='WAITING'"
            )
        )
        candidates.sort(key=lambda row: json.loads(row[2]), reverse=True)
        legacy_debt = sum(
            self.history.get("outcome-job:" + row[0], {}).get("state", "COMPLETE") != "COMPLETE"
            for row in self.store.db.execute("SELECT id FROM prototype_pool_legacy")
        )
        self.peaks["legacy_jobs"] = max(self.peaks["legacy_jobs"], legacy_debt)
        if legacy_debt:
            self.audit["legacy_blocks"] += 1
            return
        for candidate in candidates:
            active = self.store.db.execute("SELECT COUNT(*) FROM prototype_pool_slots").fetchone()[0]
            manual_count = self.store.db.execute(
                "SELECT COUNT(*) FROM prototype_pool_slots s JOIN prototype_pool_choices c ON c.id=s.id WHERE c.manual=1"
            ).fetchone()[0]
            market_count = self.store.db.execute(
                "SELECT COUNT(*) FROM prototype_pool_slots s JOIN prototype_pool_choices c ON c.id=s.id WHERE c.market=?",
                (candidate[1],),
            ).fetchone()[0]
            if (
                active >= LIMITS["obligations"]
                or (candidate[3] and manual_count >= LIMITS["manual"])
                or market_count >= 32
            ):
                self.audit["full_checks"] += 1
                continue
            rate = self.store.db.execute(
                "SELECT tokens,at_ms FROM prototype_pool_rate WHERE market=?", (candidate[1],)
            ).fetchone()
            tokens = 4.0 if rate is None else min(4.0, rate[0] + max(0, now - rate[1]) * 4 / MINUTE)
            if tokens < 1:
                self.audit["rate_checks"] += 1
                continue
            self.store.db.execute(
                "INSERT OR REPLACE INTO prototype_pool_rate VALUES(?,?,?)", (candidate[1], tokens - 1, now)
            )
            self.store.db.execute("INSERT INTO prototype_pool_slots VALUES(?,'WARMING')", (candidate[0],))
            self._peaks()
            event = self.history.get("event-head:" + candidate[4])
            episode = self.history.get("episode-head:" + candidate[0])
            self.original_admit(event, episode=episode)
            self.store.db.execute(
                "UPDATE prototype_pool_slots SET state='ACTIVE' WHERE id=?", (candidate[0],)
            )
            self.store.db.execute(
                "UPDATE prototype_pool_choices SET status='SELECTED',reason='RANK_AND_RESERVE',admitted_ms=? WHERE id=?",
                (now, candidate[0]),
            )
            self.audit["admissions"] += 1
        self._peaks()

    def report(self):
        return {
            "mode": "ISOLATED_BOUNDED_ANALYTICS_POOL",
            "limits": LIMITS,
            "audit": dict(self.audit),
            "peaks": dict(self.peaks),
            "states": dict(
                self.store.db.execute("SELECT status,COUNT(*) FROM prototype_pool_choices GROUP BY status")
            ),
            "selected": [
                list(row)
                for row in self.store.db.execute(
                    "SELECT id,event_id,market,reference_ms,reference_price,admitted_ms FROM prototype_pool_choices WHERE admitted_ms IS NOT NULL ORDER BY id"
                )
            ],
            "waiting": self.store.db.execute(
                "SELECT COUNT(*) FROM prototype_pool_choices WHERE status='WAITING'"
            ).fetchone()[0],
            "legacy_jobs": sum(
                self.history.get("outcome-job:" + row[0], {}).get("state", "COMPLETE") != "COMPLETE"
                for row in self.store.db.execute("SELECT id FROM prototype_pool_legacy")
            ),
            "limitations": [
                "No live migration; only new synthetic obligations use the new policy",
                "Pool limits do not bound global signals or 30-day turnover",
            ],
        }
