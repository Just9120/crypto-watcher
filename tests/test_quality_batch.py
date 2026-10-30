import time

import pytest

from cryptowatcher import numeric_pool, quality_frames, quality_packets
from cryptowatcher.events import PREFIX, History
from cryptowatcher.market_storage import enable
from cryptowatcher.quality_layout import put
from cryptowatcher.quality_summary import archive_facts, prepare_event_facts
from cryptowatcher.sources import Sources
from cryptowatcher.storage import encode
from tests.test_bounded_storage import bounded
from tests.test_outcomes import MINUTE, NOW, OutcomeWorker, future, observe
from tests.test_quality_packets import fact, metric, save


@pytest.mark.parametrize("framed", [False, True])
@pytest.mark.parametrize("count,extra", [(1, 8), (7, 9), (64, 32), (120, 8)])
def test_bulk_append_preserves_order_prefix_ids_leases_and_frame_gc(store, count, extra, framed):
    enable(store.db)
    values = [fact(i) for i in range(count)]
    added = [fact(10000 + i, [metric(100000 + i * 20 + j) for j in range(20)]) for i in range(extra)]
    expected = encode([*values, *added])
    with store.db:
        raw = save(store, values)
    if framed:
        quality_frames.install(store.db)
        source = store.db.execute("SELECT day,cohort,chunk,facts FROM market_quality_blocks").fetchone()
        prepared = quality_frames.prepare(store.db, source)
        assert prepared is not None
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            quality_frames.publish(store.db, [prepared])
        raw = bytes(store.db.execute("SELECT facts FROM market_quality").fetchone()[0])
    prefix = quality_packets._append_rows(store.db, raw)
    owners = set(quality_packets.ownership(store.db, raw)["numeric"])
    numbers = numeric_pool.raw_many(store.db, owners)
    with store.db:
        result = quality_packets.append_many(store.db, raw, added, "cohort", expected_count=count)
        assert quality_packets._append_rows(store.db, result)[:count] == prefix
        put(
            store.db,
            (20000, "cohort", 0, 20366, encode({"detector": "price"}), "{}", result, count + extra),
        )
    assert encode([*values, *added]) == expected
    assert encode(quality_packets.unpack(store.db, result)) == expected
    assert numeric_pool.raw_many(store.db, owners) == numbers
    if framed:
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            quality_frames.collect(store.db, time.monotonic() + 5)
        assert numeric_pool.raw_many(store.db, owners) == numbers
        assert encode(quality_packets.unpack(store.db, result)) == expected
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()


def test_bulk_append_keeps_unknown_metric_types_and_incomplete_method_metadata(store):
    enable(store.db)
    values = [fact(i) for i in range(8)]
    added = [fact(30, []), fact(31, [{"future_metric": -0.0, "integer": 2**300}])]
    added[0].update(state="EXPIRED_RETENTION_INCOMPLETE", previous_state="PENDING")
    added[0]["comparator_sets"] = [
        {"methodology_version": "old", "offset": 0, "count": 0, "limitations": ["MISSING_WINDOW"]}
    ]
    with store.db:
        raw = save(store, values)
        result = quality_packets.append_many(store.db, raw, added, "cohort", expected_count=8)
    assert encode(quality_packets.unpack(store.db, result)) == encode([*values, *added])


@pytest.mark.parametrize("extra,expected", [([], 8), ([fact(20)] * 121, 8), ([fact(20)], 7)])
def test_bulk_append_rejects_empty_oversized_or_stale_count_before_interning(store, extra, expected):
    enable(store.db)
    with store.db:
        raw = save(store, [fact(i) for i in range(8)])
    before = list(store.db.iterdump())
    with pytest.raises(ValueError):
        quality_packets.append_many(store.db, raw, extra, "cohort", expected_count=expected)
    assert list(store.db.iterdump()) == before


def test_preparing_archive_facts_does_not_write_or_mutate_frozen_inputs(store):
    bounded(store)
    worker = OutcomeWorker(store)
    observe(store, worker)
    Sources(store).put("spot/BTCUSDT/1m", future())
    worker.work(NOW + 6 * MINUTE)
    key, _ = next(store.scan_runtime(PREFIX + "event-head:"))
    event = History(store).metadata(key[len(PREFIX) :])
    frozen = encode(event)
    before = list(store.db.iterdump())
    changes = store.db.total_changes
    prepared = prepare_event_facts(store, event)
    assert prepared
    assert {group["horizon_min"] for _, group, _, _ in prepared} == set(
        event["settings_snapshot"]["outcome_horizons"]
    )
    for _, group, provenance, _ in prepared:
        assert group["settings"] == event["settings_revision"]
        assert encode(provenance["membership"]["settings_snapshot"]) == encode(event["settings_snapshot"])
        assert provenance["origin"]["settings_revision"] == event["settings_revision"]
    assert store.db.total_changes == changes
    assert list(store.db.iterdump()) == before and encode(event) == frozen


def test_byte_full_partial_chunk_is_preserved_without_speculative_writes(store):
    bounded(store)
    values = [fact(0), fact(1)]
    for value in values:
        value["limitations"] = ["x" * 600000]
    day, group, provenance = 20000, {"detector": "price"}, {}
    with store.db:
        archive_facts(store, [(day, group, provenance, values[0])])
    raw = bytes(store.db.execute("SELECT facts FROM market_quality").fetchone()[0])
    before = list(store.db.iterdump())
    with pytest.raises(quality_packets.AppendSizeError):
        quality_packets.append_many(store.db, raw, [values[1]], expected_count=1)
    assert list(store.db.iterdump()) == before
    with store.db:
        archive_facts(store, [(day, group, provenance, values[1])])
    rows = store.db.execute("SELECT chunk,n,facts FROM market_quality ORDER BY chunk").fetchall()
    assert [(chunk, n) for chunk, n, _ in rows] == [(0, 1), (1, 1)]
    assert encode([quality_packets.unpack(store.db, bytes(row[2]))[0] for row in rows]) == encode(values)
