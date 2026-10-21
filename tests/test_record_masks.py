import hashlib
import sqlite3
import time
import uuid
from contextlib import closing

import apsw
import pytest

from cryptowatcher import catalogue_reader as reader
from cryptowatcher import compressed_validation as cv
from cryptowatcher import owner_slots, packet_store, record_masks
from cryptowatcher import record_catalog as catalog
from cryptowatcher.disk_validation import validated_disk
from cryptowatcher.market_storage import enable
from cryptowatcher.record_blocks import Coordinate
from cryptowatcher.storage import IntegrityError, StateError, Store, check_integrity, encode
from tests.test_record_blocks import entries
from tests.test_record_catalog import DAY, prepare, put


def packet(store):
    first = str(uuid.UUID(int=1000))
    second = str(uuid.UUID(int=2000))
    owners = {
        first: catalog.parent(store.db, first, 100 * DAY, 130 * DAY),
        second: catalog.parent(store.db, second, 100 * DAY, 130 * DAY),
    }
    values = []
    parent_ids = {}
    for slot in range(64):
        coordinate = Coordinate("outcome", str(uuid.UUID(int=slot + 1)), 1)
        event = first if slot % 2 == 0 else second
        value = {"outcome_id": coordinate.identity, "revision": 1, "event_id": event, "unknown": -0.0}
        values.append((coordinate, value, None))
        parent_ids[coordinate] = owners[event]
    assert (
        catalog.append(
            store.db,
            values,
            bucket=130,
            owners=parent_ids,
            deadlines=dict.fromkeys(parent_ids),
        )
        == 64
    )
    return values, owners


def test_mask_last_slot_multi_owner_exact_retry_deletion_and_transaction_abort(store):
    prepare(store)
    with store.db:
        values, owners = packet(store)
    assert catalog.layout(store.db) == 3
    masks = store.db.execute(
        "SELECT parent,packet,slots,checksum FROM market_record_owners ORDER BY parent"
    ).fetchall()
    assert len(masks) == 2
    assert bytes(masks[0][2]).hex() == "5555555555555555"
    assert bytes(masks[1][2]).hex() == "aaaaaaaaaaaaaaaa"
    for parent, pid, slots, checksum in masks:
        assert owner_slots.signature(parent, pid, slots) == checksum
    assert record_masks.tallies(store.db, "record_owners")["owners"] == 2
    for coordinate, value, _ in values:
        assert encode(catalog.read(store.db, coordinate)) == encode(value)
        assert catalog.row(store.db, coordinate)[2] == owners[value["event_id"]]
    before = list(store.db.iterdump())
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        assert (
            catalog.append(
                store.db,
                values,
                bucket=130,
                owners={c: owners[v["event_id"]] for c, v, _ in values},
                deadlines={c: None for c, _, _ in values},
            )
            == 0
        )
    assert list(store.db.iterdump()) == before
    with pytest.raises(RuntimeError), store.db:
        store.db.execute(
            "DELETE FROM market_records WHERE public_uuid=?", (catalog.binary(values[-1][0].identity),)
        )
        raise RuntimeError("abort partial slot release")
    assert list(store.db.iterdump()) == before
    with store.db:
        store.db.execute(
            "DELETE FROM market_records WHERE public_uuid=?", (catalog.binary(values[-1][0].identity),)
        )
    assert store.db.execute("SELECT slots FROM market_record_owners ORDER BY parent DESC LIMIT 1").fetchone()[
        0
    ] == bytes.fromhex("2aaaaaaaaaaaaaaa")
    with store.db:
        store.db.execute("DELETE FROM market_records")
        assert record_masks.tallies(store.db, "record_owners")["owners"] == 0
        assert packet_store.collect(store.db) == 2
        store.db.execute("DELETE FROM market_record_parents")
    assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("damage", ["missing-counter", "counter-mismatch", "checksum", "missing-owner"])
