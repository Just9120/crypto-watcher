import copy
import hashlib
import json
import math
import random
import struct

import pytest
import zstandard

from cryptowatcher import candle_block_codec as codec
from cryptowatcher import source_slot_columns as columns
from cryptowatcher.storage import encode
from tests.test_candle_block_codec import fixture


def dense():
    tile, original = fixture()
    slots = [[] for _ in range(32)]
    for i in range(32):
        row = copy.deepcopy(original[0][0])
        row[3][:6] = [100.0, 103.0, 99.0, 101.0 + i / 10000, -0.0, 10.0 + i / 10]
        slots[i] = [row]
    return tile, slots


def wrapped(frame, *, tile=None):
    tile = dense()[0] if tile is None else tile
    checked = struct.pack(">QQQ", 9, tile, 60000) + frame
    return codec.COLUMN_MAGIC + hashlib.sha256(checked).digest() + checked


def raw_frame(raw, count=32):
    body = zstandard.ZstdCompressor(level=3, write_checksum=True).compress(raw)
    return struct.pack(">IH", len(raw), count) + body


def parts(frame):
    size, count, body = columns.layout(frame)
    raw = zstandard.ZstdDecompressor().decompress(body, max_output_size=size)
    n = struct.unpack(">I", raw[:4])[0]
    metadata = json.loads(raw[4 : 4 + n])
    result = []
    at = 4 + n
    for _, length in metadata[1]:
        result.append(raw[at : at + length])
        at += length
    return metadata, result, count


def altered(frame, mutation):
    metadata, values, count = parts(frame)
    mutation(metadata, values)
    raw = encode(metadata).encode()
    return raw_frame(struct.pack(">I", len(raw)) + raw + b"".join(values), count)


def test_optional_compaction_preserves_frozen_hot_writer_and_every_value_id_and_lease(store):
    tile, slots = dense()
    old = codec.pack(9, tile, 60000, slots)
    compact = codec.compact(old)
    assert old.startswith((codec.MAGIC, codec.JSON_MAGIC))
    assert compact.startswith(codec.COLUMN_MAGIC) and len(compact) < len(old)
    assert codec.pack(9, tile, 60000, slots) == old
    assert codec.compact(compact) == compact
    assert codec.preserved(old, compact) == codec.preserved(compact, old) == []
    assert codec.stats(old) == codec.stats(compact)
    for raw in (old, compact):
        for canonical in (False, True):
            reader = codec.Reader(raw, store.cached_bytes, canonical=canonical)
            for i in range(32):
                actual = reader.point(9, tile + i * 60000, 1)
                expected = codec.public(tile, 60000, i, slots[i][0][3]), *slots[i][0][1:3]
                assert encode(actual) == encode(expected)
                assert math.copysign(1, actual[0]["volume"]) == -1
        values = codec.unpack(raw, cache=store.cached_bytes)[3]
        values[0][0][3][0] = 200
        assert encode(codec.unpack(raw, cache=store.cached_bytes)[3]) == encode(slots)
    assert store.reference_cache_bytes <= 1024 * 1024


