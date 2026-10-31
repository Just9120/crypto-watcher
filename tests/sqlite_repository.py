"""Isolated direct SQLite revision repository, never selected by production.

The prototype replaces immutable publication only. The real ingress, detectors,
episode rules, finite jobs, retention, Telegram and signed agent remain in use.
A runtime head is a small compatibility index, not another stored event body.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
import threading
import time
from contextlib import ExitStack, contextmanager
from typing import Any, Iterator
from unittest.mock import patch

from cryptowatcher.events import HEAD_FIELDS, PREFIX
from cryptowatcher.events import History as OriginalHistory
from cryptowatcher.storage import RUNTIME_UPSERT, StateError, Store, encode

KINDS = ("event", "episode", "outcome", "comparison")
SHARED = frozenset(
    {
        "source_inputs",
        "future_inputs",
        "settings_snapshot",
        "effective_parameters",
        "source_context",
        "enrichment",
    }
)
OMIT = frozenset({"source_inputs", "future_inputs", "source_context", "enrichment"})
POINT = re.compile(r"(event|episode|outcome|comparison)(-head)?:([A-Za-z0-9-]+)(?::([0-9]{10}))?\Z")
DAY = 86_400_000


def installed(store: Store) -> bool:
    return (
        store.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='prototype_snapshots' AND type='table'"
        ).fetchone()
        is not None
    )


def install(store: Store) -> None:
    if store.readonly or installed(store) or store.db.in_transaction:
        raise StateError("Prototype needs a new explicit writable installation boundary")
    if store.db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise StateError("Prototype requires actual SQLite foreign keys")
    store.db.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE prototype_snapshots(
            digest TEXT PRIMARY KEY, payload TEXT NOT NULL, refs INTEGER NOT NULL DEFAULT 0 CHECK(refs>=0)) WITHOUT ROWID;
        CREATE TABLE prototype_links(
            owner TEXT NOT NULL, field TEXT NOT NULL, digest TEXT NOT NULL REFERENCES prototype_snapshots(digest),
            PRIMARY KEY(owner,field)) WITHOUT ROWID;
        CREATE INDEX prototype_unused ON prototype_snapshots(digest) WHERE refs=0;
        CREATE TRIGGER prototype_link_insert AFTER INSERT ON prototype_links BEGIN
            UPDATE prototype_snapshots SET refs=refs+1 WHERE digest=NEW.digest; END;
        CREATE TRIGGER prototype_link_delete AFTER DELETE ON prototype_links BEGIN
            UPDATE prototype_snapshots SET refs=refs-1 WHERE digest=OLD.digest; END;
        CREATE TRIGGER prototype_snapshot_immutable BEFORE UPDATE OF digest,payload ON prototype_snapshots BEGIN
            SELECT RAISE(ABORT,'Immutable prototype snapshot'); END;
        COMMIT;
    """)
    for kind in KINDS:
        store.db.executescript(f"""
            BEGIN IMMEDIATE;
            CREATE TABLE prototype_{kind}_revisions(
                id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
                public_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, fields TEXT NOT NULL,
                clock TEXT NOT NULL, origin TEXT, reference TEXT, reference_ms INTEGER, digest TEXT NOT NULL,
                PRIMARY KEY(id,revision));
            CREATE TABLE prototype_{kind}_heads(
                id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                FOREIGN KEY(id,revision) REFERENCES prototype_{kind}_revisions(id,revision) ON DELETE CASCADE);
            CREATE TRIGGER prototype_{kind}_immutable BEFORE UPDATE ON prototype_{kind}_revisions BEGIN
                SELECT RAISE(ABORT,'Immutable prototype revision'); END;
            CREATE TRIGGER prototype_{kind}_head_monotonic BEFORE UPDATE ON prototype_{kind}_heads
                WHEN NEW.revision<OLD.revision BEGIN SELECT RAISE(ABORT,'Stale prototype head'); END;
            COMMIT;
        """)
    store.db.execute("INSERT INTO market_meta VALUES('isolated_direct_repository','1')")
    store.db.commit()


