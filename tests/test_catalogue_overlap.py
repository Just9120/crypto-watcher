import hashlib
import sqlite3
import time
import uuid
from contextlib import closing

import apsw
import pytest

from cryptowatcher import catalogue_aliases as aliases
from cryptowatcher import catalogue_overlap as overlap
from cryptowatcher import compressed_validation as cv
from cryptowatcher import record_catalog as catalog
from cryptowatcher.disk_validation import validated_disk
from cryptowatcher.events import PREFIX, History
from cryptowatcher.market_storage import enable
from cryptowatcher.record_blocks import Coordinate
from cryptowatcher.storage import IntegrityError, Store, check_integrity, encode
from tests.test_catalogue_store import mixed
from tests.test_record_catalog import DAY


def test_counter_checksum_wire_and_key_helpers_reject_unbounded_integer_allocations():
    assert (
        overlap.signature("event", 0).hex()
        == "71672a39874a9bddd8244501d253ecd8b8b95ba4a1e1b372448502db9259828c"
    )
    assert overlap.signature("event", 0) != overlap.signature("event-head", 0)
    assert overlap.signature("event", 0) != overlap.signature("event", 1)
    for value in (True, -1, 2**63, 0.0, None, "0"):
        assert overlap.sql_signature("event", value) == b""
    for name in (None, "future", "event" * 8192, 1):
        assert overlap.sql_signature(name, 0) == b""
    assert overlap.sql_signature("event") == b""
    for invalid in (2**63 - 1, -1, True, None, bytes(15), bytes(17), "not-a-blob"):
        with pytest.raises(ValueError):
            overlap.head_key(0, invalid)
        with pytest.raises(ValueError):
            overlap.binding_key(None, 0, invalid)


@pytest.mark.parametrize("damage", [None, "checksum", "count", "name", "type"])
def test_overlap_checksum_in_ordinary_sqlite_apsw_and_disk_full_checks_preserves_source(store, damage):
    mixed(store)
    with store.db:
        if damage is not None:
            store.db.execute("PRAGMA ignore_check_constraints=ON")
            if damage == "checksum":
                store.db.execute(
                    "UPDATE market_catalogue_overlap SET checksum=zeroblob(32) WHERE name='event'"
                )
            elif damage == "count":
                store.db.execute("UPDATE market_catalogue_overlap SET n=n+1 WHERE name='event'")
            elif damage == "name":
                store.db.execute("UPDATE market_catalogue_overlap SET name='future' WHERE name='event'")
            else:
                store.db.execute("UPDATE market_catalogue_overlap SET n='invalid' WHERE name='event'")
    assert store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    before = hashlib.sha256(store.path.read_bytes()).digest()
    with closing(sqlite3.connect(store.path)) as db:
        if damage is None:
            check_integrity(db, deadline=time.monotonic() + 5)
        else:
            with pytest.raises(IntegrityError) as failure:
                check_integrity(db, deadline=time.monotonic() + 5)
            assert failure.value.code == "DATABASE_CORRUPT"
    with closing(apsw.Connection(str(store.path))) as db:
        if damage is None:
            cv.full_check(db, time.monotonic() + 5)
        else:
            with pytest.raises(cv.QualificationError, match="DATABASE_CORRUPT"):
                cv.full_check(db, time.monotonic() + 5)
    if damage is None:
        with validated_disk(store.path, deadline=time.monotonic() + 5):
            pass
    else:
        with (
            pytest.raises(IntegrityError) as failure,
            validated_disk(store.path, deadline=time.monotonic() + 5),
        ):
            pytest.fail("Corrupt overlap counter passed full CHECK")
        assert failure.value.code == "DATABASE_CORRUPT"
    assert hashlib.sha256(store.path.read_bytes()).digest() == before


def test_overlap_birth_and_both_publication_orders_keep_counts_and_outer_rollback(store):
    enable(store.db)
    store.bounded = True
    catalog.install(store.db, store.cached_text, store.cached_bytes)
    event = str(uuid.UUID(int=10))
    episode = str(uuid.UUID(int=101))
    coordinate = Coordinate("event", event, 1)
    body = {"event_id": event, "revision": 1, "market_observation_ms": 100 * DAY}
    head = History._head("event", body, coordinate.key, bounded=True)
    public = {
        PREFIX + coordinate.key: body,
        PREFIX + "event-head:" + event: head,
        aliases.Alias(event, 100 * DAY).key: {"event_id": event},
        aliases.Alias(event, 100 * DAY, episode).key: {"event_id": event},
    }
    for key, value in public.items():
        store.put_runtime(key, value)
    assert sum(overlap.header(store.db).values()) == 0

    def transfer():
        store.db.execute("BEGIN IMMEDIATE")
        parent = catalog.parent(store.db, event, 100 * DAY, 130 * DAY)
        catalog.append(
            store.db,
            [(coordinate, body, head)],
            bucket=130,
            owners={coordinate: parent},
            deadlines={coordinate: None},
            current=[coordinate],
        )
        for key, value in public.items():
            if aliases.point(key):
                assert aliases.publish(store.db, key, value)

    before = list(store.db.iterdump())
    with pytest.raises(RuntimeError), store.db:
        transfer()
        assert sum(overlap.header(store.db).values()) == 4
        raise RuntimeError("rollback before deleting old owners")
    assert list(store.db.iterdump()) == before
    with store.db:
        transfer()
        store.db.execute("INSERT INTO market_meta VALUES('record_catalogue_runtime','1')")
    assert sum(overlap.header(store.db).values()) == 4
    assert store.count_runtime(PREFIX) == len(public)
    with store.db:
        store.db.execute("UPDATE market_records SET current=0")
    assert overlap.header(store.db)["event-head"] == 0
    assert store.count_runtime(PREFIX) == len(public)
    with store.db:
        store.db.execute("UPDATE market_records SET current=1")
    assert overlap.header(store.db)["event-head"] == 1
    for key in public:
        with store.db:
            store.db.execute("DELETE FROM runtime WHERE key=?", (key,))
        assert encode(store.get_runtime(key)) == encode(public[key])
        assert store.count_runtime(PREFIX) == len(public)
    assert sum(overlap.header(store.db).values()) == 0
    for key, value in public.items():
        store.put_runtime(key, value)
    assert sum(overlap.header(store.db).values()) == 4
    with store.db:
        store.db.execute("DELETE FROM market_records")
        store.db.execute("DELETE FROM market_record_members")
        store.db.execute("DELETE FROM market_record_parents")
    assert sum(overlap.header(store.db).values()) == 0
    assert store.count_runtime(PREFIX) == len(public)


