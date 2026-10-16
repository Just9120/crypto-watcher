from dataclasses import asdict

import pytest

from cryptowatcher.events import History
from cryptowatcher.history_codec import MARKER, Codec
from cryptowatcher.market_storage import enable, read
from cryptowatcher.outcomes import OutcomeWorker, calculate, read_candles
from cryptowatcher.sources import Sources
from cryptowatcher.storage import Store, encode, json_payload

from .test_outcomes import MINUTE, NOW, future, observe


def bounded(store):
    enable(store.db)
    store.bounded = True
    store.clock_ms = lambda: NOW
    return store


def test_normalized_overlapping_windows_replay_and_restart_keep_exact_analytics(store):
    bounded(store)
    worker = OutcomeWorker(store)
    observe(store, worker)
    history = History(store)
    original = history.events(20)
    assert original
    count = store.db.execute("SELECT count(*) FROM market_candles").fetchone()[0]
    assert 0 < count <= 24
    observe(store, worker)
    assert history.events(20) == original
    assert store.db.execute("SELECT count(*) FROM market_candles").fetchone()[0] == count
    sources = Sources(store)
    sources.put("spot/BTCUSDT/1m", future())
    worker.work(NOW + 6 * MINUTE)
    first = history.events(1)[0]
    for _, outcome in history.items("outcome-head:"):
        if outcome["horizon_min"] == 5:
            expected = calculate(history.get("event-head:" + outcome["event_id"]), 5, future())
            assert outcome["metrics"] == expected["metrics"]
    stored = store.get_runtime("radar:v1:event:" + first["event_id"] + ":0000000001")
    assert stored[MARKER]["fields"]["source_inputs"]["mode"] == "source"
    assert all(
        "source_inputs" not in json_payload(r[0])
        for r in store.db.execute("SELECT payload_json FROM runtime WHERE key LIKE 'radar:v1:event:%'")
    )
    fresh = Store(store.path, store.owner_id, readonly=True)
    try:
        assert History(fresh).events(20) == history.events(20)
        assert [asdict(c) for c in read_candles(fresh, "spot/BTCUSDT/1m", NOW, NOW + 5 * MINUTE)] == [
            asdict(c) for c in future()
        ]
        assert fresh.db.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        fresh.close()


def test_source_reference_preserves_used_revision_when_recovery_changes_current_values(store):
    bounded(store)
    codec = Codec(store.db)
    codec.source_stream = "spot/BTCUSDT/1m"
    codec.source_expiry = NOW + 30 * 86400000
    with store.db:
        packed = codec.pack({"source_inputs": [asdict(c) for c in future()]})
    originals = codec.unpack(packed)
    values = future()
    with store.db:
        from cryptowatcher.market_storage import admit

        changed = {**asdict(values[0]), "received_ms": values[0].received_ms + 1}
        admit(store.db, "spot/BTCUSDT/1m", changed)
    assert read(store.db, "spot/BTCUSDT/1m", NOW, NOW + MINUTE)[0] == changed
    assert encode(codec.unpack(packed)) == encode(originals)


def test_retention_keeps_exact_cohorts_and_method_settings_after_detail_expiry(store):
    from cryptowatcher.analytics import cohorts
    from cryptowatcher.outcomes import DAY, Retention

    bounded(store)
    worker = OutcomeWorker(store)
    observe(store, worker)
    Sources(store).put("spot/BTCUSDT/1m", future())
    worker.work(NOW + 6 * MINUTE)
    before = cohorts(store)
    assert before["cohorts"]
    retention = Retention(store)
    retention.work(NOW + 30 * DAY - 1)
    assert History(store).events(20)
    retention.work(NOW + 30 * DAY + 1)
    assert not History(store).events(20)
    after = cohorts(store)
    assert after["cohorts"] == before["cohorts"]
    assert after["incomplete_outcomes"] == before["incomplete_outcomes"]
    assert store.db.execute("SELECT sum(n) FROM market_quality").fetchone()[0] > 0
    assert History(store).count("protected:") == 0
    # Retry of cleanup is idempotent, no duplicate summary admissions.
    total = store.db.execute("SELECT sum(n) FROM market_quality").fetchone()[0]
    retention.work(NOW + 31 * DAY)
    assert store.db.execute("SELECT sum(n) FROM market_quality").fetchone()[0] == total
    retention.work(NOW + 36 * DAY)
    assert store.db.execute("SELECT count(*) FROM market_candles").fetchone()[0] == 0
    retention.work(NOW + 400 * DAY)
    assert store.db.execute("SELECT count(*) FROM market_quality").fetchone()[0] == 0


