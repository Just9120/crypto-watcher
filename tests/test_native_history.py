import json
import math
import random
import struct
import sys

import pytest

from cryptowatcher.history_codec import Codec
from cryptowatcher.history_rebuild import _CopyCodec
from cryptowatcher.native_history import load
from cryptowatcher.storage import StateError, encode

from .test_history_codec import large_record


@pytest.fixture
def accelerator():
    if sys.platform != "linux":
        pytest.skip("Linux native artifact is built and REQUIRED in CI/image checks")
    return load("native")


def test_native_request_cannot_silently_select_python_when_artifact_missing(monkeypatch):
    from cryptowatcher import native_history

    monkeypatch.setattr(native_history.Path, "is_file", lambda path: False)
    assert load("auto") is None and load("python") is None
    with pytest.raises(StateError, match="unavailable"):
        load("native")


def test_native_canonical_gate_matches_python_for_float_bits_and_rejected_inputs(accelerator):
    rng = random.Random(310012)
    cases = []
    for _ in range(1000):
        number = struct.unpack("!d", rng.getrandbits(64).to_bytes(8, "big"))[0]
        if math.isfinite(number):
            cases.append(encode({"n": number, "zero": -0.0}).encode())
    cases += [encode(v).encode() for v in (None, True, False, 2**100 + 123, [1, 1.0], {"a\0b": "🪙"})]
    cases += [
        b'{"z":1,"a":2}',
        b'{"a":1,"a":1}',
        b'{"x":1.00}',
        b'{"x":1e0}',
        b'{"x":-0}',
        b'{ "x":1}',
        b'{"x":1E+20}',
        b'{"x":1e309}',
        b'{"x":1e-1000}',
        b"NaN",
        b'{"a":1,}',
        b'"\\u0446"',
        b'"a\\/b"',
        b"\xff",
    ]
    for raw in cases:
        try:
            value = json.loads(raw)
            expected = encode(value).encode() == raw
            count = len(value) if isinstance(value, list) else None
        except (ValueError, TypeError, UnicodeError):
            expected, count = False, None
        result = accelerator.canonical(raw)
        assert result is not None and result[0] == expected
        if expected:
            assert result[1] == count


@pytest.mark.parametrize("kind", ["event", "outcome", "episode", "comparison"])
def test_native_copy_preserves_full_canonical_values_and_mixed_formats(store, accelerator, kind):
    value = large_record(kind)
    value.update(strength=2.095370825591371, signed_zero=-0.0, huge=2**100 + 123)
    value["samples"] = [{"source_inputs": value["source_inputs"]}]
    copier = _CopyCodec(store.db, accelerator)
    with store.db:
        packed = copier.pack_canonical(encode(value))
    assert copier.canonical_json(packed) == Codec(store.db).canonical_json(packed) == encode(value).encode()
    with store.db:
        mixed = copier.pack_canonical(encode(packed))
    assert copier.canonical_json(mixed) == encode(value).encode()


def test_native_unsupported_depth_keeps_python_canonical_gate(store, accelerator):
    raw = b'{"a":0}'
    for _ in range(250):
        raw = b"[" + raw + b"]"
    assert accelerator.canonical(raw) is None
    assert _CopyCodec(store.db, accelerator).canonical(raw) == Codec(store.db).canonical(raw)
