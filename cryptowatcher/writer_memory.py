"""Joined-producer state owned by one SQLite connection.

One existing source transaction owns a finite write-through view of mutable
state. Immutable revisions, expiry/ownership validation and the actual flush
remain the existing repository. Metadata memoization ends at this boundary.
"""

from __future__ import annotations

import copy
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, cast

from .storage import StateError, encode
from .writer_episode_reads import EpisodeReads

DELETE = object()
PREFIXES = (
    "active:",
    "historical-active:",
    "suppressed-active:",
    "condition-state:",
    "agent:intent:",
    "agent:intent-due:",
    "agent:ready:",
    "outbox:telegram:",
    "pending:telegram:",
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


def selected(key: Any) -> Any:
    return key in COUNTERS or key.startswith(PREFIXES)


class Rows:
    def __init__(self, rows: Any, description: Any) -> None:
        self.rows, self.description, self.position = rows, description, 0
        self.rowcount, self.lastrowid = -1, None

    def fetchone(self) -> Any:
        if self.position == len(self.rows):
            return None
        value = self.rows[self.position]
        self.position += 1
        return value

    def fetchmany(self, size: Any = 1) -> Any:
        result = self.rows[self.position : self.position + size]
        self.position += len(result)
        return list(result)

    def fetchall(self) -> Any:
        return self.fetchmany(len(self.rows) - self.position)

    def __iter__(self) -> Any:
        return self

    def __next__(self) -> Any:
        value = self.fetchone()
        if value is None:
            raise StopIteration
        return value


class Cursor(sqlite3.Cursor):
    def execute(self, sql: Any, parameters: Any = ()) -> Any:
        cast(Connection, self.connection).invalidate(sql)
        return super().execute(sql, parameters)

    def executemany(self, sql: Any, parameters: Any) -> Any:
        cast(Connection, self.connection).invalidate(sql)
        return super().executemany(sql, parameters)


class Connection(sqlite3.Connection):
    hot: Any = None
    publication_depth = 0
    _metadata_epoch = 0
    _guard_rows: Any = None
    _guards_ready = False

    def metadata_changed(self) -> int:
        self._metadata_epoch += 1
        if self.hot is not None:
            self.hot.headers.clear()
            self.hot.episodes.clear()
        return self._metadata_epoch

    def install_metadata_guards(self) -> None:
        if self._guard_rows is not None:
            self.require_metadata_guards()
            return
        if self.in_transaction:
            raise StateError("Metadata guards require installed idle domain storage")
        # Connection-local TEMP triggers: no persistent schema/history change.
        self.create_function("cw_publication_metadata_changed", 0, self.metadata_changed)
        keys = "'record_catalogue_runtime','record_catalog','record_ownership','catalogue_cohorts'"
        names = []
        for table in ("owner_settings", "market_meta"):
            for operation in ("INSERT", "UPDATE", "DELETE"):
                name = "cw_pub_" + table + "_" + operation.lower()
                names.append(name)
                aliases = (
                    ("NEW",)
                    if operation == "INSERT"
                    else ("OLD",)
                    if operation == "DELETE"
                    else ("OLD", "NEW")
                )
                condition = (
                    ""
                    if table == "owner_settings"
                    else (" WHEN " + " OR ".join(alias + ".key IN(" + keys + ")" for alias in aliases))
                )
                self.execute(
                    f"CREATE TEMP TRIGGER {name} AFTER {operation} ON main.{table}{condition} "
                    "BEGIN SELECT cw_publication_metadata_changed(); END"
                )
        self._guard_rows = tuple(
            self.execute(
                "SELECT name,sql FROM sqlite_temp_master WHERE name LIKE 'cw_pub_%' ORDER BY name"
            ).fetchall()
        )
        if len(self._guard_rows) != len(names):
            raise StateError("Unexpected publication metadata guards")
        self._guards_ready = True

    def require_metadata_guards(self) -> None:
        if self._guard_rows is None:
            raise StateError("Publication metadata guards not installed")
        if not self._guards_ready:
            actual = tuple(
                super()
                .execute("SELECT name,sql FROM sqlite_temp_master WHERE name LIKE 'cw_pub_%' ORDER BY name")
                .fetchall()
            )
            if actual != self._guard_rows:
                raise StateError("Publication metadata guards changed; state preserved")
            self._guards_ready = True

    def invalidate(self, sql: Any) -> Any:
        verb = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
        if verb in ("CREATE", "DROP", "ALTER"):
            self._guards_ready = False
            self.metadata_changed()
        if self.hot is None and not self.publication_depth:
            return
        if verb in ("COMMIT", "END") or (verb == "ROLLBACK" and " TO " not in " " + sql.upper() + " "):
            raise StateError("Joined producer cannot end its source transaction")
        if verb == "ROLLBACK" or verb == "PRAGMA" and ("=" in sql or "(" in sql):
            self.metadata_changed()

    def set_authorizer(self, callback: Any) -> Any:
        if self.hot is not None:
            self.hot.flush()
        self.metadata_changed()
        return super().set_authorizer(callback)

    def cursor(self, factory: Any = Cursor) -> Any:
        return super().cursor(factory)

    def execute(self, sql: Any, parameters: Any = ()) -> Any:
        self.invalidate(sql)
        if self.hot is not None and sql in QUERIES:
            self.require_metadata_guards()
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

    def executemany(self, sql: Any, parameters: Any) -> Any:
        self.invalidate(sql)
        return super().executemany(sql, parameters)

    def executescript(self, script: Any) -> Any:
        if self.hot is not None:
            raise StateError("DDL is outside the joined producer boundary")
        return super().executescript(script)

    def commit(self) -> Any:
        if self.hot is not None or self.publication_depth:
            raise StateError("Joined producer cannot commit its source transaction")
        return super().commit()

    def rollback(self) -> Any:
        if self.hot is not None or self.publication_depth:
            raise StateError("Joined producer cannot roll back its source transaction")
        return super().rollback()

    def __exit__(self, *args: Any) -> Any:
        if self.hot is not None or self.publication_depth:
            raise StateError("Joined producer cannot end its source transaction")
        return super().__exit__(*args)


class Batch:
    def __init__(self, history: Any, put: Any, delete: Any, stats: Any) -> None:
        self.history, self.put, self.delete, self.stats = history, put, delete, stats
        self.headers: dict[Any, Any] = {}
        self.writes: dict[Any, Any] = {}
        self.sizes: dict[Any, int] = {}
        self.flushing = False
        self.episodes = EpisodeReads(stats)
        self.owner = None if history is None else (history.store.owner_id, history.store.readonly)

    def check(self) -> None:
        if (
            self.owner != (self.history.store.owner_id, self.history.store.readonly)
            or self.history.store.readonly
            or not self.history.store.db.in_transaction
        ):
            raise StateError("Publication owner or transaction changed")

    def write(self, key: Any, value: Any) -> Any:
        self.check()
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

    def flush(self) -> Any:
        if self.flushing:
            return
        self.check()
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
def transaction(history: Any) -> Iterator[Any]:
    db = history.store.db
    if not db.in_transaction or getattr(history.store, "_writer_batch", None) is not None:
        raise StateError("Writer batch requires its existing source transaction")
    stats = dict.fromkeys(
        (
            "observations",
            "buffered_mutations",
            "flushed_mutations",
            "peak_keys",
            "metadata_hits",
            "metadata_misses",
            "episode_row_hits",
            "episode_row_misses",
            "episode_record_hits",
            "episode_peak_bytes",
        ),
        0,
    )
    batch = Batch(history, type(history)._write_legacy, type(history)._delete_legacy, stats)
    setattr(history.store, "_writer_batch", batch)
    if isinstance(db, Connection):
        db.hot = batch
    try:
        yield batch
        batch.flush()
    finally:
        if isinstance(db, Connection):
            db.hot = None
        setattr(history.store, "_writer_batch", None)
