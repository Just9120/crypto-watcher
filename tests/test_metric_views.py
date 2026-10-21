import random

import pytest

from cryptowatcher import metric_codec, numeric_pool, quality_packets
from cryptowatcher.market_storage import enable
from cryptowatcher.record_codec import pack_binary
from cryptowatcher.storage import encode
from tests.test_quality_packets import fact, metric, save


def test_views_preserve_bits_types_and_non_derivable_neutral_ranges():
    rng = random.Random(7519)
    originals = []
    for price in (0.0, -0.0, 0, 2**80, -(2**80), 5e-324, 1e308):
        for rise, fall in ((0, -0.0), (1e308, -1e308), (2.0, -2.0)):
            originals.append(dict(zip(metric_codec.DIRECTED, (price, price, rise, fall))))
    for _ in range(1000):
        price, rise, fall = rng.uniform(-1e30, 1e30), rng.random(), -rng.random()
        original = dict(zip(metric_codec.DIRECTED, (price, price, rise, fall)))
        originals.extend((original, metric_codec.reflected(original)))
        for width in (rise - fall, rng.random(), 0, -0.0):
            originals.append(dict(zip(metric_codec.NEUTRAL, (price, rise, fall, width))))
    for value in originals:
        candidate = metric_codec.canonical(value)
        assert candidate is not None
        body, view = candidate
        restored = metric_codec.restore_view(body, view)
        assert metric_codec.pack(restored) == metric_codec.pack(value)
        assert encode(restored) == encode(value)
        assert metric_codec.canonical(body) == (body, 0)
    assert metric_codec.canonical({"unsupported": 17}) is None


def test_three_exact_views_share_one_body_and_old_references_remain_readable(store):
    enable(store.db)
    up = metric(3)
    values = [up, metric_codec.reflected(up), metric_codec.restore_view(up, 2)]
    with store.db:
        refs = [numeric_pool.encode_reference(store.db, v) for v in values]
        legacy = numeric_pool.intern(store.db, up)
    assert len({numeric_pool.reference_id(ref) for ref in refs}) == 1
    for ref, original in zip(refs, values):
        assert encode(numeric_pool.resolve(store.db, ref)) == encode(original)
    assert encode(numeric_pool.resolve(store.db, {"mode": "numeric", "id": legacy})) == encode(up)
    for invalid in (True, 0, 3, 2**65, -4, 1.0):
        with pytest.raises(ValueError):
            numeric_pool.reference_id({"mode": "numeric2", "id": invalid})


def test_mixed_cq2_cq4_and_upgrade_preserve_public_facts_and_physical_ownership(store):
    enable(store.db)
    up = metric(3)
    values = [fact(1, [up, metric_codec.reflected(up), metric_codec.restore_view(up, 2)])]
    with store.db:
        raw = save(store, values)
        rows, columns = quality_packets.packet(raw)
        old_ids = [numeric_pool.intern(store.db, v) for v in values[0]["comparator_metrics"]]
        rows[0][2] = quality_packets.intern(store.db, "set", old_ids)
        old = quality_packets.PARAMETER_MAGIC + pack_binary(encode([rows, columns]))
    assert encode(quality_packets.unpack(store.db, raw)) == encode(values)
    assert encode(quality_packets.unpack(store.db, old)) == encode(values)
    refs = quality_packets.ownership(store.db, raw)["numeric"]
    assert len(refs) == 1
    assert (
        store.db.execute("SELECT quality_refs FROM market_numeric WHERE id=?", (refs[0],)).fetchone()[0] == 1
    )
    # Reproduce the prior trigger/marker without rewriting any retained rows.
    with store.db:
        ddl = store.db.execute(
            "SELECT sql FROM sqlite_master WHERE name='market_quality_dictionary_insert'"
        ).fetchone()[0]
        store.db.execute("DROP TRIGGER market_quality_dictionary_insert")
        store.db.execute(ddl.replace("kind=NEW.kind AND kind IN('set','set4')", "kind='set'"))
        store.db.execute("UPDATE market_meta SET value='1' WHERE key='quality_packets'")
    quality_packets.install(store.db)
    assert store.db.execute("SELECT value FROM market_meta WHERE key='quality_packets'").fetchone()[0] == "2"
    assert encode(quality_packets.unpack(store.db, old)) == encode(values)
    assert encode(quality_packets.unpack(store.db, raw)) == encode(values)
    with store.db:
        # A large coordinate exercises the new set4 delta ancestor constraint.
        first = list(range(4000000, 4000080))
        quality_packets.intern(store.db, "set4", first, hint="upgrade")
        identity = quality_packets.intern(store.db, "set4", first[1:] + [4000080], hint="upgrade")
    assert quality_packets.row(store.db, identity, "set4")[1] is not None
    assert quality_packets.sequence(store.db, identity, kind="set4") == first[1:] + [4000080]


def test_archive_own_views_reuse_only_shared_or_already_quality_retained_bodies(store):
    from cryptowatcher.events import identity

    enable(store.db)
    up = metric(500)
    values = [up, metric_codec.reflected(up), metric_codec.restore_view(up, 2), up]
    owners = []
    with store.db:
        for i, value in enumerate(values):
            ref = numeric_pool.encode_reference(store.db, value)
            key = f"radar:v1:outcome:{identity('shared-own/' + str(i))}:0000000001"
            owners.append(key)
            store.put_runtime(
                key,
                {
                    "_history_storage": {
                        "schema": "cryptowatcher.shared-fields.v1",
                        "fields": {"metrics": ref},
                    }
                },
            )
        facts = [{**fact(i), "metrics": value} for i, value in enumerate(values)]
        raw = save(store, facts)
    own = [row[1] for row in quality_packets.packet(raw)[0]]
    assert all(type(v) is int for v in own)
    assert len({v // 4 for v in own}) == 1
    assert encode(quality_packets.unpack(store.db, raw)) == encode(facts)
    body = own[0] // 4
    with store.db:
        for key in owners:
            store.db.execute("DELETE FROM runtime WHERE key=?", (key,))
        numeric_pool.collect(store.db)
    assert store.db.execute(
        "SELECT detail_refs,quality_refs FROM market_numeric WHERE id=?", (body,)
    ).fetchone()[:] == (0, 1)
    assert encode(quality_packets.unpack(store.db, raw)) == encode(facts)
    with store.db:
        second = save(store, [facts[0]], 20001)
    assert type(quality_packets.packet(second)[0][0][1]) is int
    with store.db:
        store.db.execute("DELETE FROM market_quality WHERE day=20000")
        quality_packets.collect(store.db)
        numeric_pool.collect(store.db)
    assert encode(quality_packets.unpack(store.db, second)) == encode([facts[0]])
    with store.db:
        store.db.execute("DELETE FROM market_quality WHERE day=20001")
        for _ in range(12):
            quality_packets.collect(store.db)
        numeric_pool.collect(store.db)
    assert store.db.execute("SELECT 1 FROM market_numeric WHERE id=?", (body,)).fetchone() is None


def test_unique_own_result_remains_inline_and_never_creates_a_dedicated_pool_body(store):
    enable(store.db)
    original = {**fact(), "metrics": metric(500)}
    with store.db:
        assert numeric_pool.existing_reference(store.db, original["metrics"]) is None
        raw = save(store, [original])
    assert quality_packets.packet(raw)[0][0][1] == [original["metrics"]]
    assert numeric_pool.existing_reference(store.db, original["metrics"]) is None
    assert encode(quality_packets.unpack(store.db, raw)) == encode([original])
