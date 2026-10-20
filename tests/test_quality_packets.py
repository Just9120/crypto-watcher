from contextlib import closing

import pytest

from cryptowatcher import numeric_pool, quality_packets, quality_vector
from cryptowatcher.market_storage import enable
from cryptowatcher.quality_layout import put
from cryptowatcher.quality_summary import encode_facts
from cryptowatcher.storage import StateError, Store, encode


def metric(index):
    value = index / 127.0
    return {
        "price_return_pct": value,
        "directed_return_pct": value,
        "mfe_pct": value + 0.125,
        "mae_pct": -0.0,
    }


def fact(index=0, comparisons=None):
    return {
        "strength": 1.0 + index / 7,
        "noise": -0.0,
        "metrics": metric(index),
        "state": "COMPLETE",
        "previous_state": "COMPLETE",
        "classification": "CONTINUATION",
        "comparator_metrics": comparisons if comparisons is not None else [metric(i) for i in range(20)],
        "comparator_sets": [
            {"methodology_version": "fixture-2", "offset": 0, "count": 20, "limitations": []}
        ],
        "limitations": [],
        "completeness": {"expected_candles": 15, "observed_candles": 15},
    }


def save(store, values, day=20000, cohort="cohort"):
    raw = quality_packets.pack(store.db, values, cohort)
    put(store.db, (day, cohort, 0, day + 366, encode({"detector": "price"}), "{}", raw, len(values)))
    return raw


def save_v4(store, values, day=20000, cohort="cohort"):
    """Frozen CQ4 row shape, including actual set4 delta ancestors."""
    from cryptowatcher.record_codec import pack_binary

    modern = quality_packets.pack(store.db, values, cohort)
    rows, columns = quality_packets.packet(modern)
    for row in rows:
        row[2] = quality_packets.intern(store.db, "set4", quality_vector.unpack(row[2]), hint=cohort)
    raw = quality_packets.VIEW_MAGIC + pack_binary(encode([rows, columns]))
    put(store.db, (day, cohort, 0, day + 366, encode({"detector": "price"}), "{}", raw, len(values)))
    return raw


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("count", [7, 8, 127])
@pytest.mark.parametrize("transactional", [False, True])
def test_append_keeps_prefix_coordinates_and_only_interns_new_comparators(
    store, monkeypatch, legacy, count, transactional
):
    enable(store.db)
    values = [fact(i, [metric(1000 + i * 20 + j) for j in range(20)]) for i in range(count)]
    added = fact(9999, [metric(100000 + i) for i in range(20)])
    with store.db:
        raw = (save_v4 if legacy else save)(store, values)
    before = quality_packets.ownership(store.db, raw)["numeric"]
    original = numeric_pool.encode_reference
    calls = []

    def record(db, value, **kwargs):
        calls.append(encode(value))
        return original(db, value, **kwargs)

    monkeypatch.setattr(numeric_pool, "encode_reference", record)
    with store.db:
        if transactional:
            store.db.execute("BEGIN")
        result = quality_packets.append(store.db, raw, added, "cohort", expected_count=count)
        put(store.db, (20000, "cohort", 0, 20366, encode({"detector": "price"}), "{}", result, count + 1))
    assert calls == [encode(value) for value in added["comparator_metrics"]]
    assert encode(quality_packets.unpack(store.db, result)) == encode([*values, added])
    assert set(before) <= set(quality_packets.ownership(store.db, result)["numeric"])
    if count == 127:
        with pytest.raises(ValueError, match="incomplete packet"):
            quality_packets.append(store.db, result, added)


def test_append_count_failure_and_writer_rollback_preserve_existing_rows_and_owners(store):
    enable(store.db)
    values = [fact(7)]
    with store.db:
        raw = save(store, values)
    before = list(store.db.iterdump())
    with pytest.raises(ValueError, match="count disagrees"), store.db:
        quality_packets.append(store.db, raw, fact(9), expected_count=2)
    assert list(store.db.iterdump()) == before
    with pytest.raises(RuntimeError), store.db:
        value = quality_packets.append(store.db, raw, fact(9), expected_count=1)
        put(store.db, (20000, "cohort", 0, 20366, encode({"detector": "price"}), "{}", value, 2))
        raise RuntimeError("rollback appended archive and numeric/dictionary writes")
    assert list(store.db.iterdump()) == before


