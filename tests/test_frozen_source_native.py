"""Actual JS1 materialization/admission against the original domain checks."""

import copy
import hashlib
import json
import random
import struct
import sys

import pytest

from cryptowatcher import candle_block_codec as codec
from cryptowatcher import frozen_codec_native as native
from cryptowatcher import source_slot_columns as columns
from cryptowatcher import source_slot_json
from cryptowatcher.storage import encode
from tests.test_candle_block_codec import fixture
from tests.test_source_slot_columns import dense, parts


@pytest.fixture
def accelerator():
    module = native.load()
    if module is None:
        assert sys.platform != "linux", "Build the canonical native helper before Linux tests"
        pytest.skip("Actual native frozen operation artifact required")
    return module


def frame(tile, step, slots):
    body = source_slot_json.pack(slots)
    assert body is not None
    checked = struct.pack(">QQQ", 1, tile, step) + body
    return codec.JSON_MAGIC + hashlib.sha256(checked).digest() + checked


def portable(raw, monkeypatch, *, validate=True):
    with monkeypatch.context() as patch:
        patch.setattr(native, "source_values", lambda *args: None)
        return codec.unpack(raw, validate=validate)[3]


@pytest.mark.parametrize("step", codec.STEPS)
def test_source_exact_bits_types_and_metadata_all_revisions(accelerator, monkeypatch, step):
    rng = random.Random(step)
    for repeat in range(24):
        tile, slots = fixture(step)
        for part in slots:
            for row in part:
                a, b = rng.uniform(1, 1000), rng.uniform(1, 1000)
                row[3][:6] = [a, max(a, b), min(a, b), b, -0.0, 5e-324]
                row[3][6] = 'Цена "🚨"\0\n𝄞'
                if repeat % 2:
                    row[3][:6] = [100, 110.0, 90, 101, 0, 2**53]
                row[3][7:] = [2**63 - 2, 2**63 - 1]
                row[:3] = [row[0], 2**63 - 2, 2**63 - 1]
        raw = frame(tile, step, slots)
        _, _, _, body = codec.header(raw)
        offsets, expanded = source_slot_json.data(body)
        frozen = native.source_values(tile, step, offsets, expanded)
        assert frozen is not None and type(frozen) is tuple
        assert type(frozen[0]) is tuple and type(frozen[0][0][3]) is tuple
        assert encode(frozen) == encode(slots) == encode(portable(raw, monkeypatch))
        assert native.source_stats(tile, step, offsets, expanded) == (6, 2**63 - 1)
        reader = codec.Reader(raw)
        assert reader.frozen is not None
        for slot in range(32):
            expected = slots[slot]
            assert encode(reader.items(slot)) == encode(expected)
            if expected:
                reader.items(slot)[0][3][0] = -1
                assert encode(reader.items(slot)) == encode(expected)
        assert encode(codec.unpack(raw)[3]) == encode(slots)


def test_fresh_actual_bytes_are_validated_before_preferred_admission(accelerator, monkeypatch):
    tile, slots = fixture()
    calls = []
    original = accelerator.source_stats

    def counted(*args):
        calls.append(args[2])
        return original(*args)

    monkeypatch.setattr(accelerator, "source_stats", counted)
    with monkeypatch.context() as patch:
        patch.setattr(codec, "public", lambda *args: pytest.fail("Common input should be validated in C"))
        raw = codec.pack(1, tile, 60000, slots, _prefer_json=True)
    assert calls and raw.startswith(codec.JSON_MAGIC)
    assert encode(codec.unpack(raw)[3]) == encode(slots)
    changed = copy.deepcopy(slots)
    changed[0][0][3][1] = 1.0
    for _ in range(2):
        with pytest.raises(ValueError, match="Invalid frozen"):
            codec.pack(1, tile, 60000, changed, _prefer_json=True)
    # Authentication still precedes the native parser for persisted frames.
    monkeypatch.setattr(accelerator, "source_values", lambda *args: pytest.fail("Checksum bypass"))
    with pytest.raises(ValueError, match="checksum"):
        codec.unpack(raw[:-1] + bytes([raw[-1] ^ 1]))


@pytest.mark.parametrize(
    "change", ["price", "type", "zero", "source", "received", "server", "lease", "remove"]
)
def test_no_mutation_or_removal_authority(accelerator, monkeypatch, change):
    tile, slots = fixture()
    old = frame(tile, 60000, slots)
    changed = copy.deepcopy(slots)
    row = changed[7][0]
    if change == "price":
        row[3][3] += 0.125
    elif change == "type":
        row[3][0] = float(row[3][0])
    elif change == "zero":
        row[3][5] = 0.0
    elif change == "source":
        row[3][6] += "x"
    elif change == "received":
        row[3][7] += 1
    elif change == "server":
        row[3][8] += 1
    elif change == "lease":
        row[1] -= 1
    else:
        changed[7].pop(0)
    current = frame(tile, 60000, changed)
    a, b = source_slot_json.data(old[60:]), source_slot_json.data(current[60:])
    assert native.source_preserved(tile, 60000, a, b) is False
    if change == "remove":
        assert len(codec.preserved(old, current)) == 1
    else:
        with pytest.raises(ValueError):
            codec.preserved(old, current)


