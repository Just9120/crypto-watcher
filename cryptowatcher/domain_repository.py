"""Typed domain repository; activation requires explicit runtime integration.

A prepared in-memory transition cannot access SQLite. One transaction commits
immutable facts, episode member deltas and mutable state before consumers run.
Compatibility readers validate the actual SQL rows on every public read.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from . import events
from . import revision_repository as repository
from .condition_state import MEMBERS
from .events import PREFIX
from .storage import RUNTIME_UPSERT, StateError, encode

BASE_HISTORY = repository.History
BASE_INSTALL = repository.install
BASE_MAINTENANCE = repository.maintenance
STATE_PREFIXES = ("active:", "historical-active:", "suppressed-active:", "diagnostic:")
DAY = 86400000


def digest(raw: Any) -> Any:
    return hashlib.sha256(raw.encode()).hexdigest()


def install(store: Any) -> Any:
    BASE_INSTALL(
        store,
        extra_sql="""
        CREATE TABLE domain_conditions(stream TEXT PRIMARY KEY,payload TEXT NOT NULL,sha TEXT NOT NULL) WITHOUT ROWID;
        CREATE TABLE domain_state(key TEXT PRIMARY KEY,payload TEXT NOT NULL,sha TEXT NOT NULL) WITHOUT ROWID;
        CREATE TABLE domain_members(id TEXT NOT NULL,revision INTEGER NOT NULL,name TEXT NOT NULL,
            payload TEXT NOT NULL,sha TEXT NOT NULL,PRIMARY KEY(id,name,revision)) WITHOUT ROWID;
        CREATE TRIGGER domain_member_immutable BEFORE UPDATE ON domain_members BEGIN
            SELECT RAISE(ABORT,'Immutable domain member'); END;
    """,
    )


class History(BASE_HISTORY):
    _write_legacy = BASE_HISTORY._put_raw
    _delete_legacy = BASE_HISTORY._delete_raw

    def _get_raw(self, key: str, default: Any = None) -> Any:
        batch = getattr(self.store, "_writer_batch", None)
        if batch is not None and batch.owner != (self.store.owner_id, self.store.readonly):
            raise StateError("Writer owner changed within the transaction")
        if batch is not None and not batch.flushing and key in batch.writes:
            from copy import deepcopy

            from .writer_memory import DELETE

            value = batch.writes[key]
            return default if value is DELETE else self.resolve(key, deepcopy(value))
        return super()._get_raw(key, default)

    def _put_raw(self, key: str, value: Any) -> None:
        from .writer_memory import selected

        batch = getattr(self.store, "_writer_batch", None)
        if batch is not None and not batch.flushing and selected(key):
            batch.write(key, value)
            return
        super()._put_raw(key, value)

    def _delete_raw(self, key: str) -> None:
        from .writer_memory import DELETE, selected

        batch = getattr(self.store, "_writer_batch", None)
        if batch is not None and not batch.flushing and selected(key):
            batch.write(key, DELETE)
            return
        super()._delete_raw(key)

    def metadata(self, key: str) -> Any:
        batch = getattr(self.store, "_writer_batch", None)
        # Immutable revisions are written immediately. Flushing unrelated
        # mutable intents here defeats their per-source coalescing.
        if batch is not None and self.point(key) is None:
            batch.flush()
        return super().metadata(key)

    def condition_group(self, stream: Any) -> Any:
        batch = getattr(self.store, "_writer_batch", None)
        if batch is not None and not batch.flushing:
            batch.check()
            pending = batch.direct.get(("conditions", stream))
            if pending is not None:
                return json.loads(pending)
        row = self.store.db.execute(
            "SELECT payload,sha FROM domain_conditions WHERE stream=?", (stream,)
        ).fetchone()
        if row is None:
            from .condition_state import Batch

            legacy = Batch(self, stream)
            if legacy.disabled or legacy.deferred:
                raise StateError("Unsupported legacy condition state; data preserved")
            return {member: dict(record["value"]) for member, record in legacy.states.items()}
        if digest(row[0]) != row[1]:
            raise StateError("Corrupt domain conditions")
        return json.loads(row[0])

    def get(self, key: Any, default: Any = None) -> Any:
        if key.startswith("condition:"):
            _, stream, family, direction = key.split(":")
            return self.condition_group(stream).get(family + ":" + direction, default)
        if key.startswith(STATE_PREFIXES):
            batch = getattr(self.store, "_writer_batch", None)
            if batch is not None and not batch.flushing:
                batch.check()
                identity = ("state", key)
                if identity in batch.direct:
                    raw = batch.direct[identity]
                    return default if raw is None else json.loads(raw)
            row = self.store.db.execute("SELECT payload,sha FROM domain_state WHERE key=?", (key,)).fetchone()
            if row is None:
                return super().get(key, default)
            if digest(row[0]) != row[1]:
                raise StateError("Corrupt domain binding")
            return json.loads(row[0])
        return super().get(key, default)

    def put(self, key: Any, value: Any) -> Any:
        if key.startswith("condition:"):
            _, stream, family, direction = key.split(":")
            group = self.condition_group(stream)
            group[family + ":" + direction] = value
            self.conditions(stream, group)
        elif key.startswith(STATE_PREFIXES):
            batch = getattr(self.store, "_writer_batch", None)
            if batch is not None and batch.grouped and not batch.flushing:
                batch.domain("state", key, value)
                return
            if self.store.get_runtime(PREFIX + key) is not None:
                repository.OriginalHistory.delete(self, key)
            raw = encode(value)
            self.store.db.execute(
                "INSERT INTO domain_state VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload,sha=excluded.sha",
                (key, raw, digest(raw)),
            )
        else:
            super().put(key, value)

    def conditions(self, stream: Any, value: Any) -> Any:
        if len(value) > len(MEMBERS):
            raise StateError("Domain condition group exceeds fixed detector bound")
        from .condition_state import GROUP, Batch, supported

        if any(member not in MEMBERS or not supported(record) for member, record in value.items()):
            raise StateError("Invalid domain condition member")
        if not self.store.db.execute("SELECT 1 FROM domain_conditions WHERE stream=?", (stream,)).fetchone():
            legacy = Batch(self, stream)
            if legacy.disabled or legacy.deferred:
                raise StateError("Unsupported legacy condition state; data preserved")
            old = repository.LegacyHistory(self.store)
            if legacy.base is not None:
                old._delete_raw(GROUP + stream)
            for key in legacy.legacy:
                old._delete_raw(key)
        batch = getattr(self.store, "_writer_batch", None)
        if batch is not None and batch.grouped and not batch.flushing:
            batch.domain("conditions", stream, value)
            return
        raw = encode(value)
        self.store.db.execute(
            "INSERT INTO domain_conditions VALUES(?,?,?) ON CONFLICT(stream) DO UPDATE SET payload=excluded.payload,sha=excluded.sha",
            (stream, raw, digest(raw)),
        )

    def delete(self, key: Any) -> Any:
        if key.startswith(STATE_PREFIXES):
            batch = getattr(self.store, "_writer_batch", None)
            if batch is not None and batch.grouped and not batch.flushing:
                from .writer_memory import DELETE

                batch.domain("state", key, DELETE)
                return
            self.store.db.execute("DELETE FROM domain_state WHERE key=?", (key,))
            # Direct mutable rows never own legacy body/expiry indexes. Delete
            # those only when a real legacy row is present (including cutover).
            if self.store.get_runtime(PREFIX + key) is not None:
                super().delete(key)
        else:
            super().delete(key)

    def flush_domain(self, pending: Any) -> None:
        """Flush bounded, already encoded mutable state without per-row wrappers."""
        states = [(key, raw) for (kind, key), raw in pending.items() if kind == "state"]
        for key, _ in states:
            if self.store.get_runtime(PREFIX + key) is not None:
                repository.OriginalHistory.delete(self, key)
        self.store.db.executemany(
            "INSERT INTO domain_state VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload,sha=excluded.sha",
            [(key, raw, digest(raw)) for key, raw in states if raw is not None],
        )
        self.store.db.executemany(
            "DELETE FROM domain_state WHERE key=?", [(key,) for key, raw in states if raw is None]
        )
        self.store.db.executemany(
            "INSERT INTO domain_conditions VALUES(?,?,?) ON CONFLICT(stream) DO UPDATE SET payload=excluded.payload,sha=excluded.sha",
            [(key, raw, digest(raw)) for (kind, key), raw in pending.items() if kind == "conditions"],
        )

    def items(self, prefix: Any, limit: Any = None, *, reverse: Any = False) -> Any:
        batch = getattr(self.store, "_writer_batch", None)
        if batch is not None:
            batch.flush()
        if prefix.startswith(STATE_PREFIXES):
            if limit is not None and (type(limit) is not int or limit < 0):
                raise ValueError("Invalid domain scan limit")
            rows = self.store.db.execute(
                "SELECT key FROM domain_state WHERE key>=? AND key<? ORDER BY key "
                + ("DESC" if reverse else "ASC")
                + (" LIMIT ?" if limit is not None else ""),
                (prefix, prefix + "\uffff", *(() if limit is None else (limit,))),
            )
            import heapq
            from itertools import islice

            merged = heapq.merge(
                super().items(prefix, limit, reverse=reverse),
                ((r[0], self.get(r[0])) for r in rows),
                key=lambda item: item[0],
                reverse=reverse,
            )

            def unique() -> Any:
                previous = None
                for key, value in merged:
                    if key != previous:
                        yield key, value
                        previous = key

            return list(islice(unique(), limit))
        return super().items(prefix, limit, reverse=reverse)

    def _record(self, key: Any, *, metadata: Any = False) -> Any:
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

    def version(self, kind: Any, value: Any) -> Any:
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
        checked_head = self._row(kind + "-head:" + identity)
        if (
            checked_head is None
            and repository.OriginalHistory.metadata(self, kind + "-head:" + identity) is not None
        ):
            return repository.LegacyHistory(self.store).version(kind, value)
        from .packet_store import atomic

        with atomic(self.store.db):
            self._publish(kind, identity, revision, value, checked_head)

    def _publish(self, kind: Any, identity: Any, revision: Any, value: Any, checked_head: Any) -> Any:
        key = f"{kind}:{identity}:{revision:010d}"
        db = self.store.db
        # Reuse the row checked in this same synchronous writer boundary.
        # No intervening callback/await and no persistent existence cache.
        head = (
            None
            if checked_head is None
            else (checked_head[8], checked_head[4], checked_head[5], checked_head[6])
        )
        if head is not None and head[0] >= revision:
            if encode(self.get(key)) != encode(value):
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
            prior = {} if checked_head is None else json.loads(checked_head[1])
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
            snapshots: dict[str, str] = {}
            links = []
            for name in repository.SHARED & body.keys():
                raw = encode(body[name])
                if len(raw.encode()) <= 128:
                    continue
                sha = digest(raw)
                if sha in snapshots and snapshots[sha] != raw:
                    raise StateError("Conflicting domain input snapshot")
                snapshots[sha] = raw
                links.append((key, name, sha))
                fields[name] = sha
                del body[name]
            if snapshots:
                placeholders = ",".join("?" for _ in snapshots)
                query = f"SELECT digest,payload FROM direct_snapshots WHERE digest IN({placeholders})"
                found = dict(db.execute(query, tuple(snapshots)).fetchall())
                if any(found[sha] != snapshots[sha] for sha in found):
                    raise StateError("Conflicting domain input snapshot")
                missing = [(sha, raw) for sha, raw in snapshots.items() if sha not in found]
                if missing:
                    db.executemany("INSERT INTO direct_snapshots(digest,payload) VALUES(?,?)", missing)
                    # Validate actual rows after triggers, never cache a presence
                    # proof or bypass corruption checks across publications.
                    if dict(db.execute(query, tuple(snapshots)).fetchall()) != snapshots:
                        raise StateError("Conflicting domain input snapshot")
                db.executemany("INSERT INTO direct_links VALUES(?,?,?)", links)
        clock = {
            k: value[k]
            for k in (*events.HEAD_FIELDS[kind], "origin_event_id", "reference_price", "reference_time_ms")
            if k in value
        }
        raw, raw_fields, raw_clock = encode(body), encode(fields), encode(clock)
        checksum = self.checksum(identity, revision, raw, raw_fields, raw_clock, *reference)
        db.execute(
            f"INSERT INTO direct_{kind}_revisions VALUES(?,?,?,?,?,?,?,?,?,?)",
            (identity, revision, key, raw, raw_fields, raw_clock, *reference, checksum),
        )
        db.execute(
            f"INSERT INTO direct_{kind}_heads VALUES(?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision",
            (identity, revision),
        )
        db.execute(
            RUNTIME_UPSERT,
            (PREFIX + kind + "-head:" + identity, encode(self._head(kind, value, key, bounded=True))),
        )
        if revision == 1:
            self.put(kind + "-counter", self.get(kind + "-counter", 0) + 1)
        from .expiry_index import hot_binding
        from .expiry_index import put as put_expiry

        public_head = PREFIX + kind + "-head:" + identity
        category = kind + "-head"
        parent = identity if kind == "event" else value.get("origin_event_id")
        saved = hot_binding(db, public_head)
        expiry = (
            saved[0] if saved is not None else repository.retained_deadline(self.store, kind, identity, value)
        )
        if saved != (expiry, category, parent):
            put_expiry(db, public_head, expiry, category, parent)


def maintenance(store: Any, now: int) -> dict[str, Any]:
    result = BASE_MAINTENANCE(store, now)
    scanned = removed = 0

    def read(name: str, default: Any) -> Any:
        row = store.db.execute("SELECT value FROM market_meta WHERE key=?", ("domain_gc:" + name,)).fetchone()
        return json.loads(row[0]) if row else default

    def save(name: str, value: Any) -> None:
        store.db.execute(
            "INSERT INTO market_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("domain_gc:" + name, encode(value)),
        )

    with store.db:
        members = store.db.execute(
            "SELECT id,name,revision FROM domain_members WHERE (id,name,revision)>(?,?,?) ORDER BY id,name,revision LIMIT 64",
            tuple(read("members", ["", "", 0])),
        ).fetchall()
        for identity, name, revision in members:
            scanned += 1
            if not store.db.execute(
                "SELECT 1 FROM direct_episode_revisions WHERE id=? LIMIT 1", (identity,)
            ).fetchone():
                removed += store.db.execute(
                    "DELETE FROM domain_members WHERE id=? AND name=? AND revision=?",
                    (identity, name, revision),
                ).rowcount
        save("members", list(members[-1]) if len(members) == 64 else ["", "", 0])
        state = store.db.execute(
            "SELECT key,payload FROM domain_state WHERE key>? ORDER BY key LIMIT 64", (read("state", ""),)
        ).fetchall()
        history = History(store)
        cutoff = now - store.snapshot().settings.candle_retention_days * DAY
        for key, raw in state:
            scanned += 1
            value = json.loads(raw)
            expired = key.startswith("diagnostic:") and value.get("at_ms", now) < cutoff
            if key.startswith(STATE_PREFIXES[:3]):
                expired = history.episode_clock("episode-head:" + value["episode_id"]) is None
            if expired:
                removed += store.db.execute("DELETE FROM domain_state WHERE key=?", (key,)).rowcount
        save("state", state[-1][0] if len(state) == 64 else "")
        groups = store.db.execute(
            "SELECT stream,payload FROM domain_conditions WHERE stream>? ORDER BY stream LIMIT 4",
            (read("conditions", ""),),
        ).fetchall()
        for stream, raw in groups:
            scanned += 1
            value = json.loads(raw)
            if all(item.get("last_ms", now) < cutoff for item in value.values()):
                removed += store.db.execute(
                    "DELETE FROM domain_conditions WHERE stream=?", (stream,)
                ).rowcount
        save("conditions", groups[-1][0] if len(groups) == 4 else "")
    return {**result, "domain_scanned": scanned, "domain_removed": removed}