def test_append_n0_and_legacy_self_contained_packets_keep_public_facts(store):
    enable(store.db)
    values = [fact(i, []) for i in range(10)]
    with store.db:
        raw = save(store, values[:1])
        for value in values[1:]:
            raw = quality_packets.append(store.db, raw, value)
        assert encode(quality_packets.unpack(store.db, raw)) == encode(values)
        legacy = encode_facts(values[:8])
        result = quality_packets.append(store.db, legacy, values[8])
        assert encode(quality_packets.unpack(store.db, result)) == encode(values[:9])


def test_append_rejects_changed_prefix_and_rolls_back_all_speculative_writes(store, monkeypatch):
    enable(store.db)
    with store.db:
        raw = save(store, [fact(7)])
    before = list(store.db.iterdump())
    original = quality_packets.envelope

    def changed_prefix(rows, columns):
        if len(rows) > 1:
            rows[0][1] = [metric(12345)]
        return original(rows, columns)

    monkeypatch.setattr(quality_packets, "envelope", changed_prefix)
    with pytest.raises(ValueError, match="changed public facts"), store.db:
        store.db.execute("BEGIN")
        quality_packets.append(store.db, raw, fact(8), expected_count=1)
    assert list(store.db.iterdump()) == before


def test_append_still_bounds_full_decoded_public_facts(store, monkeypatch):
    enable(store.db)
    original = fact(7)
    with store.db:
        raw = save(store, [original])
    before = list(store.db.iterdump())
    monkeypatch.setattr(quality_packets, "MAXIMUM", len(encode([original]).encode()) + 1)
    with pytest.raises(ValueError, match="Decoded quality exceeds bound"), store.db:
        store.db.execute("BEGIN")
        quality_packets.append(store.db, raw, fact(8), expected_count=1)
    assert list(store.db.iterdump()) == before


def test_archive_packets_preserve_exact_facts_mixed_formats_reopen_refs_and_rollback(store):
    enable(store.db)
    values = [fact(i) for i in range(24)]
    with store.db:
        raw = save(store, values)
    assert encode(quality_packets.unpack(store.db, raw)) == encode(values)
    assert quality_packets.unpack(store.db, encode_facts(values)) == values
    before = tuple(
        tuple(r)
        for r in store.db.execute("SELECT id,detail_refs,quality_refs FROM market_numeric ORDER BY id")
    )
    with pytest.raises(RuntimeError), store.db:
        save(store, [fact(500)], 20001)
        raise RuntimeError("rollback both facts and references")
    assert (
        tuple(
            tuple(r)
            for r in store.db.execute("SELECT id,detail_refs,quality_refs FROM market_numeric ORDER BY id")
        )
        == before
    )
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        assert encode(quality_packets.unpack(reader.db, raw)) == encode(values)
        assert (
            reader.db.execute(
                "SELECT json_array_length(cw_quality_json(facts)) FROM market_quality"
            ).fetchone()[0]
            == 24
        )
    with store.db:
        store.db.execute("DELETE FROM market_quality WHERE day=20000")
        for _ in range(12):
            quality_packets.collect(store.db)
        numeric_pool.collect(store.db)
    assert store.db.execute("SELECT COUNT(*) FROM market_quality_dictionary").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM market_numeric").fetchone()[0] == 0


def test_comparator_delta_order_duplicates_depth_and_leaf_ownership_do_not_keep_dropped_metrics(store):
    enable(store.db)
    originals = []
    with store.db:
        for i in range(24):
            # Large IDs make copy/literal deltas unambiguously smaller.
            original = list(range(1000000 + i, 1000000 + i + 20)) + [1000000 + i]
            identity = quality_packets.intern(store.db, "set", original, hint="delta")
            assert quality_packets.sequence(store.db, identity) == original
            originals.append(identity)
    assert max(r[0] for r in store.db.execute("SELECT depth FROM market_quality_dictionary")) <= 8
    assert (
        store.db.execute(
            "SELECT COUNT(*) FROM market_quality_dictionary WHERE parent IS NOT NULL"
        ).fetchone()[0]
        > 0
    )
    for identity in originals:
        with store.db:
            store.db.execute("UPDATE market_quality_dictionary SET refs=refs+1 WHERE id=?", (identity,))
    with store.db:
        for identity in originals:
            store.db.execute("UPDATE market_quality_dictionary SET refs=refs-1 WHERE id=?", (identity,))
        for _ in range(12):
            quality_packets.collect(store.db)
    assert store.db.execute("SELECT COUNT(*) FROM market_quality_dictionary").fetchone()[0] == 0


