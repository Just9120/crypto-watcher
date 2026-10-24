"""Atomic hot writes against retained cold records; activation is explicit."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from . import catalogue_aliases, catalogue_overlap, catalogue_reader, packet_store, record_catalog
from .expiry_index import hot_binding
from .indexed_frame import FrameSizeError
from .record_blocks import KINDS, Coordinate
from .storage import StateError, Store, encode, json_payload

if TYPE_CHECKING:
    from .events import History


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


def publish_revision(
    history: History,
    kind: str,
    value: dict[str, Any],
    packed: Any,
    head: dict[str, Any] | None,
) -> bool:
    """Keep supported new results in the final catalogue, inside their writer tx.

    The caller has checked immutable/head identity and normalized all source
    leases. Unsupported records retain the hot path; no format is installed.
    """
    store = history.store
    if (
        kind not in ("outcome", "comparison")
        or value.get("schema") != f"cryptowatcher.{kind}.v1"
        or not store.bounded
    ):
        return False
    identity = value.get(f"{kind}_id")
    revision = value.get("revision")
    event = value.get("event_id")
    if not isinstance(identity, str) or not isinstance(event, str) or type(revision) is not int:
        return False
    coordinate = Coordinate(kind, identity, revision)
    try:
        coordinate.validate()
        Coordinate("event", event, 1).validate()
    except ValueError:
        return False  # Older non-canonical namespaces keep their existing path.
    if not catalogue_reader.reading(store.db):
        return False
    if not store.db.in_transaction:
        raise StateError("Final result publication requires the writer transaction")
    parent = store.db.execute(
        "SELECT id,expires FROM market_record_parents WHERE public_uuid=?",
        (record_catalog.binary(event),),
    ).fetchone()
    lease = hot_binding(store.db, f"radar:v1:event-head:{event}")
    observed = 0
    if parent is None:
        if lease is None or lease[1:] != ("event-head", event):
            return False
        root = history.metadata("event-head:" + event)
        if (
            not isinstance(root, dict)
            or root.get("event_id") != event
            or type(root.get("market_observation_ms")) is not int
        ):
            return False
        observed, expiry = root["market_observation_ms"], lease[0]
    else:
        if type(parent[0]) is not int or parent[0] < 1 or type(parent[1]) is not int:
            raise StateError("Invalid frozen result retention anchor")
        expiry = parent[1]
        if lease is not None and lease[0] != expiry:
            raise StateError("Conflicting frozen parent expiry")
    # The original hot route crosses the canonical JSON wire before transfer.
    # Incomplete calculations contain tuple ranges; the typed final codec must
    # receive the same JSON arrays/types rather than those Python containers.
    packed = json_payload(encode(packed))
    projected = json_payload(encode(history._head(kind, value, coordinate.key, bounded=True)))
    current = head is None or value["revision"] > head["revision"]
    try:
        with packet_store.atomic(store.db):
            # The same prepared-parent state is already used by transfer.
            # Do not publish a history alias or move the retained event here.
            owner = (
                parent[0] if parent is not None else record_catalog.parent(store.db, event, observed, expiry)
            )
            if current:
                history._delete_raw(f"{kind}-head:{coordinate.identity}")
            record_catalog.append(
                store.db,
                [(coordinate, packed, projected)],
                bucket=expiry // 86400000,
                owners={coordinate: owner},
                deadlines={coordinate: None},
                current=[coordinate] if current else [],
            )
    except FrameSizeError:
        # The savepoint restores the old hot/cold head before the fallback.
        return False
    return True


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
