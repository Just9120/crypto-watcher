import math
import struct
from contextlib import closing
from unittest.mock import patch

import pytest

from cryptowatcher.analytics import prior_population
from cryptowatcher.config import ConfigError
from cryptowatcher.detectors import DetectorUnavailable, atr14
from cryptowatcher.digest import aggregate, text
from cryptowatcher.events import History, identity
from cryptowatcher.sources import Candle
from cryptowatcher.storage import Store
from cryptowatcher.telegram_views import source_summary


def test_prior_population_matches_original_float_bits_with_gaps_and_lookbacks(store):
    candles = [
        Candle.parse(
            [i * 60000, 100, 102 + math.sin(i), 98 - math.cos(i), 100 + math.sin(i / 7), 1, 100],
            "1m",
            "fixture",
            (i + 1) * 60000,
            (i + 1) * 60000,
        )
        for i in range(500)
        if i not in (71, 72, 201)
    ]
    start, reference = 40 * 60000, 480 * 60000
    for needed in (16, 21, 256):
        with patch("cryptowatcher.analytics.read_candles", return_value=candles):
            _, actual, population = prior_population(
                store, "spot/BTCUSDT/1m", start, reference, needed, 60000
            )
        expected = []
        for i, candle in enumerate(candles):
            if not start <= candle.end < reference:
                continue
            try:
                value = 100 * atr14(candles[max(0, i - needed + 1) : i + 1]) / candle.close
            except DetectorUnavailable:
                continue
            expected.append((candle, i, value))
        assert [(c.start, i, struct.pack(">d", v)) for c, i, v in actual] == [
            (c.start, i, struct.pack(">d", v)) for c, i, v in expected
        ]
        assert population == sorted(v for _, _, v in expected)


def test_population_cache_is_one_entry_and_only_in_own_read_snapshot(store):
    with patch("cryptowatcher.analytics.read_candles", return_value=[]) as read:
        with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
            reader.db.execute("BEGIN")
            prior_population(reader, "spot/BTCUSDT/1m", 0, 60000, 16, 60000)
            prior_population(reader, "spot/BTCUSDT/1m", 0, 60000, 21, 60000)
            assert read.call_count == 1
            prior_population(reader, "spot/ETHUSDT/1m", 0, 60000, 16, 60000)
            prior_population(reader, "spot/BTCUSDT/1m", 0, 60000, 16, 60000)
            assert read.call_count == 3
        for _ in range(2):
            prior_population(store, "spot/BTCUSDT/1m", 0, 60000, 16, 60000)
        assert read.call_count == 5


def test_effective_settings_rejection_preserves_revision_and_old_settings_readable(store):
    before = store.snapshot()
    bad = before.settings.changed(watchlist=("spot/BTCUSDT",), watchlist_multiplier=0.25)
    for value in (
        bad,
        before.settings.with_parameter("turnover_lookback", 300),
        before.settings.with_parameter("volatility_lookback", 255),
    ):
        with pytest.raises(ConfigError):
            store.update(before.revision, value)
        assert store.snapshot() == before
    # Admission is stricter; immutable snapshots do not acquire new validation rules.
    from cryptowatcher.config import OwnerSettings

    assert OwnerSettings.from_payload(bad.payload()) == bad
    store.update(before.revision, before.settings.with_parameter("turnover_lookback", 255))


def test_digest_activity_cannot_be_displaced_by_five_stronger_singletons(store):
    h = History(store)
    start = 1800000000000
    with store.db:
        for i in range(25):
            h.version(
                "event",
                dict(
                    event_id=identity("activity/" + str(i)),
                    revision=1,
                    episode_id=identity("activity-episode/" + str(i)),
                    market_observation_ms=start + 1,
                    available_at_ms=start + 1,
                    market_type="spot",
                    detector_id="price",
                    symbol="BUSYUSDT" if i < 20 else "RARE" + str(i) + "USDT",
                    timeframe="1m",
                    direction="UP",
                    strength=1 if i < 20 else 100,
                    reference_price=100,
                    reference_time_ms=start,
                ),
            )
    result = aggregate(store, start, start + 60000, start + 60000)
    assert result["most_active_instruments"][0] == {"instrument": "spot/BUSYUSDT", "events": 20}
    assert len(result["strongest_episodes"]) == 5
    rendered = text(result)
    assert "BUSYUSDT" in rendered and "МСК" in rendered and "2027" in rendered
    assert "strength" not in rendered and " events" not in rendered


def test_recovery_summary_distinguishes_waiting_and_running(store):
    summary = source_summary(
        store.snapshot(), dict(at=100, active_streams=4, recovery_pending=0, recovery_working=1), 100
    )
    assert "ожидают: 0" in summary and "в работе: 1" in summary


def test_archived_intraday_csv_ndjson_and_cohorts_share_explicit_bounds(store):
    import csv
    import io
    import json

    from cryptowatcher.analytics import cohort_rows
    from cryptowatcher.export import Selection, csv_summary, ndjson
    from cryptowatcher.outcomes import Retention
    from tests.test_capacity import bounded
    from tests.test_outcomes import DAY, MINUTE, NOW, OutcomeWorker, Sources, future, observe

    bounded(store)
    worker = OutcomeWorker(store)
    observe(store, worker)
    Sources(store).put("spot/BTCUSDT/1m", future())
    worker.work(NOW + 61 * MINUTE, batch=200)
    selection = Selection(start_ms=NOW - MINUTE, end_ms=NOW + 1)
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        before = list(csv.DictReader(io.StringIO("".join(csv_summary(reader, selection)))))
    n = sum(int(r["sample_size"]) for r in before)
    assert n > 0
    for _ in range(100):
        Retention(store).work(NOW + 31 * DAY)
        if History(store).count("history:") == 0:
            break
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        after = list(csv.DictReader(io.StringIO("".join(csv_summary(reader, selection)))))
        exported = [json.loads(line) for line in ndjson(reader, selection)]
        grouped = list(cohort_rows(reader, start_ms=selection.start_ms, end_ms=selection.end_ms))
    assert sum(int(r["sample_size"]) for r in after) == sum(r["sample_size"] for r in grouped) == n
    manifest = exported[0]["selection_period"]
    assert manifest["archive_start_ms"] == selection.start_ms // DAY * DAY
    assert all(int(r["archive_start_ms"]) == manifest["archive_start_ms"] for r in after)
    assert manifest["requested_start_ms"] == selection.start_ms