def test_reset_preparation_preserves_private_configuration_and_refuses_repeat(store, tmp_path):
    import pytest

    from cryptowatcher.reset_history import prepare, private_state, signature
    from cryptowatcher.storage import StateError

    with store.db:
        store.put_runtime(
            "radar:v1:digest-preferences", {"enabled": True, "next_end_ms": 10, "changed_ms": 1}
        )
        store.put_runtime("radar:v1:agent:preference", {"enabled": False, "schema": 1})
        store.put_runtime("old-market-event", {"history": "discardable test"})
    store.mute("spot/BTCUSDT", NOW / 1000 + 10000)
    preserved = private_state(store)
    source_bytes = store.path.read_bytes()
    candidate = (tmp_path / "fresh.sqlite3").resolve()
    receipt = prepare(
        store.path.resolve(), candidate, (tmp_path / "private.json").resolve(), store.owner_id, now_ms=NOW
    )
    assert receipt["private_source_sha256"] == signature(preserved)
    assert store.path.read_bytes() == source_bytes
    restored = Store(candidate, store.owner_id)
    try:
        assert restored.snapshot() == store.snapshot()
        assert restored.enabled() == store.enabled()
        assert list(map(tuple, restored.db.execute("SELECT * FROM mutes"))) == list(
            map(tuple, store.db.execute("SELECT * FROM mutes"))
        )
        assert restored.get_runtime("old-market-event") is None
        assert restored.get_runtime("radar:v1:digest-preferences")["enabled"] is True
        assert restored.get_runtime("radar:v1:digest-preferences")["next_end_ms"] > NOW
        assert restored.get_runtime("history_generation")["started_ms"] == NOW
        with pytest.raises(StateError, match="already performed"):
            prepare(
                candidate,
                (tmp_path / "again.sqlite3").resolve(),
                (tmp_path / "again.json").resolve(),
                store.owner_id,
            )
    finally:
        restored.close()
    assert not (tmp_path / "again.sqlite3").exists()


def test_compact_context_reuses_stable_metadata_and_freezes_causal_snapshot(store):
    from cryptowatcher.source_context import at, save

    bounded(store)
    codec = Codec(store.db)
    codec.source_stream = "spot/BTCUSDT/1m"
    codec.source_expiry = NOW
    value = {
        "schema": "metadata",
        "tick_size": 0.01,
        "available_at_ms": NOW - 60000,
        "market_observation_ms": NOW - 60000,
        "metadata": {"base": "BTC", "details": "same" * 300},
    }
    with store.db:
        save(store, "instrument", "spot/BTCUSDT", value)
        save(
            store,
            "instrument",
            "spot/BTCUSDT",
            {**value, "available_at_ms": NOW, "market_observation_ms": NOW},
        )
        packed = codec.pack({"source_context": {"instrument": value, "ticker": None}})
    assert store.db.execute("SELECT count(*) FROM market_context_bodies").fetchone()[0] == 1
    assert codec.unpack(packed)["source_context"]["instrument"] == value
    assert at(store, "instrument", "spot/BTCUSDT", NOW - 1) == value
    assert at(store, "instrument", "spot/BTCUSDT", NOW)["available_at_ms"] == NOW
    assert "metadata" not in encode(packed)


def test_meter_counters_are_atomic_and_sampling_does_not_walk_large_tables(store):
    import pytest

    from cryptowatcher.storage_meter import report, sample

    bounded(store)
    Sources(store).put("spot/BTCUSDT/1m", future())
    before = report(store, NOW)
    assert before["categories"]["candles"]["records"] == 5
    with pytest.raises(RuntimeError), store.db:
        from cryptowatcher.market_storage import admit

        admit(store.db, "linear/BTCUSDT/1m", asdict(future()[0]))
        raise RuntimeError("rollback")
    assert report(store, NOW)["categories"]["candles"]["records"] == 5
    sample(store, NOW)
    sample(store, NOW + 60000)
    assert report(store, NOW + 60000)["sample_seconds"] == 60
    assert report(store, NOW + 60000)["projected_working_bytes"] is not None
    assert store.get_runtime("storage_index_measurement")["state"] in ("PASS", "UNAVAILABLE")


def test_generation_boundary_does_not_recreate_bootstrap_events_episodes_or_delivery(store):
    bounded(store)
    store.put_runtime("history_generation", {"started_ms": NOW + 1})
    worker = OutcomeWorker(store)
    observe(store, worker)
    history = History(store)
    assert history.events(20) == []
    assert history.count("episode:") == 0
    assert history.count("outbox:telegram:") == 0
    observe(store, worker, NOW + MINUTE)
    assert history.events(20)


async def test_bounded_signed_delivery_and_telegram_independence(store):
    from .test_agent_delivery import test_atomic_admission_coalescing_and_telegram_independence as scenario

    bounded(store)
    await scenario(store)


async def test_bounded_signed_retry_restart_reuses_identical_bytes(store):
    from .test_agent_delivery import (
        test_restart_ambiguous_ack_reuses_identical_bytes_and_success_state as scenario,
    )

    bounded(store)
    await scenario(store)


