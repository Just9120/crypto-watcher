from contextlib import closing

import pytest

from cryptowatcher import numeric_pool, quality_packets, quality_vector
from cryptowatcher.market_storage import enable
from cryptowatcher.quality_layout import put
from cryptowatcher.record_codec import pack_binary
from cryptowatcher.storage import Store, encode
from tests.test_quality_packets import fact, save_v4


def test_vectors_keep_order_duplicates_large_coordinates_and_unknown_metrics():
    large = (2**63 - 1) * 4 + 3
    values = [4, 9, 9, 4, large, 7]
    encoded = quality_vector.pack(values, delta=True)
    assert encoded[0] == 1 and encoded[1][3] < 0
    assert quality_vector.unpack(encoded) == values
    unknown = [[{"future_metric": -0.0, "exact_integer": 2**300}]]
    assert encode(quality_vector.unpack(quality_vector.pack(unknown, delta=True))) == encode(unknown)
    assert quality_vector.pack([], delta=True) is None
    assert quality_vector.unpack(None) == []


@pytest.mark.parametrize(
    "invalid",
    [
        [],
        [True, []],
        [2, []],
        [1, [0]],
        [1, [4, -4]],
        [1, [4, -4, 4]],
        [1, [4, 2**65, -(2**65)]],
        [1, [True]],
        [0, [True]],
        [0, [4] * 4097],
        [1, [(2**63) * 4]],
        [0, [["not metrics"]]],
    ],
)
def test_vectors_reject_invalid_modes_coordinates_and_expansion(invalid):
    with pytest.raises(ValueError):
        quality_vector.unpack(invalid)


@pytest.mark.parametrize(
    "magic", [quality_packets.LEGACY_MAGIC, quality_packets.PARAMETER_MAGIC, quality_packets.VIEW_MAGIC]
)
def test_legacy_upgrade_preserves_numeric_ids_values_leases_and_sql_readback(store, magic):
    enable(store.db)
    values = [fact(7)]
    with store.db:
        raw = save_v4(store, values)
        rows, columns = quality_packets.packet(raw)
        if magic != quality_packets.VIEW_MAGIC:
            for row in rows:
                if type(row[1]) is int:
                    assert row[1] % 4 == 0
                    row[1] //= 4
                refs = quality_packets.sequence(store.db, row[2], kind="set4")
                assert all(type(r) is int and r % 4 == 0 for r in refs)
                row[2] = quality_packets.intern(store.db, "set", [r // 4 for r in refs])
                if magic == quality_packets.LEGACY_MAGIC:
                    metadata = quality_packets.restore_template(store.db, row[0], row[3], {})
                    row[0] = quality_packets.intern(store.db, "template", metadata)
                    del row[3]
            raw = magic + pack_binary(encode([rows, columns]))
            put(store.db, (20000, "cohort", 0, 20366, encode({"detector": "price"}), "{}", raw, 1))
    before = [tuple(r) for r in store.db.execute("SELECT * FROM market_numeric ORDER BY id")]
    original_refs = quality_packets.ownership(store.db, raw)["numeric"]
    with store.db:
        modern = quality_packets.repack(store.db, raw, "cohort")
        put(store.db, (20000, "cohort", 0, 20366, encode({"detector": "price"}), "{}", modern, 1))
        assert quality_packets.repack(store.db, modern) == modern
    assert modern.startswith(quality_packets.MAGIC)
    assert encode(quality_packets.unpack(store.db, modern)) == encode(values)
    assert quality_packets.ownership(store.db, modern)["numeric"] == original_refs
    assert [tuple(r) for r in store.db.execute("SELECT * FROM market_numeric ORDER BY id")] == before
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        assert reader.db.execute("SELECT cw_quality_json(facts) FROM market_quality").fetchone()[0] == encode(
            values
        )
    with store.db:
        store.db.execute("DELETE FROM market_quality")
        for _ in range(12):
            quality_packets.collect(store.db)
    assert store.db.execute("SELECT count(*) FROM market_quality_dictionary").fetchone()[0] == 0


@pytest.mark.parametrize("magic", [quality_packets.LEGACY_MAGIC, quality_packets.PARAMETER_MAGIC])
@pytest.mark.parametrize(
    "metrics",
    [
        {"price_return_pct": 2, "directed_return_pct": -2, "mfe_pct": 3.0, "mae_pct": -0.0},
        {"price_return_pct": -0.0, "max_rise_pct": 0.2, "max_fall_pct": -0.3, "range_pct": 0.7},
        {"future_metric": -0.0, "exact_integer": 2**300},
    ],
)
def test_legacy_raw_metrics_keep_original_frames_types_and_inline_fallback(store, magic, metrics):
    enable(store.db)
    value = fact(7, comparisons=[metrics, metrics])
    value["metrics"] = metrics
    with store.db:
        identity = numeric_pool.intern(store.db, metrics)
        reference = identity if identity is not None else [metrics]
        metadata, slots = quality_packets.template_parts(value)
        modern = magic == quality_packets.PARAMETER_MAGIC
        template = quality_packets.intern(
            store.db, "template2" if modern else "template", metadata if modern else metadata[0]
        )
        comparisons = quality_packets.intern(store.db, "set", [reference, reference])
        row = [template, reference, comparisons]
        if modern:
            row.append(slots)
        row.extend([value["strength"], value["noise"]])
        raw = magic + pack_binary(encode([[row], None]))
        put(store.db, (20000, "cohort", 0, 20366, "{}", "{}", raw, 1))
    before = [tuple(r) for r in store.db.execute("SELECT * FROM market_numeric ORDER BY id")]
    with store.db:
        upgraded = quality_packets.repack(store.db, raw)
        put(store.db, (20000, "cohort", 0, 20366, "{}", "{}", upgraded, 1))
    assert encode(quality_packets.unpack(store.db, upgraded)) == encode([value])
    assert [tuple(r) for r in store.db.execute("SELECT * FROM market_numeric ORDER BY id")] == before
    assert quality_packets.ownership(store.db, upgraded)["numeric"] == (
        [] if identity is None else [identity]
    )
