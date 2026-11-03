"""An explicit, finite producer write unit; no arbitrary-SQL buffer protocol."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

from .storage import StateError
from .writer_memory import Connection, transaction


class Publication:
    def __init__(self, history: Any, batch: Any) -> None:
        self.history, self.batch = history, batch
        self.active = True
        self.owner = (history.store.owner_id, history.store.readonly)
        self.configuration = self.read_configuration()

    def read_configuration(self) -> Any:
        row = sqlite3.Connection.execute(
            self.history.store.db,
            "SELECT revision,settings_json FROM owner_settings WHERE owner_id=?",
            (self.owner[0],),
        ).fetchone()
        return tuple(row) if row is not None else None

    def check(self) -> None:
        if (
            not self.active
            or self.owner != (self.history.store.owner_id, self.history.store.readonly)
            or self.history.store.readonly
            or not self.history.store.db.in_transaction
        ):
            raise StateError("Explicit publication owner or transaction changed")
        self.batch.check()

    @contextmanager
    def callback(self) -> Iterator[None]:
        """An arbitrary callback gets ordinary SQL semantics, not queued writes."""
        self.check()
        self.batch.flush()
        store, db = self.history.store, self.history.store.db
        epoch = db._metadata_epoch if isinstance(db, Connection) else None
        # Suspend queued mutations, not the independently guarded metadata
        # view. SQL readers still validate real rows and authorizer changes.
        store._writer_batch = None
        try:
            yield
        finally:
            if not isinstance(db, Connection) or epoch != db._metadata_epoch or not db._guards_ready:
                self.batch.headers.clear()
            self.batch.episodes.clear()
            store._writer_batch = self.batch
        self.check()


@contextmanager
def publication(history: Any) -> Iterator[Publication]:
    db = history.store.db
    if not db.in_transaction:
        raise StateError("Explicit publication needs its source transaction")
    if isinstance(db, Connection):
        db.require_metadata_guards()
    # Only the explicit producer joins this savepoint. Arbitrary History.version
    # callers keep their own atomic operation, including caught callback errors.
    db.execute("SAVEPOINT cw_domain_publication")
    if isinstance(db, Connection):
        db.publication_depth += 1
    unit = None
    try:
        with transaction(history) as batch:
            unit = Publication(history, batch)
            yield unit
            unit.check()
            batch.flush()
            if unit.configuration != unit.read_configuration():
                raise StateError("Publication settings changed during final writes")
    except BaseException:
        db.execute("ROLLBACK TO cw_domain_publication")
        db.execute("RELEASE cw_domain_publication")
        raise
    else:
        db.execute("RELEASE cw_domain_publication")
    finally:
        if unit is not None:
            unit.active = False
        if isinstance(db, Connection):
            db.publication_depth -= 1
