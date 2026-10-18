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