def test_inconsistent_owner_does_not_allow_read_or_destructive_release(store, damage):
    prepare(store)
    with store.db:
        coordinate, value, _, _, _ = put(store)
    with store.db:
        if damage == "missing-counter":
            store.db.execute("DELETE FROM market_counts WHERE category='record_owners'")
        elif damage == "counter-mismatch":
            store.db.execute("UPDATE market_catalogue_index_counts SET n=n+1 WHERE name='owners'")
        else:
            store.db.execute("DROP TRIGGER market_record_owner_frozen")
            store.db.execute("PRAGMA ignore_check_constraints=ON")
            if damage == "checksum":
                store.db.execute("UPDATE market_record_owners SET checksum=zeroblob(32)")
            else:
                store.db.execute(
                    "UPDATE market_record_owners SET slots=zeroblob(8),checksum=cw_owner_digest(parent,packet,zeroblob(8))"
                )
            store.db.execute("PRAGMA ignore_check_constraints=OFF")
    before = list(store.db.iterdump())
    with pytest.raises(ValueError):
        catalog.read(store.db, coordinate)
    with pytest.raises(sqlite3.IntegrityError, match="owner"), store.db:
        store.db.execute("DELETE FROM market_records")
    assert list(store.db.iterdump()) == before
    assert value["event_id"] == coordinate.identity


def test_masks_cannot_change_or_release_retained_slots_and_membership_is_counted(store):
    prepare(store)
    with store.db:
        coordinate, _, _, parent, _ = put(store)
    for sql in (
        "DELETE FROM market_record_owners",
        "UPDATE market_record_owners SET slots=zeroblob(8),checksum=cw_owner_digest(parent,packet,zeroblob(8))",
    ):
        before = list(store.db.iterdump())
        with pytest.raises(sqlite3.IntegrityError), store.db:
            store.db.execute(sql)
        assert list(store.db.iterdump()) == before
    episode = str(uuid.UUID(int=99))
    with pytest.raises(sqlite3.IntegrityError), store.db:
        store.db.execute(
            "INSERT INTO market_record_parents(public_uuid,observed,expires,history_published) VALUES(?,?,?,1)",
            (uuid.UUID(int=98).bytes, 100 * DAY, 130 * DAY),
        )
    with store.db:
        catalog.member(store.db, episode, 100 * DAY, parent)
        catalog.member(store.db, episode, 100 * DAY, parent)
    assert record_masks.tallies(store.db, "record_aliases")["membership"] == 1
    assert store.db.execute("SELECT event_uuid FROM market_record_members").fetchone()[0] == catalog.binary(
        coordinate.identity
    )
    with pytest.raises(ValueError), store.db:
        catalog.member(store.db, episode, 101 * DAY, parent)
    with store.db:
        store.db.execute("UPDATE market_record_parents SET history_published=1 WHERE id=?", (parent,))
    assert record_masks.tallies(store.db, "record_aliases") == {
        "parents": 1,
        "owners": 1,
        "history": 1,
        "membership": 1,
    }
    with pytest.raises(sqlite3.IntegrityError), store.db:
        store.db.execute("UPDATE market_record_parents SET history_published=0")
    with store.db:
        store.db.execute("DELETE FROM market_records")
        store.db.execute("DELETE FROM market_record_members")
        store.db.execute("DELETE FROM market_record_parents")
    assert record_masks.tallies(store.db, "record_aliases") == {
        "parents": 0,
        "owners": 0,
        "history": 0,
        "membership": 0,
    }


def test_unique_heads_guards_and_corruption_rejected_by_all_full_checks(store):
    prepare(store)
    with store.db:
        coordinate, _, _, _, _ = put(store)
        put(store, 2)
        store.db.execute("INSERT INTO market_meta VALUES('record_catalogue_runtime','1')")
    before = list(store.db.iterdump())
    with pytest.raises(sqlite3.IntegrityError, match="current"), store.db:
        store.db.execute("UPDATE market_records SET current=1")
    assert list(store.db.iterdump()) == before
    with store.db:
        store.db.execute("DROP TRIGGER market_record_head_changed")
        store.db.execute("UPDATE market_records SET current=1")
    with pytest.raises(ValueError, match="current"):
        catalog.head(store.db, "event", coordinate.identity)
    with pytest.raises(StateError, match="current"):
        list(reader.scan(store.db, reader.PREFIX + "event-head:", limit=1))
    assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not catalog.valid_heads(store.db)
    assert store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    before_bytes = hashlib.sha256(store.path.read_bytes()).digest()
    with closing(sqlite3.connect(store.path)) as db:
        with pytest.raises(IntegrityError) as failure:
            check_integrity(db, deadline=time.monotonic() + 5)
        assert failure.value.code == "DATABASE_CORRUPT"
    with closing(apsw.Connection(str(store.path))) as db:
        with pytest.raises(cv.QualificationError, match="DATABASE_CORRUPT"):
            cv.full_check(db, time.monotonic() + 5)
    with pytest.raises(IntegrityError) as failure, validated_disk(store.path, deadline=time.monotonic() + 5):
        pytest.fail("Duplicate current revisions passed full validation")
    assert failure.value.code == "DATABASE_CORRUPT"
    assert hashlib.sha256(store.path.read_bytes()).digest() == before_bytes


