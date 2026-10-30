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
        for category, size in (
            ("quality", 100),
            ("quality_numeric", 200),
            ("quality_components", 300),
            ("quality_frames", 400),
            ("quality_numeric_frames", 500),
            ("quality_episodes", 600),
        ):
            store.db.execute("INSERT INTO market_counts VALUES(?,1,?,?,0)", (category, size, size))
    # report must read prequalified coefficients, never build an isolated
    # database/population on an application or observer request.
    monkeypatch.setattr(storage_model, "measure", lambda *a, **k: pytest.fail("runtime synthetic work"))
    value = report(store, NOW)
    rows = {r["category"]: r for r in value["forecast"]}
    assert all(rows[k]["retention_days_for_estimate"] == 366 for k in QUALITY_CATEGORIES)
    assert all(rows[k]["allocation_excluded"] for k in QUALITY_CATEGORIES - {"quality"})
    assert rows["quality"]["projected_logical_bytes"] == 2100 * 366
    assert value["quality_physical_allocation_bytes"] == 2100 * 366 * value["page_overhead_factor"]
    # Sampling also creates ordinary service records; they remain a separate
    # allocation. All five archive categories are replaced together.
    other_logical = sum(
        r["projected_logical_bytes"] or 0 for k, r in rows.items() if k not in QUALITY_CATEGORIES
    )
    assert value["projected_working_bytes"] == pytest.approx(
        max(
            value["working_bytes"],
            (2100 * 366 + other_logical) * value["page_overhead_factor"]
            + value["journal_reserve"]["bytes"]
            + value["freelist_bytes"],
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


def test_candle_forecast_has_no_n20_prior_window_or_lease_policy_side_effect(store):
    from dataclasses import asdict

    from cryptowatcher.market_storage import admit
    from cryptowatcher.sources import Candle

    bounded(store)
    settings = store.snapshot()
    sample(store, NOW)
    earliest = NOW - 256 * 15 * 60000
    candle = Candle.parse([earliest, 100, 101, 99, 100, 1, 100], "15m", "fixture", NOW, NOW)
    with store.db:
        admit(store.db, "spot/BTCUSDT/15m", asdict(candle), needed_until=NOW)
    value = report(store, NOW + DAY)
    projection = next(row for row in value["forecast"] if row["category"] == "candles")
    assert projection["retention_days_for_estimate"] == 33
    actual_deadline = store.db.execute("SELECT purge_after FROM market_candles").fetchone()[0]
    assert actual_deadline - earliest > settings.settings.candle_retention_days * DAY
    assert actual_deadline - earliest <= projection["retention_days_for_estimate"] * DAY
    assert store.snapshot() == settings
    assert value["candle_dependency_retention_estimate"]["configured_minimum_days"] == 3
    assert value["candle_dependency_retention_estimate"]["comparator_prior_days"] == 0


@pytest.mark.parametrize(
    "members,sha",
    [
        (1, "0701a7636e22a6e20347ecd91f09c78760ea263a9a3e875955fa7955aae10cae"),
        (4, "3a83cce0fc726bc3e9710d93e8388e8de6a1aea9197408335c1531a1d4a84976"),
        (20, "98ff9248df0450dda49a1dfb50a1a26a038aa73629a546f4f3423afd1eb520b5"),
    ],
)
def test_episode_model_uses_real_writer_and_all_physical_pages_with_exact_facts(members, sha):
    value = storage_model.measure_episodes(members=members)
    assert value["public_sha256"] == sha
    assert value["physical_bytes_per_episode"] > value["encoded_bytes_per_episode"]
    assert (
        value["physical_bytes_per_episode"]
        <= 4 * storage_model.EPISODE_RESULT_PHYSICAL + members * storage_model.EPISODE_MEMBER_PHYSICAL
    )
    assert (
        value["encoded_bytes_per_episode"]
        <= 4 * storage_model.EPISODE_RESULT_LOGICAL + members * storage_model.EPISODE_MEMBER_LOGICAL
    )
    # Native SQLite page layout varies: model coefficients are qualification
    # scenarios, not a fixed all-platform capacity assertion.
    assert value["model_version"] == storage_model.EPISODE_MODEL_VERSION


def test_episode_forecast_counts_origins_and_members_once_without_runtime_calibration(store, monkeypatch):
    bounded(store)
    sample(store, NOW - DAY)
    with store.db:
        store.put_runtime("radar:v1:event-counter", 40)
        store.put_runtime("radar:v1:episode-counter", 2)
    monkeypatch.setattr(storage_model, "measure_episodes", lambda **kw: pytest.fail("runtime synthetic work"))
    value = report(store, NOW)
    assert value["observed_episodes_per_day_extrapolated"] == 2
    assert value["observed_events_per_day_extrapolated"] == 40
    assert value["quality_physical_allocation_bytes"] == (2 * 4 * 2048 + 40 * 2048) * 366
    assert value["quality_forecast_model"]["legacy_n20_model_applied"] is False
    assert not value["forecast_scenarios"]
    before = list(store.db.iterdump())
    report(store, NOW)
    assert list(store.db.iterdump()) == before


def test_sample_scope_changes_do_not_reuse_old_generation_or_settings_frequency(store):
    bounded(store)
    sample(store, NOW - DAY)
    with store.db:
        store.put_runtime("radar:v1:event-counter", 86400)
        store.put_runtime("radar:v1:episode-counter", 86400)
        store.put_runtime("history_generation", {"generation": "next"})
    changed = report(store, NOW)
    assert changed["sample_seconds"] == 0
    assert changed["observed_episodes_per_day_extrapolated"] is None
    sample(store, NOW)
    before = store.snapshot()
    store.update(before.revision, before.settings.changed(active_detectors=("price",)))
    assert report(store, NOW + DAY)["sample_seconds"] == 0
    assert len(store.get_runtime("storage_samples")) == 2


@pytest.mark.parametrize("mode", ["hot", "tile", "fallback"])
def test_source_usage_is_unique_transactional_and_unchanged_by_transfer_renewal_or_retry(
    store, monkeypatch, mode
):
    from contextlib import closing
    from dataclasses import asdict

    from cryptowatcher import candle_blocks as blocks
    from cryptowatcher import context_tiles
    from cryptowatcher.market_storage import admit, save_context
    from cryptowatcher.sources import Candle
    from cryptowatcher.storage import Store
    from tests.test_candle_access import writer

    bounded(store)
    value = asdict(Candle.parse([NOW - 60000, 100, 101, 99, 100, 1, 100], "1m", "fixture", NOW, NOW))
    points = []
    with store.db:
        sid, start, _ = admit(store.db, "spot/BTCUSDT/1m", value)
        for i in range(8):
            context = {
                "available_at_ms": NOW + i * 60000,
                "market_observation_ms": NOW + i * 60000,
                "values": {"price": str(100 + i)},
            }
            points.append((context, save_context(store.db, "ticker", "spot/BTCUSDT", context)))
    initial = report(store, NOW)["source_usage"]
    if mode != "hot":
        writer(store)
        context_tiles.install(store.db, store.cached_bytes)
        with store.db:
            blocks.import_tile(store.db, sid, start)
            context_tiles.publish(store.db, [point for _, point in points])
        assert report(store, NOW)["source_usage"] == initial
    if mode == "fallback":

        def unavailable(*args, **kwargs):
            raise blocks.codec.Capacity("fixture fallback")

        monkeypatch.setattr(blocks, "_admit_groups", unavailable)
    context = points[0][0]
    with store.db:
        admit(store.db, "spot/BTCUSDT/1m", value, needed_until=NOW)
        save_context(store.db, "ticker", "spot/BTCUSDT", context, anchor=NOW)
    first = report(store, NOW)["source_usage"]
    assert first["candles"]["arrived_bytes"] == initial["candles"]["arrived_bytes"]
    assert first["candles"]["first_used_bytes"] == first["candles"]["arrived_bytes"]
    assert 0 < first["context"]["first_used_bytes"] < first["context"]["arrived_bytes"]
    with store.db:
        for _ in range(2):
            admit(store.db, "spot/BTCUSDT/1m", value, needed_until=NOW + 60000)
            save_context(store.db, "ticker", "spot/BTCUSDT", context, anchor=NOW + 60000)
    assert report(store, NOW)["source_usage"] == first
    with pytest.raises(RuntimeError), store.db:
        admit(store.db, "spot/BTCUSDT/1m", {**value, "close": 100.5}, needed_until=NOW)
        save_context(store.db, "ticker", "spot/BTCUSDT", points[1][0], anchor=NOW)
        raise RuntimeError("rollback whole source transaction")
    assert report(store, NOW)["source_usage"] == first
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        before = reader.db.total_changes
        assert report(reader, NOW)["source_usage"] == first
        assert reader.db.total_changes == before


def test_input_forecast_uses_observed_first_use_but_reports_uncertain_or_inconsistent_windows(store):
    from cryptowatcher.storage_meter import record_source

    bounded(store)
    sample(store, NOW - DAY)
    with store.db:
        record_source(store.db, "candles", 400, 100)
        record_source(store.db, "context", 100, 0)
    value = report(store, NOW)
    candles = value["candle_dependency_retention_estimate"]
    assert candles["days_for_estimate"] == 33
    assert candles["effective_days_for_estimate"] == 3 + 0.25 * 30
    assert candles["used_inputs"]["not_a_bound"] is True
    assert value["context_dependency_retention_estimate"]["effective_days_for_estimate"] == 3
    with store.db:
        record_source(store.db, "candles", 0, 400)  # first leases on pre-window arrivals
    value = report(store, NOW)
    assert value["candle_dependency_retention_estimate"]["effective_days_for_estimate"] == 33
    assert (
        value["candle_dependency_retention_estimate"]["used_inputs"]["basis"]
        == "CONSERVATIVE_ALL_INPUTS_USED"
    )


def test_forecast_keeps_observed_journal_peak_after_checkpoint_without_second_index_allocation(store):
    bounded(store)
    sample(store, NOW - DAY)
    samples = store.get_runtime("storage_samples")
    samples[0]["files"] = {"wal": 100_000_000, "shm": 32768, "rollback_journal": 1024}
    with store.db:
        store.put_runtime("storage_samples", samples)
    value = report(store, NOW)
    reserve = value["journal_reserve"]
    assert reserve["bytes"] >= 100_033_792
    assert reserve["observed_peak_bytes"] == 100_033_792
    assert reserve["not_a_bound"] is True
    assert value["projected_working_bytes"] >= reserve["bytes"] + value["freelist_bytes"]
