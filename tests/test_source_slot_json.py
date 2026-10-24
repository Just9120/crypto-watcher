import hashlib
import struct

import pytest
import zstandard

from cryptowatcher import candle_block_codec as codec
from cryptowatcher import candle_blocks, indexed_frame
from cryptowatcher import source_slot_json as slots
from cryptowatcher.storage import encode
from tests.test_candle_block_codec import fixture
from tests.test_candle_blocks import prepare


def legacy(stream, tile, step, values):
    checked = struct.pack(">QQQ", stream, tile, step) + indexed_frame.pack(values)
    return codec.MAGIC + hashlib.sha256(checked).digest() + checked


def test_typed_and_scalar_json_tiles_keep_complete_private_values_leases_and_fresh_cache_objects(store):
    tile, values = fixture()
    original = legacy(1, tile, 60000, values)
    current = codec.pack(1, tile, 60000, values)
    assert current.startswith(codec.JSON_MAGIC) and len(current) < len(original)
    for raw in (original, current):
        assert encode(codec.unpack(raw, cache=store.cached_bytes)[3]) == encode(values)
        assert codec.preserved(original, raw) == codec.preserved(raw, original) == []
        first = codec.Reader(raw, store.cached_bytes, canonical=True).point(1, tile, 1)
        first[0]["open"] = 555
        assert codec.point(raw, 1, tile, 1, store.cached_bytes)[0]["open"] == 100
        assert store.reference_cache_bytes <= 1024 * 1024
    assert list(codec.latest(original, 0, 2**63 - 1)) == list(codec.latest(current, 0, 2**63 - 1))


@pytest.mark.parametrize("field,value", [(6, {"future_source": [1, 2]}), (7, 2**70)])
def test_unsupported_scalar_encoding_keeps_exact_typed_source_without_truncation(field, value):
    tile, values = fixture()
    values[0][0][3][field] = value
    raw = codec.pack(1, tile, 60000, values)
    assert raw.startswith(codec.MAGIC)
    assert encode(codec.unpack(raw)[3]) == encode(values)
    # A renewal hint is advisory: unsupported scalar tails keep the ordinary
    # lossless encoding, rather than becoming a refused or truncated source.
    hinted = codec.pack(1, tile, 60000, values, _lease_encoding=codec.JSON_MAGIC)
    assert hinted == raw


def test_lease_renewal_keeps_values_revision_and_retention_without_unused_typed_encoding(store, monkeypatch):
    from contextlib import closing

    from cryptowatcher.storage import Store

    stream, tile, public = prepare(store)
    with store.db:
        original = candle_blocks.admit_many(store.db, stream, public)
    before = candle_blocks.tile(store.db, stream, tile, 60000)
    assert before.startswith(codec.JSON_MAGIC)
    anchor = tile + 40 * 86400000

    def unused_encoding(*args, **kwargs):
        raise AssertionError("Lease-only JSON renewal constructed an unused typed candidate")

    with monkeypatch.context() as patch:
        patch.setattr(indexed_frame, "pack", unused_encoding)
        with store.db:
            assert candle_blocks.admit_many(store.db, stream, public, anchor) == original
    renewed = candle_blocks.tile(store.db, stream, tile, 60000)
    assert renewed.startswith(codec.JSON_MAGIC)
    for value in public:
        point, needed, purge = codec.point(renewed, stream, value["start"], 0)
        assert encode(point) == encode(value)
        assert (needed, purge) == (anchor, anchor + 30 * 86400000)
    assert codec.preserved(before, renewed) == []
    # A new immutable correction still takes the ordinary validated chooser.
    corrected = {**public[0], "close": 102.0}
    with store.db:
        assert candle_blocks.admit_many(store.db, stream, [corrected], anchor)[0][2] == 1
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        raw = candle_blocks.tile(reader.db, stream, tile, 60000)
        assert encode(codec.point(raw, stream, tile, 0)[0]) == encode(public[0])
        assert encode(codec.point(raw, stream, tile, 1)[0]) == encode(corrected)
        assert reader.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not reader.db.execute("PRAGMA foreign_key_check").fetchall()


