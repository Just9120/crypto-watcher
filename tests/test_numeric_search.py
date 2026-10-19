import sqlite3
import time
from contextlib import closing

import pytest

from cryptowatcher import numeric_blocks as blocks
from cryptowatcher import numeric_pool, quality_packets
from cryptowatcher import numeric_search as search
from cryptowatcher.storage import Store, encode
from tests.test_numeric_blocks import owner, setup
from tests.test_quality_packets import fact, metric, save


def population(store):
    setup(store)
    blocks.upgrade_price(store.db)
    values = [fact(9999, [metric(i + 1) for i in range(129)])]
    with store.db:
        raw = save(store, values)
    search.install(store.db)
    return raw, values


def finish(store, state=None, batch=1):
    state = {} if state is None else state
    for _ in range(30):
        with store.db:
            store.db.execute("BEGIN")
            done = search.work(store.db, state, batch, time.monotonic() + 5)
        if done and not search.pending(store.db):
            return state
    pytest.fail("finite numeric search work did not finish")


def assert_accounted(store):
    allocation = [0] * 5
    for (block,) in store.db.execute("SELECT id FROM market_numeric_blocks"):
        frames, details, quality, raw = blocks.read(store.db, block)
        size = search.index_bytes(sum(bool(f) for f in frames), search.fields(store.db, block))
        current = blocks.allocation(block, frames, details, quality, len(raw), index_bytes=size)
        allocation = [a + b for a, b in zip(allocation, current, strict=True)]
    status = blocks.summary(store.db)
    assert tuple(allocation) == tuple(
        status[k] for k in ("live", "details", "quality", "detail_bytes", "quality_bytes")
    )


