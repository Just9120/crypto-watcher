"""Isolated subject-save prototype. Nothing installs it in application runtime.

A publication may reuse one checked parent deadline until an actual parent/layout
mutation invalidates it. Row conflicts and body dependencies are still checked
at the original synchronous write boundary, never deferred past consumers.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from cryptowatcher import (
    catalogue_lifecycle,
    catalogue_reader,
    domain_repository,
    domain_transition,
    events,
    expiry_index,
    key_registry,
    storage_lifecycle,
)
from cryptowatcher.reference_layout import owner_refs
from cryptowatcher.storage import RUNTIME_UPSERT, StateError, encode
from cryptowatcher.writer_memory import Connection

BASE_SAVE = events.History._put_raw
BASE_PUBLICATION = domain_transition.publication
BASE_INSTALL = domain_repository.install
BASE_AUTHORIZER = Connection.set_authorizer
BASE_CONNECTION_INIT = Connection.__init__
BASE_FREEZE = events.History._freeze_services
PREFIX = events.PREFIX
MAX_PARENTS = 64
MAX_REFS = 128
MAX_REF_BYTES = 65536


def install_lease_plan(db):
    """One explicit descriptor applies the existing hot indexes atomically.

    No row/lease/authority result is memoized here. Cold mode and custom
    authorizers keep the baseline path. SQLite FK and counter triggers remain.
    """
    db.execute(
        "CREATE TEMP VIEW cw_subject_apply AS SELECT NULL AS key,NULL AS token,NULL AS bytes,NULL AS fallback,NULL AS use_previous,NULL AS cap,NULL AS category,NULL AS parent,NULL AS refs WHERE 0"
    )
    previous = "COALESCE((SELECT expires FROM market_expiry_legacy WHERE key=NEW.key),(SELECT e.expires FROM market_expiry e JOIN market_runtime_keys k ON k.id=e.rid WHERE k.external IN(NEW.key,NEW.token)),NEW.fallback)"
    expiry = f"CASE WHEN NEW.use_previous THEN {previous} ELSE NEW.fallback END"
    expiry = f"min(({expiry}),COALESCE(NEW.cap,9223372036854775807))"
    current_refs = "SELECT body FROM market_body_links_legacy WHERE owner=NEW.key UNION SELECT b.external FROM market_references r JOIN market_body_ids b ON b.id=r.body WHERE r.owner IN(SELECT id FROM market_runtime_keys WHERE external IN(NEW.key,NEW.token))"
    changed_refs = f"(EXISTS(SELECT value FROM json_each(NEW.refs) EXCEPT {current_refs}) OR EXISTS({current_refs} EXCEPT SELECT value FROM json_each(NEW.refs)))"
    sql = f"""CREATE TEMP TRIGGER cw_subject_apply_put INSTEAD OF INSERT ON cw_subject_apply BEGIN
        SELECT CASE WHEN (
            (SELECT COUNT(*) FROM market_expiry_legacy WHERE key=NEW.key)+
            (SELECT COUNT(*) FROM market_expiry e JOIN market_runtime_keys k ON k.id=e.rid WHERE k.external IN(NEW.key,NEW.token)))>1
            THEN RAISE(ABORT,'CW_SUBJECT_CONFLICT_NATIVE_RETENTION') END;
        SELECT CASE WHEN NEW.use_previous AND (
            EXISTS(SELECT 1 FROM market_expiry_legacy WHERE key=NEW.key AND typeof(expires)<>'integer') OR
            EXISTS(SELECT 1 FROM market_expiry e JOIN market_runtime_keys k ON k.id=e.rid WHERE k.external IN(NEW.key,NEW.token) AND typeof(e.expires)<>'integer'))
            THEN RAISE(ABORT,'CW_SUBJECT_UNSUPPORTED_EXPIRY') END;
        SELECT CASE WHEN NEW.refs<>'[]' AND {changed_refs} AND EXISTS(SELECT 1 FROM json_each(NEW.refs) j
            LEFT JOIN market_body_counts c ON c.body=j.value WHERE c.body IS NULL)
            THEN RAISE(ABORT,'Missing shared body counter') END;
        INSERT OR IGNORE INTO market_event_ids(external) SELECT NEW.parent WHERE NEW.parent IS NOT NULL;
        INSERT OR IGNORE INTO market_runtime_keys(external)
            SELECT NEW.token FROM runtime WHERE key=NEW.key AND NOT EXISTS(
                SELECT 1 FROM market_runtime_keys WHERE external IN(NEW.key,NEW.token));
        INSERT INTO market_expiry(rid,expires,category,event_ref,key_bytes)
            SELECT id,{expiry},NEW.category,(SELECT id FROM market_event_ids WHERE external=NEW.parent),NEW.bytes
            FROM market_runtime_keys WHERE external IN(NEW.key,NEW.token)
            ON CONFLICT(rid) DO UPDATE SET expires=excluded.expires,category=excluded.category,
                event_ref=excluded.event_ref,key_bytes=excluded.key_bytes;
        DELETE FROM market_expiry_legacy WHERE key=NEW.key;
        DELETE FROM market_body_links_legacy WHERE owner=NEW.key AND (NEW.refs='[]' OR {changed_refs});
        DELETE FROM market_references WHERE owner IN(SELECT id FROM market_runtime_keys WHERE external IN(NEW.key,NEW.token))
            AND body NOT IN(SELECT b.id FROM market_body_ids b JOIN json_each(NEW.refs) j ON b.external=j.value);
        INSERT OR IGNORE INTO market_body_ids(external) SELECT value FROM json_each(NEW.refs) WHERE {changed_refs};
        INSERT OR IGNORE INTO market_references
            SELECT k.id,b.id FROM market_runtime_keys k JOIN json_each(NEW.refs) j JOIN market_body_ids b ON b.external=j.value
            WHERE k.external IN(NEW.key,NEW.token) AND {changed_refs};
    END"""
    db.execute(sql)


class Subject:
    def __init__(self, watcher, publication):
        self.watcher, self.publication = watcher, publication
        self.epoch = watcher.store.db._metadata_epoch
        self.parents = {}
        self.rids = {}
        self.externals = {}
        self.binary = {}
        self.disabled = watcher.authorizer_present
        self.hot_only = not catalogue_reader.reading(watcher.store.db)

    def check(self):
        self.publication.check()
        if self.epoch != self.watcher.store.db._metadata_epoch:
            self.parents.clear()
            self.rids.clear()
            self.externals.clear()
            self.binary.clear()
            self.disabled = True

    def changed(self, kind, key):
        if kind == "rid":
            identity = self.rids.get(key)
        elif kind == "external":
            identity = self.externals.get(key)
        elif kind == "legacy":
            identity = key.removeprefix(PREFIX + "event-head:") if isinstance(key, str) else None
        else:
            identity = self.binary.get(key)
        if identity in self.parents:
            del self.parents[identity]
            for mapping in (self.rids, self.externals, self.binary):
                for token in [token for token, parent in mapping.items() if parent == identity]:
                    del mapping[token]
            self.watcher.stats["parent_invalidations"] += 1

    def parent(self, identity):
        self.check()
        if self.hot_only and not self.disabled and identity in self.parents:
            self.watcher.stats["parent_reuse"] += 1
            return self.parents[identity]
        db = self.watcher.store.db
        key = PREFIX + "event-head:" + identity
        hot = expiry_index.expires(db, key)
        cold = catalogue_lifecycle.parent_expiry(db, identity)
        if cold is not None:
            if hot is not None and hot[0] != cold:
                raise ValueError("Conflicting frozen parent expiry")
            hot = (cold,)
        self.watcher.stats["parent_reads"] += 1
        if (
            self.hot_only
            and not self.disabled
            and re.fullmatch(catalogue_reader.UUID, identity) is not None
            and len(self.parents) < MAX_PARENTS
        ):
            token = key_registry.pack(key)
            rows = db.execute(
                "SELECT k.id,e.category,i.external FROM market_runtime_keys k JOIN market_expiry e ON e.rid=k.id LEFT JOIN market_event_ids i ON i.id=e.event_ref WHERE k.external IN(?,?)",
                (key, token),
            ).fetchall()
            # Only a canonical native event-head can use the narrow SQL guard.
            # Absent/legacy/noncanonical/cold-only parents retain fresh reads.
            if len(rows) != 1 or tuple(rows[0])[1:] != ("event-head", identity):
                return hot
            self.rids[rows[0][0]] = identity
            self.externals[key] = self.externals[token] = identity
            if len(identity) == 36:
                try:
                    self.binary[bytes.fromhex(identity.replace("-", ""))] = identity
                except ValueError:
                    pass
            self.parents[identity] = hot
            self.watcher.stats["peak_parents"] = max(self.watcher.stats["peak_parents"], len(self.parents))
        return hot


class Watcher:
    def __init__(self, store, stats):
        if not isinstance(store.db, Connection) or store.db.in_transaction:
            raise StateError("Subject prototype activation needs its idle writer connection")
        self.store, self.stats, self.active = store, stats, None
        self.authorizer_present = getattr(store.db, "_subject_authorizer_present", True)
        db = store.db
        db._subject_save_watcher = self
        self.prefix = "cw_subject_"
        self.tables = []
        db.create_function("cw_subject_changed", 2, self.changed)
        specifications = (
            ("market_expiry", "rid", "rid"),
            ("market_runtime_keys", "external", "external"),
            ("market_expiry_legacy", "key", "legacy"),
            ("market_record_parents", "public_uuid", "binary"),
        )
        for table, column, kind in specifications:
            if (
                db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                is None
            ):
                continue
            self.tables.append(table)
            for operation in ("INSERT", "UPDATE", "DELETE"):
                name = self.prefix + table + "_" + operation.lower()
                aliases = (
                    ("NEW",)
                    if operation == "INSERT"
                    else ("OLD",)
                    if operation == "DELETE"
                    else ("OLD", "NEW")
                )
                # Reference count changes do not alter a cold parent's deadline.
                changed_columns = (
                    " OF public_uuid,expires,id"
                    if table == "market_record_parents" and operation == "UPDATE"
                    else ""
                )
                body = "".join(f"SELECT cw_subject_changed('{kind}',{alias}.{column});" for alias in aliases)
                when = ""
                if table == "market_expiry":
                    when = " WHEN " + " OR ".join(alias + ".category='event-head'" for alias in aliases)
                elif table == "market_runtime_keys":
                    assert key_registry.ROOTS[3] == "radar:v1:event-head:"
                    token = (key_registry.MAGIC + bytes((3,))).hex()
                    when = " WHEN " + " OR ".join(
                        f"{alias}.external LIKE 'radar:v1:event-head:%' OR (typeof({alias}.external)='blob' AND substr({alias}.external,1,5)=X'{token}')"
                        for alias in aliases
                    )
                elif table == "market_expiry_legacy":
                    when = " WHEN " + " OR ".join(
                        alias + ".key LIKE 'radar:v1:event-head:%'" for alias in aliases
                    )
                db.execute(
                    f"CREATE TEMP TRIGGER {name} AFTER {operation}{changed_columns} ON main.{table}{when} BEGIN {body} END"
                )
        install_lease_plan(db)
        self.guards = dict(self.read_guards())
        self.optional_guards = set()
        if "market_expiry" not in self.tables or "market_runtime_keys" not in self.tables:
            raise StateError("Subject prototype requires normalized expiry; old layouts keep baseline")
        db.commit()

    def read_guards(self):
        return tuple(
            tuple(r)
            for r in self.store.db.execute(
                "SELECT name,sql FROM sqlite_temp_master WHERE name LIKE 'cw_subject_%' ORDER BY name"
            )
        )

    def changed(self, kind, key):
        if self.active is not None:
            self.active.changed(kind, key)
        return 0

    def require(self):
        current = dict(self.read_guards())
        for name, sql in current.items():
            if self.guards.get(name) != sql:
                raise StateError("Subject prototype parent guards changed")
        if (self.guards.keys() - current.keys()) - self.optional_guards:
            raise StateError("Subject prototype parent guards changed")
        db = self.store.db
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_record_parents'"
        ).fetchone():
            # Repack may install this table after prototype activation. No proof
            # survives a publication; install its guards before the first one.
            # If the outer source rolls back these TEMP DDLs, recreate only the
            # known optional guards at the next fresh publication boundary.
            for operation in ("INSERT", "UPDATE", "DELETE"):
                name = self.prefix + "market_record_parents_" + operation.lower()
                if name in current:
                    continue
                aliases = (
                    ("NEW",)
                    if operation == "INSERT"
                    else ("OLD",)
                    if operation == "DELETE"
                    else ("OLD", "NEW")
                )
                changed_columns = " OF public_uuid,expires,id" if operation == "UPDATE" else ""
                body = "".join(
                    f"SELECT cw_subject_changed('binary',{alias}.public_uuid);" for alias in aliases
                )
                db.execute(
                    f"CREATE TEMP TRIGGER {name} AFTER {operation}{changed_columns} ON main.market_record_parents BEGIN {body} END"
                )
                self.optional_guards.add(name)
            current = dict(self.read_guards())
            self.guards.update(current)


def activate(store, stats):
    watcher = getattr(store, "_subject_save_watcher", None)
    if watcher is None:
        watcher = Watcher(store, stats)
        store._subject_save_watcher = watcher
    return watcher


@contextmanager
def publication(history):
    watcher = getattr(history.store, "_subject_save_watcher", None)
    if watcher is None:
        with BASE_PUBLICATION(history) as unit:
            yield unit
        return
    watcher.require()
    previous = watcher.active
    try:
        with BASE_PUBLICATION(history) as unit:
            watcher.active = Subject(watcher, unit)
            yield unit
    finally:
        # Remain active through the original final batch flush and rollback.
        watcher.active = previous


def prepare_value(history, value, *, freeze, collect=True, depth=0):
    """The original service freeze and its retained refs in one traversal."""
    if not freeze:
        return value, references(value) if collect else set()
    if depth > 24:
        raise StateError("Service snapshot nesting exceeds bound")
    if isinstance(value, list):
        result, refs = [], set()
        for child in value:
            item, links = prepare_value(history, child, freeze=freeze, collect=collect, depth=depth + 1)
            result.append(item)
            refs.update(links)
        return (result if freeze else value), refs
    if not isinstance(value, dict):
        return value, set()
    if freeze:
        for kind in ("event", "outcome", "episode", "comparison"):
            if (
                value.get("schema") == f"cryptowatcher.{kind}.v1"
                and kind + "_id" in value
                and type(value.get("revision")) is int
            ):
                key = f"{kind}:{value[kind + '_id']}:{value['revision']:010d}"
                saved = history.get(key)
                if saved is not None and encode(saved) == encode(value):
                    return {
                        "_cw_revision": key,
                        "sha256": hashlib.sha256(encode(value).encode()).hexdigest(),
                    }, set()
    result, refs = {}, set()
    for key, child in value.items():
        item, links = prepare_value(
            history, child, freeze=freeze, collect=collect and key != "_history_storage", depth=depth + 1
        )
        result[key] = item
        refs.update(links)
    if collect:
        for ref in result.get("_history_storage", {}).get("fields", {}).values():
            if ref.get("mode") == "value2":
                refs.add(ref["id"])
            elif ref.get("mode") in ("value", "window"):
                refs.update(ref["ids"])
    return (result if freeze else value), refs


def references(value):
    found = set()

    def visit(item):
        if isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, dict):
            for ref in item.get("_history_storage", {}).get("fields", {}).values():
                if ref.get("mode") == "value2":
                    found.add(ref["id"])
                elif ref.get("mode") in ("value", "window"):
                    found.update(ref["ids"])
            for key, child in item.items():
                if key != "_history_storage":
                    visit(child)

    visit(value)
    return found


def link_delta(store, key, wanted, stats):
    current = owner_refs(store.db, key)
    removed, added = current - wanted, wanted - current
    if (removed or added) and wanted:
        names = ",".join("?" for _ in wanted)
        known = {
            r[0]
            for r in store.db.execute(
                f"SELECT body FROM market_body_counts WHERE body IN({names})", tuple(wanted)
            )
        }
        if known != wanted:
            raise sqlite3.IntegrityError("Missing shared body counter")
    if removed:
        store.db.executemany(
            "DELETE FROM market_body_links WHERE owner=? AND body=?", ((key, b) for b in sorted(removed))
        )
    if added:
        store.db.executemany("INSERT INTO market_body_links VALUES (?,?)", ((key, b) for b in sorted(added)))
    stats["fallback_references_removed"] += len(removed)
    stats["fallback_references_added"] += len(added)


def hot_plan(subject, store, key, value, refs):
    """Prepare product metadata once, without reconstructing it after each SQL."""
    lifecycle = storage_lifecycle
    subject.check()
    if subject.disabled or not subject.hot_only:
        return False
    if any(not isinstance(ref, str) for ref in refs) or len(refs) > MAX_REFS:
        return False
    now = store.clock_ms()
    days = store.snapshot().settings.detail_retention_days
    name = key[len(PREFIX) :]
    event = lifecycle.parent_event(store, value, name)
    expiry, use_previous = now + days * lifecycle.DAY, True
    if isinstance(value, dict):
        anchor = value.get(
            "market_observation_ms",
            value.get("reference_time_ms", value.get("started_ms", value.get("created_ms"))),
        )
        if type(anchor) is int:
            expiry, use_previous = anchor + days * lifecycle.DAY, False
    if event:
        parent = subject.parent(event)
        if parent:
            expiry, use_previous = parent[0], False
    category = name.split(":", 1)[0]
    if category in ("active", "historical-active", "suppressed-active"):
        expiry, use_previous = now + days * lifecycle.DAY, False
    elif category in ("condition", "diagnostic", "oi-attempt"):
        expiry, use_previous = now + store.snapshot().settings.candle_retention_days * lifecycle.DAY, False
    cap = (
        value.get("expires_ms") if isinstance(value, dict) and type(value.get("expires_ms")) is int else None
    )
    token = store._cached_value("registry-pack", "", key, lambda: key_registry.pack(key))
    size = len(token) if isinstance(token, bytes) else len(token.encode())
    subject.check()
    if subject.disabled:
        return False
    if type(expiry) is not int:
        return False
    try:
        store.db.execute(
            "INSERT INTO cw_subject_apply VALUES (?,?,?,?,?,?,?,?,?)",
            (key, token, size, expiry, int(use_previous), cap, category, event, encode(sorted(refs))),
        )
    except sqlite3.IntegrityError as exc:
        if str(exc) == "CW_SUBJECT_UNSUPPORTED_EXPIRY":
            return False
        if str(exc) == "CW_SUBJECT_CONFLICT_NATIVE_RETENTION":
            raise ValueError("Conflicting native retention bindings") from exc
        raise
    if name.startswith("hold:"):
        from cryptowatcher.market_storage import stream_id

        sid = stream_id(store.db, name.split(":")[1], create=True)
        # The unshortened parent/default lease used by the original hold row.
        if use_previous:
            actual = expiry_index.expires(store.db, key)
            expiry = actual[0] if actual else expiry
        elif cap is not None:
            expiry = min(expiry, cap)
        store.db.execute(
            "INSERT INTO market_holds VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET start=excluded.start,end=excluded.end,expires=excluded.expires",
            (key, sid, value["start"], value["end"], expiry),
        )
    subject.watcher.stats["planned"] += 1
    return True


def track(subject, store, key, value, refs):
    lifecycle = storage_lifecycle
    name = key[len(PREFIX) :]
    if (
        name in lifecycle.PRIVATE
        or name in ("target:telegram", "agent:target")
        or name.startswith(lifecycle.FIXED)
        or ":" not in name
        or name.startswith("condition-state:")
    ):
        lifecycle.track(store, key, value)
        return
    terminal = (
        isinstance(value, dict)
        and value.get("state") in lifecycle.TERMINAL_DELIVERY | {"COMPLETE", "EXPIRED_RETENTION_INCOMPLETE"}
        and name.startswith(
            ("agent:attempt:", "agent:outbox:", "outbox:telegram:", "analytics-gap:", "digest-outbox:")
        )
    )
    if not terminal and hot_plan(subject, store, key, value, refs):
        return
    subject.check()
    now = store.clock_ms()
    event_id = lifecycle.parent_event(store, value, name)
    days = store.snapshot().settings.detail_retention_days
    previous = expiry_index.expires(store.db, key)
    expiry = previous[0] if previous else now + days * lifecycle.DAY
    if isinstance(value, dict):
        anchor = value.get(
            "market_observation_ms",
            value.get("reference_time_ms", value.get("started_ms", value.get("created_ms"))),
        )
        if type(anchor) is int:
            expiry = anchor + days * lifecycle.DAY
    if event_id:
        parent = subject.parent(event_id)
        if parent:
            expiry = parent[0]
    if isinstance(value, dict) and type(value.get("expires_ms")) is int:
        expiry = min(expiry, value["expires_ms"])
    category = name.split(":", 1)[0]
    if category in ("active", "historical-active", "suppressed-active"):
        expiry = now + days * lifecycle.DAY
    elif category in ("condition", "diagnostic", "oi-attempt"):
        expiry = now + store.snapshot().settings.candle_retention_days * lifecycle.DAY
    expiry_index.put(store.db, key, expiry, category, event_id)
    if (
        isinstance(value, dict)
        and value.get("state") in lifecycle.TERMINAL_DELIVERY | {"COMPLETE", "EXPIRED_RETENTION_INCOMPLETE"}
        and name.startswith(
            ("agent:attempt:", "agent:outbox:", "outbox:telegram:", "analytics-gap:", "digest-outbox:")
        )
    ):
        finished = value.get("completed_ms", value.get("at_ms", now))
        if type(finished) is not int:
            finished = now
        expiry_index.renew(store.db, key, finished + lifecycle.DAY, shorten=True)
    link_delta(store, key, refs, subject.watcher.stats)
    if name.startswith("hold:"):
        from cryptowatcher.market_storage import stream_id

        sid = stream_id(store.db, name.split(":")[1], create=True)
        store.db.execute(
            "INSERT INTO market_holds VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET start=excluded.start,end=excluded.end,expires=excluded.expires",
            (key, sid, value["start"], value["end"], expiry),
        )


def save(history, key, value):
    original = value
    store = history.store
    watcher = getattr(store, "_subject_save_watcher", None)
    subject = None if watcher is None else watcher.active
    if subject is None or not store.bounded or type(history)._freeze_services is not BASE_FREEZE:
        return BASE_SAVE(history, key, value)
    subject.check()
    if subject.disabled:
        return BASE_SAVE(history, key, value)
    if (
        isinstance(value, dict)
        and key.startswith(("agent:outbox:", "outbox:telegram:", "digest-outbox:"))
        and value.get("state") in storage_lifecycle.TERMINAL_DELIVERY
    ):
        parent = (
            value.get("event_id")
            or value.get("intent", {}).get("event_id")
            or next(iter(value.get("event_ids", [])), None)
        )
        value = {
            k: v
            for k, v in value.items()
            if k not in ("intent", "body", "episode_snapshot", "snapshot", "text", "text_entities")
        }
        if parent:
            value["event_id"] = parent
    freeze = key.startswith(("agent:", "outbox:"))
    value, refs = prepare_value(history, value, freeze=freeze)
    # Fast-path bounds are never product rejection rules. Preserve the exact
    # original conversion/SQLite errors and reference policy for other inputs.
    if (
        len(refs) > MAX_REFS
        or any(not isinstance(ref, str) for ref in refs)
        or sum(len(ref.encode()) for ref in refs) > MAX_REF_BYTES
    ):
        return BASE_SAVE(history, key, original)
    plain = encode(value)
    payload = plain
    from cryptowatcher.record_codec import pack_binary

    compact = pack_binary(plain)
    if len(compact) < len(plain.encode()) or '"source-cells"' in plain:
        payload = compact
    from cryptowatcher.catalogue_write import mutation

    with mutation(store, PREFIX + key, value) as write:
        if not write:
            return
        store.db.execute(RUNTIME_UPSERT, (PREFIX + key, payload))
        track(subject, store, PREFIX + key, value, refs)
        if key.startswith(("outcome-job:", "comparison-job:")):
            from cryptowatcher.queue_health import sync_job

            sync_job(history, key, value)
    watcher.stats["saved"] += 1


@contextmanager
def installed(enabled=True):
    stats = dict.fromkeys(
        (
            "saved",
            "planned",
            "parent_reads",
            "parent_reuse",
            "parent_invalidations",
            "peak_parents",
            "fallback_references_added",
            "fallback_references_removed",
        ),
        0,
    )
    if not enabled:
        yield stats
        return

    def install(store):
        BASE_INSTALL(store)
        if isinstance(store.db, Connection) and getattr(store.db, "_subject_owned", False):
            expiry = store.db.execute(
                "SELECT type FROM sqlite_master WHERE name='market_lifetimes'"
            ).fetchone()
            links = store.db.execute(
                "SELECT type FROM sqlite_master WHERE name='market_body_links'"
            ).fetchone()
            if expiry is not None and links is not None and expiry[0] == links[0] == "view":
                activate(store, stats)

    def initialize_connection(db, *args, **kwargs):
        BASE_CONNECTION_INIT(db, *args, **kwargs)
        db._subject_owned = True
        db._subject_authorizer_present = False

    def authorizer(db, callback):
        result = BASE_AUTHORIZER(db, callback)
        # Opaque SQL policies may name the old compatibility views. Do not
        # bypass those policies through a different prepared statement.
        db._subject_authorizer_present = callback is not None
        watcher = getattr(db, "_subject_save_watcher", None)
        if watcher is not None:
            watcher.authorizer_present = callback is not None
        return result

    with ExitStack() as stack:
        stack.enter_context(patch.object(Connection, "__init__", initialize_connection))
        stack.enter_context(patch.object(Connection, "set_authorizer", authorizer))
        stack.enter_context(patch.object(domain_repository, "install", install))
        stack.enter_context(patch.object(domain_transition, "publication", publication))
        stack.enter_context(patch.object(events.History, "_put_raw", save))
        stack.enter_context(patch.object(domain_repository.History, "_write_legacy", save))
        yield stats
