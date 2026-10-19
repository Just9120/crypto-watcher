import pytest

from cryptowatcher import storage_model
from cryptowatcher.storage_meter import QUALITY_CATEGORIES, report, sample
from tests.test_capacity import bounded
from tests.test_outcomes import DAY, NOW


@pytest.mark.parametrize("coefficient", storage_model.quality_scenarios())
def test_current_archive_coefficients_reproduce_exact_public_facts_and_include_pools(coefficient):
    value = storage_model.measure(
        coefficient["facts_per_cohort_day"],
        coefficient["comparators_per_fact"],
        shared=coefficient["comparator_reuse"] == "EXPLICIT_OVERLAPPING_FIXTURE",
    )
    assert value["public_sha256"] == coefficient["public_sha256"]
    assert value["encoded_bytes_per_fact"] == coefficient["encoded_bytes_per_fact"]
    assert value["physical_bytes_per_fact"] > 0
    if value["comparators_per_fact"]:
        assert value["encoded_bytes_per_fact"] > value["packet_bytes_per_fact"]
    # SQLite versions may allocate a different final page. This is a scenario
    # qualification, never an assertion of production or mature capacity fit.


def test_forecast_replaces_all_archive_categories_once_and_preserves_annual_components(store, monkeypatch):
    bounded(store)
    sample(store, NOW - DAY)
    with store.db:
        for category, size in (("quality", 100), ("quality_numeric", 200), ("quality_components", 300)):
            store.db.execute("INSERT INTO market_counts VALUES(?,1,?,?,0)", (category, size, size))
    # report must read prequalified coefficients, never build an isolated
    # database/population on an application or observer request.
    monkeypatch.setattr(storage_model, "measure", lambda *a, **k: pytest.fail("runtime synthetic work"))
    value = report(store, NOW)
    rows = {r["category"]: r for r in value["forecast"]}
    assert all(rows[k]["retention_days_for_estimate"] == 366 for k in QUALITY_CATEGORIES)
    assert all(rows[k]["allocation_excluded"] for k in QUALITY_CATEGORIES - {"quality"})
    assert rows["quality"]["projected_logical_bytes"] == 600 * 366
    assert value["quality_physical_allocation_bytes"] == 600 * 366 * value["page_overhead_factor"]
    # Sampling also creates ordinary service records; they remain a separate
    # allocation. Only the three archive categories are replaced together.
    other_logical = sum(
        r["projected_logical_bytes"] or 0 for k, r in rows.items() if k not in QUALITY_CATEGORIES
    )
    assert value["projected_working_bytes"] == pytest.approx(
        max(
            value["working_bytes"],
            (600 * 366 + other_logical) * value["page_overhead_factor"]
            + value["files"]["wal"]
            + value["files"]["shm"],
        ),
        rel=1e-14,
    )
    assert value["quality_replaced_categories"] == sorted(QUALITY_CATEGORIES)


def test_completed_conversion_starts_new_byte_flow_without_discarding_event_frequency_or_old_samples(store):
    bounded(store)
    sample(store, NOW - DAY)
    with store.db:
        store.put_runtime("radar:v1:event-counter", 86400)
        store.db.execute("INSERT INTO market_counts VALUES('numeric_details',1,1000000,1000000,0)")
        store.put_runtime("storage_repack", {"version": 7, "state": "COMPLETE"})
    value = report(store, NOW)
    assert value["sample_seconds"] == 0
    assert value["projected_working_bytes"] is None
    assert value["observed_events_per_day_extrapolated"] == 86400
    sample(store, NOW)
    with store.db:
        store.db.execute(
            "UPDATE market_counts SET inserted_bytes=inserted_bytes+10,logical_bytes=logical_bytes+10 WHERE category='numeric_details'"
        )
    value = report(store, NOW + DAY)
    row = next(r for r in value["forecast"] if r["category"] == "numeric_details")
    assert row["projected_logical_bytes"] == 300
    assert len(store.get_runtime("storage_samples")) == 2
    assert store.get_runtime("storage_samples")[0]["completed_repack_version"] is None


def test_candle_forecast_includes_comparator_dependency_lease_without_changing_retention(store):
    from dataclasses import asdict

    from cryptowatcher.market_storage import admit
    from cryptowatcher.sources import Candle

    bounded(store)
    settings = store.snapshot()
    sample(store, NOW)
    earliest = NOW - 28 * DAY - 256 * 15 * 60000
    candle = Candle.parse([earliest, 100, 101, 99, 100, 1, 100], "15m", "fixture", NOW, NOW)
    with store.db:
        admit(store.db, "spot/BTCUSDT/15m", asdict(candle), needed_until=NOW)
    value = report(store, NOW + DAY)
    projection = next(row for row in value["forecast"] if row["category"] == "candles")
    assert projection["retention_days_for_estimate"] == 61
    actual_deadline = store.db.execute("SELECT purge_after FROM market_candles").fetchone()[0]
    assert actual_deadline - earliest > settings.settings.candle_retention_days * DAY
    assert actual_deadline - earliest <= projection["retention_days_for_estimate"] * DAY
    assert store.snapshot() == settings
    assert value["candle_dependency_retention_estimate"]["configured_minimum_days"] == 35
