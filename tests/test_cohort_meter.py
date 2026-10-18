import hashlib
import json
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest

from cryptowatcher import cohort_meter
from cryptowatcher.capacity import prospective_cohorts
from cryptowatcher.events import History, identity
from cryptowatcher.market_storage import enable
from cryptowatcher.outcomes import OutcomeWorker
from cryptowatcher.storage import Store, encode
from tests.test_outcomes import DAY, NOW


def event(store, index, *, day=0):
    return {
        "schema": "cryptowatcher.event.v1",
        "event_id": identity(f"counter/{index}/{day}"),
        "revision": 1,
        "market_observation_ms": NOW + day * DAY,
        "detector_id": ("price", "turnover", "breakout", "volatility", "composite")[index % 5],
        "detector_version": "1",
        "settings_revision": index % 3 + 1,
        "market_type": "spot" if index % 2 else "linear",
        "timeframe": ("1m", "3m", "5m", "15m")[index % 4],
        "symbol": f"FIXTURE{index % 7}USDT",
        "direction": ("UP", "DOWN", "NEUTRAL")[index % 3],
        "strength": (0.0, -0.0, 2, 2.0, 1 << 1024, index / 7)[index % 6],
        "settings_snapshot": store.snapshot().settings.payload(),
    }


def save(store, value):
    key = f"radar:v1:event:{value['event_id']}:{value['revision']:010d}"
    store.db.execute(
        "INSERT INTO runtime VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload_json=excluded.payload_json",
        (key, encode(value)),
    )
    return key


def compare(store):
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        reader.db.execute("BEGIN")
        measured = prospective_cohorts(reader)
        with patch("cryptowatcher.cohort_meter.report", return_value=None):
            original = prospective_cohorts(reader)
    for field in (
        "state",
        "event_revisions",
        "event_records",
        "facts_if_archived",
        "cohort_days_if_archived",
        "density_histogram",
        "density_mean",
        "density_median",
        "dimension_cardinality",
        "distinct_without_dimension",
        "distinct_strengths",
    ):
        assert measured[field] == original[field], field
    return measured


def test_cohort_counters_equal_full_decoder_for_dates_dimensions_revisions_and_exact_numeric_strengths(store):
    enable(store.db)
    store.bounded = True
    OutcomeWorker(store)
    with store.db:
        for day in range(8):
            for index in range(60):
                save(store, event(store, index, day=day))
        second = event(store, 1)
        second.update(revision=2, strength=1.25)
        save(store, second)
    assert compare(store)["event_records"] == 481
    with pytest.raises(RuntimeError), store.db:
        save(store, event(store, 300, day=7))
        raise RuntimeError("rollback row and all projections")
    assert compare(store)["event_records"] == 481
    with store.db:
        old = event(store, 0)
        key = save(store, old)
        changed = {**old, "strength": 17.5, "settings_revision": 999, "market_observation_ms": NOW + 12 * DAY}
        save(store, changed)
        store.db.execute("DELETE FROM runtime WHERE key=?", (key,))
    assert compare(store)["event_records"] == 480
    # Other runtime namespaces must not run the event metadata projector.
    store.db.create_function(
        "cw_cohort_record", 2, lambda *args: (_ for _ in ()).throw(AssertionError("unrelated"))
    )
    store.put_runtime("unrelated:counter-test", {"not": "an event"})


def test_finite_cohort_backfill_churn_generations_unknown_rows_and_restart(store):
    enable(store.db)
    store.bounded = True
    OutcomeWorker(store)
    with store.db:
        for i in range(25):
            save(store, event(store, i))
    # A schema change invalidates transient physical accounting without dropping history.
    store.db.execute("CREATE TABLE counter_schema_fixture(id INTEGER)")
    assert cohort_meter.report(store.db)["state"] == "PENDING_FINITE_COHORT_BACKFILL"
    with store.db:
        assert not cohort_meter.drain(store.db, 7, time.monotonic() + 1)
        save(store, event(store, 100, day=1))
        store.db.execute(
            "DELETE FROM runtime WHERE key=?", (f"radar:v1:event:{event(store, 0)['event_id']}:0000000001",)
        )
    for _ in range(100):
        with store.db:
            if cohort_meter.drain(store.db, 7, time.monotonic() + 1):
                break
    assert compare(store)["event_records"] == 25
    with store.db:
        store.db.execute("INSERT INTO runtime VALUES('radar:v1:event:legacy:0000000001','{}')")
    assert cohort_meter.report(store.db)["state"] == "UNSET_EVENT_METADATA"
    with store.db:
        store.db.execute("DELETE FROM runtime WHERE key='radar:v1:event:legacy:0000000001'")
        for _ in range(100):
            if not cohort_meter.collect_current(store.db, 7):
                break
    assert compare(store)["event_records"] == 25
    generation = store.db.execute("SELECT generation FROM market_cohort_scan").fetchone()[0]
    assert (
        store.db.execute("SELECT COUNT(*) FROM market_cohorts WHERE generation<>?", (generation,)).fetchone()[
            0
        ]
        == 0
    )
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        queries = []
        reader.db.set_trace_callback(queries.append)
        assert cohort_meter.report(reader.db)["state"] == "MEASURED_PROSPECTIVE"
        assert not any("payload_json FROM runtime" in q for q in queries)


