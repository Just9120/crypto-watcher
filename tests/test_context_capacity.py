"""Context capacity follows the actual selector and retention, not candle priors."""

import pytest

from cryptowatcher import context_tile_codec, context_tiles, source_context
from cryptowatcher.events import History, identity
from cryptowatcher.market_storage import DAY, context_window, enable, stream_id
from cryptowatcher.storage import encode
from cryptowatcher.storage_lifecycle import BoundedRetention
from cryptowatcher.storage_meter import report, sample
from tests.test_bounded_storage import bounded
from tests.test_outcomes import NOW, OutcomeWorker, observe

START = NOW // DAY * DAY
KINDS = ("ticker", "instrument")
INSTRUMENT = "spot/BTCUSDT"


def prepare(store, *, gap=False, cold=False, exceptions=False):
    enable(store.db)
    store.bounded = True
    store.clock_ms = lambda: START + 80 * DAY
    days = [day for day in range(80) if not gap or day <= 20 or day >= 65]
    originals = {}
    with store.db:
        for day in days:
            originals[day] = {}
            for kind in KINDS:
                value = {
                    "available_at_ms": START + day * DAY,
                    "market_observation_ms": START + day * DAY,
                    "values": {"kind": kind, "day": day, "zero": -0.0},
                }
                source_context.save(store, kind, INSTRUMENT, value)
                originals[day][kind] = value
        for day in range(80):
            cutoff = START + day * DAY + DAY - 1
            selected = source_context.context(store, INSTRUMENT, cutoff)
            # Event snapshots only. Comparator samples use older cutoffs but
            # retain dependencies against the newer parent event anchor.
            context_window(store.db, INSTRUMENT + "/1m", selected, cutoff - 20_000)
        if exceptions:
            # Older supplied snapshots are a valid compatibility surface. A
            # latest-selector capacity estimate cannot silently discard them.
            for day in (0, 1):
                context_window(store.db, INSTRUMENT + "/1m", originals[day], START + 79 * DAY)
    if cold:
        context_tiles.install(store.db)
        keys = list(store.db.execute("SELECT stream,kind,at FROM market_context ORDER BY stream,kind,at"))
        for key in keys:
            with store.db:
                store.db.execute("BEGIN IMMEDIATE")
                context_tiles.publish(store.db, [tuple(key)])
    return days, originals


def expire(store):
    retention = BoundedRetention(store)
    for _ in range(400):
        if not retention.work(START + 80 * DAY, batch=17):
            return
    pytest.fail("Bounded context retention did not drain")


def remaining(store):
    result = {kind: {} for kind in KINDS}
    for row in store.db.execute(
        "SELECT stream,kind,at,body,timing,needed_anchor,purge_after,integrity FROM market_context"
    ):
        point = context_tiles.hot_point(store.db, row)
        result[point.kind][point.at] = context_tile_codec.public(point)
    if context_tiles.installed(store.db):
        for row in store.db.execute("SELECT " + context_tiles.FIELDS + " FROM market_context_tiles"):
            for point in context_tile_codec.points(context_tiles._tile(row)):
                assert point.at not in result[point.kind]
                result[point.kind][point.at] = context_tile_codec.public(point)
    return result


@pytest.mark.parametrize("cold", [False, True])
@pytest.mark.parametrize("gap", [False, True])
def test_latest_selector_has_only_one_old_predecessor_per_kind_after_retention(store, cold, gap):
    days, originals = prepare(store, cold=cold, gap=gap)
    expire(store)
    retained = remaining(store)
    raw_days = {day for day in days if day > 45}
    expected_days = raw_days | ({20} if gap else set())
    for kind in KINDS:
        assert set(retained[kind]) == {START + day * DAY for day in expected_days}
        assert len([at for at in retained[kind] if at <= START + 45 * DAY]) == int(gap)
        for day in expected_days:
            assert encode(retained[kind][START + day * DAY]) == encode(originals[day][kind])
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("cold", [False, True])
def test_older_legacy_dependencies_remain_additional_to_latest_selector_model(store, cold):
    _, originals = prepare(store, cold=cold, exceptions=True)
    expire(store)
    retained = remaining(store)
    for kind in KINDS:
        extra = [at for at in retained[kind] if at <= START + 45 * DAY]
        assert set(extra) == {START, START + DAY}
        for day in (0, 1):
            assert encode(retained[kind][START + day * DAY]) == encode(originals[day][kind])


@pytest.mark.parametrize("cold", [False, True])
@pytest.mark.parametrize("lookback_days", [20, 28])
def test_normal_comparison_keeps_historical_context_beyond_raw35_days(store, cold, lookback_days):
    bounded(store)
    observe(store, OutcomeWorker(store))
    settings = store.snapshot()
    history = History(store)
    event = history.events(1)[0]
    anchor = event["market_observation_ms"]
    old_at = anchor - lookback_days * DAY
    sample(store, anchor - DAY)
    old = {
        "available_at_ms": old_at,
        "market_observation_ms": old_at,
        "values": {"turnover24h": "1234567.890123456789", "zero": -0.0},
    }
    instrument = event["market_type"] + "/" + event["symbol"]
    with store.db:
        source_context.save(store, "ticker", instrument, old)
        source_context.save(
            store, "ticker", instrument, {**old, "available_at_ms": anchor, "market_observation_ms": anchor}
        )
    historical = source_context.context(store, instrument, old_at)
    assert historical["ticker"] == old
    comparison = {
        "schema": "cryptowatcher.comparison.v1",
        "comparison_id": identity(f"comparison/{event['event_id']}/1"),
        "event_id": event["event_id"],
        "event_revision": event["revision"],
        "methodology_version": 1,
        "revision": 1,
        "available_at_ms": anchor,
        "state": "COMPLETE_LIMITED",
        "sample_size": 1,
        "limitations": ["LOW_N"],
        "samples": [{"reference_time_ms": old_at, "source_context": historical}],
    }
    with store.db:
        history.version("comparison", comparison)
    key = (stream_id(store.db, instrument), "ticker", old_at)
    lease = store.db.execute(
        "SELECT needed_anchor,purge_after,integrity FROM market_context WHERE stream=? AND kind=? AND at=?",
        key,
    ).fetchone()
    assert lease[0] == anchor and lease[1] == anchor + 30 * DAY and lease[2] is not None
    assert lease[1] - old_at == (lookback_days + 30) * DAY
    if cold:
        context_tiles.install(store.db)
        with store.db:
            context_tiles.publish(store.db, [key])
    now = old_at + 36 * DAY
    assert now < anchor + 30 * DAY  # Parent event is still within its detail retention.
    BoundedRetention(store).work(now)
    expected = encode(comparison)
    assert encode(history.get(f"comparison:{comparison['comparison_id']}:0000000001")) == expected
    assert encode(source_context.context(store, instrument, old_at)["ticker"]) == encode(old)
    before = store.db.total_changes
    measured = report(store, now)
    assert store.db.total_changes == before and store.snapshot() == settings
    context_rows = [
        row
        for row in measured["forecast"]
        if row["category"] in ("context", "context_bodies", "context_integrity", "context_tiles")
    ]
    assert context_rows and all(row["retention_days_for_estimate"] == 58 for row in context_rows)
    assert measured["context_dependency_retention_estimate"]["days_for_estimate"] == 58
    assert measured["context_dependency_retention_estimate"]["limitations"]
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()
