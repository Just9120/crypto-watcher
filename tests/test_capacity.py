import copy
import json
import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock, patch

import pytest

from cryptowatcher.capacity import (
    explain_forecast,
    interval,
    prospective_cohorts,
    runtime_ledger,
    snapshot,
    stable_reason,
)
from cryptowatcher.events import DECISION_HEAD, PREFIX, EventEngine, History, identity
from cryptowatcher.market_storage import enable
from cryptowatcher.storage import StateError, Store
from cryptowatcher.storage_repack import async_work, work
from scripts.observe_capacity import advance, atomic

from .test_outcomes import DAY, NOW, OutcomeWorker, Retention, Sources, future, observe


def bounded(store):
    enable(store.db)
    store.bounded = True
    store.clock_ms = lambda: NOW


def test_decision_pointer_keeps_revisions_reads_and_restart_and_refuses_corruption(store):
    bounded(store)
    engine = EventEngine(store)
    eid = identity("decision-fixture")
    first = {"event_id": eid, "reason": "original", "at_ms": NOW}
    with store.db:
        engine.decision(eid, first)
    key = "decision:telegram:" + eid
    h = History(store)
    assert store.get_runtime(PREFIX + key) == {DECISION_HEAD: 1}
    assert h.get(key) == {**first, "revision": 1}
    assert h.metadata(key) == h.get(key)
    with pytest.raises(RuntimeError), store.db:
        engine.decision(eid, {**first, "reason": "rollback"})
        raise RuntimeError()
    assert h.get(key)["revision"] == 1
    with store.db:
        engine.decision(eid, {**first, "reason": "retry"})
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        assert History(reader).get(key)["reason"] == "retry"
        assert History(reader).get(key + ":0000000001") == {**first, "revision": 1}
    with store.db:
        h.delete(key + ":0000000002")
    with pytest.raises(StateError, match="Missing or conflicting"):
        h.get(key)
    with pytest.raises(StateError, match="Invalid decision"):
        h.resolve("wrong-key", {DECISION_HEAD: 1})


def test_completed_repack_resumes_decision_pass_without_discarding_progress(store):
    bounded(store)
    h = History(store)
    key = "decision:telegram:" + identity("old-full-decision")
    value = {
        "event_id": identity("old-full-decision"),
        "reason": "historical_observation",
        "at_ms": NOW,
        "revision": 1,
    }
    previous = {
        "version": 3,
        "state": "COMPLETE",
        "records": 1000,
        "cursor": "radar:v1:zzz",
        "saved_payload_bytes": 123,
        "saved_categories": {"details": 123},
        "records_complete": True,
        "quality_complete": True,
    }
    with store.db:
        h.put(key, value)
        h.put(key + ":0000000001", value)
        store.put_runtime("storage_repack", previous)
    original = store.put_runtime

    def fail_checkpoint(name, content):
        if name == "storage_repack":
            raise RuntimeError("Interrupted before durable checkpoint")
        return original(name, content)

    with patch.object(store, "put_runtime", side_effect=fail_checkpoint), pytest.raises(RuntimeError):
        work(store, NOW, batch=10, seconds=1)
    assert store.get_runtime("storage_repack") == previous
    assert store.get_runtime(PREFIX + key) == value
    state = work(store, NOW, batch=10, seconds=1)
    assert state["state"] == "COMPLETE" and state["decisions_complete"]
    assert state["records"] == 1000 and state["cursor"] == previous["cursor"]
    assert state["saved_payload_bytes"] > 123
    assert h.get(key) == value and store.get_runtime(PREFIX + key) == {DECISION_HEAD: 1}
    assert work(store, NOW + 1) == state


@pytest.mark.asyncio
async def test_repack_slices_yield_and_stop_when_finite_queue_completes(store):
    import asyncio

    bounded(store)
    scheduled = []

    async def telegram_like_task():
        for _ in range(12):
            scheduled.append("delivery")
            await asyncio.sleep(0)

    task = asyncio.create_task(telegram_like_task())
    try:
        with patch("cryptowatcher.storage_repack.work", return_value={"state": "IN_PROGRESS"}) as tick:
            result = await async_work(store, NOW)
        assert tick.call_count == 8 and result["state"] == "IN_PROGRESS"
        assert len(scheduled) >= 7  # Delivery can run between maintenance transactions.
        with patch("cryptowatcher.storage_repack.work", return_value={"state": "COMPLETE"}) as tick:
            assert (await async_work(store, NOW))["state"] == "COMPLETE"
        assert tick.call_count == 1
    finally:
        await task