class History(OriginalHistory):
    def __new__(cls, store: Store):
        if not installed(store):
            return OriginalHistory(store)
        return object.__new__(cls)

    def __init__(self, store: Store) -> None:
        super().__init__(store)
        if not getattr(store, "_prototype_sql_function", False):

            def payload(key):
                if not isinstance(key, str):
                    return None
                value = self.get(key[len(PREFIX) :]) if key.startswith(PREFIX) else store.get_runtime(key)
                return None if value is None else encode(value)

            store.db.create_function("cw_runtime_payload", 1, payload)
            store._prototype_sql_function = True

    @staticmethod
    def point(key: str):
        found = POINT.fullmatch(key)
        if found is None:
            return None
        kind, head, identity, revision = found.groups()
        if bool(head) == bool(revision):
            return None
        return kind, identity, int(revision) if revision else None

    @staticmethod
    def checksum(identity, revision, payload, fields, clock, origin, reference, reference_ms):
        return hashlib.sha256(
            encode([identity, revision, payload, fields, clock, origin, reference, reference_ms]).encode()
        ).hexdigest()

    def _row(self, key: str):
        point = self.point(key)
        if point is None:
            return None
        kind, identity, revision = point
        if revision is None:
            row = self.store.db.execute(
                f"SELECT r.payload,r.fields,r.clock,r.digest,r.origin,r.reference,r.reference_ms,r.id,r.revision,x.payload_json FROM prototype_{kind}_heads h "
                f"JOIN prototype_{kind}_revisions r ON r.id=h.id AND r.revision=h.revision "
                "JOIN runtime x ON x.key=? WHERE h.id=?",
                (PREFIX + key, identity),
            ).fetchone()
        else:
            row = self.store.db.execute(
                f"SELECT payload,fields,clock,digest,origin,reference,reference_ms,id,revision FROM prototype_{kind}_revisions WHERE id=? AND revision=?",
                (identity, revision),
            ).fetchone()
        if row is not None:
            if row[3] != self.checksum(row[7], row[8], row[0], row[1], row[2], row[4], row[5], row[6]):
                raise StateError("Corrupt direct immutable revision")
            if revision is None:
                clock = json.loads(row[2])
                expected = self._head(
                    kind, clock, f"{kind}:{identity}:{clock['revision']:010d}", bounded=True
                )
                if encode(expected) != encode(json.loads(row[9])):
                    raise StateError("Conflicting direct history head projection")
        return row

    def _record(self, key: str, *, metadata: bool = False):
        row = self._row(key)
        if row is None:
            return None
        value, fields = json.loads(row[0]), json.loads(row[1])
        point = self.point(key)
        assert point is not None
        kind, identity, revision = point
        clock = json.loads(row[2])
        if (
            not isinstance(value, dict)
            or not isinstance(fields, dict)
            or not set(fields) <= SHARED
            or value.get(kind + "_id") != identity
            or value.get("revision") != (revision if revision is not None else clock.get("revision"))
            or value.get("schema") == "cryptowatcher.history-head.v1"
            or encode(
                self._head(kind, value, f"{kind}:{identity}:{value.get('revision', 0):010d}", bounded=True)
            )
            != encode(
                self._head(kind, clock, f"{kind}:{identity}:{clock.get('revision', 0):010d}", bounded=True)
            )
        ):
            raise StateError("Invalid direct revision fields")
        for name, digest in fields.items():
            if metadata and name in OMIT:
                continue
            raw = self.store.db.execute(
                "SELECT payload FROM prototype_snapshots WHERE digest=?", (digest,)
            ).fetchone()
            if raw is None or hashlib.sha256(raw[0].encode()).hexdigest() != digest:
                raise StateError("Missing or corrupt direct snapshot")
            value[name] = json.loads(raw[0])
        if metadata:
            value = {k: v for k, v in value.items() if k not in OMIT}
        return value

    def get(self, key: str, default: Any = None) -> Any:
        if self.point(key) is None:
            return super().get(key, default)
        value = self._record(key)
        return default if value is None else value

    def metadata(self, key: str) -> Any:
        if self.point(key) is None:
            return super().metadata(key)
        return self._record(key, metadata=True)

    def episode_clock(self, key: str) -> Any:
        if self.point(key) is None or not key.startswith("episode-head:"):
            return super().episode_clock(key)
        row = self._row(key)
        return json.loads(row[2]) if row else None

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
                or self.point(value["_cw_revision"]) is None
                or self.point(value["_cw_revision"])[2] is None
            ):
                raise StateError("Invalid service revision reference")
            restored = self.get(value["_cw_revision"])
            if restored is None or hashlib.sha256(encode(restored).encode()).hexdigest() != value["sha256"]:
                raise StateError("Missing or conflicting retained service revision")
            return self._expand_services(restored, depth + 1)
        return {k: self._expand_services(v, depth + 1) for k, v in value.items()}

    def version(self, kind: str, value: dict[str, Any]) -> None:
        if kind not in KINDS or type(value.get("revision")) is not int or value["revision"] < 1:
            raise StateError("Invalid direct revision identity")
        identity, revision = value.get(kind + "_id"), value["revision"]
        if (
            not isinstance(identity, str)
            or re.fullmatch(r"[A-Za-z0-9-]+", identity) is None
            or "_history_storage" in value
        ):
            raise StateError("Invalid direct public record")
        key = f"{kind}:{identity}:{revision:010d}"
        if not self.store.db.in_transaction:
            self.store.db.execute("BEGIN IMMEDIATE")
        from cryptowatcher.packet_store import atomic

        with atomic(self.store.db):
            self._row(kind + "-head:" + identity)
            previous = self.get(key)
            if previous is not None:
                if encode(previous) != encode(value):
                    raise StateError("Conflicting immutable history revision")
            else:
                head = self.store.db.execute(
                    f"SELECT r.origin,r.reference,r.reference_ms FROM prototype_{kind}_heads h "
                    f"JOIN prototype_{kind}_revisions r ON r.id=h.id AND r.revision=h.revision WHERE h.id=?",
                    (identity,),
                ).fetchone()
                if head and kind in ("event", "episode"):
                    frozen = (
                        value.get("origin_event_id"),
                        encode(value.get("reference_price")),
                        value.get("reference_time_ms"),
                    )
                    if (kind == "event" and tuple(head)[1:] != frozen[1:]) or (
                        kind == "episode" and tuple(head) != frozen
                    ):
                        raise StateError("Episode or event outcome reference is immutable")
                body, fields = dict(value), {}
                for name in SHARED & value.keys():
                    raw = encode(value[name])
                    if len(raw.encode()) <= 128:
                        continue
                    digest = hashlib.sha256(raw.encode()).hexdigest()
                    found = self.store.db.execute(
                        "SELECT payload FROM prototype_snapshots WHERE digest=?", (digest,)
                    ).fetchone()
                    if found is not None and found[0] != raw:
                        raise StateError("Conflicting direct snapshot")
                    if found is None:
                        self.store.db.execute(
                            "INSERT INTO prototype_snapshots(digest,payload) VALUES(?,?)", (digest, raw)
                        )
                    self.store.db.execute("INSERT INTO prototype_links VALUES(?,?,?)", (key, name, digest))
                    fields[name] = digest
                    del body[name]
                clock = {
                    k: value[k]
                    for k in (*HEAD_FIELDS[kind], "origin_event_id", "reference_price", "reference_time_ms")
                    if k in value
                }
                self.store.db.execute(
                    f"INSERT INTO prototype_{kind}_revisions VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        identity,
                        revision,
                        key,
                        encode(body),
                        encode(fields),
                        encode(clock),
                        value.get("origin_event_id"),
                        encode(value.get("reference_price")),
                        value.get("reference_time_ms"),
                        self.checksum(
                            identity,
                            revision,
                            encode(body),
                            encode(fields),
                            encode(clock),
                            value.get("origin_event_id"),
                            encode(value.get("reference_price")),
                            value.get("reference_time_ms"),
                        ),
                    ),
                )
                if kind in ("event", "episode") and revision == 1:
                    self.put(kind + "-counter", self.get(kind + "-counter", 0) + 1)
            self.store.db.execute(
                f"INSERT INTO prototype_{kind}_heads VALUES(?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision WHERE excluded.revision>revision",
                (identity, revision),
            )
            actual = self.store.db.execute(
                f"SELECT revision FROM prototype_{kind}_heads WHERE id=?", (identity,)
            ).fetchone()[0]
            if actual == revision:
                # Explicit small compatibility index; no retained body copy.
                self.store.db.execute(
                    RUNTIME_UPSERT,
                    (PREFIX + kind + "-head:" + identity, encode(self._head(kind, value, key, bounded=True))),
                )
                from cryptowatcher.expiry_index import put as put_expiry

                if kind == "event":
                    expiry = (
                        value["market_observation_ms"]
                        + int(value["settings_snapshot"]["detail_retention_days"]) * DAY
                    )
                    parent = identity
                elif kind == "episode":
                    expiry = value["started_ms"] + self.store.snapshot().settings.detail_retention_days * DAY
                    parent = value.get("origin_event_id")
                else:
                    from cryptowatcher.expiry_index import hot_binding

                    parent = value.get("event_id")
                    binding = hot_binding(self.store.db, PREFIX + "event-head:" + str(parent))
                    expiry = (
                        binding[0]
                        if binding
                        else self.store.clock_ms()
                        + self.store.snapshot().settings.detail_retention_days * DAY
                    )
                put_expiry(self.store.db, PREFIX + kind + "-head:" + identity, expiry, kind + "-head", parent)

    def put(self, key: str, value: Any) -> None:
        point = self.point(key)
        if point is None:
            return super().put(key, value)
        kind, identity, revision = point
        if revision is not None:
            if value.get(kind + "_id") != identity or value.get("revision") != revision:
                raise StateError("Conflicting direct identity")
            self.version(kind, value)
            return
        raise StateError("Direct heads may only be published with their immutable revision")

    def _delete_raw(self, key: str) -> None:
        point = self.point(key)
        if point is not None:
            kind, identity, revision = point
            if revision is None:
                self.store.db.execute(f"DELETE FROM prototype_{kind}_heads WHERE id=?", (identity,))
            else:
                self.store.db.execute("DELETE FROM prototype_links WHERE owner=?", (key,))
                self.store.db.execute(
                    f"DELETE FROM prototype_{kind}_revisions WHERE id=? AND revision=?", (identity, revision)
                )
        super()._delete_raw(key)

    def scan(self, prefix: str, *, reverse: bool = False) -> Iterator[tuple[str, Any]]:
        names = [key for key, _ in super().scan(prefix, reverse=reverse)]
        for kind in KINDS:
            if prefix.startswith(kind + ":") or (kind + ":").startswith(prefix):
                names.extend(
                    r[0]
                    for r in self.store.db.execute(
                        f"SELECT public_key FROM prototype_{kind}_revisions WHERE public_key>=? AND public_key<?",
                        (prefix, prefix + "\uffff"),
                    )
                )
        for key in sorted(set(names), reverse=reverse):
            yield key, self.get(key)

    def items(self, prefix: str, limit: int | None = None, *, reverse: bool = False):
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ValueError("Invalid history scan limit")
        # Ordinary mutable pending prefixes keep their indexed bounded read.
        if not any(prefix.startswith(k) or k.startswith(prefix) for k in KINDS):
            return super().items(prefix, limit, reverse=reverse)
        from itertools import islice

        return list(islice(self.scan(prefix, reverse=reverse), limit))

    def count(self, prefix: str) -> int:
        if any(prefix.startswith(k + ":") or (k + ":").startswith(prefix) for k in KINDS):
            return sum(1 for _ in self.scan(prefix))
        return super().count(prefix)


