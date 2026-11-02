"""Isolated observation publication; no runtime activation or history migration.

One source transaction owns prepared immutable records. SQL rows, permissions,
head projections and linked snapshot bytes remain authoritative on EVERY read.
Only identical, previously checked bytes reuse their assembled public object.
"""

from __future__ import annotations

import copy
import hashlib
import json
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from unittest.mock import patch

from cryptowatcher.events import HEAD_FIELDS, PREFIX, EventEngine
from cryptowatcher.packet_store import atomic
from cryptowatcher.storage import RUNTIME_UPSERT, StateError, encode
from tests import sqlite_repository as repository

ACTIVE = ContextVar("isolated_publication_unit", default=None)
MAX_KEYS, MAX_BYTES = 128, 1024**2


@dataclass
class Record:
    row: tuple
    fields: dict
    snapshots: dict
    value: dict


class Publication:
    def __init__(self, history, stats):
        self.history, self.store, self.stats = history, history.store, stats
        self.owner = (self.store.owner_id, self.store.readonly)
        self.records, self.sizes = {}, {}
        self.closed = False

    def guard(self):
        if self.closed or not self.store.db.in_transaction:
            raise StateError("Publication requires the same active source transaction")
        if self.owner != (self.store.owner_id, self.store.readonly) or self.store.readonly:
            raise StateError("Publication owner or write mode changed")

    def remember(self, key, record):
        size = len(key.encode()) + sum(len(x.encode()) for x in record.row if isinstance(x, str))
        size += sum(len(k.encode()) + len(v.encode()) for k, v in record.snapshots.items())
        total = sum(self.sizes.values()) - self.sizes.get(key, 0) + size
        if len(self.records) + (key not in self.records) > MAX_KEYS or total > MAX_BYTES:
            raise StateError("Publication record budget exceeded")
        self.records[key], self.sizes[key] = record, size
        self.stats["peak_keys"] = max(self.stats["peak_keys"], len(self.records))
        self.stats["peak_serialized_bytes"] = max(self.stats["peak_serialized_bytes"], total)

    def read(self, key, *, metadata=False):
        self.guard()
        kind, identity, revision = self.history.point(key)
        db = self.store.db
        if revision is None:
            row = db.execute(
                f"SELECT r.payload,r.fields,r.clock,r.digest,r.origin,r.reference,r.reference_ms,r.id,r.revision,x.payload_json FROM prototype_{kind}_heads h "
                f"JOIN prototype_{kind}_revisions r ON r.id=h.id AND r.revision=h.revision "
                "LEFT JOIN runtime x ON x.key=? WHERE h.id=?",
                (PREFIX + key, identity),
            ).fetchone()
        else:
            row = db.execute(
                f"SELECT payload,fields,clock,digest,origin,reference,reference_ms,id,revision,NULL FROM prototype_{kind}_revisions WHERE id=? AND revision=?",
                (identity, revision),
            ).fetchone()
        self.stats["row_checks"] += 1
        if row is None:
            self.records.pop(key, None)
            self.sizes.pop(key, None)
            return None
        row = tuple(row)
        prior = self.records.get(key)
        fields = prior.fields if prior is not None and row == prior.row else json.loads(row[1])
        if not isinstance(fields, dict) or not set(fields) <= repository.SHARED:
            raise StateError("Invalid direct revision fields")
        if any(not isinstance(v, str) for v in fields.values()):
            raise StateError("Invalid direct snapshot digest")
        digests = list(dict.fromkeys(fields.values()))
        snapshots = {}
        if digests:
            marks = ",".join("?" for _ in digests)
            snapshots = dict(
                db.execute(
                    f"SELECT digest,payload FROM prototype_snapshots WHERE digest IN({marks})", digests
                ).fetchall()
            )
            self.stats["snapshot_checks"] += len(digests)
            if set(snapshots) != set(digests):
                raise StateError("Missing or corrupt direct snapshot")
        if prior is not None and row == prior.row and snapshots == prior.snapshots:
            value = prior.value
            self.stats["reused_records"] += 1
        else:
            if row[3] != self.history.checksum(row[7], row[8], *row[:3], *row[4:7]):
                raise StateError("Corrupt direct immutable revision")
            value, clock = json.loads(row[0]), json.loads(row[2])
            if not isinstance(value, dict) or not isinstance(clock, dict):
                raise StateError("Invalid direct revision fields")
            actual_key = f"{kind}:{identity}:{row[8]:010d}"
            expected = self.history._head(kind, clock, actual_key, bounded=True)
            if revision is None and (row[9] is None or encode(expected) != encode(json.loads(row[9]))):
                raise StateError("Conflicting direct history head projection")
            if (
                value.get(kind + "_id") != identity
                or value.get("revision") != row[8]
                or value.get("schema") == "cryptowatcher.history-head.v1"
                or encode(self.history._head(kind, value, actual_key, bounded=True)) != encode(expected)
            ):
                raise StateError("Invalid direct revision fields")
            for name, digest in fields.items():
                raw = snapshots[digest]
                if hashlib.sha256(raw.encode()).hexdigest() != digest:
                    raise StateError("Missing or corrupt direct snapshot")
                value[name] = json.loads(raw)
            self.remember(key, Record(row, fields, snapshots, value))
        return copy.deepcopy({k: v for k, v in value.items() if not metadata or k not in repository.OMIT})

    def publish(self, kind, value):
        self.guard()
        revision = value.get("revision")
        identity = value.get(kind + "_id")
        if type(revision) is not int or revision < 1:
            raise StateError("Invalid direct revision identity")
        key = f"{kind}:{identity}:{revision:010d}"
        if self.history.point(key) != (kind, identity, revision) or "_history_storage" in value:
            raise StateError("Invalid direct public record")
        db = self.store.db
        # Keep the original per-call rollback guarantee if a consumer catches a
        # publication exception inside the surrounding observation transaction.
        with atomic(db):
            head = self.read(kind + "-head:" + identity, metadata=True)
            previous = self.read(key)
            if previous is not None:
                if encode(previous) != encode(value):
                    raise StateError("Conflicting immutable history revision")
            else:

                def frozen(v):
                    return (
                        v.get("origin_event_id"),
                        encode(v.get("reference_price")),
                        v.get("reference_time_ms"),
                    )

                if head is not None and (
                    frozen(head)[1:] != frozen(value)[1:]
                    if kind == "event"
                    else frozen(head) != frozen(value)
                ):
                    raise StateError("Episode or event outcome reference is immutable")
                body, fields, snapshots = dict(value), {}, {}
                for name in repository.SHARED & value.keys():
                    raw = encode(value[name])
                    if len(raw.encode()) <= 128:
                        continue
                    digest = hashlib.sha256(raw.encode()).hexdigest()
                    found = db.execute(
                        "SELECT payload FROM prototype_snapshots WHERE digest=?", (digest,)
                    ).fetchone()
                    if found is not None and found[0] != raw:
                        raise StateError("Conflicting direct snapshot")
                    if found is None:
                        db.execute(
                            "INSERT INTO prototype_snapshots(digest,payload) VALUES(?,?)", (digest, raw)
                        )
                    db.execute("INSERT INTO prototype_links VALUES(?,?,?)", (key, name, digest))
                    fields[name], snapshots[digest] = digest, raw
                    del body[name]
                clock = {
                    k: value[k]
                    for k in (*HEAD_FIELDS[kind], "origin_event_id", "reference_price", "reference_time_ms")
                    if k in value
                }
                # These exact encoded values are reused by SQL and checksum.
                payload, field_text, clock_text = encode(body), encode(fields), encode(clock)
                origin, reference, reference_ms = (
                    value.get("origin_event_id"),
                    encode(value.get("reference_price")),
                    value.get("reference_time_ms"),
                )
                digest = self.history.checksum(
                    identity, revision, payload, field_text, clock_text, origin, reference, reference_ms
                )
                db.execute(
                    f"INSERT INTO prototype_{kind}_revisions VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        identity,
                        revision,
                        key,
                        payload,
                        field_text,
                        clock_text,
                        origin,
                        reference,
                        reference_ms,
                        digest,
                    ),
                )
                self.stats["prepared_revisions"] += 1
                self.stats["body_encodes"] += 1
                prepared = Record(
                    (
                        payload,
                        field_text,
                        clock_text,
                        digest,
                        origin,
                        reference,
                        reference_ms,
                        identity,
                        revision,
                        None,
                    ),
                    fields,
                    snapshots,
                    copy.deepcopy(value),
                )
                self.remember(key, prepared)
                if revision == 1:
                    self.history.put(kind + "-counter", self.history.get(kind + "-counter", 0) + 1)
            db.execute(
                f"INSERT INTO prototype_{kind}_heads VALUES(?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision WHERE excluded.revision>revision",
                (identity, revision),
            )
            actual = db.execute(
                f"SELECT revision FROM prototype_{kind}_heads WHERE id=?", (identity,)
            ).fetchone()[0]
            if actual == revision:
                projection = encode(self.history._head(kind, value, key, bounded=True))
                db.execute(RUNTIME_UPSERT, (PREFIX + kind + "-head:" + identity, projection))
                from cryptowatcher.expiry_index import put as put_expiry

                if kind == "event":
                    expiry = (
                        value["market_observation_ms"]
                        + int(value["settings_snapshot"]["detail_retention_days"]) * repository.DAY
                    )
                    parent = identity
                else:
                    expiry = (
                        value["started_ms"]
                        + self.store.snapshot().settings.detail_retention_days * repository.DAY
                    )
                    parent = value.get("origin_event_id")
                put_expiry(db, PREFIX + kind + "-head:" + identity, expiry, kind + "-head", parent)
                record = self.records.get(key)
                if record is not None:
                    self.remember(
                        kind + "-head:" + identity,
                        Record((*record.row[:9], projection), record.fields, record.snapshots, record.value),
                    )