def test_ledger_is_exact_no_sql_temp_sort_and_cohort_ablation_matches_frozen_events(store):
    bounded(store)
    worker = OutcomeWorker(store)
    observe(store, worker)
    Sources(store).put("spot/BTCUSDT/1m", future())
    worker.work(NOW + 61 * 60_000)
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        # It must work with SQLite temp storage unavailable; no GROUP BY sort of runtime.
        reader.db.execute("PRAGMA temp_store=FILE")
        ledger = runtime_ledger(reader, seconds=5)
        assert ledger["state"] == "MEASURED"
        exact = reader.db.execute(
            "SELECT COUNT(*),SUM(length(CAST(key AS BLOB))),SUM(length(CAST(payload_json AS BLOB))) FROM runtime"
        ).fetchone()
        for field, expected in zip(("records", "key_bytes", "payload_bytes"), exact):
            assert sum(v[field] for v in ledger["namespaces"].values()) == expected
        cohorts = prospective_cohorts(reader)
        assert cohorts["state"] == "MEASURED_PROSPECTIVE"
        assert cohorts["facts_if_archived"] == cohorts["event_records"] * 4
        assert cohorts["dimension_cardinality"]["horizon"] == 4
        assert cohorts["distinct_without_dimension"]["horizon"] * 4 == cohorts["cohort_days_if_archived"]
        partial = runtime_ledger(reader, seconds=0)
        assert partial["state"] == "PARTIAL_BUDGET" and partial["records_scanned"] == 0


def test_quick_capacity_never_walks_runtime_or_quality_and_interval_does_not_invent_day(store):
    bounded(store)
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        queries = []
        reader.db.set_trace_callback(queries.append)
        value = snapshot(reader)
        assert value["runtime_ledger"]["state"] == value["physical_snapshot"]["state"] == "NOT_REQUESTED"
        assert value["quality_actual"]["state"] == "COUNTERS_ONLY"
        assert not any(
            "FROM market_quality" in q or "FROM dbstat" in q or "ORDER BY rowid" in q for q in queries
        )
    value["stability_blockers"] = []
    later = copy.deepcopy(value)
    later["at_ms"] += 120_000
    later["storage"]["working_bytes"] += 100
    result = interval(value, later)
    assert result["working_net_bytes"] == 100 and result["daily_extrapolation_bytes"] is None
    later["at_ms"] = value["at_ms"] + DAY
    assert interval(value, later)["state"] == "OBSERVED_24H"
    later["configuration_revision"] += 1
    assert interval(value, later)["state"] == "PARTIAL_OR_UNSTABLE"


def test_stable_baseline_distinguishes_referenced_backfill_from_normal_pending_horizons(store):
    bounded(store)
    with store.db:
        store.put_runtime(
            "ingestion",
            {
                "at": NOW / 1000,
                "bootstrap_pending_streams": 0,
                "blocked_streams": 0,
                "recovery_pending": 0,
                "recovery_working": 0,
                "historical_gaps_pending": 0,
                "subscriptions": {"spot:0": {"state": "CONNECTED", "requested": 1, "acknowledged": 1}},
            },
        )
        store.put_runtime("radar:v1:outcome-admission", {"complete": True})
        store.put_runtime("storage_repack", {"state": "COMPLETE"})
        store.put_runtime("analytics", {"state": "OK", "at_ms": NOW})
        History(store).put("outcome-pending:future-horizon", {"key": "normal-scheduled-work"})
    with patch("cryptowatcher.capacity.time.time", return_value=NOW / 1000):
        assert stable_reason(store) == []
        with store.db:
            History(store).put("analytics-gap-pending:queued", {"id": "referenced-backfill"})
        assert stable_reason(store) == ["ANALYTICS_RECOVERY_PENDING"]
        with store.db:
            History(store).delete("analytics-gap-pending:queued")
        assert stable_reason(store) == []


def test_forecast_attribution_does_not_add_indexes_twice_and_sampling_is_finite():
    value = {
        "page_overhead_factor": 2,
        "forecast": [{"category": "details", "projected_logical_bytes": 300}],
        "projected_working_bytes": 1000,
    }
    ledger = {"namespaces": {"event": {"payload_bytes": 10}, "outcome": {"payload_bytes": 20}}}
    result = explain_forecast(value, ledger)
    assert (
        sum(v["allocated_logical_forecast_bytes"] for v in result["runtime_namespace_allocation"].values())
        == 300
    )
    assert result["point_bytes"] == 1000
    state = {"state": "WAITING_STABLE", "created_at": 0}
    pending = {"at_ms": 1000, "stability_blockers": ["REPACK"]}
    assert advance(state, pending, 1)["state"] == "WAITING_STABLE"
    good = {"at_ms": 2000, "stability_blockers": []}
    state = advance(state, good, 2)
    assert state["state"] == "WAITING_STABLE"
    assert advance(state, good, 302)["state"] == "READY_FOR_BASELINE"
    assert advance(state, pending, 12 * 3600 + 1)["state"] == "WAIT_LIMIT_REACHED"
    observing = {"state": "OBSERVING", "created_at": 0, "baseline_ms": 2000}
    assert advance(observing, good, 86402)["state"] == "READY_FOR_FINAL"