def maintenance(store: Store, now: int) -> dict[str, Any]:
    removed = 0
    with store.db:
        for kind in KINDS:
            stale = store.db.execute(
                f"SELECT h.id FROM prototype_{kind}_heads h WHERE NOT EXISTS(SELECT 1 FROM runtime WHERE key=?||h.id) LIMIT 64",
                (PREFIX + kind + "-head:",),
            ).fetchall()
            for (identity,) in stale:
                owners = store.db.execute(
                    f"SELECT public_key FROM prototype_{kind}_revisions WHERE id=?", (identity,)
                ).fetchall()
                for (owner,) in owners:
                    store.db.execute("DELETE FROM prototype_links WHERE owner=?", (owner,))
                store.db.execute(f"DELETE FROM prototype_{kind}_heads WHERE id=?", (identity,))
                removed += store.db.execute(
                    f"DELETE FROM prototype_{kind}_revisions WHERE id=?", (identity,)
                ).rowcount
        removed += store.db.execute(
            "DELETE FROM prototype_snapshots WHERE digest IN (SELECT digest FROM prototype_snapshots WHERE refs=0 LIMIT 64)"
        ).rowcount
    # Actual SQLite maintenance, not fabricated progress or a disabled GC stub.
    store.db.execute("PRAGMA incremental_vacuum(16)")
    checkpoint = tuple(store.db.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())
    value = {
        "state": "DIRECT_SQLITE_CORE",
        "at_ms": now,
        "removed": removed,
        "checkpoint": checkpoint,
        "initial_reclaim_complete": True,
    }
    store.put_runtime("storage_repack", value)
    return value


