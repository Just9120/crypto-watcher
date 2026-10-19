import sqlite3
import time
import uuid
from contextlib import closing

import pytest

from cryptowatcher import catalogue_compaction, packet_bundles, packet_store, record_relocation
from cryptowatcher.storage import StateError, Store
from tests.test_record_relocation import fragmented, public, references


def settle(store, state):
    for _ in range(500):
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            catalogue_compaction.advance(store, state, 7, time.monotonic() + 0.04)
            packet_store.collect(store.db)
        pending = packet_bundles.verify(store.db)
        if state.get("initial_complete") and not pending["count"] and not pending["sparse"]:
            return
    pytest.fail("Finite bundle work did not settle")


def test_finite_parent_regroup_preserves_all_public_private_references_and_reopen(store):
    records = fragmented(store)
    expected, refs = public(store), references(store)
    owners = store.db.execute("SELECT count(*) FROM market_record_owners").fetchone()[0]
    packet_bundles.install(store.db)
    assert packet_bundles.header(store.db)["unknown"]
    state = {}
    settle(store, state)
    assert state["records_moved"] == len(records)
    assert not packet_bundles.header(store.db)["unknown"]
    assert store.db.execute("SELECT count(*) FROM market_record_owners").fetchone()[0] < owners
    assert public(store) == expected and references(store) == refs
    for old in records:
        fresh = record_relocation.capture(store, old.coordinate)
        assert (fresh.value, fresh.projection, fresh.expires, fresh.binding[2:]) == (
            old.value,
            old.projection,
            old.expires,
            old.binding[2:],
        )
    with closing(Store(store.path, store.owner_id)) as reopened:
        assert public(reopened) == expected and references(reopened) == refs
        before = dict(state)
        settle(reopened, state)
        assert before == state
    assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_partial_packet_reference_loss_repends_once_and_keeps_original_age(store):
    records = fragmented(store)
    packet_bundles.install(store.db)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        packet_bundles.tag(store.db, 2**63 - 1, 256)
        combined = record_relocation.relocate(store, records)
        packet_bundles.checked(store.db, combined)
        packet_store.collect(store.db)
    assert packet_bundles.verify(store.db)["count"] == 0
    store.clock_ms = lambda: 1000
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        first = record_relocation.capture(store, records[0].coordinate)
        record_relocation.relocate(store, [first])
    assert packet_bundles.verify(store.db)["count"] == 2
    assert (
        store.db.execute("SELECT bundle_since FROM market_packets WHERE id=?", (combined,)).fetchone()[0]
        == 1000
    )
    store.clock_ms = lambda: 2000
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        second = record_relocation.capture(store, records[1].coordinate)
        record_relocation.relocate(store, [second])
    assert (
        store.db.execute("SELECT bundle_since FROM market_packets WHERE id=?", (combined,)).fetchone()[0]
        == 1000
    )
    assert packet_bundles.verify(store.db)["oldest_ms"] == 1000
    expected, refs = public(store), references(store)
    settle(store, {})
    assert public(store) == expected and references(store) == refs


def test_late_publication_failure_rolls_back_bundle_metadata_and_progress(store, monkeypatch):
    fragmented(store)
    packet_bundles.install(store.db)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        packet_bundles.tag(store.db, 2**63 - 1, 256)
    before = list(store.db.iterdump())
    original = record_relocation.relocate

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected bundle publication")

    monkeypatch.setattr(record_relocation, "relocate", fail)
    with pytest.raises(RuntimeError, match="bundle publication"):
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            catalogue_compaction.advance(store, {}, 7, time.monotonic() + 1)
    assert list(store.db.iterdump()) == before


@pytest.mark.parametrize("damage", ["counter", "marker", "index", "trigger"])
def test_damaged_layout_is_rejected_without_reinstallation_or_mutation(store, damage):
    fragmented(store)
    packet_bundles.install(store.db)
    with store.db:
        if damage == "counter":
            store.db.execute("DELETE FROM market_counts WHERE category='bundle_pending'")
        elif damage == "marker":
            store.db.execute("UPDATE market_meta SET value='future' WHERE key='packet_bundles'")
        elif damage == "index":
            store.db.execute("DROP INDEX market_bundle_age")
        else:
            store.db.execute("DROP TRIGGER market_bundle_dirty")
    before = list(store.db.iterdump())
    with pytest.raises(StateError):
        packet_bundles.install(store.db)
    assert list(store.db.iterdump()) == before


def test_unknown_checkpoint_and_new_packet_cannot_skip_metadata(store):
    fragmented(store)
    packet_bundles.install(store.db)
    with pytest.raises(StateError):
        catalogue_compaction.validate({"initial_complete": True})
    before = list(store.db.iterdump())
    with pytest.raises(sqlite3.IntegrityError, match="Invalid bundle admission"):
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            row = store.db.execute("SELECT arena,offset,length,sha256 FROM market_packets LIMIT 1").fetchone()
            store.db.execute(
                "INSERT INTO market_packets(arena,offset,length,sha256) VALUES(?,?,?,?)", tuple(row)
            )
    assert list(store.db.iterdump()) == before


