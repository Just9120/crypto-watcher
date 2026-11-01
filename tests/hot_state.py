"""Isolated joined-producer hot state; never activated by the runtime.

One existing source transaction owns a finite write-through view of mutable
state. Immutable revisions, expiry/ownership validation and the actual flush
remain the existing repository. Metadata memoization ends at this boundary.
"""

from __future__ import annotations

import copy
import sqlite3
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from cryptowatcher.storage import StateError, encode

DELETE = object()
PREFIXES = (
    "active:",
    "historical-active:",
    "suppressed-active:",
    "condition-state:",
    "agent:intent:",
    "agent:intent-due:",
    "agent:ready:",
)
COUNTERS = ("event-counter", "episode-counter")
QUERIES = frozenset(
    (
        "SELECT type FROM sqlite_master WHERE name='market_lifetimes'",
        "SELECT type FROM sqlite_master WHERE name='market_body_links'",
        "SELECT key,value FROM market_meta WHERE key IN('record_catalogue_runtime','record_catalog','record_ownership','catalogue_cohorts')",
        "SELECT revision,settings_json FROM owner_settings WHERE owner_id=?",
    )
)


def selected(key):
    return key in COUNTERS or key.startswith(PREFIXES)


class Rows:
    def __init__(self, rows, description):
        self.rows, self.description, self.position = rows, description, 0
        self.rowcount, self.lastrowid = -1, None

    def fetchone(self):
        if self.position == len(self.rows):
            return None
        value = self.rows[self.position]
        self.position += 1
        return value

    def fetchmany(self, size=1):
        result = self.rows[self.position : self.position + size]
        self.position += len(result)
        return list(result)

    def fetchall(self):
        return self.fetchmany(len(self.rows) - self.position)

    def __iter__(self):
        return self

    def __next__(self):
        value = self.fetchone()
        if value is None:
            raise StopIteration
        return value


class Cursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        self.connection.invalidate(sql)
        return super().execute(sql, parameters)

    def executemany(self, sql, parameters):
        self.connection.invalidate(sql)
        return super().executemany(sql, parameters)


class Connection(sqlite3.Connection):
    hot = None

    def invalidate(self, sql):
        if self.hot is None:
            return
        verb = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
        if verb in ("COMMIT", "END") or (verb == "ROLLBACK" and " TO " not in " " + sql.upper() + " "):
            raise StateError("Joined producer cannot end its source transaction")
        if verb not in ("SELECT", "WITH", "PRAGMA") and (
            verb in ("CREATE", "DROP", "ALTER", "ROLLBACK")
            or "market_meta" in sql.lower()
            or "owner_settings" in sql.lower()
        ):
            self.hot.headers.clear()

    def cursor(self, factory=Cursor):
        return super().cursor(factory)

    def execute(self, sql, parameters=()):
        self.invalidate(sql)
        if self.hot is not None and sql in QUERIES:
            key = (sql, tuple(parameters))
            previous = self.hot.headers.get(key)
            if previous is None:
                cursor = super().execute(sql, parameters)
                previous = (tuple(cursor.fetchall()), cursor.description)
                if len(self.hot.headers) >= 16 or len(previous[0]) > 4:
                    raise StateError("Joined metadata budget exceeded")
                self.hot.headers[key] = previous
                self.hot.stats["metadata_misses"] += 1
            else:
                self.hot.stats["metadata_hits"] += 1
            return Rows(*previous)
        return super().execute(sql, parameters)

    def executemany(self, sql, parameters):
        self.invalidate(sql)
        return super().executemany(sql, parameters)

    def executescript(self, script):
        if self.hot is not None:
            raise StateError("DDL is outside the joined producer boundary")
        return super().executescript(script)

    def commit(self):
        if self.hot is not None:
            raise StateError("Joined producer cannot commit its source transaction")
        return super().commit()

    def rollback(self):
        if self.hot is not None:
            raise StateError("Joined producer cannot roll back its source transaction")
        return super().rollback()

    def __exit__(self, *args):
        if self.hot is not None:
            raise StateError("Joined producer cannot end its source transaction")
        return super().__exit__(*args)


