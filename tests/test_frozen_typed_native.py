"""Frozen CJ3 bytes/types/failures against the original independent reader/writer."""

import math
import random
import struct
import sys

import pytest

from cryptowatcher import compact_json as codec
from cryptowatcher import frozen_codec_native as native


@pytest.fixture
def accelerator():
    module = native.load()
    if module is None:
        assert sys.platform != "linux", "Build the canonical native helper before Linux tests"
        pytest.skip("Actual native frozen operation artifact required")
    return module


def portable_pack(value, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(native, "typed_pack", lambda *args: None)
        return codec.pack(value)


def portable_unpack(raw, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(native, "typed_unpack", lambda *args: NotImplemented)
        return codec.unpack(raw, max(1, len(raw)))


def exact(actual, expected):
    assert type(actual) is type(expected)
    if type(actual) is float:
        assert struct.pack(">d", actual) == struct.pack(">d", expected)
    elif type(actual) is dict:
        assert list(actual) == list(expected)
        for key in expected:
            exact(actual[key], expected[key])
    elif type(actual) is list:
        assert len(actual) == len(expected)
        for left, right in zip(actual, expected, strict=True):
            exact(left, right)
    else:
        assert actual == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        False,
        True,
        0,
        -1,
        -(2**63),
        2**63 - 1,
        2**63,
        -(2**8000),
        0.0,
        -0.0,
        sys.float_info.max,
        5e-324,
        "",
        "🚨 Цена\0𝄞 пробой",
        "c707340a-4e24-494f-a16b-ca2179e7c375",
        "1a" * 32,
        "C707340a-4e24-494f-a16b-ca2179e7c375",
        "g" * 64,
        [],
        {},
        [{"state": "COMPLETE", "value": float(i), "start": i} for i in range(20)],
        [{"b": i, "a": i + 1} if i != 3 else {"a": i, "b": i + 1} for i in range(6)],
        [[{"a": [i, -0.0, None]} for i in range(5)]] * 2,
        {word: word for word in codec.WORDS},
    ],
)
def test_exact_frozen_bytes_values_and_type_bits(accelerator, monkeypatch, value):
    raw = portable_pack(value, monkeypatch)
    assert codec.pack(value) == raw
    direct = accelerator.typed_pack(value, codec.WORD_IDS)
    assert direct is NotImplemented or direct == raw
    expected = portable_unpack(raw, monkeypatch)
    exact(codec.unpack(raw, len(raw)), expected)
    direct = accelerator.typed_unpack(raw, codec.WORDS)
    if direct is not NotImplemented:
        exact(direct, expected)


def test_random_typed_trees_and_real_column_layout(accelerator, monkeypatch):
    rng = random.Random(25102026)

    def tree(depth):
        choices = 6 if depth > 4 else 8
        tag = rng.randrange(choices)
        if tag == 0:
            return rng.choice([None, False, True])
        if tag == 1:
            return rng.randrange(-(2**63), 2**63)
        if tag == 2:
            return math.ldexp(rng.uniform(-1, 1), rng.randrange(-1000, 1000))
        if tag == 3:
            return rng.choice(codec.WORDS)
        if tag == 4:
            return "".join(rng.choices("Цена 🚨\0א𝄞", k=rng.randrange(32)))
        if tag == 5:
            return rng.randbytes(32).hex()
        if tag == 6:
            return [tree(depth + 1) for _ in range(rng.randrange(8))]
        return {key: tree(depth + 1) for key in rng.sample(["state", "a", "price", "🚨"], rng.randrange(5))}

    for _ in range(1000):
        value = tree(0)
        raw = portable_pack(value, monkeypatch)
        assert accelerator.typed_pack(value, codec.WORD_IDS) == raw
        exact(accelerator.typed_unpack(raw, codec.WORDS), portable_unpack(raw, monkeypatch))
    rows = [{"state": "COMPLETE", "price": -0.0, "start": i} for i in range(20)]
    raw = portable_pack(rows, monkeypatch)
    assert raw[0] == 11
    assert accelerator.typed_pack(rows, codec.WORD_IDS) == raw
    first = accelerator.typed_unpack(raw, codec.WORDS)
    first[0]["start"] = -999
    assert accelerator.typed_unpack(raw, codec.WORDS)[0]["start"] == 0