def test_full_and_partial_mixed_counts_need_no_body_decode_and_match_every_public_prefix(store, monkeypatch):
    expected, _ = mixed(store)
    prefixes = {"", "radar:", PREFIX, "absent"}
    for key in expected:
        prefixes.update(key[:length] for length in (15, 25, 40, 47, len(key) - 1, len(key)))

    def forbidden(*args, **kwargs):
        pytest.fail("Count decoded a record/frame")

    monkeypatch.setattr("cryptowatcher.record_blocks.read", forbidden)
    monkeypatch.setattr("cryptowatcher.record_blocks.projected", forbidden)
    for prefix in prefixes:
        assert store.count_runtime(prefix) == sum(key.startswith(prefix) for key in expected)
    assert overlap.header(store.db) == {
        **dict.fromkeys(overlap.NAMES, 0),
        "event-head": 1,
        "outcome": 1,
        "outcome-head": 1,
        "history": 1,
    }


@pytest.mark.parametrize("damage", ["missing-row", "missing-bytes", "unknown-marker", "underflow"])
def test_counter_damage_blocks_count_and_destructive_release_atomically(store, damage):
    expected, values = mixed(store)
    coordinate, _, _ = values[1]
    with store.db:
        if damage == "missing-row":
            store.db.execute("DELETE FROM market_catalogue_overlap WHERE name='outcome'")
        elif damage == "missing-bytes":
            store.db.execute("DELETE FROM market_counts WHERE category='catalogue_overlap'")
        elif damage == "unknown-marker":
            store.db.execute("UPDATE market_meta SET value='future' WHERE key='catalogue_overlap'")
        else:
            store.db.execute(
                "UPDATE market_catalogue_overlap SET n=0,checksum=cw_catalogue_shadow_digest(name,0) WHERE name='outcome'"
            )
    before = list(store.db.iterdump())
    if damage != "underflow":
        with pytest.raises(ValueError, match="overlap"):
            store.count_runtime(PREFIX)
    for sql, parameters in (
        ("DELETE FROM runtime WHERE key=?", (PREFIX + coordinate.key,)),
        ("DELETE FROM market_records WHERE kind=1 AND public_uuid=?", (catalog.binary(coordinate.identity),)),
    ):
        with pytest.raises(sqlite3.DatabaseError), store.db:
            store.db.execute(sql, parameters)
        assert list(store.db.iterdump()) == before
    assert encode(store.get_runtime(PREFIX + coordinate.key)) == encode(expected[PREFIX + coordinate.key])


def test_reader_opened_before_additive_catalogue_can_count_and_renamed_hot_key_is_tracked(store):
    enable(store.db)
    store.bounded = True
    with closing(Store(store.path, store.owner_id, readonly=True)) as earlier:
        catalog.install(store.db, store.cached_text, store.cached_bytes)
        event = str(uuid.UUID(int=99))
        coordinate = Coordinate("event", event, 1)
        body = {"event_id": event, "revision": 1}
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            catalog.append(
                store.db,
                [(coordinate, body, None)],
                bucket=130,
                owners={coordinate: None},
                deadlines={coordinate: 130 * DAY},
            )
            store.db.execute("INSERT INTO market_meta VALUES('record_catalogue_runtime','1')")
        assert earlier.count_runtime(PREFIX + "event:") == 1
        assert earlier.count_runtime(PREFIX + "event:" + event[:8]) == 1
        store.put_runtime(PREFIX + coordinate.key, body)
        assert earlier.count_runtime(PREFIX + "event:" + event[:8]) == 1
        with store.db:
            store.db.execute(
                "UPDATE runtime SET key=? WHERE key=?", (PREFIX + "saved-value", PREFIX + coordinate.key)
            )
        assert overlap.header(store.db)["event"] == 0
        assert earlier.count_runtime(PREFIX) == 2
        assert earlier.count_runtime(PREFIX + "event:") == 1