@pytest.mark.parametrize("change", ["ohlc", "negative", "unclosed", "order", "revision", "empty"])
def test_full_semantic_failure_keeps_original_error_and_unvalidated_reader(accelerator, monkeypatch, change):
    tile, slots = fixture()
    if change == "ohlc":
        slots[0][0][3][1] = 1.0
    elif change == "negative":
        slots[0][0][3][4] = -1.0
    elif change == "unclosed":
        slots[0][0][3][8] = tile
    elif change == "order":
        slots[0].reverse()
    elif change == "revision":
        slots[0][1][0] = slots[0][0][0]
    else:
        slots = [[] for _ in range(32)]
    raw = frame(tile, 60000, slots)
    assert native.source_values(tile, 60000, *source_slot_json.data(raw[60:])) is None
    with pytest.raises(ValueError):
        codec.unpack(raw)
    with pytest.raises(ValueError):
        portable(raw, monkeypatch)
    if change in ("ohlc", "negative", "unclosed"):
        assert encode(codec.unpack(raw, validate=False)[3]) == encode(slots)


def test_source_capacity_fallback_uint_precision_escapes_references_and_retained_rows(
    accelerator, monkeypatch
):
    tile, slots = fixture()
    template = slots[0][0]
    slots = [[[r, template[1], template[2], template[3][:]] for r in range(8)] for _ in range(32)]
    raw = frame(tile, 60000, slots)
    body = source_slot_json.data(raw[60:])
    assert native.source_stats(tile, 60000, *body)[0] == 256
    extended = copy.deepcopy(slots)
    extended[0][0][1] += 1
    extended[0][0][2] += 1
    assert native.source_preserved(
        tile, 60000, body, source_slot_json.data(frame(tile, 60000, extended)[60:])
    )
    for _ in range(128):
        assert encode(native.source_values(tile, 60000, *body)) == encode(slots)
    too_many = copy.deepcopy(slots)
    too_many[0].append([8, template[1], template[2], template[3][:]])
    assert (
        native.source_values(tile, 60000, *source_slot_json.data(frame(tile, 60000, too_many)[60:])) is None
    )
    with pytest.raises(codec.Capacity):
        codec.pack(1, tile, 60000, too_many, _prefer_json=True)
    # Wide integer prices retain the original exact compatibility path.
    slots[0][0][3][:6] = [2**53 + 2] * 4 + [0, 0]
    raw = frame(tile, 60000, slots)
    assert native.source_values(tile, 60000, *source_slot_json.data(raw[60:])) is None
    assert encode(codec.unpack(raw)[3]) == encode(portable(raw, monkeypatch))
    # Equivalent JSON lexemes preserve signed floating zero and integer -0.
    offsets, expanded = body
    parts = [expanded[a:b] for a, b in zip(offsets, offsets[1:])]
    parts[0] = parts[0].replace(b"100,", b"100e0,", 1)
    offsets = tuple([0] + [sum(map(len, parts[:i])) for i in range(1, 33)])
    frozen = native.source_values(tile, 60000, offsets, b"".join(parts))
    assert frozen is not None
    assert encode(frozen[0]) == encode(json.loads(parts[0]))


def test_column_native_public_path_fresh_objects_input_lifetime_and_original_parity(accelerator, monkeypatch):
    _, slots = dense()
    raw = columns.pack(slots)
    assert raw is not None
    metadata, encoded, count = parts(raw)
    counts, descriptors = metadata
    kinds = [kind for kind, _ in descriptors]
    values = [json.loads(block) for block in encoded]
    original = accelerator.column_values
    calls = []

    def counted(*args):
        calls.append(args[-1])
        return original(*args)

    monkeypatch.setattr(accelerator, "column_values", counted)
    actual = columns.all_values(raw)
    assert calls == [count]
    with monkeypatch.context() as patch:
        patch.setattr(native, "column_values", lambda *args: None)
        assert encode(actual) == encode(columns.all_values(raw)) == encode(slots)
    refs = tuple(sys.getrefcount(value) for value in (counts, kinds, values))
    for _ in range(1000):
        fresh = original(counts, kinds, values, count)
        assert encode(fresh) == encode(slots)
        fresh[0][0][3][0] = -1
    assert tuple(sys.getrefcount(value) for value in (counts, kinds, values)) == refs
    invalid = copy.deepcopy(values)
    decimal = kinds.index("d")
    invalid[decimal][2] = 1 << count
    assert original(counts, kinds, invalid, count) is NotImplemented


def test_immutable_bytes_subclass_cache_retains_portable_source_reader_contract(accelerator):
    class ImmutableBytes(bytes):
        pass

    tile, slots = fixture()
    raw = frame(tile, 60000, slots)
    offsets, expanded = source_slot_json.data(raw[60:])
    assert native.source_values(tile, 60000, offsets, ImmutableBytes(expanded)) is None

    def cache(kind, key, value, read):
        return ImmutableBytes(read())

    reader = codec.Reader(raw, cache)
    assert reader.frozen is None
    for slot in range(32):
        assert encode(reader.items(slot)) == encode(slots[slot])