async def test_bounded_signed_late_outcome_preserves_causal_context(store):
    from .test_agent_delivery import test_episode_closure_and_late_outcome_keep_original_context as scenario

    bounded(store)
    await scenario(store)


def test_finished_jobs_are_not_recreated_by_bootstrap_and_owner_can_recompare(store):
    from cryptowatcher.analytics import ComparisonWorker

    bounded(store)
    worker = OutcomeWorker(store)
    observe(store, worker)
    Sources(store).put("spot/BTCUSDT/1m", future())
    worker.work(NOW + 6 * MINUTE)
    comparison = ComparisonWorker(store)
    eid = History(store).events(1)[0]["event_id"]
    for _ in range(10):
        comparison.work(NOW + 6 * MINUTE)
    assert History(store).get("comparison-job:" + eid) is None
    completed = History(store).count("outcome-head:")
    with store.db:
        History(store).delete("outcome-admission")
    worker.bootstrap(100)
    assert all(v["horizon_min"] != 5 for _, v in History(store).items("outcome-job:"))
    assert History(store).count("outcome-head:") == completed
    assert comparison.requeue(eid, NOW + 7 * MINUTE) == "PENDING"
    assert History(store).get("comparison-job:" + eid)["event_revision"] == 1


@pytest.mark.parametrize("fault", ["trailer", "checksum", "size", "hash", "dictionary", "extra"])
def test_binary_records_refuse_corruption_and_preserve_original_json_types(fault):
    import pytest

    from cryptowatcher.record_codec import binary_payload, pack_binary
    from cryptowatcher.storage import StateError

    value = {"number": 0.0175454454105349, "integer": 2, "real": 2.0, "notes": "история" * 40}
    raw = encode(value)
    payload = pack_binary(raw)
    assert binary_payload(payload) == raw
    if fault == "trailer":
        damaged = payload[:-1]
    elif fault == "checksum":
        damaged = payload[:-1] + bytes([payload[-1] ^ 1])
    elif fault == "hash":
        damaged = payload[:4] + bytes([payload[4] ^ 1]) + payload[5:]
    elif fault == "dictionary":
        damaged = b"CW3\0" + payload[4:]
    elif fault == "size":
        damaged = payload[:36] + b"bad-frame"
    else:
        damaged = payload + b"extra"
    with pytest.raises(StateError):
        binary_payload(damaged)


def test_retention_archive_failure_rolls_back_details_and_aggregates(store, monkeypatch):
    import pytest

    import cryptowatcher.quality_summary as quality
    from cryptowatcher.outcomes import DAY, Retention

    bounded(store)
    worker = OutcomeWorker(store)
    observe(store, worker)
    before = History(store).events(100)
    real = quality.archive_event

    def fail(*args):
        real(*args)
        raise RuntimeError("injected aggregate failure")

    monkeypatch.setattr(quality, "archive_event", fail)
    with pytest.raises(RuntimeError):
        Retention(store).work(NOW + 31 * DAY)
    assert History(store).events(100) == before
    assert store.db.execute("SELECT count(*) FROM market_quality").fetchone()[0] == 0


def test_summary_expiry_is_twelve_calendar_months_and_context_inputs_are_leased(store):
    from datetime import datetime, timezone

    from cryptowatcher.outcomes import DAY, Retention
    from cryptowatcher.quality_summary import month_boundary
    from cryptowatcher.source_context import save

    assert (
        month_boundary(int(datetime(2024, 2, 29, tzinfo=timezone.utc).timestamp()) // 86400, 12)
        == int(datetime(2025, 2, 28, tzinfo=timezone.utc).timestamp()) // 86400
    )
    bounded(store)
    value = {"available_at_ms": NOW - 28 * DAY, "market_observation_ms": NOW - 28 * DAY, "tick_size": 0.01}
    codec = Codec(store.db)
    codec.source_stream = "spot/BTCUSDT/1m"
    codec.source_expiry = NOW
    with store.db:
        save(store, "instrument", "spot/BTCUSDT", value)
        reference = codec.pack({"source_context": {"instrument": value, "ticker": None}})
    Retention(store).work(NOW + 8 * DAY)
    assert codec.unpack(reference)["source_context"]["instrument"] == value
    Retention(store).work(NOW + 31 * DAY)
    assert store.db.execute("SELECT count(*) FROM market_context").fetchone()[0] == 0


def test_forecast_includes_annual_quality_before_first_retention_pass(store):
    from cryptowatcher.storage_meter import report, sample

    bounded(store)
    sample(store, NOW - 60000)
    worker = OutcomeWorker(store)
    observe(store, worker)
    sample(store, NOW)
    value = report(store, NOW)
    assert value["observed_events_total"] > 0
    assert value["observed_events_per_day_extrapolated"] > 0
    quality = next(row for row in value["forecast"] if row["category"] == "quality")
    assert quality["model_applied"] is True
    assert quality["projected_logical_bytes"] > 0
    assert value["quality_forecast_model"]["actual_production_measurement"] is False
