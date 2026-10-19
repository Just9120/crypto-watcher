import math
import struct
from contextlib import closing
from unittest.mock import patch

import pytest

from cryptowatcher import analytics
from cryptowatcher.analytics import prior_population
from cryptowatcher.detectors import DetectorUnavailable, atr14
from cryptowatcher.market_storage import enable
from cryptowatcher.sources import Candle, Sources
from cryptowatcher.storage import Store

MINUTE = 60_000


def bits(value):
    return (
        [c.start for c in value[0]],
        [(c.start, i, struct.pack(">d", v)) for c, i, v in value[1]],
        [struct.pack(">d", v) for v in value[2]],
    )


def original(candles, start, reference, needed):
    points = []
    for i, candle in enumerate(candles):
        if not start <= candle.end < reference:
            continue
        try:
            value = 100 * atr14(candles[max(0, i - needed + 1) : i + 1]) / candle.close
        except DetectorUnavailable:
            continue
        points.append((candle, i, value))
    return candles, points, sorted(v for _, _, v in points)


@pytest.mark.parametrize("width", [1, 15])
def test_snapshot_advance_matches_original_bits_gaps_and_cutoffs(store, width):
    step = width * MINUTE
    candles = [
        Candle.parse(
            [i * step, 100, 102 + math.sin(i), 98 - math.cos(i), 100 + math.sin(i / 7), 1, 400],
            str(width) + "m",
            "fixture",
            1000 * step,
            1000 * step,
        )
        for i in range(900)
        if i not in (280, 281, 301, 527)
    ]
    calls = []

    def read(_store, _native, left, right):
        calls.append((left, right))
        return [c for c in candles if left <= c.start < right]

    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        reader.db.execute("BEGIN")
        with patch.object(analytics, "read_candles", side_effect=read):
            for begin, end, needed in [
                (260, 500, 16),
                (262, 502, 21),
                (300, 540, 21),
                (305, 545, 256),
                (300, 540, 21),
                (600, 840, 21),
            ]:
                start, reference = begin * step, end * step
                actual = prior_population(
                    reader, "spot/FIXTUREUSDT/" + str(width) + "m", start, reference, needed, step
                )
                source = [
                    c for c in candles if max(0, start - max(256, needed) * step) <= c.start < reference
                ]
                assert bits(actual) == bits(original(source, start, reference, needed))
            assert calls[:2] == [(4 * step, 500 * step), (500 * step, 502 * step)]
            assert calls[4] == (44 * step, 540 * step)  # Backwards cutoff requires a full read.
            assert calls[5] == (344 * step, 840 * step)  # Large jump also keeps the original path.


@pytest.mark.parametrize("bounded", [False, True])
def test_advance_never_hides_recovery_after_snapshot_ends(store, bounded):
    if bounded:
        enable(store.db)
        store.bounded = True
    candles = [
        Candle.parse(
            [i * MINUTE, 100, 103, 98, 100 + (i % 13) / 100, 1, 400],
            "1m",
            "fixture",
            600 * MINUTE,
            600 * MINUTE,
        )
        for i in range(600)
    ]
    source = Sources(store)
    source.put("spot/FIXTUREUSDT/1m", candles)
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        reader.db.execute("BEGIN")
        first = prior_population(reader, "spot/FIXTUREUSDT/1m", 260 * MINUTE, 500 * MINUTE, 21, MINUTE)
        replacement = Candle.parse(
            [300 * MINUTE, 100, 103, 98, 102.75, 1, 400], "1m", "fixture", 600 * MINUTE, 600 * MINUTE
        )
        source.put("spot/FIXTUREUSDT/1m", [replacement], recovery=True)
        same = prior_population(reader, "spot/FIXTUREUSDT/1m", 262 * MINUTE, 502 * MINUTE, 21, MINUTE)
        assert next(c for c in same[0] if c.start == replacement.start).close != replacement.close
        reader.db.rollback()
        reader.db.execute("BEGIN")
        later = prior_population(reader, "spot/FIXTUREUSDT/1m", 264 * MINUTE, 504 * MINUTE, 21, MINUTE)
        assert next(c for c in later[0] if c.start == replacement.start) == replacement
        golden = original(
            analytics.read_candles(reader, "spot/FIXTUREUSDT/1m", 8 * MINUTE, 504 * MINUTE),
            264 * MINUTE,
            504 * MINUTE,
            21,
        )
        assert bits(later) == bits(golden)
        assert first[1]
