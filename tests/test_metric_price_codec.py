import random

import pytest

from cryptowatcher import metric_codec
from cryptowatcher import metric_price_codec as codec
from cryptowatcher.compact_json import integer
from cryptowatcher.outcomes import calculate
from cryptowatcher.sources import Candle
from cryptowatcher.storage import encode


def outcome(prices, direction):
    reference, close, high, low = prices
    now = 1800000000000
    candle = Candle.parse(
        [now, reference, high, low, close, 1, 200], "1m", "fixture", now + 60000, now + 60000
    )
    return calculate(
        {"reference_time_ms": now, "reference_price": reference, "direction": direction}, 1, [candle]
    )["metrics"]


def test_frozen_price_frame_golden_and_exact_current_outcome_float_bits():
    value = outcome([100.0, 101.0, 102.0, 99.0], "UP")
    original = metric_codec.pack(value)
    raw = codec.encode(original, [100.0, 101.0, 102.0, 99.0])
    assert raw == bytes.fromhex("1101e807142813")
    assert codec.frame(raw).hex() == "01003ff00000000000044000000000000004bff0000000000004"
    assert codec.frame(raw) == original
    assert encode(codec.unpack(raw)) == encode(value)
    assert codec.frame(original) == original


def test_decimal_operands_preserve_every_current_direction_and_canonical_numeric_view():
    rng = random.Random(4502)
    for _ in range(300):
        reference = rng.randrange(1000000, 10000000)
        close = reference + rng.randrange(-2000, 2001)
        high = max(reference, close) + rng.randrange(1, 2000)
        low = min(reference, close) - rng.randrange(1, 2000)
        exponent = rng.choice((-1, -2, -7))
        prices = [float(str(n) + "e" + str(exponent)) for n in (reference, close, high, low)]
        for direction in ("UP", "DOWN", "NEUTRAL"):
            value = outcome(prices, direction)
            body, view = metric_codec.canonical(value)
            original = metric_codec.pack(body)
            raw = codec.encode(original, prices)
            assert raw is not None and len(raw) < len(original)
            assert codec.frame(raw) == original
            assert encode(metric_codec.restore_view(codec.unpack(raw), view)) == encode(value)


def test_integer_zero_flags_negative_zero_and_unsupported_values_never_normalize():
    values = [
        dict(zip(metric_codec.DIRECTED, (a, a, b, c)))
        for a, b, c in (
            (0.0, 0, 0),
            (0, 0, 0),
            (0.0, -0.0, -0.0),
            (-0.0, 0, -0.0),
            (1.0, 2.0, -1.0),
        )
    ]
    accepted = []
    for value in values:
        original = metric_codec.pack(value)
        raw = codec.encode(original, [100.0] * 4)
        if raw is not None:
            accepted.append(value)
            assert codec.frame(raw) == original
            assert encode(codec.unpack(raw)) == encode(value)
    assert accepted == [values[0]]


@pytest.mark.parametrize(
    "prices",
    [
        None,
        [],
        [100, 100, 100],
        [100, True, 100, 100],
        [100, "100", 100, 100],
        [0, 100, 100, 100],
        [-1, 100, 100, 100],
        [float("nan"), 100, 100, 100],
        [float("inf"), 100, 100, 100],
        [2**10000, 100, 100, 100],
        [5e-324, 1e308, 1e308, 5e-324],
        [1e308, 1e-300, 1e308, 1e-300],
    ],
)
def test_unsupported_operands_fall_back_without_changing_the_original_frame(prices):
    original = metric_codec.pack(outcome([100.0, 101.0, 102.0, 99.0], "UP"))
    assert codec.encode(original, prices) is None
    assert codec.frame(original) == original


def test_frames_are_self_contained_after_input_objects_are_discarded():
    prices = [100.0, 101.0, 102.0, 99.0]
    original = metric_codec.pack(outcome(prices, "UP"))
    raw = codec.encode(original, prices)
    prices[:] = [0, 0, 0, 0]
    assert codec.frame(raw) == original


def test_malformed_truncated_noncanonical_overflow_and_nonfinite_recipe_bytes_fail():
    raw = bytes.fromhex("1101e807142813")
    invalid = [
        b"",
        raw[:-1],
        raw + b"\x00",
        b"\x10" + raw[1:],
        b"\x11\x80\x00\x01\x00\x00\x00",
        b"\x11" + b"\xff" * 10 + b"\x00" * 4,
        b"\x11" + integer(codec.zig(-325)) + b"\x01\x00\x00\x00",
        b"\x11\x00\x00\x00\x00\x00",
        b"\x11\x00\x01\x01\x00\x00",
        b"\x11" + integer(codec.zig(308)) + b"\x02\x00\x00\x00",
        b"\x11" + integer(codec.zig(-324)) + b"\x01\x00" + integer(2**64 - 2) + b"\x00",
    ]
    for candidate in invalid:
        with pytest.raises(ValueError):
            codec.unpack(candidate)
    with pytest.raises(ValueError):
        codec.frame(b"\x01\xff" + b"\x00" * 24)


def test_nb2_candidate_is_explicit_and_nb1_bytes_and_fixed_slot_contract_remain_frozen():
    from cryptowatcher import numeric_block_codec as blocks

    original = metric_codec.pack(outcome([100.0, 101.0, 102.0, 99.0], "UP"))
    operands = codec.encode(original, [100.0, 101.0, 102.0, 99.0])
    frames = [b""] * 64
    frames[0] = original
    frames[63] = original
    nb1 = blocks.pack(87, frames)
    assert nb1 == blocks.pack(87, frames, recipes=True)
    assert nb1[:4] == b"NB1\x00"
    frames[63] = operands
    with pytest.raises(ValueError, match="explicit NB2"):
        blocks.pack(87, frames)
    nb2 = blocks.pack(87, frames, recipes=True)
    assert nb2[:4] == b"NB2\x00"
    restored = blocks.unpack(nb2, 87)
    assert restored == frames
    assert codec.frame(restored[63]) == original
    assert blocks.unpack(nb1, 87)[63] == original
    # A different magic may not silently widen frozen NB1 length semantics.
    disguised = blocks.seal(b"NB1\x00", blocks.header(nb2))
    with pytest.raises(ValueError):
        blocks.unpack(disguised, 87, validate=False)
    with pytest.raises(ValueError):
        blocks.unpack(nb2[:-1] + bytes((nb2[-1] ^ 1,)), 87)
    with pytest.raises(ValueError):
        blocks.unpack(nb2, 88)