def test_initial_ceiling_is_finite_with_new_arrivals_and_vacuum_restart(store):
    from cryptowatcher import record_catalog
    from cryptowatcher.record_blocks import Coordinate
    from tests.test_record_catalog import DAY, prepare, put

    prepare(store)
    with store.db:
        first, value, head, _, _ = put(store)
    packet_bundles.install(store.db)
    upper = store.db.execute("SELECT max(id) FROM market_packets").fetchone()[0]
    state = {"version": 1, "upper": upper}
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        # This other event arrives after the original ceiling and cannot extend it.
        coordinate = Coordinate("event", str(uuid.uuid4()), 1)
        owner = record_catalog.parent(store.db, coordinate.identity, 100 * DAY, 130 * DAY)
        record_catalog.append(
            store.db,
            [
                (
                    coordinate,
                    {**value, "event_id": coordinate.identity},
                    {**head, "event_id": coordinate.identity, "revision_key": coordinate.key},
                )
            ],
            bucket=130,
            owners={coordinate: owner},
            deadlines={coordinate: None},
            current=[coordinate],
        )
        catalogue_compaction.advance(store, state, 1, time.monotonic() + 1)
    assert state["initial_complete"] and state["upper"] == upper
    assert packet_bundles.verify(store.db)["count"] == 1
    expected = record_relocation.capture(store, first)
    store.db.execute("VACUUM")
    with closing(Store(store.path, store.owner_id)) as reopened:
        settle(reopened, state)
        assert state["upper"] == upper
        assert record_relocation.capture(reopened, first).value == expected.value


def test_oversized_parent_stays_readable_and_reports_fallback_without_endless_retry(store):
    from tests.test_record_catalog import prepare, put

    prepare(store)
    with store.db:
        coordinates = [put(store, revision)[0] for revision in range(1, 66)]
    before = [record_relocation.capture(store, coordinate) for coordinate in coordinates]
    packet_bundles.install(store.db)
    state = {}
    settle(store, state)
    assert state["fallbacks"] == 65 and state["records_moved"] == 0
    after = [record_relocation.capture(store, coordinate) for coordinate in coordinates]
    assert before == after
    progress = dict(state)
    settle(store, state)
    assert state == progress


def test_worker_queries_use_partial_indexes_and_no_temporary_sort(store):
    fragmented(store)
    packet_bundles.install(store.db)
    queries = (
        "SELECT id FROM market_packets WHERE bundle_checked IS NULL AND id<=100 ORDER BY id LIMIT 7",
        "SELECT id FROM market_packets WHERE bundle_checked=0 AND refs>0 AND id<=100 ORDER BY id LIMIT 1",
        "SELECT id,bundle_since FROM market_packets WHERE bundle_checked=0 AND refs>0 ORDER BY bundle_since,id LIMIT 1",
    )
    for query in queries:
        plan = " ".join(row[3] for row in store.db.execute("EXPLAIN QUERY PLAN " + query))
        assert "market_bundle_" in plan and "TEMP B-TREE" not in plan


def test_fresh_reader_rejects_missing_bundle_guard(store):
    fragmented(store)
    packet_bundles.install(store.db)
    with store.db:
        store.db.execute("DROP TRIGGER market_bundle_dirty")
    with pytest.raises(StateError, match="bundle guard"):
        Store(store.path, store.owner_id, readonly=True)


def sparse_arena(store):
    from tests.test_record_catalog import prepare

    prepare(store)
    packet_bundles.install(store.db)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        identities = [
            packet_store.append(store.db, "detail_packets", 130, bytes([i]) * 300000) for i in range(3)
        ]
        for identity in identities:
            packet_store.retain(store.db, identity)
            packet_bundles.checked(store.db, identity)
        for identity in identities[:2]:
            packet_store.release(store.db, identity)
        packet_store.collect(store.db)
    return identities[-1]


def test_sparse_arena_evacuation_keeps_packet_id_bytes_ownership_and_old_wal_snapshot(store):
    identity = sparse_arena(store)
    arena = packet_bundles.sparse(store.db)
    assert arena is not None
    raw = packet_store.read(store.db, identity)
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        reader.db.execute("BEGIN")
        assert packet_bundles.sparse(reader.db) == arena
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            assert catalogue_compaction.evacuate(store) == 1
            packet_store.collect(store.db)
        assert packet_store.read(reader.db, identity) == raw
        assert packet_bundles.sparse(reader.db) == arena
        reader.db.rollback()
        assert packet_store.read(reader.db, identity) == raw
        assert packet_bundles.sparse(reader.db) is None
    assert store.db.execute(
        "SELECT refs,bundle_checked FROM market_packets WHERE id=?", (identity,)
    ).fetchone()[:] == (1, 1)
    assert store.db.execute("SELECT id FROM market_packet_arenas WHERE id=?", (arena,)).fetchone() is None
    assert packet_bundles.verify(store.db)["count"] == 0
    assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_sparse_arena_failure_rolls_back_new_blob_bytes_position_and_counters(store, monkeypatch):
    sparse_arena(store)
    before = list(store.db.iterdump())
    original = packet_store.relocate

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected arena relocation")

    monkeypatch.setattr(packet_store, "relocate", fail)
    with pytest.raises(RuntimeError, match="arena relocation"):
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            catalogue_compaction.evacuate(store)
    assert list(store.db.iterdump()) == before
