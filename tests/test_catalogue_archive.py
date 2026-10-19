import json
import sqlite3

import pytest

from cryptowatcher import catalogue_aliases, catalogue_lifecycle, record_catalog
from cryptowatcher.analytics import ComparisonWorker
from cryptowatcher.events import PREFIX, History
from cryptowatcher.expiry_index import analytic_heads, hot_binding
from cryptowatcher.outcomes import DAY, OutcomeWorker
from cryptowatcher.quality_packets import unpack
from cryptowatcher.quality_summary import archive_event
from cryptowatcher.record_blocks import Coordinate
from cryptowatcher.sources import Sources
from cryptowatcher.storage import encode
from cryptowatcher.storage_lifecycle import BoundedRetention, forget
from tests.test_analytics import series
from tests.test_bounded_storage import bounded
from tests.test_outcomes import MINUTE, NOW, future, observe


def public_archive(store):
    history = History(store)
    return sorted(
        encode(
            [
                day,
                json.loads(dimensions),
                history.codec.unpack(json.loads(provenance)),
                unpack(store.db, bytes(raw)),
            ]
        )
        for day, dimensions, provenance, raw in store.db.execute(
            "SELECT day,dimensions,provenance,facts FROM market_quality"
        )
    )


@pytest.mark.parametrize("full_expiry", [False, True])
def test_mixed_analytic_heads_archive_all_numeric_and_comparator_facts_exactly_and_rollback(
    store, full_expiry
):
    bounded(store)
    series(store, days=2)
    worker = OutcomeWorker(store)
    observe(store, worker)
    Sources(store).put("spot/BTCUSDT/1m", future(), recovery=True)
    worker.work(NOW + 6 * MINUTE)
    history = History(store)
    event = history.events(1)[0]
    comparison = ComparisonWorker(store)
    comparison.requeue(event["event_id"], NOW + 7 * MINUTE)
    for _ in range(20):
        comparison.work(NOW + 8 * MINUTE)
        job = history.get("comparison-job:" + event["event_id"])
        if job is None or job["state"] != "PENDING":
            break
    before = analytic_heads(store.db, event["event_id"], store.cached_text)
    assert {category for _, category in before} >= {"outcome-head", "outcome-job", "comparison-head"}
    assert any(
        history.metadata(key[len(PREFIX) :]).get("sample_size", 0) > 0
        for key, category in before
        if category == "comparison-head"
    )
    with store.db:
        store.db.execute("SAVEPOINT archive_baseline")
        events = (
            sorted(history.events(100), key=lambda value: (value["market_observation_ms"], value["event_id"]))
            if full_expiry
            else [event]
        )
        for value in events:
            archive_event(store, value)
        expected = public_archive(store)
        assert expected
        store.db.execute("ROLLBACK TO archive_baseline")
        store.db.execute("RELEASE archive_baseline")
    record_catalog.install(store.db, store.cached_text, store.cached_bytes)
    moved = []
    with store.db:
        for key, category in before:
            if category not in ("outcome-head", "comparison-head"):
                continue
            projection = store.get_runtime(key)
            revision_key = PREFIX + projection["revision_key"]
            body = store.get_runtime(revision_key)
            coordinate = Coordinate(
                category.removesuffix("-head"), body[category.removesuffix("-head") + "_id"], body["revision"]
            )
            deadline = hot_binding(store.db, revision_key)[0]
            owner = record_catalog.parent(
                store.db, event["event_id"], event["market_observation_ms"], deadline
            )
            record_catalog.append(
                store.db,
                [(coordinate, body, projection)],
                bucket=deadline // DAY,
                owners={coordinate: owner},
                deadlines={coordinate: None},
                current=[coordinate],
            )
            history.delete(revision_key[len(PREFIX) :])
            history.delete(key[len(PREFIX) :])
            moved.append(coordinate)
        store.db.execute("INSERT INTO market_meta VALUES('record_catalogue_runtime','1')")
    assert moved
    assert analytic_heads(store.db, event["event_id"], store.cached_text) == before
    assert {
        coordinate
        for coordinate, current in catalogue_lifecycle.owned_records(
            store.db, event["event_id"], store.cached_text
        )
        if current
    } == set(moved)
    before_records = list(history.scan("outcome-head:"))
    with pytest.raises(RuntimeError, match="interrupted"), store.db:
        archive_event(store, event)
        raise RuntimeError("interrupted archive before deletion")
    assert public_archive(store) == []
    assert list(history.scan("outcome-head:")) == before_records
    if full_expiry:
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            coordinate = Coordinate("event", event["event_id"], event["revision"])
            key = PREFIX + coordinate.key
            body = store.get_runtime(key)
            projection = store.get_runtime(PREFIX + "event-head:" + event["event_id"])
            deadline = hot_binding(store.db, key)[0]
            owner = record_catalog.parent(
                store.db, event["event_id"], event["market_observation_ms"], deadline
            )
            record_catalog.append(
                store.db,
                [(coordinate, body, projection)],
                bucket=deadline // DAY,
                owners={coordinate: owner},
                deadlines={coordinate: None},
                current=[coordinate],
            )
            alias = catalogue_aliases.Alias(event["event_id"], event["market_observation_ms"])
            assert catalogue_aliases.publish(
                store.db, alias.key, {"event_id": event["event_id"]}, store.cached_bytes
            )
            # The transfer path removes hot copies without unpublishing cold values.
            for old_key in (key, PREFIX + "event-head:" + event["event_id"], alias.key):
                forget(store, old_key)
                store.db.execute("DELETE FROM runtime WHERE key=?", (old_key,))
        assert history.get("event-head:" + event["event_id"]) == event
        assert history.count("history:") == len(events)
        from cryptowatcher.catalogue_retention import remove_event

        with store.db:
            store.db.execute(
                "CREATE TRIGGER reject_cold_expiry BEFORE DELETE ON market_records WHEN OLD.kind=0 BEGIN SELECT RAISE(ABORT,'injected cold expiry failure'); END"
            )
        before_counts = [
            tuple(row) for row in store.db.execute("SELECT * FROM market_counts ORDER BY category")
        ]
        with pytest.raises(sqlite3.IntegrityError, match="injected cold"), store.db:
            store.db.execute("BEGIN IMMEDIATE")
            archive_event(store, event)
            remove_event(store, event["event_id"], lambda category, state: None)
        assert public_archive(store) == []
        assert history.get("event-head:" + event["event_id"]) == event
        assert analytic_heads(store.db, event["event_id"], store.cached_text) == before
        assert [
            tuple(row) for row in store.db.execute("SELECT * FROM market_counts ORDER BY category")
        ] == before_counts
        with store.db:
            store.db.execute("DROP TRIGGER reject_cold_expiry")
        retention = BoundedRetention(store)
        # Preserve the complete native cold boundary when the alias is absent in runtime.
        from cryptowatcher.catalogue_retention import due_events

        assert any(
            binding["event_id"] == event["event_id"]
            for _, binding in due_events(store, NOW + 31 * DAY, NOW + DAY, 100).rows
        )
        for _ in range(100):
            retention.work(NOW + 31 * DAY, batch=1)
            if (
                history.count("event-head:") == 0
                and not store.db.execute("SELECT 1 FROM market_record_parents LIMIT 1").fetchone()
            ):
                break
        assert history.count("event-head:") == 0
        assert history.count("outcome-head:") == 0
        assert history.count("history:") == 0
        assert not store.db.execute("SELECT 1 FROM market_records LIMIT 1").fetchone()
        assert not store.db.execute("SELECT 1 FROM market_record_owners LIMIT 1").fetchone()
        assert not store.db.execute("SELECT 1 FROM market_record_parents LIMIT 1").fetchone()
        before_archive = public_archive(store)
        retention.work(NOW + 31 * DAY, batch=100)
        assert public_archive(store) == before_archive
    else:
        with store.db:
            archive_event(store, event)
    assert public_archive(store) == expected
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()