@contextmanager
def installed(enabled=True):
    stats = dict.fromkeys(
        (
            "observations",
            "completed_units",
            "failed_units",
            "prepared_revisions",
            "body_encodes",
            "row_checks",
            "snapshot_checks",
            "reused_records",
            "peak_keys",
            "peak_serialized_bytes",
        ),
        0,
    )
    if not enabled:
        yield stats
        return
    history = repository.History
    observe = EventEngine.observe
    get, metadata, clock, version = history.get, history.metadata, history.episode_clock, history.version

    def current(instance, key):
        unit = ACTIVE.get()
        if unit is None or unit.closed or unit.store.db is not instance.store.db:
            return None
        point = instance.point(key)
        if point is None or point[0] not in ("event", "episode"):
            return None
        unit.guard()
        if unit.owner != (instance.store.owner_id, instance.store.readonly):
            raise StateError("Publication history owner or mode changed")
        return unit

    def reading(instance, key, default=None):
        unit = current(instance, key)
        if unit is None:
            return get(instance, key, default)
        value = unit.read(key)
        return default if value is None else value

    def inspecting(instance, key):
        unit = current(instance, key)
        return metadata(instance, key) if unit is None else unit.read(key, metadata=True)

    def episode_clock(instance, key):
        unit = current(instance, key)
        if unit is None or not key.startswith("episode-head:"):
            return clock(instance, key)
        value = unit.read(key, metadata=True)
        if value is None:
            return None
        return json.loads(unit.records[key].row[2])

    def publishing(instance, kind, value):
        unit = ACTIVE.get()
        if (
            unit is None
            or unit.closed
            or unit.store.db is not instance.store.db
            or kind not in ("event", "episode")
        ):
            return version(instance, kind, value)
        unit.guard()
        if unit.owner != (instance.store.owner_id, instance.store.readonly):
            raise StateError("Publication history owner or mode changed")
        return unit.publish(kind, value)

    def observing(engine, *args, **kwargs):
        eligible = (
            kwargs.get("commit") is False
            and isinstance(engine.history, history)
            and engine.on_event is engine._snapshot_event_callback
            and engine.on_event_snapshot is not None
        )
        if not eligible:
            return observe(engine, *args, **kwargs)
        if ACTIVE.get() is not None:
            raise StateError("Nested publication unit is not supported")
        unit = Publication(engine.history, stats)
        unit.guard()
        stats["observations"] += 1
        token = ACTIVE.set(unit)
        try:
            with atomic(engine.store.db):
                result = observe(engine, *args, **kwargs)
            stats["completed_units"] += 1
            return result
        except BaseException:
            stats["failed_units"] += 1
            raise
        finally:
            unit.closed = True
            unit.records.clear()
            ACTIVE.reset(token)

    with ExitStack() as stack:
        for target, name, value in (
            (history, "get", reading),
            (history, "metadata", inspecting),
            (history, "episode_clock", episode_clock),
            (history, "version", publishing),
            (EventEngine, "observe", observing),
        ):
            stack.enter_context(patch.object(target, name, value))
        yield stats
