"""Atomic hot writes against retained cold records; activation is explicit."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from . import catalogue_aliases, catalogue_overlap, catalogue_reader, packet_store, record_catalog
from .record_blocks import KINDS, Coordinate
from .storage import StateError, Store, encode


@contextmanager
def scope(store: Store) -> Iterator[bool]:
    if not store.bounded or not catalogue_reader.reading(store.db):
        yield False
        return
    if record_catalog.layout(store.db) != 3:
        raise StateError("Catalogue writes require the complete current layout")
    catalogue_overlap.header(store.db)
    if not store.db.in_transaction:
        store.db.execute("BEGIN IMMEDIATE")
    with packet_store.atomic(store.db):
        if not catalogue_reader.reading(store.db):
            raise StateError("Concurrent catalogue activation change")
        yield True


def checked_write(store: Store, key: str, value: Any) -> bool:
    """Return true only when an identical immutable cold value already owns it."""
    selected = catalogue_reader.point(key)
    alias = catalogue_aliases.point(key)
    if selected is not None and selected.revision is not None:
        coordinate = Coordinate(selected.kind, selected.identity, selected.revision)
        if (
            not isinstance(value, dict)
            or value.get(coordinate.field) != coordinate.identity
            or type(value.get("revision")) is not int
            or value["revision"] != coordinate.revision
        ):
            raise StateError("Conflicting immutable revision identity")
    if alias is not None and (not isinstance(value, dict) or value.get("event_id") != alias.event):
        raise StateError("Conflicting history binding identity")
    previous = store.get_runtime(key, catalogue_reader.MISSING)
    if selected is None:
        if alias is not None and previous is not catalogue_reader.MISSING:
            if encode(previous) != encode(value):
                raise StateError("Conflicting immutable history binding")
            return catalogue_aliases.lookup(store.db, key) is not catalogue_reader.MISSING
        return False
    if selected.revision is not None:
        if previous is not catalogue_reader.MISSING and not catalogue_reader.equal_public(
            store.db, previous, value, store.cached_bytes
        ):
            raise StateError("Conflicting immutable history revision")
        return catalogue_reader.cold(store.db, selected, store.cached_bytes) is not catalogue_reader.MISSING
    new_revision = catalogue_reader.revision(value, selected)
    if (
        previous is not catalogue_reader.MISSING
        and catalogue_reader.revision(previous, selected) > new_revision
    ):
        raise StateError("Stale history head revision")
    coordinate = Coordinate(selected.kind, selected.identity, new_revision)
    body = store.get_runtime(catalogue_reader.PREFIX + coordinate.key, catalogue_reader.MISSING)
    if body is catalogue_reader.MISSING:
        raise StateError("Missing new history head revision")
    from .events import DECISION_HEAD, History

    history = History(store)
    public, compact = history._resolve_with_head(key[len(catalogue_reader.PREFIX) :], value)
    if compact and not (isinstance(body, dict) and DECISION_HEAD in body):
        # The resolver just read this exact revision and checked every head
        # field in our writer snapshot. Reuse its complete restoration; no
        # mutable DB lookup or proof is cached across calls. Keep the same
        # service normalization comparison as a direct revision read.
        actual = history._expand_services(public) if store.bounded else public
    else:
        actual = history.resolve(coordinate.key, body)
    if encode(public) != encode(actual):
        raise StateError("Conflicting new history head projection")
    current = record_catalog.head(store.db, selected.kind, selected.identity, store.cached_bytes)
    if current is None:
        return False
    if current[0].revision == new_revision:
        return True
    if current[0].revision > new_revision:
        raise StateError("Stale cold history head revision")
    changed = store.db.execute(
        "UPDATE market_records SET current=0 WHERE kind=? AND public_uuid=? AND revision=? AND current=1",
        (KINDS.index(selected.kind), record_catalog.binary(selected.identity), current[0].revision),
    ).rowcount
    if changed != 1:
        raise StateError("Concurrent cold history head change")
    return False


@contextmanager
def mutation(store: Store, key: str, value: Any) -> Iterator[bool]:
    if catalogue_reader.point(key) is None and catalogue_aliases.point(key) is None:
        yield True
        return
    with scope(store) as active:
        yield not active or not checked_write(store, key, value)


@contextmanager
def deletion(store: Store, key: str) -> Iterator[None]:
    selected = catalogue_reader.point(key)
    alias = catalogue_aliases.point(key)
    if selected is None and alias is None:
        yield
        return
    with scope(store) as active:
        if active and selected is not None:
            if selected.revision is None:
                current = record_catalog.head(store.db, selected.kind, selected.identity, store.cached_bytes)
                if current is not None:
                    store.db.execute(
                        "UPDATE market_records SET current=0 WHERE kind=? AND public_uuid=? AND current=1",
                        (KINDS.index(selected.kind), record_catalog.binary(selected.identity)),
                    )
            else:
                coordinate = Coordinate(selected.kind, selected.identity, selected.revision)
                saved = record_catalog.row(store.db, coordinate)
                if saved is not None:
                    if saved[4]:
                        raise StateError("Remove the current head before its immutable revision")
                    store.db.execute(
                        "DELETE FROM market_records WHERE kind=? AND public_uuid=? AND revision=?",
                        (
                            KINDS.index(selected.kind),
                            record_catalog.binary(selected.identity),
                            selected.revision,
                        ),
                    )
        elif active and alias is not None:
            if alias.episode is None:
                store.db.execute(
                    "UPDATE market_record_parents SET history_published=2 WHERE public_uuid=? AND observed=? AND history_published=1",
                    (record_catalog.binary(alias.event), alias.observed),
                )
            else:
                store.db.execute(
                    "DELETE FROM market_record_members WHERE episode=? AND observed=? AND event_uuid=?",
                    (
                        record_catalog.binary(alias.episode),
                        alias.observed,
                        record_catalog.binary(alias.event),
                    ),
                )
        yield
