import hashlib
import struct

import pytest

from cryptowatcher import candle_block_codec as codec
from cryptowatcher.storage import encode


def fixture(step=60000):
    tile = 1000 * 32 * step
    slots = [[] for _ in range(32)]
    for slot in (0, 7, 31):
        for revision in (1, 2):
            tail = [
                100,
                103.0,
                99.0,
                101.0 + revision / 4,
                0 if slot == 0 else 1.0,
                -0.0 if slot == 7 else 100.0,
                "проверка",
                tile + 100 * step + 12,
                tile + 100 * step,
            ]
            slots[slot].append([revision, 0 if slot == 31 else tile, tile + 100000 * step, tail])
    return tile, slots


@pytest.mark.parametrize("sid", [1, 2])
@pytest.mark.parametrize("step", codec.STEPS)
def test_source_block_keeps_all_fields_types_revisions_gaps_and_lease_metadata(store, sid, step):
    tile, slots = fixture(step)
    raw = codec.pack(sid, tile, step, slots)
    assert encode(codec.unpack(raw)[3]) == encode(slots)
    assert codec.stats(raw) == (sid, tile, step, 6, tile + 100000 * step)
    for slot, items in enumerate(slots):
        for revision, needed, purge, tail in items:
            value, actual_needed, actual_purge = codec.point(
                raw, sid, tile + slot * step, revision, store.cached_bytes
            )
            assert encode(value) == encode(codec.public(tile, step, slot, tail))
            assert (actual_needed, actual_purge) == (needed, purge)
            value["volume"] = 1000
            assert (
                codec.point(raw, sid, tile + slot * step, revision, store.cached_bytes)[0]["volume"] != 1000
            )
    assert codec.point(raw, sid, tile + 2 * step, 1) is None
    assert codec.point(raw, sid, tile, 99) is None
    assert store.reference_cache_bytes <= 1024 * 1024


def test_source_block_immutable_prefix_detects_signed_zero_and_type_changes_and_reports_removals():
    tile, slots = fixture()
    old = codec.pack(1, tile, 60000, slots)
    extended = codec.unpack(old)[3]
    extended[0][0][1] += 1
    extended[0][0][2] += 1000
    assert codec.preserved(old, codec.pack(1, tile, 60000, extended)) == []
    for slot, field, value in ((7, 5, 0.0), (0, 4, 0.0)):
        changed = codec.unpack(old)[3]
        changed[slot][0][3][field] = value
        with pytest.raises(ValueError, match="Immutable"):
            codec.preserved(old, codec.pack(1, tile, 60000, changed))
    removed = codec.unpack(old)[3]
    removed[0].pop(0)
    dropped = codec.preserved(old, codec.pack(1, tile, 60000, removed))
    assert len(dropped) == 1 and dropped[0][1:3] == (tile, 1)


def test_source_block_rejects_wrong_identity_bad_revision_order_and_actual_corruption_with_warm_cache(store):
    tile, slots = fixture()
    raw = codec.pack(1, tile, 60000, slots)
    codec.point(raw, 1, tile, 1, store.cached_bytes)
    for sid, at in ((2, tile), (1, tile + 1), (1, tile + 32 * 60000)):
        with pytest.raises(ValueError, match="identity"):
            codec.point(raw, sid, at, 1, store.cached_bytes)
    damaged = bytearray(raw)
    damaged[-1] ^= 1
    with pytest.raises(ValueError, match="checksum"):
        codec.point(bytes(damaged), 1, tile, 1, store.cached_bytes)
    wrong_header = struct.pack(">QQQ", 2, tile, 60000) + raw[60:]
    rebound = codec.MAGIC + hashlib.sha256(wrong_header).digest() + wrong_header
    with pytest.raises(ValueError, match="identity"):
        codec.preserved(raw, rebound)
    slots[0].reverse()
    with pytest.raises(ValueError, match="order"):
        codec.pack(1, tile, 60000, slots)


def test_source_block_does_not_allow_empty_tile_boolean_metadata_or_unknown_steps():
    tile, slots = fixture()
    with pytest.raises(ValueError, match="row bound"):
        codec.pack(1, tile, 60000, [[] for _ in range(32)])
    slots[0][0][1] = True
    with pytest.raises(ValueError):
        codec.pack(1, tile, 60000, slots)
    with pytest.raises(ValueError):
        codec.pack(1, 0, 120000, slots)