def test_columns_keep_ieee_edges_original_scalar_types_and_all256_revisions():
    tile, _ = dense()
    slots = [[] for _ in range(32)]
    edges = [math.ulp(0.0), float.fromhex("0x1.fffffffffffffp+1023"), -0.0, 0.0, 1.2345678901234567]
    for i in range(256):
        tail = [
            100,
            100.0,
            100.0,
            100.0,
            edges[i % len(edges)],
            i if i % 2 else float(i),
            "rest",
            2**62,
            2**62 - 1,
        ]
        slots[i // 8].append([i % 8, 2**62, 2**62 + 1, tail])
    frame = columns.pack(slots)
    assert frame is not None
    actual = codec.unpack(wrapped(frame, tile=tile))[3]
    assert encode(actual) == encode(slots)
    for before, after in zip((r for s in slots for r in s), (r for s in actual for r in s)):
        for a, b in zip(before[3], after[3]):
            assert type(a) is type(b)
            if type(a) is float:
                assert struct.pack(">d", a) == struct.pack(">d", b)


def test_columns_roundtrip_independent_finite_float_bits_and_int_float_mixtures():
    randomizer = random.Random(94712)
    for _ in range(12):
        _, slots = dense()
        for slot in slots:
            for j in range(6):
                number = struct.unpack(">d", randomizer.randbytes(8))[0]
                slot[0][3][j] = number if math.isfinite(number) else -0.0
        frame = columns.pack(slots)
        assert frame is not None
        assert encode(columns.all_values(frame)) == encode(slots)
    # Generic numeric columns retain mixed types instead of coercing to float.
    slots[0][0][3][0] = 1
    assert encode(columns.all_values(columns.pack(slots))) == encode(slots)


@pytest.mark.parametrize("change", ["checksum", "identity", "count", "expanded", "trailing", "no_checksum"])
def test_actual_outer_and_compression_mutations_fail_with_warm_cache(store, change):
    tile, slots = dense()
    frame = columns.pack(slots)
    good = wrapped(frame)
    codec.point(good, 9, tile, 1, store.cached_bytes)
    if change == "checksum":
        bad = good[:-1] + bytes([good[-1] ^ 1])
    elif change == "identity":
        checked = struct.pack(">QQQ", 10, tile, 60000) + frame
        bad = good[:4] + hashlib.sha256(checked).digest() + checked
    elif change == "count":
        bad = wrapped(frame[:4] + struct.pack(">H", 257) + frame[6:])
    elif change == "expanded":
        bad = wrapped(struct.pack(">I", 512 * 1024 + 1) + frame[4:])
    elif change == "trailing":
        bad = wrapped(frame + b"trailing")
    else:
        size, count, body = columns.layout(frame)
        raw = zstandard.ZstdDecompressor().decompress(body)
        body = zstandard.ZstdCompressor(write_checksum=False).compress(raw)
        bad = wrapped(struct.pack(">IH", size, count) + body)
    with pytest.raises(ValueError):
        codec.point(bad, 9, tile, 1, store.cached_bytes)


@pytest.mark.parametrize(
    "change",
    [
        "slot_count",
        "row_count",
        "bool_count",
        "kind",
        "length",
        "zero_length",
        "delta_count",
        "scale",
        "zero_mask",
        "nonzero_mask",
        "column_type",
        "nonfinite",
        "object",
        "integer_bound",
    ],
)
def test_validly_checksummed_columns_reject_malformed_shape_values_and_allocation(change):
    _, slots = dense()
    frame = columns.pack(slots)

    def mutation(metadata, values):
        counts, descriptors = metadata
        if change == "slot_count":
            counts.pop()
        elif change == "row_count":
            counts[-1] += 1
        elif change == "bool_count":
            counts[0] = True
        elif change == "kind":
            descriptors[0][0] = "unknown"
        elif change == "length":
            descriptors[0][1] = 2**63
        elif change == "zero_length":
            descriptors[0][1] = 0
        else:
            index = 3 if change in ("scale", "zero_mask", "nonzero_mask", "nonfinite") else 0
            value = json.loads(values[index])
            if change == "delta_count":
                value.pop()
            elif change == "scale":
                value[0] = -325
            elif change == "zero_mask":
                value[2] = 1 << 32
            elif change == "nonzero_mask":
                value[2] = 1
            elif change == "column_type":
                value[0] = "1"
            elif change == "object":
                value[0] = {"x": 1}
            elif change == "nonfinite":
                value[0] = 308
                value[1][0] = 10**100
            else:
                value[0] = 2**8193
            values[index] = encode(value).encode()
            descriptors[index][1] = len(values[index])

    with pytest.raises(ValueError):
        codec.unpack(wrapped(altered(frame, mutation)))


def test_empty_unsupported_oversized_frames_fall_back_without_altering_hot_bytes():
    assert columns.pack([[] for _ in range(32)]) is None
    _, slots = dense()
    slots[0][0][3][0] = True
    assert columns.pack(slots) is None
    _, slots = dense()
    for slot in slots:
        slot[0][3][6] = "x" * (512 * 1024 // 32)
    assert columns.pack(slots) is None
    for frame in (b"", b"x" * 7, struct.pack(">IH", 20, 0) + b"x" * 20):
        with pytest.raises(ValueError):
            columns.all_values(frame)