def test_legacy_sql_tile_can_be_recoded_renewed_and_corrected_with_rollback_and_old_values_preserved(store):
    stream, tile, public = prepare(store)
    with store.db:
        candle_blocks.admit_many(store.db, stream, public)
        current = candle_blocks.tile(store.db, stream, tile, 60000)
        frozen = codec.unpack(current)[3]
        original = legacy(stream, tile, 60000, frozen)
        candle_blocks.put(store.db, original, current)
    modern = codec.pack(stream, tile, 60000, frozen)
    assert modern.startswith(codec.JSON_MAGIC)
    with store.db:
        candle_blocks.put(store.db, modern, original)
        candle_blocks.admit_many(store.db, stream, [public[0]], tile + 40 * 86400000)
    renewed = candle_blocks.tile(store.db, stream, tile, 60000)
    assert encode(codec.point(renewed, stream, tile, 0)[0]) == encode(public[0])
    assert codec.point(renewed, stream, tile, 0)[1] == tile + 40 * 86400000
    before = list(store.db.iterdump())
    with pytest.raises(RuntimeError), store.db:
        candle_blocks.admit_many(store.db, stream, [{**public[0], "close": 102.0}])
        raise RuntimeError("interrupted correction")
    assert list(store.db.iterdump()) == before
    with store.db:
        assert candle_blocks.admit_many(store.db, stream, [{**public[0], "close": 102.0}])[0][2] == 1
    changed = candle_blocks.tile(store.db, stream, tile, 60000)
    assert encode(codec.point(changed, stream, tile, 0)[0]) == encode(public[0])
    assert codec.point(changed, stream, tile, 1)[0]["close"] == 102.0


def test_json_frame_allocation_corruption_and_cross_encoding_binding_fail_before_decompression(monkeypatch):
    tile, values = fixture()
    raw = slots.pack(values)
    assert raw is not None
    frame = codec.pack(1, tile, 60000, values)
    wrong_encoding = codec.MAGIC + frame[4:]
    expanded = raw[36:38] + (slots.MAX_BYTES + 1).to_bytes(4, "big") + raw[42:]
    invalid = slots.MAGIC + hashlib.sha256(expanded).digest() + expanded
    monkeypatch.setattr(
        indexed_frame.zstandard,
        "ZstdDecompressor",
        lambda **kwargs: pytest.fail("allocated before verified source JSON bounds"),
    )
    for candidate in (raw[:-1], invalid):
        with pytest.raises(ValueError):
            slots.data(candidate)
    with pytest.raises(ValueError):
        codec.header(wrong_encoding)


@pytest.mark.parametrize(
    "raw",
    [
        b"[[0,0,0,[1,1,1,1,1,1,{},1,1]]]",
        b'[[0,0,0,[NaN,1,1,1,1,1,"x",1,1]]]',
        b'[[0,0,0,[1e999,1,1,1,1,1,"x",1,1]]]',
        b"[" * 2000,
        b" " * (slots.MAX_BYTES + 1),
    ],
    ids=["object", "nan", "infinite-exponent", "deep", "oversized"],
)
def test_invalid_json_shapes_nonfinite_values_deep_input_and_oversized_slots_are_rejected(raw):
    with pytest.raises(ValueError):
        slots.slot(raw)


def test_extra_frame_and_actual_corruption_reject_with_warm_cache(store):
    _, values = fixture()
    raw = slots.pack(values)
    assert raw is not None
    slots.data(raw, store.cached_bytes)
    with pytest.raises(ValueError):
        slots.data(raw[:-1] + bytes((raw[-1] ^ 1,)), store.cached_bytes)
    checked = raw[36:] + zstandard.ZstdCompressor().compress(b"another frame")
    extra = slots.MAGIC + hashlib.sha256(checked).digest() + checked
    with pytest.raises((ValueError, zstandard.ZstdError)):
        slots.data(extra, store.cached_bytes)