def test_materialized_cohort_missing_counter_refuses_to_delete_its_source(store):
    import sqlite3

    enable(store.db)
    store.bounded = True
    OutcomeWorker(store)
    value = event(store, 2)
    with store.db:
        key = save(store, value)
    with store.db:
        store.db.execute("DELETE FROM market_cohort_strengths")
    with pytest.raises(sqlite3.IntegrityError, match="Missing prospective"), store.db:
        store.db.execute("DELETE FROM runtime WHERE key=?", (key,))
    assert encode(store.get_runtime(key)) == encode(value)


def test_projection_cache_checks_immutable_settings_body_on_every_reuse(store):
    from cryptowatcher.storage import StateError
    from tests.test_outcomes import observe

    enable(store.db)
    store.bounded = True
    observe(store, OutcomeWorker(store))
    history = History(store)
    key, _ = history.items("event:", 1)[0]
    raw = store.get_runtime("radar:v1:" + key)
    reference = raw["_history_storage"]["fields"]["settings_snapshot"]
    projector = cohort_meter.Projector(store.db)
    encoded = encode(raw)
    first = projector.read("radar:v1:" + key, encoded)
    assert first == projector.read("radar:v1:" + key, encoded)
    assert projector.size <= cohort_meter.MAX_CACHE
    with store.db:
        # Fault injection is confined to this disposable fixture.
        store.db.execute("DELETE FROM runtime WHERE key=?", ("historyblob:v1:" + reference["id"],))
    with pytest.raises(StateError):
        projector.read("radar:v1:" + key, encoded)


@pytest.mark.skipif(sys.platform != "linux", reason="Observer address-space/CPU qualification requires Linux")
def test_full_month_cohort_counter_fits_unchanged_observer_resource_budget(tmp_path):
    from tests.cohort_population import build

    path = tmp_path / "cohort-budget.sqlite3"
    expected = build(path)
    with path.open("rb") as source:
        original = hashlib.file_digest(source, "sha256").hexdigest()
    code = """import json,resource,sys,time
resource.setrlimit(resource.RLIMIT_AS,(256*1024*1024,256*1024*1024))
resource.setrlimit(resource.RLIMIT_CPU,(90,90))
import cryptowatcher.__main__
from pathlib import Path
from cryptowatcher.storage import Store
from cryptowatcher.cohort_meter import report
store=Store(Path(sys.argv[1]),233382575,readonly=True)
store.db.execute('BEGIN')
started=time.monotonic()
result=report(store.db,seconds=60)
store.close()
print(json.dumps({'result':result,'seconds':time.monotonic()-started,'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024}))
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", code, str(path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=130,
        check=True,
    )
    value = json.loads(result.stdout)
    report = value["result"]
    assert report["state"] == "MEASURED_PROSPECTIVE"
    assert report["event_records"] == expected["events"] and report["facts_if_archived"] == expected["facts"]
    assert (
        report["cohort_days_if_archived"] == expected["groups"]
        and report["distinct_strengths"] == expected["strengths"]
    )
    assert report["distinct_without_dimension"]["day"] == 31232
    assert report["distinct_without_dimension"]["horizon"] * 4 == expected["groups"]
    assert report["dimension_cardinality"]["instrument"] == 244
    assert value["seconds"] < 60 and value["peak_rss_bytes"] < 256 * 1024 * 1024
    with path.open("rb") as source:
        assert hashlib.file_digest(source, "sha256").hexdigest() == original
