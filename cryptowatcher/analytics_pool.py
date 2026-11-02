"""Durable bounded admission for detailed episode analytics.

Detection, immutable signals and consumer deliveries are independent of selection.
Existing outcome obligations survive activation and block admission while draining.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from typing import Any

from .events import PREFIX, identity
from .storage import StateError, encode

MINUTE = 60_000
DAY = 86_400_000
LIMITS = {
    "obligations": 64,
    "horizon_jobs": 256,
    "warming": 4,
    "manual": 16,
    "admissions_per_minute": 8,
    "waitlist": 128,
    "waitlist_bytes": 128 * 1024,
    "delivery_pending": 16384,
    "delivery_pending_bytes": 64 * 1024**2,
}


class Pool:
    def __init__(self, worker: Any, *, manual: tuple[str, ...] = ()) -> None:
        self.worker, self.store, self.history = worker, worker.store, worker.history
        self.manual = tuple(sorted(manual))
        self.limits = LIMITS
        if len(self.manual) > LIMITS["manual"]:
            raise ValueError("Manual pins exceed the bounded pool reservation")
        if self.store.readonly or self.store.db.in_transaction:
            raise StateError("Pool installation requires a writable idle connection")
        self.original_admit = worker.admit_selected
        self.store.db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS analytics_pool_policy(id INTEGER PRIMARY KEY CHECK(id=1),body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS analytics_pool_choices(
                id TEXT PRIMARY KEY,event_id TEXT NOT NULL,market TEXT NOT NULL,
                reference_ms INTEGER NOT NULL,reference_price TEXT NOT NULL,
                status TEXT NOT NULL,reason TEXT NOT NULL,rank TEXT NOT NULL,
                fresh_ms INTEGER NOT NULL,admitted_ms INTEGER,manual INTEGER NOT NULL,
                seen_ms INTEGER NOT NULL,expires_ms INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS analytics_pool_expiry ON analytics_pool_choices(expires_ms);
            CREATE INDEX IF NOT EXISTS analytics_pool_wait ON analytics_pool_choices(status,fresh_ms);
            CREATE TABLE IF NOT EXISTS analytics_pool_slots(
                id TEXT PRIMARY KEY REFERENCES analytics_pool_choices(id) ON DELETE CASCADE,
                state TEXT NOT NULL CHECK(state IN ('WARMING','ACTIVE','DRAINING')));
            CREATE TABLE IF NOT EXISTS analytics_pool_jobs(
                id TEXT NOT NULL REFERENCES analytics_pool_slots(id) ON DELETE CASCADE,
                job_id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS analytics_pool_rate(
                market TEXT PRIMARY KEY,tokens REAL NOT NULL CHECK(tokens>=0 AND tokens<=4),at_ms INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS analytics_pool_legacy(id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS analytics_pool_cutover(
                id INTEGER PRIMARY KEY CHECK(id=1),cursor TEXT NOT NULL,complete INTEGER NOT NULL CHECK(complete IN(0,1)));
            INSERT OR IGNORE INTO analytics_pool_cutover VALUES(1,'',0);
            COMMIT;
        """)
        policy = encode({"limits": LIMITS, "manual": self.manual, "method": "bounded-pool-v1"})
        previous = self.store.db.execute("SELECT body FROM analytics_pool_policy WHERE id=1").fetchone()
        if previous and previous[0] != policy:
            raise StateError("Pool policy differs from the persisted admission contract")
        self.store.db.execute("INSERT OR IGNORE INTO analytics_pool_policy VALUES(1,?)", (policy,))
        self.store.db.commit()
        self.audit: Counter[str] = Counter()
        self.peaks: Counter[str] = Counter()

    def activate(self) -> None:
        if self.worker.admission_policy is not None:
            raise StateError("An outcome admission policy is already installed")
        self.worker.admission_policy = self.offer

    def offer(self, event: dict[str, Any], *, episode: dict[str, Any] | None = None) -> None:
        if event.get("outcome_unit") in (None, "EVENT_REFERENCE"):
            # Preserve already admitted legacy jobs, not a new research sweep
            # over retained pre-cutover history during worker bootstrap.
            self.audit["legacy_offers_not_admitted"] += 1
            return
        if event.get("outcome_unit") != "EPISODE":
            raise StateError("Unsupported frozen outcome unit")
        episode = episode or self.history.get("episode-head:" + event["episode_id"])
        if not episode or not episode.get("origin_event_id"):
            raise StateError("Missing immutable episode origin at selection")
        if event["event_id"] != episode["origin_event_id"]:
            self.store.db.execute(
                "UPDATE analytics_pool_choices SET rank=?,fresh_ms=MAX(fresh_ms,?) WHERE id=? AND status='WAITING'",
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
            "SELECT reference_ms,reference_price FROM analytics_pool_choices WHERE id=?",
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
            "INSERT INTO analytics_pool_choices VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                event["reference_time_ms"] + event["settings_snapshot"]["detail_retention_days"] * DAY,
            ),
        )
        self.audit["offers"] += 1
        waiting = list(
            self.store.db.execute("SELECT id,rank FROM analytics_pool_choices WHERE status='WAITING'")
        )
        waiting.sort(key=lambda row: json.loads(row[1]), reverse=True)
        used = 0
        for index, row in enumerate(waiting):
            used += len(row[1].encode()) + len(row[0]) + 128
            if index >= LIMITS["waitlist"] or used > LIMITS["waitlist_bytes"]:
                self.store.db.execute(
                    "UPDATE analytics_pool_choices SET status='NOT_SELECTED',reason='WAITLIST_BUDGET' WHERE id=?",
                    (row[0],),
                )
                self.audit["waitlist_rejected"] += 1
        self._peaks()

    def _peaks(self) -> None:
        reserved = self.store.db.execute("SELECT COUNT(*) FROM analytics_pool_jobs").fetchone()[0]
        self.peaks["horizon_jobs"] = max(self.peaks["horizon_jobs"], reserved)
        if reserved > LIMITS["horizon_jobs"]:
            raise StateError("Pool horizon-job budget exceeded")
        counts = dict(self.store.db.execute("SELECT state,COUNT(*) FROM analytics_pool_slots GROUP BY state"))
        for name, value in {"obligations": sum(counts.values()), "warming": counts.get("WARMING", 0)}.items():
            self.peaks[name] = max(self.peaks[name], value)
            if value > LIMITS[name]:
                raise StateError("Pool budget exceeded: " + name)
        waiting = self.store.db.execute(
            "SELECT COUNT(*) FROM analytics_pool_choices WHERE status='WAITING'"
        ).fetchone()[0]
        self.peaks["waitlist"] = max(self.peaks["waitlist"], waiting)
        if waiting > LIMITS["waitlist"]:
            raise StateError("Waitlist budget exceeded")

    def tick(self, now: int) -> None:
        with self.store.db:
            self._tick(now)

    def _tick(self, now: int) -> None:
        # Classify at most64 old jobs per turn before admitting new obligations.
        # Pending old jobs are preserved, including future horizons. A restart
        # resumes the cursor instead of rescanning retained thirty-day results.
        cursor, complete = self.store.db.execute(
            "SELECT cursor,complete FROM analytics_pool_cutover WHERE id=1"
        ).fetchone()
        if not complete:
            prefix = PREFIX + "outcome-job:"
            rows = self.store.db.execute(
                "SELECT key FROM runtime WHERE key>? AND key<? ORDER BY key LIMIT 65",
                (cursor or prefix, prefix + "~"),
            ).fetchall()
            for (key,) in rows[:64]:
                job = self.history.get(key[len(PREFIX) :])
                if job is None:
                    raise StateError("Legacy outcome job disappeared within its transaction")
                chosen = self.store.db.execute(
                    "SELECT 1 FROM analytics_pool_choices WHERE id=? AND admitted_ms IS NOT NULL",
                    (job.get("episode_id"),),
                ).fetchone()
                if chosen is None and job["state"] not in ("COMPLETE", "COMPLETE_LIMITED"):
                    self.store.db.execute(
                        "INSERT OR IGNORE INTO analytics_pool_legacy VALUES(?)", (job["outcome_id"],)
                    )
            self.store.db.execute(
                "UPDATE analytics_pool_cutover SET cursor=?,complete=? WHERE id=1",
                (rows[min(len(rows), 64) - 1][0] if rows else cursor, int(len(rows) <= 64)),
            )
            if len(rows) > 64:
                self.audit["legacy_scan_slices"] += 1
                return
        # Bounded reads: at most K obligations, at most four configured horizons.
        for row in list(self.store.db.execute("SELECT id FROM analytics_pool_slots")):
            expiry = self.store.db.execute(
                "SELECT expires_ms FROM analytics_pool_choices WHERE id=?", (row[0],)
            ).fetchone()[0]
            episode = self.history.episode_clock("episode-head:" + row[0])
            if episode is None and now < expiry:
                raise StateError("Selected episode disappeared before terminal release")
            if episode is not None and episode["state"] == "ACTIVE" and now < expiry:
                continue
            self.store.db.execute("UPDATE analytics_pool_slots SET state='DRAINING' WHERE id=?", (row[0],))
            jobs = [
                item[0]
                for item in self.store.db.execute(
                    "SELECT job_id FROM analytics_pool_jobs WHERE id=?", (row[0],)
                )
            ]
            if not jobs:
                raise StateError("Selected episode has no reserved horizon obligations")
            if any(
                self.history.get("outcome-job:" + job, {}).get("state", "COMPLETE")
                not in ("COMPLETE", "COMPLETE_LIMITED")
                for job in jobs
            ):
                continue
            if now < expiry and any(self.history.metadata("outcome-head:" + job) is None for job in jobs):
                raise StateError("Outcome obligation lost without terminal result")
            self.store.db.execute("DELETE FROM analytics_pool_slots WHERE id=?", (row[0],))
            self.store.db.execute(
                "UPDATE analytics_pool_choices SET status='FINISHED',reason='TERMINAL' WHERE id=?", (row[0],)
            )
            self.audit["releases"] += 1
        expired = self.store.db.execute(
            "UPDATE analytics_pool_choices SET status='NOT_SELECTED',reason='EXPIRED_WAIT' WHERE status='WAITING' AND fresh_ms<?",
            (now,),
        ).rowcount
        self.audit["expired_wait"] += expired
        self.store.db.execute(
            "DELETE FROM analytics_pool_choices WHERE id IN (SELECT id FROM analytics_pool_choices WHERE expires_ms<? AND id NOT IN (SELECT id FROM analytics_pool_slots) ORDER BY expires_ms LIMIT 64)",
            (now,),
        )
        candidates = list(
            self.store.db.execute(
                "SELECT id,market,rank,manual,event_id FROM analytics_pool_choices WHERE status='WAITING'"
            )
        )
        candidates.sort(key=lambda row: json.loads(row[2]), reverse=True)
        for (job_id,) in self.store.db.execute(
            "SELECT id FROM analytics_pool_legacy ORDER BY id LIMIT 64"
        ).fetchall():
            job = self.history.get("outcome-job:" + job_id)
            if job is None or job["state"] in ("COMPLETE", "COMPLETE_LIMITED"):
                self.store.db.execute("DELETE FROM analytics_pool_legacy WHERE id=?", (job_id,))
        legacy_debt = self.store.db.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM analytics_pool_legacy LIMIT 65)"
        ).fetchone()[0]
        self.peaks["legacy_jobs"] = max(self.peaks["legacy_jobs"], legacy_debt)
        if legacy_debt:
            self.audit["legacy_blocks"] += 1
            return
        for candidate in candidates:
            active = self.store.db.execute("SELECT COUNT(*) FROM analytics_pool_slots").fetchone()[0]
            manual_count = self.store.db.execute(
                "SELECT COUNT(*) FROM analytics_pool_slots s JOIN analytics_pool_choices c ON c.id=s.id WHERE c.manual=1"
            ).fetchone()[0]
            market_count = self.store.db.execute(
                "SELECT COUNT(*) FROM analytics_pool_slots s JOIN analytics_pool_choices c ON c.id=s.id WHERE c.market=?",
                (candidate[1],),
            ).fetchone()[0]
            if (
                active >= LIMITS["obligations"]
                or (candidate[3] and manual_count >= LIMITS["manual"])
                or market_count >= 32
            ):
                self.audit["full_checks"] += 1
                continue
            event = self.history.get("event-head:" + candidate[4])
            episode = self.history.get("episode-head:" + candidate[0])
            if event is None or episode is None:
                raise StateError("Selected origin disappeared within its detail lifetime")
            reserved_jobs = tuple(
                identity(f"outcome/{candidate[0]}/{h}/2")
                for h in event["settings_snapshot"]["outcome_horizons"]
            )
            reserved = self.store.db.execute("SELECT COUNT(*) FROM analytics_pool_jobs").fetchone()[0]
            if len(reserved_jobs) + reserved > LIMITS["horizon_jobs"]:
                self.audit["horizon_budget_checks"] += 1
                continue
            rate = self.store.db.execute(
                "SELECT tokens,at_ms FROM analytics_pool_rate WHERE market=?", (candidate[1],)
            ).fetchone()
            tokens = 4.0 if rate is None else min(4.0, rate[0] + max(0, now - rate[1]) * 4 / MINUTE)
            if tokens < 1:
                self.audit["rate_checks"] += 1
                continue
            self.store.db.execute(
                "INSERT OR REPLACE INTO analytics_pool_rate VALUES(?,?,?)", (candidate[1], tokens - 1, now)
            )
            self.store.db.execute("INSERT INTO analytics_pool_slots VALUES(?,'WARMING')", (candidate[0],))
            self.store.db.executemany(
                "INSERT INTO analytics_pool_jobs VALUES(?,?)", ((candidate[0], job) for job in reserved_jobs)
            )
            self._peaks()
            self.original_admit(event, episode=episode)
            self.store.db.execute(
                "UPDATE analytics_pool_slots SET state='ACTIVE' WHERE id=?", (candidate[0],)
            )
            self.store.db.execute(
                "UPDATE analytics_pool_choices SET status='SELECTED',reason='RANK_AND_RESERVE',admitted_ms=? WHERE id=?",
                (now, candidate[0]),
            )
            self.audit["admissions"] += 1
        self._peaks()

    def report(self) -> dict[str, Any]:
        return {
            "mode": "BOUNDED_ANALYTICS_POOL",
            "limits": LIMITS,
            "audit": dict(self.audit),
            "peaks": dict(self.peaks),
            "states": dict(
                self.store.db.execute("SELECT status,COUNT(*) FROM analytics_pool_choices GROUP BY status")
            ),
            "selected": [
                list(row)
                for row in self.store.db.execute(
                    "SELECT id,event_id,market,reference_ms,reference_price,admitted_ms FROM analytics_pool_choices WHERE admitted_ms IS NOT NULL ORDER BY id"
                )
            ],
            "waiting": self.store.db.execute(
                "SELECT COUNT(*) FROM analytics_pool_choices WHERE status='WAITING'"
            ).fetchone()[0],
            "legacy_jobs": self.store.db.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM analytics_pool_legacy LIMIT 65)"
            ).fetchone()[0],
            "legacy_count_capped_at": 65,
            "cutover_complete": bool(
                self.store.db.execute("SELECT complete FROM analytics_pool_cutover WHERE id=1").fetchone()[0]
            ),
            "limitations": [
                "Existing obligations drain independently; no history is reset",
                "Pool limits do not bound global signals or 30-day turnover",
            ],
        }
