import random
from contextlib import closing

import pytest

from cryptowatcher import metric_codec, metric_price_codec, numeric_pool, quality_packets
from cryptowatcher import numeric_blocks as blocks
from cryptowatcher.storage import Store, encode
from tests.test_metric_price_codec import outcome
from tests.test_numeric_blocks import owner, setup
from tests.test_quality_packets import fact, save


def population(store):
    rng = random.Random(4502)
    values, candidates, ids = [], [], []
    with store.db:
        for _ in range(64):
            reference = rng.randrange(1000000, 10000000)
            close = reference + rng.randrange(-2000, 2001)
            prices = [
                float(str(n) + "e-2")
                for n in (
                    reference,
                    close,
                    max(reference, close) + rng.randrange(1, 2000),
                    min(reference, close) - rng.randrange(1, 2000),
                )
            ]
            body, _view = metric_codec.canonical(outcome(prices, "UP"))
            values.append(body)
            candidate = metric_price_codec.encode(metric_codec.pack(body), prices)
            assert candidate is not None
            candidates.append(candidate)
            ids.append(numeric_pool.intern(store.db, body))
    assert len(set(ids)) == 64
    return ids, values, candidates


def activate(store):
    blocks.upgrade_price(store.db)
    with store.db:
        store.db.execute("INSERT INTO market_meta VALUES('numeric_price_runtime','1')")


def test_explicit_price_layout_preserves_nb1_and_no_implicit_writer_or_open_activation(store):
    setup(store)
    ids, values, candidates = population(store)
    before = list(store.db.iterdump())
    assert not blocks.offer_price(store.db, ids[0], candidates[0])
    assert list(store.db.iterdump()) == before
    blocks.upgrade_price(store.db)
    assert not blocks.price_writing(store.db)
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        assert blocks.price_layout(reader.db)
        assert not blocks.price_writing(reader.db)
        assert numeric_pool.load(reader.db, ids[0]) == values[0]
    assert blocks.read(store.db, 0)[3][:4] == b"NB1\x00"


def test_whole_block_choice_keeps_exact_fingerprints_ids_leases_views_and_reopen(store):
    setup(store)
    ids, values, candidates = population(store)
    with store.db:
        owners = [owner(store, i, {"mode": "numeric", "id": identity}) for i, identity in enumerate(ids)]
        numeric_pool.adjust(store.db, set(ids[::3]), "quality_refs", 1)
    before = numeric_pool.raw_many(store.db, set(ids))
    fingerprints = [
        tuple(r) for r in store.db.execute("SELECT * FROM market_numeric_block_lookup ORDER BY 1,2")
    ]
    activate(store)
    accepted = 0
    for identity, candidate in zip(ids, candidates, strict=True):
        old = blocks.read(store.db, 0)
        was = list(store.db.iterdump())
        with store.db:
            selected = blocks.offer_price(store.db, identity, candidate)
        if selected:
            accepted += 1
            assert len(blocks.read(store.db, 0)[3]) < len(old[3])
        else:
            assert list(store.db.iterdump()) == was
    assert accepted > 0
    after = numeric_pool.raw_many(store.db, set(ids))
    assert before == after
    assert [
        tuple(r) for r in store.db.execute("SELECT * FROM market_numeric_block_lookup ORDER BY 1,2")
    ] == fingerprints
    assert blocks.read(store.db, 0)[3][:4] == b"NB2\x00"
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        assert [encode(numeric_pool.load(reader.db, i)) for i in ids] == list(map(encode, values))
    with store.db:
        # Legacy-style raw admissions also coexist in the same NB2 writer.
        extra = numeric_pool.intern(store.db, outcome([100.0, 101.0, 102.0, 99.0], "UP"))
        numeric_pool.adjust(store.db, {extra}, "detail_refs", 1)
        for key in owners:
            store.db.execute("DELETE FROM runtime WHERE key=?", (key,))
    assert blocks.summary(store.db)["issued"] == max(extra, max(ids))


def test_price_recode_rollback_changed_values_and_duplicate_offers_are_atomic(store):
    setup(store)
    ids, values, candidates = population(store)
    activate(store)
    before = list(store.db.iterdump())
    with pytest.raises(RuntimeError), store.db:
        assert blocks.offer_price(store.db, ids[0], candidates[0])
        raise RuntimeError("interrupted verified recode")
    assert list(store.db.iterdump()) == before
    with pytest.raises(ValueError, match="changed retained"), store.db:
        blocks.offer_price(store.db, ids[0], candidates[1])
    assert list(store.db.iterdump()) == before
    with store.db:
        assert blocks.offer_price(store.db, ids[0], candidates[0])
    before = list(store.db.iterdump())
    with store.db:
        assert not blocks.offer_price(store.db, ids[0], candidates[0])
    assert list(store.db.iterdump()) == before


def test_quality_packets_promote_and_release_recipes_without_source_dependencies(store):
    setup(store)
    ids, values, candidates = population(store)
    activate(store)
    with store.db:
        for identity, candidate in zip(ids, candidates, strict=True):
            blocks.offer_price(store.db, identity, candidate)
        original = [fact(i, values[i : i + 3]) for i in range(8)]
        raw = save(store, original)
    assert encode(quality_packets.unpack(store.db, raw)) == encode(original)
    with store.db:
        numeric_pool.collect(store.db, 256)
    assert encode(quality_packets.unpack(store.db, raw)) == encode(original)
    with store.db:
        store.db.execute("DELETE FROM market_quality")
        for _ in range(12):
            quality_packets.collect(store.db)
        numeric_pool.collect(store.db, 256)
    assert blocks.summary(store.db)["live"] == 0
    assert store.db.execute("SELECT COUNT(*) FROM market_numeric_block_lookup").fetchone()[0] == 0
    assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_actual_nb2_bytes_and_feature_marker_are_rechecked_after_warm_cache(store):
    setup(store)
    ids, values, candidates = population(store)
    activate(store)
    with store.db:
        assert blocks.offer_price(store.db, ids[0], candidates[0])
    assert numeric_pool.load(store.db, ids[0]) == values[0]
    with store.db:
        store.db.execute("UPDATE market_meta SET value='1' WHERE key='numeric_blocks'")
    with pytest.raises(ValueError):
        numeric_pool.load(store.db, ids[0])
    with store.db:
        store.db.execute("UPDATE market_meta SET value='2' WHERE key='numeric_blocks'")
    assert numeric_pool.load(store.db, ids[0]) == values[0]
    with store.db:
        store.db.execute("DROP TRIGGER market_numeric_block_guard")
        store.db.execute("UPDATE market_numeric_blocks SET payload=zeroblob(length(payload)) WHERE id=0")
    with pytest.raises(ValueError):
        numeric_pool.load(store.db, ids[0])
    assert store.reference_cache_bytes <= 1024 * 1024