def test_mutated_wire_and_nonminimal_varints_preserve_portable_decisions(accelerator, monkeypatch):
    rng = random.Random(2592)
    seeds = [
        portable_pack([{word: i, "price": float(i)} for i in range(5)], monkeypatch)
        for word in ("start", "state", "comparator_metrics")
    ]
    malformed = [
        b"",
        b"\x03",
        b"\x03" + b"\xff" * 1171,
        b"\x05\x01\xff",
        b"\x08\xff\xff\x7f",
        b"\x04" + struct.pack(">d", float("nan")),
        b"\x0b\x04\x06\x01\x03\x00\x06\x04\0\0\0\0",
        b"\x07\x02\x05\x01a\0\x05\x01a\0",
        (b"\x06" + codec.integer(1_000_000)) * 65,
    ]
    for _ in range(1500):
        raw = bytearray(rng.choice(seeds))
        if rng.randrange(2):
            raw[rng.randrange(len(raw))] = rng.randrange(256)
        else:
            del raw[rng.randrange(len(raw)) :]
        malformed.append(bytes(raw))
    for raw in malformed:
        try:
            expected = portable_unpack(raw, monkeypatch)
        except (ValueError, UnicodeError) as error:
            with pytest.raises(type(error)):
                codec.unpack(raw, max(1, len(raw)))
            assert accelerator.typed_unpack(raw, codec.WORDS) is NotImplemented
        else:
            exact(codec.unpack(raw, len(raw)), expected)
    # CJ3 accepts redundant zero groups; NR1/NR2 lease varints do not.
    for raw in (b"\x03\x80\x00", b"\x03" + b"\x80" * 12 + b"\x00", b"\x06\x80\x00"):
        exact(codec.unpack(raw, len(raw)), portable_unpack(raw, monkeypatch))


def test_limits_invalid_values_subclasses_and_references(accelerator, monkeypatch):
    for value in (float("inf"), float("nan"), 1 << 8191, {2: "bad"}, object(), "\ud800"):
        assert accelerator.typed_pack(value, codec.WORD_IDS) is NotImplemented
        with pytest.raises((ValueError, UnicodeError)):
            codec.pack(value)
    deep = None
    for _ in range(65):
        deep = [deep]
    with pytest.raises(ValueError, match="bound"):
        codec.pack(deep)
    cycle = []
    cycle.extend([{"column": cycle}] * 4)
    refs = sys.getrefcount(cycle)
    assert accelerator.typed_pack(cycle, codec.WORD_IDS) is NotImplemented
    with pytest.raises(ValueError, match="bound"):
        codec.pack(cycle)
    assert sys.getrefcount(cycle) == refs

    class Number(int):
        pass

    assert accelerator.typed_pack(Number(2), codec.WORD_IDS) is NotImplemented
    assert codec.pack(Number(2)) == portable_pack(2, monkeypatch)
    raw = portable_pack({"source": [{"start": i, "price": -0.0} for i in range(20)]}, monkeypatch)
    value = portable_unpack(raw, monkeypatch)
    refs = (sys.getrefcount(value), sys.getrefcount(raw), sys.getrefcount(codec.WORDS))
    for _ in range(1000):
        assert accelerator.typed_pack(value, codec.WORD_IDS) == raw
        accelerator.typed_unpack(raw, codec.WORDS)
    assert (sys.getrefcount(value), sys.getrefcount(raw), sys.getrefcount(codec.WORDS)) == refs
    # The native size bound selects the full reader/writer; it narrows no contract.
    large = "x" * (4 * 1024 * 1024)
    assert accelerator.typed_pack(large, codec.WORD_IDS) is NotImplemented
    raw = codec.pack(large)
    assert accelerator.typed_unpack(raw, codec.WORDS) is NotImplemented
    assert codec.unpack(raw, len(raw)) == large
    with pytest.raises(ValueError, match="size"):
        codec.unpack(raw, len(raw) - 1)
