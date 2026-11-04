"""Explicit subject jobs and deliveries for the isolated service trial.

Installation is never selected by normal startup. Only the comparative fixture
calls install(). Existing runtime rows stay with their original owner; there is
no conversion, reset, SQL cache or TEMP counter overlay.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import wraps
from typing import Any

from .storage import StateError, Store, encode

DAY = 86_400_000
VERSION = "1"
TABLES = {
    "subject_sources": "subject_source_links",
    "subject_outcomes": "subject_outcome_work",
    "subject_deliveries": "subject_delivery_work",
    "subject_delivery_payloads": "subject_delivery_payloads",
    "subject_attempts": "subject_delivery_attempts",
}
DDL: tuple[str, ...] = (
    """CREATE TABLE subject_sources(
        event_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
        episode_id TEXT NOT NULL, reference_ms INTEGER NOT NULL,
        reference_price TEXT NOT NULL, expires_ms INTEGER NOT NULL,
        digest TEXT NOT NULL CHECK(length(digest)=64),
        PRIMARY KEY(event_id,revision),
        FOREIGN KEY(event_id,revision) REFERENCES direct_event_revisions(id,revision)
    ) WITHOUT ROWID""",
    """CREATE TRIGGER subject_source_immutable BEFORE UPDATE ON subject_sources
        BEGIN SELECT RAISE(ABORT,'Immutable subject source binding'); END""",
    """CREATE TABLE subject_outcomes(
        id TEXT PRIMARY KEY, event_id TEXT NOT NULL, event_revision INTEGER NOT NULL,
        episode_id TEXT NOT NULL, horizon INTEGER NOT NULL CHECK(horizon>0 AND horizon<=4320),
        method TEXT NOT NULL, stream INTEGER NOT NULL REFERENCES market_streams(id),
        stream_key TEXT NOT NULL, due_ms INTEGER NOT NULL, next_ms INTEGER NOT NULL,
        expires_ms INTEGER NOT NULL, state TEXT NOT NULL,
        pending INTEGER NOT NULL CHECK(pending IN(0,1)),
        held INTEGER NOT NULL CHECK(held IN(0,1)),
        hold_start INTEGER NOT NULL, hold_end INTEGER NOT NULL,
        extra TEXT NOT NULL CHECK(json_valid(extra)), queue_ms INTEGER NOT NULL,
        FOREIGN KEY(event_id,event_revision) REFERENCES subject_sources(event_id,revision),
        UNIQUE(episode_id,horizon,method), CHECK(hold_end>=hold_start)
    ) WITHOUT ROWID""",
    """CREATE INDEX subject_outcome_due ON subject_outcomes(pending,queue_ms,id)""",
    """CREATE INDEX subject_outcome_expiry ON subject_outcomes(expires_ms,id)""",
    """CREATE INDEX subject_outcome_hold ON subject_outcomes(stream,held,hold_start,hold_end)""",
    """CREATE TRIGGER subject_outcome_origin BEFORE UPDATE OF id,event_id,event_revision,
        episode_id,horizon,method,stream,stream_key,due_ms,expires_ms,hold_start,hold_end
        ON subject_outcomes WHEN NEW.id<>OLD.id OR NEW.event_id<>OLD.event_id
        OR NEW.event_revision<>OLD.event_revision OR NEW.episode_id<>OLD.episode_id
        OR NEW.horizon<>OLD.horizon OR NEW.method<>OLD.method OR NEW.stream<>OLD.stream
        OR NEW.stream_key<>OLD.stream_key OR NEW.due_ms<>OLD.due_ms
        OR NEW.expires_ms<>OLD.expires_ms OR NEW.hold_start<>OLD.hold_start
        OR NEW.hold_end<>OLD.hold_end
        BEGIN SELECT RAISE(ABORT,'Immutable subject outcome origin'); END""",
    """CREATE TABLE subject_deliveries(
        key TEXT PRIMARY KEY, channel TEXT NOT NULL CHECK(channel IN('telegram','agent')),
        stage TEXT NOT NULL CHECK(stage IN('intent','outbox')),
        id TEXT NOT NULL, event_id TEXT NOT NULL, event_revision INTEGER NOT NULL,
        episode_id TEXT NOT NULL, due_ms INTEGER NOT NULL, expires_ms INTEGER NOT NULL,
        state TEXT NOT NULL, historical INTEGER NOT NULL CHECK(historical IN(0,1)),
        queued INTEGER NOT NULL CHECK(queued IN(0,1)),
        mutable TEXT NOT NULL CHECK(json_valid(mutable)), queue_id TEXT NOT NULL,
        ready INTEGER NOT NULL CHECK(ready IN(0,1)), fresh_ms INTEGER NOT NULL,
        FOREIGN KEY(event_id,event_revision) REFERENCES subject_sources(event_id,revision),
        UNIQUE(channel,stage,id)
    ) WITHOUT ROWID""",
    """CREATE INDEX subject_delivery_due ON subject_deliveries(channel,stage,queued,historical,due_ms,queue_id)""",
    """CREATE UNIQUE INDEX subject_delivery_queue_identity ON subject_deliveries(channel,stage,queue_id)""",
    """CREATE INDEX subject_delivery_queue ON subject_deliveries(channel,stage,queued,due_ms,queue_id)""",
    """CREATE INDEX subject_delivery_ready ON subject_deliveries(channel,stage,queued,ready,historical,due_ms,queue_id)""",
    """CREATE INDEX subject_delivery_parent ON subject_deliveries(event_id,event_revision)""",
    """CREATE INDEX subject_outcome_parent ON subject_outcomes(event_id,event_revision)""",
    """CREATE INDEX subject_delivery_expiry ON subject_deliveries(expires_ms,key)""",
    """CREATE TRIGGER subject_outbox_source BEFORE UPDATE OF event_id,event_revision,id,channel,stage
        ON subject_deliveries WHEN OLD.stage='outbox'
        AND EXISTS(SELECT 1 FROM subject_delivery_payloads p WHERE p.key=OLD.key AND p.frozen=1) AND
        (NEW.event_id<>OLD.event_id OR NEW.event_revision<>OLD.event_revision OR NEW.id<>OLD.id
        OR NEW.channel<>OLD.channel OR NEW.stage<>OLD.stage)
        BEGIN SELECT RAISE(ABORT,'Immutable delivery source'); END""",
    """CREATE TABLE subject_delivery_payloads(
        key TEXT PRIMARY KEY REFERENCES subject_deliveries(key) ON DELETE CASCADE,
        payload TEXT NOT NULL CHECK(json_valid(payload)),
        digest TEXT NOT NULL CHECK(length(digest)=64),
        frozen INTEGER NOT NULL CHECK(frozen IN(0,1))
    ) WITHOUT ROWID""",
    """CREATE TRIGGER subject_payload_frozen BEFORE UPDATE ON subject_delivery_payloads
        WHEN OLD.frozen=1 AND (NEW.payload<>OLD.payload OR NEW.digest<>OLD.digest OR NEW.frozen<>1)
        BEGIN SELECT RAISE(ABORT,'Immutable delivery retry payload'); END""",
    """CREATE TABLE subject_attempts(
        key TEXT PRIMARY KEY, delivery_id TEXT NOT NULL, number INTEGER NOT NULL CHECK(number>0),
        event_id TEXT NOT NULL, event_revision INTEGER NOT NULL, expires_ms INTEGER NOT NULL,
        payload TEXT NOT NULL CHECK(json_valid(payload)), digest TEXT NOT NULL CHECK(length(digest)=64),
        FOREIGN KEY(event_id,event_revision) REFERENCES subject_sources(event_id,revision),
        UNIQUE(delivery_id,number)
    ) WITHOUT ROWID""",
    """CREATE INDEX subject_attempt_parent ON subject_attempts(event_id,event_revision)""",
    """CREATE INDEX subject_attempt_expiry ON subject_attempts(expires_ms,key)""",
    """CREATE TRIGGER subject_attempt_immutable BEFORE UPDATE ON subject_attempts
        BEGIN SELECT RAISE(ABORT,'Immutable subject delivery attempt'); END""",
)
DDL += (
    """CREATE VIEW subject_holds AS SELECT key,stream,start,end,expires FROM market_holds
        UNION ALL SELECT 'radar:v1:hold:'||stream_key||':'||id,stream,hold_start,hold_end,expires_ms
        FROM subject_outcomes WHERE held=1""",
)

OUTCOME_FIELDS = frozenset(
    {
        "outcome_id",
        "event_id",
        "event_revision",
        "horizon_min",
        "methodology_version",
        "stream",
        "state",
        "next_attempt_ms",
        "due_ms",
        "pending_key",
        "hold_key",
    }
)
PAYLOAD_FIELDS = {
    ("telegram", "outbox"): frozenset(
        {
            "episode_snapshot",
            "event_ids",
            "event_revisions",
            "notification_state",
            "text",
            "text_entities",
            "reason",
        }
    ),
    ("agent", "intent"): frozenset({"episode", "outcomes"}),
    ("agent", "outbox"): frozenset({"body", "body_sha256", "intent", "notice"}),
}


def checksum(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def definition_names() -> tuple[str, ...]:
    names = []
    for sql in DDL:
        match = re.match(r"CREATE (?:UNIQUE )?(?:TABLE|INDEX|TRIGGER|VIEW) (\w+)", sql)
        if match is None:
            raise StateError("Invalid owned subject schema definition")
        names.append(match[1])
    return tuple(names)


def installed(store: Store) -> bool:
    if not store.bounded:
        return False
    marker = store.db.execute("SELECT value FROM market_meta WHERE key='subject_services'").fetchone()
    if marker is None:
        return False
    if marker[0] != VERSION:
        raise StateError("Unknown subject service layout; data preserved")
    return True


def validate(store: Store, *, meter: bool = False) -> None:
    names = definition_names()
    rows = dict(
        store.db.execute(
            "SELECT name,sql FROM sqlite_master WHERE name IN(" + ",".join("?" for _ in names) + ")", names
        )
    )
    expected = {name: " ".join(sql.split()) for name, sql in zip(names, DDL, strict=True)}
    if {name: " ".join(sql.split()) for name, sql in rows.items()} != expected:
        raise StateError("Unknown or incomplete subject service schema; data preserved")
    if meter:
        from .repository_meter import trigger_sql

        for table, category in TABLES.items():
            columns = [row[1] for row in store.db.execute("PRAGMA table_info(" + table + ")")]
            for name, expected_sql in trigger_sql(table, category, columns).items():
                actual = store.db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
                if actual is None or " ".join(actual[0].split()) != " ".join(expected_sql.split()):
                    raise StateError("Unknown subject accounting trigger; data preserved")


def install(store: Store) -> Repository:
    if store.readonly or not store.bounded or not store.writer_connection or store.db.in_transaction:
        raise StateError("Subject installation requires an idle bounded owned writer")
    store.snapshot()
    if not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='direct_event_revisions'").fetchone():
        raise StateError("Subject services require the installed domain repository")
    if installed(store):
        validate(store)
    else:
        if any(
            store.db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone()
            for name in definition_names()
        ):
            raise StateError("Unmarked subject service data preserved")
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            for sql in DDL:
                store.db.execute(sql)
            store.db.execute("INSERT INTO market_meta VALUES('subject_services',?)", (VERSION,))
        validate(store)
    from .repository_meter import install as install_meter

    install_meter(store, "subject", TABLES)
    repository = Repository(store)
    store.services = repository
    setattr(store.db, "subject_services", True)
    return repository


@dataclass(frozen=True)
class Source:
    event_id: str
    revision: int
    episode_id: str
    reference_ms: int
    reference_price: str
    expires_ms: int
    digest: str


def atomic_method(method: Any) -> Any:
    @wraps(method)
    def operation(self: Any, *args: Any, **kwargs: Any) -> Any:
        from .packet_store import atomic

        self.check(write=True)
        with atomic(self.store.db):
            return method(self, *args, **kwargs)

    return operation


class Repository:
    def __init__(self, store: Store):
        self.store = store
        self.owner = store.owner_id
        self.stats: dict[str, int] = {
            name: 0
            for name in (
                "sources",
                "job_reservations",
                "job_updates",
                "delivery_writes",
                "status_only_updates",
                "payload_writes",
                "attempt_writes",
                "expired",
            )
        }

    def check(self, *, write: bool = False) -> None:
        if self.owner != self.store.owner_id or write and self.store.readonly:
            raise StateError("Subject service owner or write permissions changed")
        if write and not self.store.db.in_transaction:
            raise StateError("Subject mutation needs its owning transaction")
        if write:
            self.store.snapshot()

    def source(self, history: Any, event_id: str, revision: int) -> Source | None:
        self.check(write=True)
        if type(revision) is not int or revision < 1:
            raise StateError("Invalid subject source revision")
        saved = self.store.db.execute(
            "SELECT * FROM subject_sources WHERE event_id=? AND revision=?", (event_id, revision)
        ).fetchone()
        if saved is not None:
            return Source(*tuple(saved))
        # Only new typed identities enter this repository. A legacy identity is
        # left on its original path; no old source is converted or discarded.
        row = history._row(f"event:{event_id}:{revision:010d}")
        if row is None:
            return None
        event = history.metadata(f"event:{event_id}:{revision:010d}")
        if event is None:
            raise StateError("Missing published subject event")
        from .revision_repository import retained_deadline

        source = Source(
            event_id,
            revision,
            event["episode_id"],
            event["reference_time_ms"],
            encode(event["reference_price"]),
            retained_deadline(self.store, "event", event_id, event),
            row[3],
        )
        self.store.db.execute(
            "INSERT INTO subject_sources VALUES(?,?,?,?,?,?,?)", tuple(source.__dict__.values())
        )
        self.stats["sources"] += 1
        return source

    @atomic_method
    def reserve(self, history: Any, job: dict[str, Any], event: dict[str, Any]) -> bool:
        self.check(write=True)
        source = self.source(history, job["event_id"], job["event_revision"])
        if source is None:
            return False
        if event["event_id"] != source.event_id or event["revision"] != source.revision:
            raise StateError("Conflicting subject admission event")
        horizon = job["horizon_min"]
        if type(horizon) is not int or not 0 < horizon <= 4320:
            raise StateError("Subject horizon outside72hours")
        if job["due_ms"] != source.reference_ms + horizon * 60_000:
            raise StateError("Subject admission cannot shift its immutable origin")
        from .market_storage import stream_id

        sid = stream_id(self.store.db, job["stream"], create=True)
        extra = encode({key: value for key, value in job.items() if key not in OUTCOME_FIELDS})
        self.store.db.execute(
            "INSERT INTO subject_outcomes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job["outcome_id"],
                source.event_id,
                source.revision,
                source.episode_id,
                horizon,
                job.get("methodology_version", "1"),
                sid,
                job["stream"],
                job["due_ms"],
                job["next_attempt_ms"],
                source.expires_ms,
                job["state"],
                1,
                1,
                source.reference_ms,
                job["due_ms"],
                extra,
                job["next_attempt_ms"],
            ),
        )
        self.stats["job_reservations"] += 1
        return True

    @staticmethod
    def job(row: Any) -> dict[str, Any]:
        from .outcomes import pending_key

        result = json.loads(row[16])
        result.update(
            outcome_id=row[0],
            event_id=row[1],
            event_revision=row[2],
            horizon_min=row[4],
            methodology_version=row[5],
            stream=row[7],
            due_ms=row[8],
            next_attempt_ms=row[9],
            state=row[11],
            pending_key=pending_key(row[9], row[0]),
            hold_key=f"hold:{row[7]}:{row[0]}",
        )
        return result

    def get_job(self, identity: str) -> dict[str, Any] | None:
        self.check()
        row = self.store.db.execute("SELECT * FROM subject_outcomes WHERE id=?", (identity,)).fetchone()
        return None if row is None else self.job(row)

    def update_job(self, job: dict[str, Any]) -> bool:
        self.check(write=True)
        row = self.store.db.execute(
            "SELECT * FROM subject_outcomes WHERE id=?", (job["outcome_id"],)
        ).fetchone()
        if row is None:
            return False
        original = self.job(row)
        fields = ("event_id", "event_revision", "horizon_min", "methodology_version", "stream", "due_ms")
        if any(
            job.get(name, "1" if name == "methodology_version" else None) != original[name] for name in fields
        ):
            raise StateError("Subject job origin is immutable")
        self.store.db.execute(
            "UPDATE subject_outcomes SET next_ms=?,state=?,extra=? WHERE id=?",
            (
                job["next_attempt_ms"],
                job["state"],
                encode({k: v for k, v in job.items() if k not in OUTCOME_FIELDS}),
                job["outcome_id"],
            ),
        )
        self.stats["job_updates"] += 1
        return True

    def pending_jobs(self, limit: int) -> list[tuple[str, dict[str, Any]]]:
        self.check()
        rows = self.store.db.execute(
            "SELECT id,queue_ms FROM subject_outcomes WHERE pending=1 ORDER BY queue_ms,id LIMIT ?", (limit,)
        ).fetchall()
        from .outcomes import pending_key

        return [(pending_key(due, identity), {"outcome_id": identity}) for identity, due in rows]

    def finish_job(self, identity: str) -> bool:
        self.check(write=True)
        changed = self.store.db.execute("DELETE FROM subject_outcomes WHERE id=?", (identity,)).rowcount
        return changed > 0

    @atomic_method
    def write_delivery(
        self,
        history: Any,
        key: str,
        record: dict[str, Any],
        *,
        channel: str,
        stage: str,
        event_id: str,
        revision: int,
        historical: bool,
        due_ms: int,
        expires_ms: int,
        frozen: bool,
    ) -> bool:
        self.check(write=True)
        source = self.source(history, event_id, revision)
        if source is None:
            return False
        payload_fields = PAYLOAD_FIELDS[(channel, stage)]
        body = encode({k: v for k, v in record.items() if k in payload_fields})
        mutable = encode({k: v for k, v in record.items() if k not in payload_fields})
        digest = checksum(body)
        from .storage_lifecycle import TERMINAL_DELIVERY

        terminal = record.get("state") in TERMINAL_DELIVERY
        if terminal:
            expires_ms = min(expires_ms, record.get("completed_ms", self.store.clock_ms()) + DAY)
        old = self.store.db.execute(
            "SELECT channel,stage FROM subject_deliveries WHERE key=?", (key,)
        ).fetchone()
        if old is not None and tuple(old) != (channel, stage):
            raise StateError("Subject delivery kind cannot change")
        from .events import identity as key_identity

        identity = record.get("delivery_id", key.split(":", 2)[-1])
        state = record.get("state", "PENDING")
        self.store.db.execute(
            "INSERT INTO subject_deliveries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET event_id=excluded.event_id,event_revision=excluded.event_revision,"
            "due_ms=excluded.due_ms,expires_ms=excluded.expires_ms,state=excluded.state,"
            "historical=excluded.historical,mutable=excluded.mutable",
            (
                key,
                channel,
                stage,
                identity,
                source.event_id,
                source.revision,
                source.episode_id,
                due_ms,
                min(expires_ms, source.expires_ms),
                state,
                int(historical),
                0,
                mutable,
                key_identity(key) if stage == "intent" else identity,
                0,
                record.get("expires_ms", record.get("fresh_until_ms", source.expires_ms)),
            ),
        )
        saved = self.store.db.execute(
            "SELECT payload,digest,frozen FROM subject_delivery_payloads WHERE key=?", (key,)
        ).fetchone()
        if saved is None:
            self.store.db.execute(
                "INSERT INTO subject_delivery_payloads VALUES(?,?,?,?)", (key, body, digest, int(frozen))
            )
            self.stats["payload_writes"] += 1
        elif tuple(saved) != (body, digest, int(frozen)):
            if terminal:
                self.store.db.execute("DELETE FROM subject_delivery_payloads WHERE key=?", (key,))
                self.store.db.execute(
                    "INSERT INTO subject_delivery_payloads VALUES(?,?,?,?)", (key, body, digest, 1)
                )
                self.stats["payload_writes"] += 1
                return True
            self.store.db.execute(
                "UPDATE subject_delivery_payloads SET payload=?,digest=?,frozen=? WHERE key=?",
                (body, digest, int(frozen), key),
            )
            self.stats["payload_writes"] += 1
        self.stats["delivery_writes"] += 1
        return True

    def get_delivery(self, key: str) -> dict[str, Any] | None:
        self.check()
        row = self.store.db.execute(
            "SELECT d.mutable,p.payload,p.digest FROM subject_deliveries d "
            "JOIN subject_delivery_payloads p USING(key) WHERE d.key=?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        if checksum(row[1]) != row[2]:
            raise StateError("Corrupt subject delivery payload")
        return {**json.loads(row[1]), **json.loads(row[0])}

    def update_delivery(self, key: str, record: dict[str, Any]) -> bool:
        """Known status transition: never encode or rewrite the frozen payload."""
        self.check(write=True)
        row = self.store.db.execute(
            "SELECT channel,stage,expires_ms FROM subject_deliveries WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return False
        fields = PAYLOAD_FIELDS[(row[0], row[1])]
        if fields & record.keys():
            raise StateError("Status-only transition cannot accept immutable payload fields")
        mutable = encode(record)
        due = record.get("next_attempt_ms", record.get("next_ms", record.get("due_ms")))
        if type(due) is not int:
            raise StateError("Missing subject delivery schedule")
        self.store.db.execute(
            "UPDATE subject_deliveries SET state=?,due_ms=?,mutable=?,expires_ms=min(expires_ms,?) WHERE key=?",
            (
                record.get("state", "PENDING"),
                due,
                mutable,
                terminal_deadline(record, row[2], self.store.clock_ms()),
                key,
            ),
        )
        self.stats["status_only_updates"] += 1
        return True


def terminal_deadline(record: dict[str, Any], expiry: int, now: int) -> int:
    from .storage_lifecycle import TERMINAL_DELIVERY

    return (
        min(expiry, record.get("completed_ms", now) + DAY)
        if record.get("state") in TERMINAL_DELIVERY
        else expiry
    )


def holds_table(db: Any) -> str:
    return "subject_holds" if getattr(db, "subject_services", False) else "market_holds"
