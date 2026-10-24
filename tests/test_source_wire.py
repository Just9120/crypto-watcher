import hashlib
import json
from contextlib import closing

import pytest

from cryptowatcher import candle_block_codec as blocks
from cryptowatcher import market_storage as market
from cryptowatcher.history_codec import Codec
from cryptowatcher.storage import StateError, Store, encode
from tests.test_bounded_storage import bounded
from tests.test_candle_access import writer
from tests.test_candle_block_codec import fixture


def values(step):
    tile, slots = fixture(step)
    candles = [blocks.public(tile, step, slot, slots[slot][0][3]) for slot in (0, 7, 31)]
    return [candles[2], candles[0], candles[1], candles[0], {**candles[0], "close": 102.0}]


@pytest.mark.parametrize("stream", ["spot/BTCUSDT/1m", "linear/ETHUSDT/15m"])
def test_direct_checked_source_values_keep_bytes_order_duplicates_bits_and_mutation_isolation(store, stream):
    bounded(store)
    writer(store)
    expected = values(int(stream.rsplit("/", 1)[1][:-1]) * 60000)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        reference = market.window(store.db, stream, expected, expected[0]["start"], cache=store.cached_bytes)
    assert reference["mode"] == "source-cells"
    codec = Codec(store.db, binary_cache=store.cached_bytes)
    changes = store.db.total_changes
    for _ in range(2):
        restored = codec.field(reference)
        assert encode(restored).encode() == codec.field_raw(reference) == encode(expected).encode()
        restored[0]["close"] = -1
        restored.append({"caller": "mutation"})
    assert store.db.total_changes == changes
    assert store.reference_cache_bytes <= 1024**2


def test_direct_legacy_and_interned_references_still_require_original_full_window_hash(store):
    bounded(store)
    expected = values(60000)
    with store.db:
        reference = market.window(store.db, "spot/BTCUSDT/1m", expected, expected[0]["start"])
        points = market.admit_many(store.db, "spot/BTCUSDT/1m", expected)
    assert reference["mode"] == "source-ref"
    full = market.compact_reference(
        points[0][0], [[p[1], p[2]] for p in points], hashlib.sha256(encode(expected).encode()).hexdigest()
    )
    codec = Codec(store.db, binary_cache=store.cached_bytes)
    for ref in (reference, full):
        assert codec.field(ref) == json.loads(codec.field_raw(ref))
        assert encode(codec.field(ref)) == encode(expected)
    for checksum in (None, "0" * 64):
        for reader in (codec.field, codec.field_raw):
            with pytest.raises(StateError, match="normalized source"):
                reader({**full, "sha256": checksum})


@pytest.mark.parametrize("fault", ["nan", "infinite", "nested-price", "unicode"])
def test_hashless_admission_keeps_real_source_validation_and_rolls_back_failure(store, fault):
    bounded(store)
    writer(store)
    candle = values(60000)[0]
    if fault == "unicode":
        candle["source"] = "\ud800"
    else:
        candle["close"] = {"nan": float("nan"), "infinite": float("inf"), "nested-price": []}[fault]
    before = list(store.db.iterdump())
    with pytest.raises((ValueError, TypeError, UnicodeError)), store.db:
        store.db.execute("BEGIN IMMEDIATE")
        market.window(store.db, "spot/BTCUSDT/1m", [candle], candle["start"], cache=store.cached_bytes)
    assert list(store.db.iterdump()) == before


def test_direct_source_read_keeps_wal_snapshot_and_rejects_changed_current_bytes_after_cache_hit(store):
    bounded(store)
    writer(store)
    expected = values(60000)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        reference = market.window(
            store.db, "spot/BTCUSDT/1m", expected, expected[0]["start"], cache=store.cached_bytes
        )
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        reader.db.execute("BEGIN")
        codec = Codec(reader.db, binary_cache=reader.cached_bytes)
        assert encode(codec.field(reference)) == encode(expected)
        identity, raw = store.db.execute("SELECT id,payload FROM market_candle_blocks LIMIT 1").fetchone()
        with store.db, store.db.blobopen("market_candle_blocks", "payload", identity, readonly=False) as blob:
            blob.seek(len(raw) - 1)
            blob.write(bytes([raw[-1] ^ 1]))
        assert encode(codec.field(reference)) == encode(expected)
        reader.db.rollback()
        for read in (codec.field, codec.field_raw):
            with pytest.raises(StateError, match="normalized source"):
                read(reference)
        assert reader.db.total_changes == 0


@pytest.mark.parametrize("fault", ["nonfinite", "unicode"])
def test_object_source_reader_retains_original_json_validation_after_a_bad_cache_result(store, fault):
    bounded(store)
    writer(store)
    expected = values(60000)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        reference = market.window(
            store.db, "spot/BTCUSDT/1m", expected, expected[0]["start"], cache=store.cached_bytes
        )

    def bad_cache(kind, key, raw, read):
        value = store.cached_bytes(kind, key, raw, read)
        if kind == "source-tile-slots":
            slots = json.loads(value)
            for slot in slots:
                for row in slot:
                    row[3][3 if fault == "nonfinite" else 6] = (
                        float("nan") if fault == "nonfinite" else "\ud800"
                    )
            return json.dumps(slots, ensure_ascii=True).encode()
        return value

    codec = Codec(store.db, binary_cache=bad_cache)
    with pytest.raises(StateError, match="normalized source"):
        codec.field_raw(reference)
    with pytest.raises(StateError, match="normalized source"):
        codec.field(reference)
