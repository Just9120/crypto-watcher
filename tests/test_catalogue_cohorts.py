import sqlite3
import time

import pytest

from cryptowatcher import catalogue_cohorts, cohort_meter
from cryptowatcher import record_catalog as catalog
from cryptowatcher.events import History
from cryptowatcher.expiry_index import expires
from cryptowatcher.outcomes import OutcomeWorker
from cryptowatcher.record_blocks import Coordinate
from cryptowatcher.storage import encode
from tests.test_bounded_storage import bounded
from tests.test_outcomes import observe

DAY = 86400000


def drain(store):
    with store.db:
        for _ in range(100):
            if cohort_meter.drain(store.db, 128, time.monotonic() + 10):
                return
    pytest.fail("bounded fixture did not complete")


def prepare(store):
    bounded(store)
    catalog.install(store.db, store.cached_text)
    observe(store, OutcomeWorker(store))
    drain(store)


def move(store):
    history = History(store)
    key, value = history.items("event:", 1)[0]
    coordinate = Coordinate("event", value["event_id"], value["revision"])
    raw = store.get_runtime("radar:v1:" + key)
    projection = store.get_runtime("radar:v1:event-head:" + coordinate.identity)
    deadline = expires(store.db, "radar:v1:" + key)[0]
    with store.db:
        owner = catalog.parent(store.db, coordinate.identity, value["market_observation_ms"], deadline)
        catalog.append(
            store.db,
            [(coordinate, raw, projection)],
            bucket=deadline // DAY,
            owners={coordinate: owner},
            deadlines={coordinate: None},
            current=[coordinate],
        )
        history.delete(key)
        # Fixture-only activation of the counter source, not a production
        # runtime activation or qualification of the unfinished adapters.
        store.db.execute("INSERT OR IGNORE INTO market_meta VALUES('record_catalogue_runtime','1')")
    return coordinate, value


def test_mixed_counters_survive_atomic_transfer_rollback_schema_rebuild_and_vacuum(store):
    prepare(store)
    before = cohort_meter.report(store.db)
    assert before["state"] == "MEASURED_PROSPECTIVE"
    coordinate, value = move(store)
    assert encode(cohort_meter.report(store.db)) == encode(before)
    assert catalogue_cohorts.status(store.db)["event_records"] == 1
    frozen = catalog.read(store.db, coordinate)
    assert frozen["event_id"] == value["event_id"]
    with pytest.raises(RuntimeError), store.db:
        store.db.execute("DELETE FROM market_records")
        raise RuntimeError("interrupted cleanup")
    assert encode(cohort_meter.report(store.db)) == encode(before)
    store.db.execute("CREATE TABLE unrelated_fixture_ddl(value INTEGER)")
    assert cohort_meter.report(store.db)["state"] == "PENDING_FINITE_COHORT_BACKFILL"
    drain(store)
    assert encode(cohort_meter.report(store.db)) == encode(before)
    store.db.execute("VACUUM")
    drain(store)
    assert encode(cohort_meter.report(store.db)) == encode(before)
    assert cohort_meter.ready(store.db)


def test_missing_cold_scalar_or_derived_counter_is_never_reported_complete_and_blocks_deletion(store):
    prepare(store)
    coordinate, _ = move(store)
    with store.db:
        store.db.execute("DELETE FROM market_cold_cohort_revisions")
    assert catalogue_cohorts.status(store.db)["state"] == "UNSET_CATALOGUE_COUNTER_MISMATCH"
    assert not cohort_meter.ready(store.db)
    assert cohort_meter.report(store.db)["state"] == "UNSET_CATALOGUE_COUNTER_MISMATCH"
    with pytest.raises(sqlite3.IntegrityError, match="prospective counter"), store.db:
        store.db.execute("DELETE FROM market_records")
    assert catalog.read(store.db, coordinate) is not None
    with store.db:
        store.db.execute("DELETE FROM market_catalogue_counts WHERE kind=0")
    assert catalogue_cohorts.status(store.db)["state"] == "UNSET_CATALOGUE_COUNTER_HEADER"
    with pytest.raises(sqlite3.IntegrityError), store.db:
        store.db.execute("DELETE FROM market_records")


def test_unclassified_cold_event_and_missing_header_reject_complete_result(store):
    import uuid

    prepare(store)
    coordinate = Coordinate("event", str(uuid.UUID(int=7)), 1)
    value = {"event_id": coordinate.identity, "revision": 1, "market_observation_ms": 100 * DAY}
    with store.db:
        owner = catalog.parent(store.db, coordinate.identity, 100 * DAY, 130 * DAY)
        catalog.append(
            store.db,
            [(coordinate, value, None)],
            bucket=130,
            owners={coordinate: owner},
            deadlines={coordinate: None},
        )
        store.db.execute("INSERT INTO market_meta VALUES('record_catalogue_runtime','1')")
    state = catalogue_cohorts.status(store.db)
    assert state == {"state": "UNSET_EVENT_METADATA", "unclassified_event_records": 1}
    assert not cohort_meter.ready(store.db)
    assert cohort_meter.report(store.db) == state
    with store.db:
        store.db.execute("DELETE FROM market_cold_cohort_scan")
    with pytest.raises(sqlite3.IntegrityError), store.db:
        store.db.execute("DELETE FROM market_records")
    assert catalog.read(store.db, coordinate) == value