class Batch:
    def __init__(self, history, put, delete, stats):
        self.history, self.put, self.delete, self.stats = history, put, delete, stats
        self.headers, self.writes, self.sizes = {}, {}, {}
        self.flushing = False

    def write(self, key, value):
        frozen = DELETE if value is DELETE else copy.deepcopy(value)
        size = len(key.encode()) + (0 if value is DELETE else len(encode(value).encode()))
        if (
            len(self.writes) + (key not in self.writes) > 128
            or sum(self.sizes.values()) - self.sizes.get(key, 0) + size > 1024**2
        ):
            raise StateError("Joined mutable-state budget exceeded")
        self.stats["buffered_mutations"] += 1
        self.writes[key], self.sizes[key] = frozen, size
        self.stats["peak_keys"] = max(self.stats["peak_keys"], len(self.writes))

    def flush(self):
        if self.flushing:
            return
        self.flushing = True
        try:
            for key, value in self.writes.items():
                if value is DELETE:
                    self.delete(self.history, key)
                else:
                    self.put(self.history, key, value)
                self.stats["flushed_mutations"] += 1
            self.writes.clear()
            self.sizes.clear()
        finally:
            self.flushing = False


@contextmanager
def installed(enabled):
    stats = dict.fromkeys(
        (
            "observations",
            "buffered_mutations",
            "flushed_mutations",
            "peak_keys",
            "metadata_hits",
            "metadata_misses",
        ),
        0,
    )
    if not enabled:
        yield stats
        return
    from cryptowatcher.events import EventEngine
    from tests.sqlite_repository import History

    connect, observe = sqlite3.connect, EventEngine.observe
    get, put, delete = History._get_raw, History._put_raw, History._delete_raw
    items, metadata = History.items, History.metadata

    def connection(*args, **kwargs):
        if "factory" in kwargs or len(args) > 5:
            raise ValueError("Custom connection outside isolated hot-state scope")
        return connect(*args, factory=Connection, **kwargs)

    def current(history):
        return getattr(history.store.db, "hot", None)

    def reading(history, key, default=None):
        batch = current(history)
        if batch is not None and not batch.flushing and key in batch.writes:
            value = batch.writes[key]
            return default if value is DELETE else history.resolve(key, copy.deepcopy(value))
        return get(history, key, default)

    def writing(history, key, value):
        batch = current(history)
        if batch is not None and not batch.flushing and selected(key):
            batch.write(key, value)
            return
        return put(history, key, value)

    def deleting(history, key):
        batch = current(history)
        if batch is not None and not batch.flushing and selected(key):
            batch.write(key, DELETE)
            return
        return delete(history, key)

    def scanning(history, *args, **kwargs):
        batch = current(history)
        if batch is not None:
            batch.flush()
        return items(history, *args, **kwargs)

    def inspecting(history, *args, **kwargs):
        batch = current(history)
        if batch is not None:
            batch.flush()
        return metadata(history, *args, **kwargs)

    def observing(engine, *args, **kwargs):
        eligible = (
            kwargs.get("commit") is False
            and engine.on_event is engine._snapshot_event_callback
            and engine.on_event_snapshot is not None
            and isinstance(engine.history, History)
        )
        if not eligible:
            return observe(engine, *args, **kwargs)
        db = engine.store.db
        if not isinstance(db, Connection) or not db.in_transaction or db.hot is not None:
            raise StateError("Hot state requires one existing joined writer transaction")
        batch = Batch(engine.history, put, delete, stats)
        db.hot = batch
        stats["observations"] += 1
        try:
            result = observe(engine, *args, **kwargs)
            batch.flush()
            return result
        finally:
            db.hot = None

    with ExitStack() as stack:
        for target, name, value in (
            (sqlite3, "connect", connection),
            (History, "_get_raw", reading),
            (History, "_put_raw", writing),
            (History, "_delete_raw", deleting),
            (History, "items", scanning),
            (History, "metadata", inspecting),
            (EventEngine, "observe", observing),
        ):
            stack.enter_context(patch.object(target, name, value))
        yield stats
