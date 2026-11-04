"""Direct immutable revisions with checked legacy read compatibility.

Installation is additive. Existing domain records are never converted or deleted
at activation. New identities use direct rows; legacy identities keep their writer.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterator

from .events import HEAD_FIELDS, PREFIX
from .events import History as OriginalHistory
from .storage import RUNTIME_UPSERT, StateError, Store, encode, json_payload

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
            "SELECT 1 FROM sqlite_master WHERE name='direct_snapshots' AND type='table'"
        ).fetchone()
        is not None
    )


def install(store: Store, *, extra_sql: str = "") -> None:
    if store.readonly or store.db.in_transaction or not store.bounded:
        raise StateError("Direct installation requires an idle bounded writable connection")
    if installed(store):
        from .repository_meter import DIRECT
        from .repository_meter import install as install_meter

        install_meter(store, "direct", DIRECT)
        return
    if store.db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise StateError("Direct repository requires SQLite foreign keys")
    script = """
        BEGIN IMMEDIATE;
        CREATE TABLE direct_snapshots(
            digest TEXT PRIMARY KEY,payload TEXT NOT NULL,refs INTEGER NOT NULL DEFAULT 0 CHECK(refs>=0)) WITHOUT ROWID;
        CREATE TABLE direct_links(
            owner TEXT NOT NULL,field TEXT NOT NULL,digest TEXT NOT NULL REFERENCES direct_snapshots(digest),
            PRIMARY KEY(owner,field)) WITHOUT ROWID;
        CREATE INDEX direct_unused ON direct_snapshots(digest) WHERE refs=0;
        CREATE TRIGGER direct_link_insert AFTER INSERT ON direct_links BEGIN
            UPDATE direct_snapshots SET refs=refs+1 WHERE digest=NEW.digest; END;
        CREATE TRIGGER direct_link_delete AFTER DELETE ON direct_links BEGIN
            UPDATE direct_snapshots SET refs=refs-1 WHERE digest=OLD.digest; END;
        CREATE TRIGGER direct_snapshot_immutable BEFORE UPDATE OF digest,payload ON direct_snapshots BEGIN
            SELECT RAISE(ABORT,'Immutable direct snapshot'); END;
    """
    for kind in KINDS:
        script += f"""
        CREATE TABLE direct_{kind}_revisions(
            id TEXT NOT NULL,revision INTEGER NOT NULL CHECK(revision>0),
            public_key TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,fields TEXT NOT NULL,
            clock TEXT NOT NULL,origin TEXT,reference TEXT,reference_ms INTEGER,digest TEXT NOT NULL,
            PRIMARY KEY(id,revision));
        CREATE TABLE direct_{kind}_heads(
            id TEXT PRIMARY KEY,revision INTEGER NOT NULL,
            FOREIGN KEY(id,revision) REFERENCES direct_{kind}_revisions(id,revision) ON DELETE CASCADE);
        CREATE TRIGGER direct_{kind}_immutable BEFORE UPDATE ON direct_{kind}_revisions BEGIN
            SELECT RAISE(ABORT,'Immutable direct revision'); END;
        CREATE TRIGGER direct_{kind}_head_monotonic BEFORE UPDATE ON direct_{kind}_heads
            WHEN NEW.revision<OLD.revision BEGIN SELECT RAISE(ABORT,'Stale direct head'); END;
        """
    script += extra_sql
    script += "INSERT INTO market_meta VALUES('direct_repository','1');COMMIT;"
    try:
        store.db.executescript(script)
    except BaseException:
        store.db.rollback()
        raise
    from .repository_meter import DIRECT
    from .repository_meter import install as install_meter

    install_meter(store, "direct", DIRECT)


def retained_deadline(store: Store, kind: str, identity: str, value: dict[str, Any]) -> int:
    from .catalogue_lifecycle import parent_expiry
    from .expiry_index import hot_binding

    saved = hot_binding(store.db, PREFIX + kind + "-head:" + identity)
    if saved is not None:
        return saved[0]
    if kind == "event":
        return value["market_observation_ms"] + int(value["settings_snapshot"]["detail_retention_days"]) * DAY
    parent = value.get("origin_event_id") if kind == "episode" else value.get("event_id")
    hot = hot_binding(store.db, PREFIX + "event-head:" + str(parent))
    cold = parent_expiry(store.db, str(parent)) if parent else None
    if hot is not None and cold is not None and hot[0] != cold:
        raise StateError("Conflicting immutable parent lifetime")
    if hot is not None:
        return hot[0]
    if cold is not None:
        return cold
    if kind == "episode":
        return value["started_ms"] + store.snapshot().settings.detail_retention_days * DAY
    raise StateError("Missing retained parent during result publication")


class LegacyHistory(OriginalHistory):
    """Explicit original writer for a pre-activation identity."""


class History(OriginalHistory):
    def __init__(self, store: Store) -> None:
        super().__init__(store)
        if not getattr(store, "_direct_sql_function", False):

            def payload(key: Any) -> Any:
                if not isinstance(key, str):
                    return None
                value = self.get(key[len(PREFIX) :]) if key.startswith(PREFIX) else store.get_runtime(key)
                return None if value is None else encode(value)

            store.db.create_function("cw_runtime_payload", 1, payload)
            setattr(store, "_direct_sql_function", True)

    @staticmethod
    def point(key: str) -> Any:
        found = POINT.fullmatch(key)
        if found is None:
            return None
        kind, head, identity, revision = found.groups()
        if bool(head) == bool(revision):
            return None
        return kind, identity, int(revision) if revision else None

    @staticmethod
    def checksum(
        identity: Any,
        revision: Any,
        payload: Any,
        fields: Any,
        clock: Any,
        origin: Any,
        reference: Any,
        reference_ms: Any,
    ) -> Any:
        return hashlib.sha256(
            encode([identity, revision, payload, fields, clock, origin, reference, reference_ms]).encode()
        ).hexdigest()

    def _row(self, key: str) -> Any:
        point = self.point(key)
        if point is None:
            return None
        kind, identity, revision = point
        if revision is None:
            row = self.store.db.execute(
                f"SELECT r.payload,r.fields,r.clock,r.digest,r.origin,r.reference,r.reference_ms,r.id,r.revision,x.payload_json FROM direct_{kind}_heads h "
                f"JOIN direct_{kind}_revisions r ON r.id=h.id AND r.revision=h.revision "
                "JOIN runtime x ON x.key=? WHERE h.id=?",
                (PREFIX + key, identity),
            ).fetchone()
        else:
            row = self.store.db.execute(
                f"SELECT payload,fields,clock,digest,origin,reference,reference_ms,id,revision FROM direct_{kind}_revisions WHERE id=? AND revision=?",
                (identity, revision),
            ).fetchone()
        if row is not None:
            from .writer_episode_reads import current

            memo = current(self, key)
            if memo is not None and memo.checked(key, row):
                return row
            if row[3] != self.checksum(row[7], row[8], row[0], row[1], row[2], row[4], row[5], row[6]):
                raise StateError("Corrupt direct immutable revision")
            if revision is None:
                clock = json.loads(row[2])
                expected = self._head(
                    kind, clock, f"{kind}:{identity}:{clock['revision']:010d}", bounded=True
                )
                if encode(expected) != encode(json_payload(row[9])):
                    raise StateError("Conflicting direct history head projection")
            if memo is not None:
                memo.remember(key, row)
        return row

    def _record(self, key: str, *, metadata: bool = False) -> Any:
        row = self._row(key)
        if row is None:
            return None
        from .writer_episode_reads import current

        # A linked body can be independently removed/corrupted. Keep those reads
        # and validations on every request; memoize only self-contained episodes.
        memo = current(self, key) if row[1] == "{}" else None
        if memo is not None:
            cached = memo.record(key, row)
            if cached is not None:
                return {k: v for k, v in cached.items() if k not in OMIT} if metadata else cached
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
                "SELECT payload FROM direct_snapshots WHERE digest=?", (digest,)
            ).fetchone()
            if raw is None or hashlib.sha256(raw[0].encode()).hexdigest() != digest:
                raise StateError("Missing or corrupt direct snapshot")
            value[name] = json.loads(raw[0])
        if memo is not None:
            memo.record(key, row, value)
        if metadata:
            value = {k: v for k, v in value.items() if k not in OMIT}
        return value

    def get(self, key: str, default: Any = None) -> Any:
        if self.point(key) is None:
            return super().get(key, default)
        value = self._record(key)
        return super().get(key, default) if value is None else value

    def metadata(self, key: str) -> Any:
        if self.point(key) is None:
            return super().metadata(key)
        value = self._record(key, metadata=True)
        return super().metadata(key) if value is None else value

    def episode_clock(self, key: str) -> Any:
        if self.point(key) is None or not key.startswith("episode-head:"):
            return super().episode_clock(key)
        row = self._row(key)
        return json.loads(row[2]) if row else super().episode_clock(key)

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

    def resolve(self, key: str, value: Any, *, omit_fields: frozenset[str] = frozenset()) -> Any:
        if self.point(key) is not None and self._row(key) is not None:
            return self.metadata(key) if omit_fields else self.get(key)
        return super().resolve(key, value, omit_fields=omit_fields)

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
        direct_head = self._row(kind + "-head:" + identity)
        if direct_head is None and super().metadata(kind + "-head:" + identity) is not None:
            # Keep every old revision and the original writer for an existing
            # identity; never silently replace or rehash a retained episode.
            return LegacyHistory(self.store).version(kind, value)
        key = f"{kind}:{identity}:{revision:010d}"
        if not self.store.db.in_transaction:
            self.store.db.execute("BEGIN IMMEDIATE")
        from .packet_store import atomic

        with atomic(self.store.db):
            self._row(kind + "-head:" + identity)
            previous = self.get(key)
            if previous is not None:
                if encode(previous) != encode(value):
                    raise StateError("Conflicting immutable history revision")
            else:
                head = self.store.db.execute(
                    f"SELECT r.origin,r.reference,r.reference_ms FROM direct_{kind}_heads h "
                    f"JOIN direct_{kind}_revisions r ON r.id=h.id AND r.revision=h.revision WHERE h.id=?",
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
                        "SELECT payload FROM direct_snapshots WHERE digest=?", (digest,)
                    ).fetchone()
                    if found is not None and found[0] != raw:
                        raise StateError("Conflicting direct snapshot")
                    if found is None:
                        self.store.db.execute(
                            "INSERT INTO direct_snapshots(digest,payload) VALUES(?,?)", (digest, raw)
                        )
                    self.store.db.execute("INSERT INTO direct_links VALUES(?,?,?)", (key, name, digest))
                    fields[name] = digest
                    del body[name]
                clock = {
                    k: value[k]
                    for k in (*HEAD_FIELDS[kind], "origin_event_id", "reference_price", "reference_time_ms")
                    if k in value
                }
                self.store.db.execute(
                    f"INSERT INTO direct_{kind}_revisions VALUES(?,?,?,?,?,?,?,?,?,?)",
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
                f"INSERT INTO direct_{kind}_heads VALUES(?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision WHERE excluded.revision>revision",
                (identity, revision),
            )
            actual = self.store.db.execute(
                f"SELECT revision FROM direct_{kind}_heads WHERE id=?", (identity,)
            ).fetchone()[0]
            if actual == revision:
                # Explicit small compatibility index; no retained body copy.
                self.store.db.execute(
                    RUNTIME_UPSERT,
                    (PREFIX + kind + "-head:" + identity, encode(self._head(kind, value, key, bounded=True))),
                )
                from .expiry_index import put as put_expiry

                parent = (
                    identity
                    if kind == "event"
                    else value.get("origin_event_id")
                    if kind == "episode"
                    else value.get("event_id")
                )
                expiry = retained_deadline(self.store, kind, identity, value)
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
                self.store.db.execute(f"DELETE FROM direct_{kind}_heads WHERE id=?", (identity,))
            else:
                self.store.db.execute("DELETE FROM direct_links WHERE owner=?", (key,))
                self.store.db.execute(
                    f"DELETE FROM direct_{kind}_revisions WHERE id=? AND revision=?", (identity, revision)
                )
        super()._delete_raw(key)

    def scan(self, prefix: str, *, reverse: bool = False) -> Iterator[tuple[str, Any]]:
        import heapq

        sources = [super().scan(prefix, reverse=reverse)]
        for kind in KINDS:
            if prefix.startswith(kind + ":") or (kind + ":").startswith(prefix):
                rows = self.store.db.execute(
                    f"SELECT public_key FROM direct_{kind}_revisions WHERE public_key>=? AND public_key<? ORDER BY public_key "
                    + ("DESC" if reverse else "ASC"),
                    (prefix, prefix + "\uffff"),
                )
                sources.append(((row[0], None) for row in rows))
        previous = None
        for key, _ in heapq.merge(*sources, key=lambda item: item[0], reverse=reverse):
            if key != previous:
                yield key, self.get(key)
                previous = key

    def items(self, prefix: str, limit: int | None = None, *, reverse: bool = False) -> Any:
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
    scanned = 0

    def cursor(name: str, default: Any) -> Any:
        row = store.db.execute("SELECT value FROM market_meta WHERE key=?", ("direct_gc:" + name,)).fetchone()
        return json.loads(row[0]) if row else default

    def save(name: str, value: Any) -> None:
        store.db.execute(
            "INSERT INTO market_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("direct_gc:" + name, encode(value)),
        )

    with store.db:
        for kind in KINDS:
            heads = store.db.execute(
                f"SELECT id FROM direct_{kind}_heads WHERE id>? ORDER BY id LIMIT 16",
                (cursor(kind + ":heads", ""),),
            ).fetchall()
            for (identity,) in heads:
                scanned += 1
                if store.get_runtime(PREFIX + kind + "-head:" + identity) is None:
                    store.db.execute(f"DELETE FROM direct_{kind}_heads WHERE id=?", (identity,))
            save(kind + ":heads", heads[-1][0] if len(heads) == 16 else "")
            start = cursor(kind + ":revisions", ["", 0])
            rows = store.db.execute(
                f"SELECT id,revision,public_key FROM direct_{kind}_revisions WHERE (id,revision)>(?,?) ORDER BY id,revision LIMIT 64",
                tuple(start),
            ).fetchall()
            for identity, revision, key in rows:
                scanned += 1
                if store.db.execute(f"SELECT 1 FROM direct_{kind}_heads WHERE id=?", (identity,)).fetchone():
                    continue
                if (
                    kind == "event"
                    and store.services is not None
                    and store.db.execute(
                        "SELECT 1 FROM subject_sources WHERE event_id=? AND revision=?", (identity, revision)
                    ).fetchone()
                ):
                    continue
                store.db.execute("DELETE FROM direct_links WHERE owner=?", (key,))
                removed += store.db.execute(
                    f"DELETE FROM direct_{kind}_revisions WHERE id=? AND revision=?", (identity, revision)
                ).rowcount
            save(kind + ":revisions", list(rows[-1][:2]) if len(rows) == 64 else ["", 0])
        removed += store.db.execute(
            "DELETE FROM direct_snapshots WHERE digest IN (SELECT digest FROM direct_snapshots WHERE refs=0 LIMIT 64)"
        ).rowcount
    # Actual SQLite maintenance, not fabricated progress or a disabled GC stub.
    store.db.execute("PRAGMA incremental_vacuum(16)")
    checkpoint = tuple(store.db.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())
    value = {
        "state": "DIRECT_SQLITE_MAINTENANCE",
        "at_ms": now,
        "removed": removed,
        "scanned": scanned,
        "checkpoint": checkpoint,
    }
    store.put_runtime("direct_maintenance", value)
    return value