def test_retention_telemetry_is_transactional_and_completed_receipts_expire(store):
    bounded(store)
    h = History(store)
    with store.db:
        h.put("analytics-gap:completed", {"state": "COMPLETE", "completed_ms": NOW, "id": "completed"})
        h.put("analytics-gap:pending", {"state": "PENDING", "id": "pending"})
    retention = Retention(store)
    retention.work(NOW + DAY - 1)
    assert h.get("analytics-gap:completed") is not None
    retention.work(NOW + DAY + 1)
    assert h.get("analytics-gap:completed") is None and h.get("analytics-gap:pending") is not None
    totals = store.get_runtime("market_retention_totals")
    assert totals["removed_services"] >= 1 and totals["runs"] == 2
    store.db.executescript("""CREATE TEMP TRIGGER interrupt_retention BEFORE UPDATE ON runtime
        WHEN NEW.key='market_retention_totals' BEGIN SELECT RAISE(ABORT,'Injected telemetry write failure'); END;""")
    with pytest.raises(sqlite3.IntegrityError):
        retention.work(NOW + 31 * DAY)
    assert store.get_runtime("market_retention_totals") == totals
    assert h.get("analytics-gap:pending") is not None


def test_observer_metadata_write_is_atomic_and_private(tmp_path):
    path = tmp_path / "status.json"
    atomic(path, {"state": "WAITING_STABLE"})
    atomic(path, {"state": "OBSERVING"})
    assert json.loads(path.read_text(encoding="utf-8"))["state"] == "OBSERVING"
    assert not path.with_suffix(".tmp").exists()
    with patch.object(path.__class__, "is_symlink", return_value=True), pytest.raises(ValueError):
        atomic(path, {"state": "must not write"})


def test_legacy_capacity_is_explicitly_unmetered_and_flags_do_not_affect_other_commands(
    store, monkeypatch, infrastructure, capsys
):
    from .test_cli import cli

    assert cli(monkeypatch, infrastructure, "capacity") == 0
    value = json.loads(capsys.readouterr().out)
    assert value["state"] == "N/A_LEGACY_LAYOUT"
    assert cli(monkeypatch, infrastructure, "diagnostics", "--capacity-ledger") == 2


def test_controlled_62week_population_plateaus_and_all_numeric_results_survive(tmp_path):
    from scripts.rehearse_retention import measure

    value = measure(tmp_path / "rolling-retention.sqlite3")
    assert len(value["families"]) == 5 and value["markets"] == 2 and len(value["timeframes"]) == 4
    assert value["numeric_and_comparator_multiset_equality"] == value["private_equality"] == "PASS"
    assert value["restarts"] == 1 and not any(value["final_remaining"].values())
    stable = value["steady_detail_population"][-8:]
    assert {s["events"] for s in stable} == {50}
    assert {s["quality_facts"] for s in stable} == {1920}
    assert {s["source_rows"] for s in stable} == {840}
    # Observed file variation fits within already allocated free-page slack.
    # This is an isolated allocator observation, not a production20GB proof.
    assert max(s["allocated_bytes"] for s in stable) - min(s["allocated_bytes"] for s in stable) <= max(
        s["free_bytes"] for s in stable
    )


