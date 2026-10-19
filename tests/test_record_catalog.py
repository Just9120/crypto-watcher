import sqlite3
import uuid
from contextlib import closing

import pytest

from cryptowatcher import packet_store
from cryptowatcher import record_catalog as catalog
from cryptowatcher.market_storage import enable
from cryptowatcher.record_blocks import Coordinate
from cryptowatcher.storage import Store, encode
from tests.test_record_blocks import entries

DAY = 86400000


def prepare(store):
    enable(store.db)
    catalog.install(store.db)


def qualified(store, revision=1):
    original = entries()[0]
    coordinate = Coordinate("event", original[0].identity, revision)
    value = {**original[1], "revision": revision}
    head = {**original[2], "revision": revision, "revision_key": coordinate.key}
    owner = catalog.parent(store.db, coordinate.identity, 100 * DAY, 130 * DAY)
    return coordinate, value, head, owner


def put(store, revision=1):
    if not store.db.in_transaction:
        store.db.execute("BEGIN IMMEDIATE")
    coordinate, value, head, owner = qualified(store, revision)
    added = catalog.append(
        store.db,
        [(coordinate, value, head)],
        bucket=130,
        owners={coordinate: owner},
        deadlines={coordinate: None},
        current=[coordinate],
    )
    return coordinate, value, head, owner, added


def test_catalogue_exact_read_retry_head_revision_snapshot_and_reopen(store):
    prepare(store)
    with store.db:
        first, value, projection, _, added = put(store)
    assert added == 1
    with store.db:
        assert put(store)[-1] == 0
    assert encode(catalog.read(store.db, first)) == encode(value)
    assert catalog.head(store.db, first.kind, first.identity) == (first, projection)
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        reader.db.execute("BEGIN")
        assert catalog.head(reader.db, first.kind, first.identity)[0] == first
        with store.db:
            second, second_value, second_head, _, added = put(store, 2)
        assert added == 1
        assert catalog.head(reader.db, first.kind, first.identity)[0] == first
        reader.db.rollback()
        assert catalog.head(reader.db, second.kind, second.identity) == (second, second_head)
        assert encode(catalog.read(reader.db, second)) == encode(second_value)
        assert encode(catalog.read(reader.db, first)) == encode(value)
    with closing(Store(store.path, store.owner_id)) as writer:
        with writer.db:
            assert put(writer, 3)[-1] == 1
    plan = store.db.execute(
        "EXPLAIN QUERY PLAN SELECT packet,slot FROM market_records WHERE kind=? AND public_uuid=? AND revision=?",
        (0, catalog.binary(first.identity), 1),
    ).fetchall()
    assert any("SEARCH" in r[3] and ("INDEX" in r[3] or "PRIMARY KEY" in r[3]) for r in plan)


def test_catalogue_stale_or_conflicting_batch_and_outer_abort_preserve_every_object(store):
    prepare(store)
    with store.db:
        first, value, head, owner, _ = put(store)
        put(store, 3)
    before = list(store.db.iterdump())
    for revision in (1, 2):
        with store.db:
            with pytest.raises(ValueError, match="Stale"):
                put(store, revision)
    assert list(store.db.iterdump()) == before
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        later, later_value, later_head, _ = qualified(store, 2)
        with pytest.raises(ValueError, match="Conflicting current"):
            catalog.append(
                store.db,
                [(first, value, head), (later, later_value, later_head)],
                bucket=130,
                owners={first: owner, later: owner},
                deadlines={first: None, later: None},
                current=[first, later],
            )
    assert list(store.db.iterdump()) == before
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="Conflicting immutable"):
            catalog.append(
                store.db,
                [(first, {**value, "extra": "changed"}, head)],
                bucket=130,
                owners={first: owner},
                deadlines={first: None},
            )
    assert list(store.db.iterdump()) == before
    with pytest.raises(RuntimeError), store.db:
        put(store, 4)
        raise RuntimeError("interrupted before commit")
    assert list(store.db.iterdump()) == before


def test_catalogue_invalid_retention_ownership_and_missing_counter_fail_closed(store):
    prepare(store)
    with store.db:
        coordinate, value, head, owner = qualified(store)
        with pytest.raises(ValueError, match="bucket"):
            catalog.append(
                store.db,
                [(coordinate, value, head)],
                bucket=131,
                owners={coordinate: owner},
                deadlines={coordinate: None},
            )
        with pytest.raises(ValueError, match="parent"):
            catalog.append(
                store.db,
                [(coordinate, {**value, "event_id": str(uuid.uuid4())}, head)],
                bucket=130,
                owners={coordinate: owner},
                deadlines={coordinate: None},
            )
        put(store)
    with pytest.raises(sqlite3.IntegrityError), store.db:
        store.db.execute("DELETE FROM market_record_parents")
    with pytest.raises(sqlite3.IntegrityError), store.db:
        store.db.execute("UPDATE market_records SET slot=slot+1")
    with store.db:
        store.db.execute("DELETE FROM market_counts WHERE category='record_catalog'")
    with pytest.raises(sqlite3.IntegrityError, match="ownership"), store.db:
        store.db.execute("DELETE FROM market_records")
    assert encode(catalog.read(store.db, coordinate)) == encode(value)


def test_catalogue_last_record_and_membership_control_packet_and_parent_collection(store):
    prepare(store)
    with store.db:
        coordinate, _, _, owner, _ = put(store)
        packet = catalog.row(store.db, coordinate)[0]
        catalog.member(store.db, str(uuid.UUID(int=99)), 100 * DAY, owner)
        with pytest.raises(sqlite3.IntegrityError):
            store.db.execute("DELETE FROM market_packets WHERE id=?", (packet,))
        store.db.execute("DELETE FROM market_records")
        assert packet_store.collect(store.db) == 2
        with pytest.raises(sqlite3.IntegrityError):
            store.db.execute("DELETE FROM market_record_parents")
        store.db.execute("DELETE FROM market_record_members")
        store.db.execute("DELETE FROM market_record_parents")
    assert catalog.read(store.db, coordinate) is None
    assert packet_store.summary(store.db)["reserved_payload_bytes"] == 0