def test_leaf_metric_refs_remain_until_last_owner_and_missing_values_fail_closed(store):
    import sqlite3

    enable(store.db)
    with store.db:
        first = save(store, [fact()])
        second = save(store, [fact()], 20001)
    ids = quality_packets.ownership(store.db, first)["numeric"]
    assert all(r[0] == 2 for r in store.db.execute("SELECT quality_refs FROM market_numeric"))
    with pytest.raises(sqlite3.IntegrityError), store.db:
        store.db.execute("DELETE FROM market_numeric WHERE id=?", (ids[0],))
    with store.db:
        store.db.execute("DELETE FROM market_quality WHERE day=20000")
        quality_packets.collect(store.db)
        numeric_pool.collect(store.db)
    assert encode(quality_packets.unpack(store.db, second)) == encode([fact()])
    assert all(r[0] == 1 for r in store.db.execute("SELECT quality_refs FROM market_numeric"))
    # Corruption is injected only in this disposable database.
    store.db.execute("DROP TRIGGER market_numeric_retained")
    with store.db:
        store.db.execute("DELETE FROM market_numeric WHERE id=?", (ids[0],))
    with pytest.raises(ValueError, match="Missing retained numeric"):
        quality_packets.unpack(store.db, second)
    with pytest.raises((sqlite3.IntegrityError, sqlite3.OperationalError)), store.db:
        put(store.db, (20002, "cohort", 0, 20368, '{"detector":"price"}', "{}", second, 1))


def test_archive_dictionary_hash_collision_unsupported_shapes_and_invalid_frame(store, monkeypatch):
    enable(store.db)
    with monkeypatch.context() as patcher:
        patcher.setattr(quality_packets, "signature", lambda raw: b"collision")
        with store.db:
            one = quality_packets.intern(store.db, "template", {"version": "one"})
            two = quality_packets.intern(store.db, "template", {"version": "two"})
        assert one != two and quality_packets.row(store.db, one, "template")[0] == {"version": "one"}
    for shape in ([], [{"metrics": {"unknown": 17}, "strength": 1.25}]):
        assert encode(quality_packets.unpack(store.db, quality_packets.pack(store.db, shape))) == encode(
            shape
        )
    with pytest.raises((ValueError, StateError)):
        quality_packets.unpack(store.db, quality_packets.MAGIC + b"invalid")


def test_delta_ancestor_keeps_coordinates_but_not_expired_numeric_values(store):
    enable(store.db)
    with store.db:
        # Force multi-byte coordinates so this tiny fixture uses a real delta.
        store.db.execute("INSERT INTO sqlite_sequence(name,seq) VALUES('market_numeric',1000000)")
        first = save_v4(store, [fact(0, [metric(i) for i in range(20)])])
        second = save_v4(store, [fact(1, [metric(i) for i in range(1, 21)])], 20001)
    assert (
        store.db.execute(
            "SELECT COUNT(*) FROM market_quality_dictionary WHERE parent IS NOT NULL"
        ).fetchone()[0]
        > 0
    )
    old_only = set(quality_packets.ownership(store.db, first)["numeric"]) - set(
        quality_packets.ownership(store.db, second)["numeric"]
    )
    assert old_only
    with store.db:
        store.db.execute("DELETE FROM market_quality WHERE day=20000")
        quality_packets.collect(store.db)
        numeric_pool.collect(store.db)
    for identity in old_only:
        assert store.db.execute("SELECT 1 FROM market_numeric WHERE id=?", (identity,)).fetchone() is None
    assert encode(quality_packets.unpack(store.db, second)) == encode(
        [fact(1, [metric(i) for i in range(1, 21)])]
    )


def test_parameterized_templates_keep_population_values_types_and_multiple_methods(store):
    enable(store.db)
    values = [fact(i) for i in range(24)]
    for index, value in enumerate(values):
        value["comparator_sets"] = [
            {"methodology_version": "one", "population_size": 100 + index, "offset": 0},
            {
                "methodology_version": "two",
                "population_size": None if index % 2 else 100.0 + index,
                "offset": 20,
            },
        ]
    with store.db:
        raw = save(store, values)
    assert encode(quality_packets.unpack(store.db, raw)) == encode(values)
    assert (
        store.db.execute("SELECT COUNT(*) FROM market_quality_dictionary WHERE kind='template2'").fetchone()[
            0
        ]
        == 1
    )


