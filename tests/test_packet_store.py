import sqlite3
from contextlib import closing

import pytest

from cryptowatcher import packet_store as packets
from cryptowatcher.market_storage import enable
from cryptowatcher.storage import Store


def prepare(store):
    enable(store.db)
    packets.install(store.db)


def test_arena_append_is_exact_and_visible_only_after_commit_to_new_snapshot(store):
    prepare(store)
    first = bytes(range(256)) * 17
    second = b"second" * 2000
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        one = packets.append(store.db, "detail_packets", 20000, first)
        packets.retain(store.db, one)
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        reader.db.execute("BEGIN")
        assert packets.read(reader.db, one) == first
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            two = packets.append(store.db, "detail_packets", 20000, second)
            packets.retain(store.db, two)
            with pytest.raises(ValueError, match="Missing retained"):
                packets.read(reader.db, two)
        with pytest.raises(ValueError, match="Missing retained"):
            packets.read(reader.db, two)
        reader.db.rollback()
        assert packets.read(reader.db, two) == second
    assert packets.summary(store.db)["live_bytes"] == len(first) + len(second)
    assert store.db.execute("SELECT COUNT(*) FROM market_packet_arenas").fetchone()[0] == 1


def test_failed_append_savepoint_and_outer_rollback_leave_published_prefix_unchanged(store):
    prepare(store)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        one = packets.append(store.db, "detail_packets", 20000, b"original")
        packets.retain(store.db, one)
    old = packets.summary(store.db)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        store.db.create_function("cw_packet_digest", 3, lambda *args: b"x" * 32)
        with pytest.raises(sqlite3.IntegrityError):
            packets.append(store.db, "detail_packets", 20000, b"refused")
        packets.install_functions(store.db)
        assert packets.summary(store.db) == old
        assert packets.read(store.db, one) == b"original"
    with pytest.raises(RuntimeError), store.db:
        store.db.execute("BEGIN IMMEDIATE")
        packets.append(store.db, "detail_packets", 20000, b"outer rollback")
        raise RuntimeError("interrupted")
    assert packets.summary(store.db) == old
    assert packets.read(store.db, one) == b"original"


def test_relocation_preserves_identity_and_all_counters_and_reclaims_empty_arenas(store):
    prepare(store)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        one = packets.append(store.db, "detail_packets", 20000, b"same bytes")
        packets.retain(store.db, one)
        other = packets.append(store.db, "quality_packets", 20000, b"same bytes")
        packets.retain(store.db, other)
    source = store.db.execute("SELECT arena FROM market_packets WHERE id=?", (one,)).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError), store.db:
        store.db.execute(
            "UPDATE market_packets SET arena=(SELECT arena FROM market_packets WHERE id=?) WHERE id=?",
            (other, one),
        )
    counters = [tuple(r) for r in store.db.execute("SELECT * FROM market_counts ORDER BY category")]
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        packets.relocate(store.db, one)
        assert packets.collect(store.db) == 1
    assert store.db.execute("SELECT 1 FROM market_packet_arenas WHERE id=?", (source,)).fetchone() is None
    assert packets.read(store.db, one) == packets.read(store.db, other) == b"same bytes"
    assert [tuple(r) for r in store.db.execute("SELECT * FROM market_counts ORDER BY category")] == counters
    with store.db:
        packets.release(store.db, one)
        packets.release(store.db, other)
        assert packets.collect(store.db) == 4
    assert packets.summary(store.db)["reserved_payload_bytes"] == 0


def test_ref_guards_missing_counter_fk_and_corrupted_bytes_fail_closed(store):
    prepare(store)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        one = packets.append(store.db, "detail_packets", 20000, b"retained")
        packets.retain(store.db, one)
    with pytest.raises(sqlite3.IntegrityError), store.db:
        store.db.execute("DELETE FROM market_packets WHERE id=?", (one,))
    with store.db:
        packets.release(store.db, one)
        store.db.execute("DELETE FROM market_counts WHERE category='detail_packets'")
    with pytest.raises(sqlite3.IntegrityError), store.db:
        store.db.execute("BEGIN IMMEDIATE")
        packets.collect(store.db)
    assert packets.read(store.db, one) == b"retained"
    with store.db:
        store.db.execute("INSERT INTO market_counts VALUES('detail_packets',1,8,8,0)")
        store.db.execute("CREATE TABLE packet_test_owner(packet INTEGER REFERENCES market_packets(id))")
        store.db.execute("INSERT INTO packet_test_owner VALUES(?)", (one,))
    with pytest.raises(sqlite3.IntegrityError), store.db:
        store.db.execute("BEGIN IMMEDIATE")
        packets.collect(store.db)
    assert packets.read(store.db, one) == b"retained"
    arena, offset = store.db.execute("SELECT arena,offset FROM market_packets WHERE id=?", (one,)).fetchone()
    with store.db:
        with store.db.blobopen("market_packet_arenas", "payload", arena, readonly=False) as blob:
            blob.seek(offset)
            blob.write(b"X")
    with pytest.raises(ValueError, match="checksum"):
        packets.read(store.db, one)


def test_packet_work_is_bounded_and_append_requires_an_outer_transaction(store):
    prepare(store)
    with pytest.raises(ValueError, match="active writer"):
        packets.append(store.db, "detail_packets", 20000, b"not admitted")
    for value in (True, -1, 0, 2**63):
        with pytest.raises(ValueError):
            packets.read(store.db, value)
    for batch in (-1, 0, 1000000, True):
        with pytest.raises(ValueError):
            packets.collect(store.db, batch)
