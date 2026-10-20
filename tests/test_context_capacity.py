"""Context capacity follows the actual selector and retention, not candle priors."""

import pytest

from cryptowatcher import context_tile_codec, context_tiles, source_context
from cryptowatcher.market_storage import DAY, context_window, enable
from cryptowatcher.storage import encode
from cryptowatcher.storage_lifecycle import BoundedRetention
from tests.test_outcomes import NOW

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
            # This is the production direction: observation <= selector cutoff.
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