def test_readonly_reopen_registers_functions_without_ddl_or_runtime_activation(store):
    prepare(store)
    with store.db:
        coordinate, value, _, _, _ = put(store)
    with closing(Store(store.path, store.owner_id, readonly=True)) as reopened:
        before = reopened.db.execute("PRAGMA schema_version").fetchone()[0]
        assert encode(catalog.read(reopened.db, coordinate)) == encode(value)
        assert not reader.reading(reopened.db)
        assert reopened.db.execute("PRAGMA schema_version").fetchone()[0] == before
        with pytest.raises(sqlite3.OperationalError):
            reopened.db.execute("DELETE FROM market_records")


def test_unknown_catalogue_refuses_before_any_new_tables_or_counters(store):
    enable(store.db)
    with store.db:
        store.db.execute("INSERT INTO market_meta VALUES('record_catalog','future')")
    before = list(store.db.iterdump())
    with pytest.raises(ValueError, match="Unknown"):
        catalog.install(store.db)
    assert list(store.db.iterdump()) == before


def test_catalogue_meter_keeps_parent_alias_and_owner_bytes_and_invalidates_old_dbstat(store):
    from cryptowatcher.catalogue_overlap import NAMES
    from cryptowatcher.storage_meter import report

    prepare(store)
    store.bounded = True
    with store.db:
        put(store)
        store.put_runtime(
            "storage_index_measurement",
            {
                "state": "PASS",
                "tables": {
                    name: 4096
                    for name in (
                        "market_expiry",
                        "market_quality_blocks",
                        "market_source_windows",
                        "market_numeric",
                        "market_namespaces",
                    )
                },
            },
        )
    value = report(store, 101 * DAY)
    assert value["catalogue_layout"] == {
        "state": "INACTIVE_DOMAIN_CATALOGUE",
        "version": 3,
        "overlaps": dict.fromkeys(NAMES, 0),
        "counts": {
            "parents": 1,
            "owners": 1,
            "history": 0,
            "membership": 0,
        },
    }
    assert value["categories"]["record_parents"]["logical_bytes"] == 41
    assert value["categories"]["record_owners"]["logical_bytes"] == 56
    assert value["indexes"]["state"] == "STALE"


def test_previous_catalogue_parent_column_and_unique_head_remain_readable(store):
    from cryptowatcher import record_blocks

    enable(store.db)
    packet_store.install(store.db)
    store.db.executescript("""
        CREATE TABLE market_record_parents(id INTEGER PRIMARY KEY,public_uuid BLOB UNIQUE,observed INTEGER,expires INTEGER);
        CREATE TABLE market_records(kind INTEGER,public_uuid BLOB,revision INTEGER,parent INTEGER,packet INTEGER,
            slot INTEGER,current INTEGER,expires INTEGER,PRIMARY KEY(kind,public_uuid,revision)) WITHOUT ROWID;
        CREATE UNIQUE INDEX market_record_heads ON market_records(kind,public_uuid) WHERE current=1;
        CREATE TABLE market_record_members(episode BLOB,observed INTEGER,parent INTEGER,
            PRIMARY KEY(episode,observed,parent)) WITHOUT ROWID;
        INSERT INTO market_meta VALUES('record_catalog','2');
    """)
    coordinate, value, projection = entries()[0]
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        pid = packet_store.append(
            store.db, "detail_packets", 130, record_blocks.pack([(coordinate, value, projection)])
        )
        parent = catalog.parent(store.db, coordinate.identity, 100 * DAY, 130 * DAY)
        store.db.execute(
            "INSERT INTO market_records VALUES(0,?,?,?,?,?,1,NULL)",
            (catalog.binary(coordinate.identity), coordinate.revision, parent, pid, 0),
        )
        catalog.member(store.db, str(uuid.UUID(int=99)), 100 * DAY, parent)
    assert catalog.layout(store.db) == 2
    assert catalog.row(store.db, coordinate)[2] == parent
    assert encode(catalog.read(store.db, coordinate)) == encode(value)
    assert catalog.head(store.db, "event", coordinate.identity) == (coordinate, projection)
    assert catalog.valid_heads(store.db)
    assert store.db.execute("SELECT parent FROM market_record_members").fetchone()[0] == parent
    with closing(Store(store.path, store.owner_id, readonly=True)) as reopened:
        assert encode(catalog.read(reopened.db, coordinate)) == encode(value)
