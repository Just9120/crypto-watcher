"""Isolated two-counter SQL ledger. Never imported by application startup.

Only owned disposable fixtures may activate it. Ordinary counter SQL executes in
constraint-identical TEMP tables; durable tables receive exact final values before
the existing commit. The original application SQL and transaction guards remain.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from cryptowatcher.storage import StateError, Store
from cryptowatcher.writer_memory import Connection, Cursor

TABLES = ("market_counts", "market_namespaces")
KEYS = {"market_counts": "category", "market_namespaces": "namespace"}
BASE_EXECUTE = Connection.execute
BASE_MANY = Connection.executemany
BASE_SCRIPT = Connection.executescript
BASE_COMMIT = Connection.commit
BASE_ROLLBACK = Connection.rollback
BASE_EXIT = Connection.__exit__
BASE_CLOSE = Store.close
BASE_STORE_INIT = Store.__init__
BASE_CURSOR_EXECUTE = Cursor.execute
BASE_CURSOR_MANY = Cursor.executemany
BASE_AUTH = Connection.set_authorizer
BASE_INIT = Connection.__init__
WRITE_COUNTER = re.compile(
    r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:main\.)?(?:market_counts|market_namespaces)\b",
    re.I,
)
QUALIFIED = re.compile(r"\bmain\s*\.\s*[\"\[]?(market_counts|market_namespaces)\b", re.I)


def quote(name):
    return '"' + name.replace('"', '""') + '"'


class Ledger:
    def __init__(self, store, stats):
        if store.db.in_transaction or store.readonly:
            raise StateError("Counter prototype needs an idle owned writer")
        self.store, self.db, self.stats = store, store.db, stats
        self.owner = store.owner_id
        self.active = False
        self.internal = False
        self.templates = []
        self.columns = {}
        self.dirty = "cw_counter_dirty"
        if getattr(self.db, "_counter_has_authorizer", True):
            stats["unsupported_authorizer"] += 1
            return
        for table in TABLES:
            row = self.raw("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if row is None:
                return
            self.columns[table] = [r[1] for r in self.raw("PRAGMA table_info(" + quote(table) + ")")]
            if not self.columns[table] or self.columns[table][0] != KEYS[table]:
                raise StateError("Unexpected counter schema")
        # Counter-target user triggers must retain their exact immediate failure boundary.
        if self.raw(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name IN('market_counts','market_namespaces')"
        ).fetchone():
            stats["unsupported_target_trigger"] += 1
            return
        self.internal = True
        try:
            self.raw("BEGIN IMMEDIATE")
            self.raw(
                "CREATE TABLE IF NOT EXISTS cw_counter_originals(name TEXT PRIMARY KEY,sql TEXT NOT NULL,sha TEXT NOT NULL)"
            )
            if self.raw("SELECT 1 FROM cw_counter_originals").fetchone():
                raise StateError("Unrecovered owned counter prototype")
            for name, sql in self.raw("SELECT name,sql FROM sqlite_master WHERE type='trigger'").fetchall():
                if WRITE_COUNTER.search(sql or "") or re.search(
                    r"\b(?:market_counts|market_namespaces)\b", sql or "", re.I
                ):
                    self.templates.append((name, sql))
            for name, sql in self.templates:
                self.raw(
                    "INSERT INTO cw_counter_originals VALUES(?,?,?)",
                    (name, sql, hashlib.sha256(sql.encode()).hexdigest()),
                )
                self.raw("DROP TRIGGER main." + quote(name))
            for table in TABLES:
                source = self.raw(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()[0]
                temp = re.sub(r"^CREATE\s+TABLE", "CREATE TEMP TABLE", source, count=1, flags=re.I)
                self.raw(temp)
                self.raw("INSERT INTO temp." + quote(table) + " SELECT * FROM main." + quote(table))
            self.raw(
                "CREATE TEMP TABLE cw_counter_dirty(table_name TEXT NOT NULL,key TEXT NOT NULL,PRIMARY KEY(table_name,key)) WITHOUT ROWID"
            )
            for table in TABLES:
                key = KEYS[table]
                for op in ("INSERT", "UPDATE", "DELETE"):
                    alias = "OLD" if op == "DELETE" else "NEW"
                    sql = (
                        "CREATE TEMP TRIGGER "
                        + quote("cw_counter_mark_" + table + "_" + op)
                        + " AFTER "
                        + op
                        + " ON temp."
                        + quote(table)
                        + " BEGIN INSERT INTO cw_counter_dirty SELECT '"
                        + table
                        + "',"
                        + alias
                        + "."
                        + quote(key)
                        + " WHERE NOT EXISTS(SELECT 1 FROM cw_counter_dirty WHERE table_name='"
                        + table
                        + "' AND key="
                        + alias
                        + "."
                        + quote(key)
                        + "); END"
                    )
                    self.raw(sql)
            for _, sql in self.templates:
                self.raw(re.sub(r"^CREATE\s+TRIGGER", "CREATE TEMP TRIGGER", sql, count=1, flags=re.I))
            self.active = True
            self.reconcile_meter()
            self.flush()
            sqlite3.Connection.commit(self.db)
            self.stats["activations"] += 1
            self.db._guards_ready = False
        except BaseException:
            sqlite3.Connection.rollback(self.db)
            raise
        finally:
            self.internal = False

    def raw(self, sql, parameters=()):
        return sqlite3.Connection.execute(self.db, sql, parameters)

    def check(self):
        if self.store.owner_id != self.owner or self.store.readonly:
            raise StateError("Counter ledger owner/rights changed")

    def reconcile_meter(self):
        if self.raw("SELECT 1 FROM sqlite_master WHERE name='market_namespace_scan'").fetchone():
            from cryptowatcher.runtime_meter import drain

            for _ in range(256):
                if drain(self.db, batch=256, deadline=time.monotonic() + 1):
                    return
            raise StateError("Counter prototype namespace reconciliation bound")

    def flush(self):
        if not self.active:
            return
        self.check()
        if not self.db.in_transaction:
            if self.raw("SELECT 1 FROM temp.cw_counter_dirty").fetchone():
                raise StateError("Counter dirty state escaped its source transaction")
            return
        was = self.internal
        self.internal = True
        began = time.thread_time()
        before = self.db.total_changes
        try:
            dirty = self.raw("SELECT COUNT(*) FROM temp.cw_counter_dirty").fetchone()[0]
            if dirty == 0:
                return
            for table in TABLES:
                key = KEYS[table]
                cols = self.columns[table]
                target = "main." + quote(table)
                self.raw(
                    "DELETE FROM "
                    + target
                    + " WHERE "
                    + quote(key)
                    + " IN(SELECT key FROM temp.cw_counter_dirty WHERE table_name=?) AND NOT EXISTS(SELECT 1 FROM temp."
                    + quote(table)
                    + " t WHERE t."
                    + quote(key)
                    + "="
                    + target
                    + "."
                    + quote(key)
                    + ")",
                    (table,),
                )
                same = " AND ".join("b." + quote(c) + " IS t." + quote(c) for c in cols)
                updates = ",".join(quote(c) + "=excluded." + quote(c) for c in cols[1:])
                sql = (
                    "INSERT INTO "
                    + target
                    + " SELECT t.* FROM temp."
                    + quote(table)
                    + " t JOIN temp.cw_counter_dirty d ON d.table_name=? AND d.key=t."
                    + quote(key)
                    + " WHERE NOT EXISTS(SELECT 1 FROM "
                    + target
                    + " b WHERE b."
                    + quote(key)
                    + "=t."
                    + quote(key)
                    + " AND "
                    + same
                    + ") ON CONFLICT("
                    + quote(key)
                    + ") DO UPDATE SET "
                    + updates
                )
                self.raw(sql, (table,))
            changed = self.db.total_changes - before
            self.raw("DELETE FROM temp.cw_counter_dirty")
            self.stats["flushes"] += 1
            self.stats["dirty_keys"] += dirty
            self.stats["durable_counter_changes"] += changed
            self.stats["max_dirty_keys"] = max(self.stats["max_dirty_keys"], dirty)
        finally:
            self.stats["flush_cpu_seconds"] += time.thread_time() - began
            self.internal = was

    def refresh(self):
        # Explicit main-qualified writes remain visible through the public shadow.
        was = self.internal
        self.internal = True
        try:
            for table in TABLES:
                self.raw("DELETE FROM temp." + quote(table))
                self.raw("INSERT INTO temp." + quote(table) + " SELECT * FROM main." + quote(table))
            self.raw("DELETE FROM temp.cw_counter_dirty")
        finally:
            self.internal = was

    def stop(self):
        if not self.active:
            return
        self.flush()
        was = self.internal
        self.internal = True
        began = time.thread_time()
        own = not self.db.in_transaction
        try:
            if own:
                self.raw("BEGIN IMMEDIATE")
            for name, _ in self.templates:
                self.raw("DROP TRIGGER IF EXISTS temp." + quote(name))
            for table in TABLES:
                self.raw("DROP TABLE temp." + quote(table))
            self.raw("DROP TABLE temp.cw_counter_dirty")
            for _, sql in self.templates:
                self.raw(sql)
            self.raw("DROP TABLE cw_counter_originals")
            self.active = False
            self.reconcile_meter()
            if own:
                sqlite3.Connection.commit(self.db)
            self.stats["deactivations"] += 1
            self.db._guards_ready = False
        except BaseException:
            if own:
                sqlite3.Connection.rollback(self.db)
                self.recheck()
            raise
        finally:
            self.stats["restore_cpu_seconds"] += time.thread_time() - began
            self.internal = was

    def recheck(self):
        actual = (
            self.raw(
                "SELECT 1 FROM sqlite_temp_master WHERE type='table' AND name='cw_counter_dirty'"
            ).fetchone()
            is not None
        )
        self.active = actual

    def before(self, sql):
        if self.internal or not self.active:
            return False
        self.check()
        verb = sql.lstrip().split(None, 1)[0].upper() if isinstance(sql, str) and sql.strip() else ""
        if verb in ("COMMIT", "END"):
            authorize_end(self.db)
            self.flush()
        elif verb == "RELEASE":
            # Also covers a top-level SAVEPOINT ending the original transaction.
            self.flush()
        if (
            verb in ("CREATE", "DROP", "ALTER", "VACUUM", "ATTACH", "DETACH")
            or verb == "PRAGMA"
            and ("=" in sql or "(" in sql)
        ):
            # Unknown/schema operations keep their original semantics and guards.
            self.stop()
            self.stats["ddl_fallbacks"] += 1
        if QUALIFIED.search(sql):
            self.flush()
            return True
        return False


def recover(store):
    """Owned fixture recovery after process loss; counter values were committed first."""
    db = store.db
    saved = sqlite3.Connection.execute(
        db, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cw_counter_originals'"
    ).fetchone()
    if saved is None or store.readonly:
        return
    if db.in_transaction:
        raise StateError("Counter recovery needs an idle owned connection")
    templates = sqlite3.Connection.execute(db, "SELECT name,sql,sha FROM cw_counter_originals").fetchall()
    if not templates:
        raise StateError("Missing counter recovery descriptors")
    for name, sql, sha in templates:
        if hashlib.sha256(sql.encode()).hexdigest() != sha or not re.match(r"CREATE\s+TRIGGER\b", sql, re.I):
            raise StateError("Corrupt counter recovery descriptor")
    sqlite3.Connection.execute(db, "BEGIN IMMEDIATE")
    try:
        for name, sql, _ in templates:
            sqlite3.Connection.execute(db, "DROP TRIGGER IF EXISTS main." + quote(name))
            sqlite3.Connection.execute(db, sql)
        sqlite3.Connection.execute(db, "DROP TABLE cw_counter_originals")
        from cryptowatcher.runtime_meter import drain

        for _ in range(256):
            if drain(db, batch=256, deadline=time.monotonic() + 1):
                break
        else:
            raise StateError("Counter recovery reconciliation bound")
        sqlite3.Connection.commit(db)
    except BaseException:
        sqlite3.Connection.rollback(db)
        raise


def ledger(db):
    return getattr(db, "_counter_ledger", None)


def authorize_end(db):
    if db.hot is not None or db.publication_depth:
        raise StateError("Joined producer cannot end its source transaction")


@contextmanager
def installed(enabled=True):
    stats = dict.fromkeys(
        (
            "activations",
            "deactivations",
            "flushes",
            "dirty_keys",
            "durable_counter_changes",
            "max_dirty_keys",
            "unsupported_authorizer",
            "unsupported_target_trigger",
            "ddl_fallbacks",
        ),
        0,
    )
    stats.update(flush_cpu_seconds=0.0, restore_cpu_seconds=0.0, snapshots=[])

    def initialize_store(store, *args, **kwargs):
        BASE_STORE_INIT(store, *args, **kwargs)
        if isinstance(store.db, Connection):
            try:
                recover(store)
            except BaseException:
                BASE_CLOSE(store)
                raise

    def init(db, *args, **kwargs):
        BASE_INIT(db, *args, **kwargs)
        db._counter_has_authorizer = False

    def execute(db, sql, parameters=()):
        item = ledger(db)
        refresh = item.before(sql) if item else False
        try:
            result = BASE_EXECUTE(db, sql, parameters)
        except BaseException:
            if item:
                item.recheck()
            raise
        if item and re.match(r"\s*ROLLBACK\b", sql, re.I):
            item.recheck()
        if refresh and item.active and re.match(r"\s*(?:INSERT|UPDATE|DELETE|REPLACE)\b", sql, re.I):
            item.refresh()
        return result

    def many(db, sql, parameters):
        item = ledger(db)
        refresh = item.before(sql) if item else False
        result = BASE_MANY(db, sql, parameters)
        if refresh and item.active:
            item.refresh()
        return result

    def cursor_execute(cursor, sql, parameters=()):
        item = ledger(cursor.connection)
        refresh = item.before(sql) if item else False
        try:
            result = BASE_CURSOR_EXECUTE(cursor, sql, parameters)
        except BaseException:
            if item:
                item.recheck()
            raise
        if item and re.match(r"\s*ROLLBACK\b", sql, re.I):
            item.recheck()
        if refresh and item.active and re.match(r"\s*(?:INSERT|UPDATE|DELETE|REPLACE)\b", sql, re.I):
            item.refresh()
        return result

    def cursor_many(cursor, sql, parameters):
        item = ledger(cursor.connection)
        refresh = item.before(sql) if item else False
        result = BASE_CURSOR_MANY(cursor, sql, parameters)
        if refresh and item.active:
            item.refresh()
        return result

    def commit(db):
        authorize_end(db)
        item = ledger(db)
        if item:
            item.flush()
        return BASE_COMMIT(db)

    def exit_db(db, *args):
        authorize_end(db)
        item = ledger(db)
        if item and args[0] is None:
            try:
                item.flush()
            except BaseException as exc:
                # Match sqlite3 context-manager commit failure semantics: roll
                # back the same source transaction, including its TEMP deltas.
                BASE_EXIT(db, type(exc), exc, exc.__traceback__)
                item.recheck()
                raise
        result = BASE_EXIT(db, *args)
        if item and args[0] is not None:
            item.recheck()
        return result

    def rollback(db):
        result = BASE_ROLLBACK(db)
        item = ledger(db)
        if item:
            item.recheck()
        return result

    def script(db, sql):
        if db.hot is not None:
            raise StateError("DDL is outside the joined producer boundary")
        item = ledger(db)
        if item and item.active:
            item.stop()
        try:
            return BASE_SCRIPT(db, sql)
        finally:
            if item:
                item.recheck()

    def authorizer(db, callback):
        item = ledger(db)
        if item and item.active:
            if db.hot is not None:
                db.hot.flush()
            item.stop()
        db._counter_has_authorizer = callback is not None
        return BASE_AUTH(db, callback)

    def close(store):
        item = ledger(store.db)
        if item:
            item.recheck()
        if item and item.active:
            if store.db.in_transaction:
                # Store.close rolls back, it must never commit uncommitted user work.
                sqlite3.Connection.rollback(store.db)
                item.recheck()
            item.stop()
            sqlite3.Connection.commit(store.db)
        if isinstance(store.db, Connection):
            # Equal post-case evidence for both paths. This independent canonical
            # reconciliation is outside paced timers but inside complete case CPU.
            if sqlite3.Connection.execute(
                store.db, "SELECT 1 FROM sqlite_master WHERE name='market_namespace_scan'"
            ).fetchone():
                from cryptowatcher.runtime_meter import drain

                with store.db:
                    for _ in range(256):
                        if drain(store.db, batch=256, deadline=time.monotonic() + 1):
                            break
                    else:
                        raise StateError("Final counter evidence reconciliation bound")
            snapshot = {}
            for table in TABLES:
                if sqlite3.Connection.execute(
                    store.db, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone():
                    snapshot[table] = [
                        tuple(r)
                        for r in sqlite3.Connection.execute(
                            store.db, "SELECT * FROM main." + quote(table) + " ORDER BY 1"
                        )
                    ]
            stats["snapshots"].append(snapshot)
        return BASE_CLOSE(store)

    from cryptowatcher.bot import Controller

    original_controller = Controller.__init__

    def controller(self, *args, **kwargs):
        original_controller(self, *args, **kwargs)
        if enabled and isinstance(self.store.db, Connection):
            item = Ledger(self.store, stats)
            self.store.db._counter_ledger = item

    with ExitStack() as stack:
        if enabled:
            for obj, name, method in (
                (Connection, "__init__", init),
                (Connection, "execute", execute),
                (Connection, "executemany", many),
                (Cursor, "execute", cursor_execute),
                (Cursor, "executemany", cursor_many),
                (Connection, "commit", commit),
                (Connection, "rollback", rollback),
                (Connection, "__exit__", exit_db),
                (Connection, "executescript", script),
                (Connection, "set_authorizer", authorizer),
            ):
                stack.enter_context(patch.object(obj, name, method))
            stack.enter_context(patch.object(Store, "__init__", initialize_store))
            stack.enter_context(patch.object(Controller, "__init__", controller))
        stack.enter_context(patch.object(Store, "close", close))
        yield stats


def activate(store, stats):
    item = Ledger(store, stats)
    store.db._counter_ledger = item
    return item