def test_repeated_numeric_views_restore_independent_public_dictionaries(store):
    enable(store.db)
    values = [fact(i, [metric(7), metric(7)]) for i in range(16)]
    with store.db:
        raw = save(store, values)
    restored = quality_packets.unpack(store.db, raw)
    assert encode(restored) == encode(values)
    restored[0]["comparator_metrics"][0]["mae_pct"] = 999
    assert encode(restored[0]["comparator_metrics"][1]) == encode(metric(7))
    assert encode(restored[1]["comparator_metrics"][0]) == encode(metric(7))
    assert encode(quality_packets.unpack(store.db, raw)) == encode(values)


def test_sparse_empty_comparators_share_metadata_without_inventing_samples(store):
    enable(store.db)
    values = [fact(7, [])]
    with store.db:
        raw = save(store, values)
    assert raw.startswith(quality_packets.MAGIC)
    assert encode(quality_packets.unpack(store.db, raw)) == encode(values)
    assert quality_packets.ownership(store.db, raw)["numeric"] == []
    assert store.db.execute("SELECT COUNT(*) FROM market_numeric").fetchone()[0] == 0
    dense = [fact(i, []) for i in range(32)]
    with store.db:
        assert quality_packets.pack(store.db, dense) == encode_facts(dense)
    with store.db:
        store.db.execute("DELETE FROM market_quality")
        quality_packets.collect(store.db)
    assert store.db.execute("SELECT COUNT(*) FROM market_quality_dictionary").fetchone()[0] == 0


def test_discarded_empty_packet_candidate_rolls_back_every_speculative_write(store, monkeypatch):
    enable(store.db)
    values = [fact(19, [])]
    original = quality_packets._pack
    before = list(store.db.iterdump())

    def oversized(db, facts, cohort=None):
        return original(db, facts, cohort) + bytes(4096)

    monkeypatch.setattr(quality_packets, "_pack", oversized)
    with store.db:
        assert quality_packets.pack(store.db, values, "empty-candidate") == encode_facts(values)
    assert list(store.db.iterdump()) == before


@pytest.mark.parametrize("cold", [False, True])
@pytest.mark.parametrize("owners", [1, 2, 3])
def test_shared_own_metric_survives_detail_expiry_without_promoting_singletons(store, cold, owners):
    from cryptowatcher import numeric_blocks
    from cryptowatcher.events import identity

    enable(store.db)
    if cold:
        numeric_blocks.install(store.db, store.cached_bytes)
        with store.db:
            store.db.execute("INSERT INTO market_meta VALUES('numeric_block_runtime','1')")
    value = fact(0, [metric(100 + i) for i in range(20)])
    keys = []
    with store.db:
        ref = numeric_pool.encode_reference(store.db, value["metrics"])
        for index in range(owners):
            key = f"radar:v1:outcome:{identity(f'shared-own/{index}')}:0000000001"
            keys.append(key)
            store.put_runtime(
                key,
                {
                    "_history_storage": {
                        "schema": "cryptowatcher.shared-fields.v1",
                        "fields": {"metrics": ref},
                    }
                },
            )
        raw = save(store, [value])
    metric_id = numeric_pool.reference_id(ref)
    assert numeric_pool.raw_many(store.db, {metric_id})[metric_id][1:3] == (
        owners,
        int(cold and owners >= 2),
    )
    with store.db:
        store.db.executemany("DELETE FROM runtime WHERE key=?", [(key,) for key in keys])
        numeric_pool.collect(store.db, 256)
    assert encode(quality_packets.unpack(store.db, raw)) == encode([value])
    with store.db:
        store.db.execute("DELETE FROM market_quality")
        for _ in range(12):
            quality_packets.collect(store.db)
        numeric_pool.collect(store.db, 256)
    assert not store.db.execute("SELECT 1 FROM market_quality_dictionary").fetchone()
    assert not store.db.execute("SELECT 1 FROM market_numeric").fetchone()
    if cold:
        assert numeric_blocks.summary(store.db)["live"] == 0