def test_twenty_comparators_frozen_versions_retention_boundaries_and_source_leases(store):
    from cryptowatcher.analytics import ComparisonWorker, cohorts
    from cryptowatcher.market_storage import decoded
    from cryptowatcher.outcomes import DEFINITION, MINUTE

    from .test_analytics import series
    from .test_outcomes import Candle

    bounded(store)
    config = store.snapshot()
    store.update(config.revision, config.settings.changed(outcome_horizons=(5, 15)))
    series(store, days=6)
    worker = OutcomeWorker(store)
    observe(store, worker)
    sources = [
        Candle.parse(
            [NOW + i * MINUTE, 102, 104, 100, 103, 1, 400],
            "1m",
            "fixture",
            NOW + 16 * MINUTE,
            NOW + 16 * MINUTE,
        )
        for i in range(15)
    ]
    Sources(store).put("spot/BTCUSDT/1m", sources, recovery=True)
    h = History(store)
    comparison = ComparisonWorker(store)
    for _ in range(h.count("comparison-pending:")):
        comparison.work(NOW + 16 * MINUTE)
    worker.work(NOW + 16 * MINUTE, batch=200)
    comparisons = [value for _, value in h.items("comparison-head:")]
    assert max(v["sample_size"] for v in comparisons) == 20
    event = next(e for e in h.events(20) if e["detector_id"] == "price")
    original = h.get("outcome-head:" + identity(f"outcome/{event['event_id']}/5/1"))
    alternate = {
        **original,
        "methodology_version": "fixture-2",
        "outcome_id": identity("alternate-method"),
        "revision": 1,
    }
    with store.db:
        h.put("outcome-method:fixture-2", {**DEFINITION, "version": "fixture-2"})
        h.version("outcome", alternate)
    before = cohorts(store)
    assert {r["group"]["methodology_version"] for r in before["cohorts"]} == {"1", "fixture-2"}
    retention = Retention(store)
    retention.work(NOW + 30 * DAY - 1)
    assert h.get("comparison-head:" + identity(f"comparison/{event['event_id']}/1"))["sample_size"] == 20
    for _ in range(100):
        retention.work(NOW + 30 * DAY + 1)
        if h.count("history:") == 0:
            break
    assert h.count("event-head:") == h.count("outcome-job:") == h.count("hold:") == 0
    after = cohorts(store)
    assert after["cohorts"] == before["cohorts"]
    metric_sets = [
        f["comparator_metrics"]
        for r in store.db.execute("SELECT facts FROM market_quality")
        for f in decoded(bytes(r[0]), 1024 * 1024)
    ]
    assert max(map(len, metric_sets)) == 20
    # A retry of detail cleanup must not duplicate numeric admissions.
    facts = store.db.execute("SELECT SUM(n) FROM market_quality").fetchone()[0]
    retention.work(NOW + 31 * DAY)
    assert store.db.execute("SELECT SUM(n) FROM market_quality").fetchone()[0] == facts
    for _ in range(100):
        retention.work(NOW + 36 * DAY)
    assert store.db.execute("SELECT COUNT(*) FROM market_candles").fetchone()[0] == 0
    assert h.count("comparison-job:") == 0
    for _ in range(100):
        retention.work(NOW + 400 * DAY)
    assert store.db.execute("SELECT COUNT(*) FROM market_quality").fetchone()[0] == 0


async def test_ownerless_recovery_skips_source_scan_and_terminal_receipt_can_rearm(store, monkeypatch):
    from cryptowatcher.analytics_recovery import AnalyticsRecovery
    from cryptowatcher.ingestion import BybitRadar
    from cryptowatcher.outcomes import MINUTE

    bounded(store)
    radar = BybitRadar(store)
    monkeypatch.setattr(radar, "server_now", lambda: NOW + 6 * MINUTE)
    radar._get = AsyncMock(side_effect=AssertionError("No REST for unused work"))
    recovery = AnalyticsRecovery(store, radar)
    h = History(store)
    stream = "spot/BTCUSDT/1m"
    hold = "hold:" + stream + ":test-owner"
    with store.db:
        h.put(hold, {"start": NOW, "end": NOW + 5 * MINUTE})
    recovery.request(stream, NOW, NOW + 5 * MINUTE)
    original = recovery.complete_locally
    with store.db:
        h.delete(hold)
    monkeypatch.setattr(
        recovery,
        "complete_locally",
        lambda *args: (_ for _ in ()).throw(AssertionError("Ownerless source scan")),
    )
    await recovery.work(NOW + 6 * MINUTE)
    job = h.items("analytics-gap:")[0][1]
    assert job["completion"] == "NO_RETAINED_OWNER" and h.count("analytics-gap-pending:") == 0
    with store.db:
        h.put(hold, {"start": NOW, "end": NOW + 5 * MINUTE})
    monkeypatch.setattr(recovery, "complete_locally", original)
    recovery.request(stream, NOW, NOW + 5 * MINUTE)
    assert h.get("analytics-gap:" + job["id"])["state"] == "PENDING"
    assert h.count("analytics-gap-pending:") == 1
    Sources(store).put(stream, future())
    await recovery.work(NOW + 6 * MINUTE)
    assert h.get("analytics-gap:" + job["id"])["state"] == "COMPLETE"
    assert h.count("analytics-gap-pending:") == 0
    radar._get.assert_not_awaited()
    await radar.close()