def stats(store: Store) -> dict[str, Any]:
    return {
        "mode": "ISOLATED_DIRECT_SQLITE_REPOSITORY",
        "revisions": {
            kind: store.db.execute(f"SELECT COUNT(*) FROM prototype_{kind}_revisions").fetchone()[0]
            for kind in KINDS
        },
        "snapshots": list(
            store.db.execute(
                "SELECT COUNT(*),COALESCE(SUM(length(CAST(payload AS BLOB))),0),COALESCE(SUM(refs),0) FROM prototype_snapshots"
            ).fetchone()
        ),
        "foreign_key_violations": [list(row) for row in store.db.execute("PRAGMA foreign_key_check")],
        "limitations": [
            "Fresh isolated DB only; no migration or production activation",
            "Legacy mutable jobs/deliveries/source and retention are actual existing components",
            "Annual storage layout unchanged; mature capacity and full integration are not qualified",
        ],
    }


@contextmanager
def repository_context(enabled: bool):
    if not enabled:
        yield
        return
    from cryptowatcher import analytics_worker, catalogue_sql
    from cryptowatcher.bot import Controller

    original_source = catalogue_sql.source
    original_reader = analytics_worker.read_snapshot
    reader_lock = threading.Lock()
    active_readers = 0

    def read_snapshot(*args, **kwargs):
        nonlocal active_readers
        with reader_lock:
            active_readers += 1
        try:
            return original_reader(*args, **kwargs)
        finally:
            with reader_lock:
                active_readers -= 1

    async def finish_readers():
        # Cancelling to_thread's awaiter does not stop an already running reader.
        # Join actual snapshots before restoring adapters/closing the fixture.
        deadline = time.monotonic() + 5
        while True:
            with reader_lock:
                if active_readers == 0:
                    return
            if time.monotonic() >= deadline:
                raise RuntimeError("Prototype readonly snapshot did not terminate")
            await asyncio.sleep(0.01)

    originals = [
        module
        for name, module in list(sys.modules.items())
        if name.startswith("cryptowatcher.") and getattr(module, "History", None) is OriginalHistory
    ]
    source_aliases = [
        module
        for name, module in list(sys.modules.items())
        if name.startswith("cryptowatcher.") and getattr(module, "source", None) is original_source
    ]

    def source(store, kind, *, head=False):
        if not installed(store):
            return original_source(store, kind, head=head)
        if kind not in KINDS or type(head) is not bool:
            raise ValueError("Unsupported direct domain namespace")
        History(store)  # Register the real readonly connection's current-snapshot adapter.
        if head:
            root = PREFIX + kind + "-head:"
            return f"(SELECT key,cw_runtime_payload(key) AS payload_json FROM runtime WHERE key>='{root}' AND key<'{root}~')"
        return f"(SELECT '{PREFIX}'||public_key AS key,cw_runtime_payload('{PREFIX}'||public_key) AS payload_json FROM prototype_{kind}_revisions)"

    async def repack(self, context):
        if not isinstance(self.history, History):
            raise StateError("Unexpected repository in prototype maintenance")
        if self.repack_lock.locked() or not self.maintenance_ready():
            return
        async with self.repack_lock:
            self.market._measure("storage_repack", lambda: maintenance(self.store, self.market.server_now()))

    with ExitStack() as stack:
        for module in originals:
            stack.enter_context(patch.object(module, "History", History))
        for module in source_aliases:
            stack.enter_context(patch.object(module, "source", source))
        stack.enter_context(patch.object(Controller, "repack_job", repack))
        stack.enter_context(patch.object(analytics_worker, "read_snapshot", read_snapshot))
        try:
            yield finish_readers
        finally:
            for name, module in list(sys.modules.items()):
                if not name.startswith("cryptowatcher."):
                    continue
                if module not in originals and getattr(module, "History", None) is History:
                    module.History = OriginalHistory
                if module not in source_aliases and getattr(module, "source", None) is source:
                    module.source = original_source