def test_initial_seal_preserves_frames_leases_public_values_restart_and_finite_geometry(store):
    raw, values = population(store)
    before = [
        tuple(r)
        for r in store.db.execute(
            "SELECT b.id,b.payload,r.counts FROM market_numeric_blocks b JOIN market_numeric_block_refs r ON b.id=r.id ORDER BY b.id"
        )
    ]
    with store.db:
        store.db.execute("BEGIN")
        state = {}
        assert not search.work(store.db, state, 1, time.monotonic() + 5)
    assert state["numeric_search_upper"] == 2 and state["numeric_search_cursor"] == 0
    with closing(Store(store.path, store.owner_id)) as reopened:
        final = finish(reopened, state)
        assert final["numeric_search_upper"] == final["numeric_search_cursor"] == 2
        assert [
            tuple(r)
            for r in reopened.db.execute(
                "SELECT id FROM market_numeric_block_refs WHERE search_seal IS NOT NULL ORDER BY id"
            )
        ] == [(0,), (1,)]
        assert reopened.db.execute("SELECT COUNT(*) FROM market_numeric_block_lookup").fetchone()[0] == 1
        assert [
            tuple(r)
            for r in reopened.db.execute(
                "SELECT b.id,b.payload,r.counts FROM market_numeric_blocks b JOIN market_numeric_block_refs r ON b.id=r.id ORDER BY b.id"
            )
        ] == before
        assert encode(quality_packets.unpack(reopened.db, raw)) == encode(values)
        assert reopened.db.execute("SELECT cw_quality_json(facts) FROM market_quality").fetchone()[
            0
        ] == encode(values)
        assert_accounted(reopened)
        assert finish(reopened, final) == final
        assert reopened.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_detail_renewal_atomically_restores_whole_index_and_reseals_after_final_owner(store):
    raw, values = population(store)
    state = finish(store)
    before = list(store.db.iterdump())
    with pytest.raises(RuntimeError), store.db:
        owner(store, 33, {"mode": "numeric2", "id": 4})
        assert not search.sealed(store.db, 0)
        raise RuntimeError("rollback renewed owner and whole search index")
    assert list(store.db.iterdump()) == before
    with store.db:
        key = owner(store, 33, {"mode": "numeric2", "id": 4})
    assert not search.sealed(store.db, 0)
    assert blocks.find(store.db, numeric_pool.pack(metric(1)))[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM market_numeric_block_lookup").fetchone()[0] == 65
    assert_accounted(store)
    with store.db:
        store.db.execute("DELETE FROM runtime WHERE key=?", (key,))
    assert search.pending(store.db)
    assert finish(store, state) == state
    assert search.sealed(store.db, 0)
    assert encode(quality_packets.unpack(store.db, raw)) == encode(values)
    assert_accounted(store)


def test_sealed_values_gc_in_small_batches_does_not_reissue_ids_or_lose_counters(store):
    population(store)
    finish(store)
    with store.db:
        store.db.execute("DELETE FROM market_quality")
    for _ in range(30):
        with store.db:
            removed = numeric_pool.collect(store.db, 7)
        assert_accounted(store)
        if not removed:
            break
    assert blocks.summary(store.db)["live"] == 0
    assert blocks.summary(store.db)["issued"] == 129
    with store.db:
        ref = numeric_pool.encode_reference(store.db, metric(999))
    assert numeric_pool.reference_id(ref) == 130


@pytest.mark.parametrize("damage", ["missing", "corrupt", "detail-count", "unexpected-lookup"])
def test_positive_seal_corruption_is_rejected_by_value_and_lease_readers(store, damage):
    population(store)
    finish(store)
    with store.db:
        if damage == "missing":
            store.db.execute("UPDATE market_numeric_block_refs SET search_seal=NULL WHERE id=0")
        elif damage == "corrupt":
            store.db.execute("UPDATE market_numeric_block_refs SET search_seal=zeroblob(32) WHERE id=0")
        elif damage == "detail-count":
            store.db.execute("UPDATE market_numeric_block_refs SET detail_slots=1 WHERE id=0")
        else:
            frame = numeric_pool.pack(metric(1))
            store.db.execute(
                "INSERT INTO market_numeric_block_lookup VALUES(?,1)", (numeric_pool.fingerprint(frame),)
            )
    before = list(store.db.iterdump())
    with pytest.raises(ValueError):
        numeric_pool.load(store.db, 1)
    with pytest.raises(ValueError):
        numeric_pool.raw_many(store.db, {1})
    with pytest.raises(sqlite3.OperationalError), store.db:
        owner(store, 77, {"mode": "numeric2", "id": 4})
    assert list(store.db.iterdump()) == before


def test_new_duplicate_uses_new_id_and_old_quality_prefix_does_not_reintern_sealed_values(store):
    raw, values = population(store)
    finish(store)
    assert blocks.find(store.db, numeric_pool.pack(metric(1))) is None
    with store.db:
        new = numeric_pool.encode_reference(store.db, metric(1))
        assert numeric_pool.reference_id(new) == 130
        # This fact's comparators are current admitted values. Prefix IDs must
        # remain retained even though their search index has been retired.
        added = fact(500, [metric(1)])
        modern = quality_packets.append(store.db, raw, added, expected_count=1)
    assert encode(quality_packets.unpack(store.db, modern)) == encode([*values, added])
    assert set(range(1, 130)) <= set(quality_packets.ownership(store.db, modern)["numeric"])
    assert numeric_pool.load(store.db, 1) == numeric_pool.resolve(store.db, new)


@pytest.mark.parametrize("index_damage", ["missing", "wrong-definition"])
def test_reopen_rejects_missing_or_changed_search_index_without_any_writes(store, index_damage):
    population(store)
    finish(store)
    store.db.execute("DROP INDEX market_numeric_search_due")
    if index_damage == "wrong-definition":
        store.db.execute("CREATE INDEX market_numeric_search_due ON market_numeric_block_refs(id)")
    before = list(store.db.iterdump())
    with pytest.raises(ValueError, match="search index"):
        Store(store.path, store.owner_id)
    assert list(store.db.iterdump()) == before


@pytest.mark.parametrize(
    "bad",
    [
        {"numeric_search_upper": True},
        {"numeric_search_upper": 0, "numeric_search_cursor": 1},
        {"numeric_search_complete": True},
        {"numeric_search_complete": 1},
        {"numeric_search_cursor": 0},
        {"numeric_search_upper": 0},
    ],
)
def test_invalid_numeric_search_checkpoint_is_rejected_before_mutation(store, bad):
    population(store)
    before = list(store.db.iterdump())
    with pytest.raises(ValueError, match="(?i)numeric search"), store.db:
        store.db.execute("BEGIN")
        search.work(store.db, bad, 1, time.monotonic() + 5)
    assert list(store.db.iterdump()) == before


def test_current_repack_cannot_reinstall_missing_search_marker(store):
    from cryptowatcher.storage_repack import VERSION, work
    from tests.test_bounded_storage import bounded
    from tests.test_catalogue_repack import complete
    from tests.test_outcomes import NOW

    bounded(store)
    complete(store)
    assert store.get_runtime("storage_repack")["version"] == VERSION
    with store.db:
        store.db.execute("UPDATE market_meta SET value='2' WHERE key='numeric_blocks'")
    before = list(store.db.iterdump())
    with pytest.raises(ValueError, match="Missing numeric search checkpoint"):
        work(store, NOW)
    assert list(store.db.iterdump()) == before
