import asyncio
import time
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from cryptowatcher.analytics_recovery import AnalyticsRecovery
from cryptowatcher.bot import Controller, application
from cryptowatcher.events import History
from cryptowatcher.ingestion import BybitRadar
from cryptowatcher.market import MarketError, RadarResult
from cryptowatcher.market_storage import enable
from cryptowatcher.record_codec import TYPED_MAGIC, pack_binary, pack_binary_v2
from cryptowatcher.storage import StateError, Store, encode
from cryptowatcher.storage_repack import work

from .test_outcomes import MINUTE, NOW


def seed(store, monkeypatch, count=6):
    enable(store.db)
    store.bounded = True
    store.clock_ms = lambda: NOW
    radar = BybitRadar(store)
    monkeypatch.setattr(radar, "server_now", lambda: NOW + 10 * MINUTE)
    recovery = AnalyticsRecovery(store, radar)
    h = History(store)
    for i in range(count):
        stream = f"spot/TEST{i}USDT/1m"
        with store.db:
            h.put("hold:" + stream + ":owner", {"start": NOW, "end": NOW + MINUTE})
        recovery.request(stream, NOW, NOW + MINUTE)

    async def fetch(endpoint, params):
        return {"time": NOW + 10 * MINUTE, "result": {"list": [[NOW, 100, 101, 99, 100, 1, 10]]}}

    radar._get = AsyncMock(side_effect=fetch)
    return radar, recovery, h, fetch


async def test_backfill_batches_retry_restart_and_preserves_exact_sources(store, monkeypatch):
    radar, recovery, h, fetch = seed(store, monkeypatch)
    failed = False

    async def fail_first(endpoint, params):
        nonlocal failed
        if not failed:
            failed = True
            raise MarketError("rate_limited")
        return await fetch(endpoint, params)

    radar._get.side_effect = fail_first
    await recovery.work(NOW + 10 * MINUTE, batch=4, seconds=10)
    assert radar._get.await_count == 4
    response_timing = radar._work_timing["referenced_recovery_response"]
    assert response_timing["calls"] == 3  # The failed REST wait is outside synchronous response work.
    assert response_timing["failures"] == 0
    assert response_timing["cpu_seconds"] >= 0 and response_timing["active_seconds"] >= 0
    assert h.count("analytics-gap-pending:") == 3
    assert sorted(v["state"] for _, v in h.items("analytics-gap:")) == [
        "COMPLETE",
        "COMPLETE",
        "COMPLETE",
        "PENDING",
        "PENDING",
        "RETRY",
    ]
    stored = [
        tuple(r) for r in store.db.execute("SELECT * FROM market_candles ORDER BY stream,start,revision")
    ]
    with closing(Store(store.path, store.owner_id)) as reopened:
        worker = AnalyticsRecovery(reopened, radar)
        await worker.work(NOW + 10 * MINUTE, seconds=10)
        assert worker.history.count("analytics-gap-pending:") == 1  # Retry is still in the future.
        await worker.work(NOW + 11 * MINUTE, seconds=10)
        assert worker.history.count("analytics-gap-pending:") == 0
        for row in stored:
            assert (
                tuple(
                    reopened.db.execute(
                        "SELECT * FROM market_candles WHERE stream=? AND start=? AND revision=?", row[:3]
                    ).fetchone()
                )
                == row
            )
        calls = radar._get.await_count
        await worker.work(NOW + 12 * MINUTE)
        assert radar._get.await_count == calls
    await radar.close()


@pytest.mark.parametrize("condition,pages", [("normal", 2), ("live_priority", 0), ("slow_page", 1)])
async def test_retirement_scheduler_delay_cannot_starve_rest_admission(store, monkeypatch, condition, pages):
    radar, recovery, h, fetch = seed(store, monkeypatch, 2)
    clock = [0.0]
    yielded = False
    original_sleep = asyncio.sleep

    async def delayed_yield(seconds):
        nonlocal yielded
        if seconds == 0 and not yielded:
            # Retirement's own work is small; another coroutine runs while it
            # yields. Its elapsed wall time must not consume REST admission.
            clock[0] += 1.1
            yielded = True
            if condition == "live_priority":
                radar._pending["linear/BTCUSDT/1m"] = (0, 1)
        await original_sleep(0)

    async def response(endpoint, params):
        if condition == "slow_page":
            clock[0] += 1.1
        return await fetch(endpoint, params)

    radar._get.side_effect = response
    try:
        with (
            patch("cryptowatcher.analytics_recovery.time.monotonic", side_effect=lambda: clock[0]),
            patch("cryptowatcher.analytics_recovery.asyncio.sleep", side_effect=delayed_yield),
        ):
            await recovery.work(NOW + 10 * MINUTE, seconds=1)
        assert yielded and radar._get.await_count == pages
        assert h.count("analytics-gap-pending:") == 2 - pages
        assert sum(job["state"] == "COMPLETE" for _, job in h.items("analytics-gap:")) == pages
        assert store.db.execute("SELECT count(*) FROM market_candles").fetchone()[0] == pages
        assert not radar.sources.windows and not radar.sources.live
        assert not store.db.in_transaction
    finally:
        await radar.close()


async def test_backfill_yields_to_new_live_gap_and_cancellation_preserves_pending(store, monkeypatch):
    radar, recovery, h, fetch = seed(store, monkeypatch)

    async def live_gap(endpoint, params):
        radar._pending["linear/BTCUSDT/1m"] = (0, 1)
        return await fetch(endpoint, params)

    radar._get.side_effect = live_gap
    await recovery.work(NOW + 10 * MINUTE, seconds=10)
    assert radar._get.await_count == 1 and h.count("analytics-gap-pending:") == 5
    radar._pending.clear()
    entered = asyncio.Event()

    async def blocked(endpoint, params):
        entered.set()
        await asyncio.Event().wait()

    radar._get.side_effect = blocked
    before = h.items("analytics-gap:")
    task = asyncio.create_task(recovery.work(NOW + 10 * MINUTE))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert h.items("analytics-gap:") == before and not store.db.in_transaction
    await radar.close()


async def test_independent_backfill_pages_overlap_with_two_slot_and_batch_bounds(store, monkeypatch):
    radar, recovery, h, fetch = seed(store, monkeypatch, 6)
    ready, release = asyncio.Event(), asyncio.Event()
    active = peak = 0
    symbols = []

    async def delayed(endpoint, params):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        symbols.append(params["symbol"])
        if active == 2:
            ready.set()
        try:
            await release.wait()
            await asyncio.sleep(0)
            return await fetch(endpoint, params)
        finally:
            active -= 1

    radar._get.side_effect = delayed
    task = asyncio.create_task(recovery.work(NOW + 10 * MINUTE, batch=5, seconds=10))
    try:
        await asyncio.wait_for(ready.wait(), 2)
        assert active == peak == 2 and radar._get.await_count == 2
        release.set()
        await task
        assert active == 0 and peak == 2 and len(symbols) == len(set(symbols)) == 5
        assert h.count("analytics-gap-pending:") == 1
        assert store.db.execute("SELECT count(*) FROM market_candles").fetchone()[0] == 5
        assert not radar.sources.windows and not store.db.in_transaction
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await radar.close()


async def test_parallel_backfill_cancellation_joins_every_page_without_consuming_jobs(store, monkeypatch):
    radar, recovery, h, _ = seed(store, monkeypatch, 3)
    ready = asyncio.Event()
    active = 0
    cancelled = []

    async def blocked(endpoint, params):
        nonlocal active
        active += 1
        if active == 2:
            ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1
            cancelled.append(params["symbol"])

    radar._get.side_effect = blocked
    before = h.items("analytics-gap:")
    task = asyncio.create_task(recovery.work(NOW + 10 * MINUTE))
    try:
        await asyncio.wait_for(ready.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert active == 0 and len(cancelled) == len(set(cancelled)) == 2
        assert h.items("analytics-gap:") == before and h.count("analytics-gap-pending:") == 3
        assert store.db.execute("SELECT count(*) FROM market_candles").fetchone()[0] == 0
        assert not store.db.in_transaction
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await radar.close()


async def test_live_recovery_arriving_during_parallel_io_preempts_next_pages(store, monkeypatch):
    radar, recovery, h, fetch = seed(store, monkeypatch, 6)
    ready, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def blocked(endpoint, params):
        nonlocal calls
        calls += 1
        if calls == 2:
            ready.set()
        await release.wait()
        return await fetch(endpoint, params)

    radar._get.side_effect = blocked
    task = asyncio.create_task(recovery.work(NOW + 10 * MINUTE, seconds=10))
    try:
        await asyncio.wait_for(ready.wait(), 2)
        radar._pending["linear/BTCUSDT/1m"] = (0, 1)
        release.set()
        await task
        assert calls == 2 and h.count("analytics-gap-pending:") == 4
        assert store.db.execute("SELECT count(*) FROM market_candles").fetchone()[0] == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await radar.close()


async def test_inflight_superseded_backfill_cannot_resurrect_old_pending(store, monkeypatch):
    radar, recovery, h, fetch = seed(store, monkeypatch, 1)

    async def supersede(endpoint, params):
        name, job = h.items("analytics-gap:")[0]
        with store.db:
            h.delete(job["pending_key"])
            h.put(name, {**job, "state": "SUPERSEDED"})
        return await fetch(endpoint, params)

    radar._get.side_effect = supersede
    await recovery.work(NOW + 10 * MINUTE, seconds=10)
    assert h.count("analytics-gap-pending:") == 0
    assert h.items("analytics-gap:")[0][1]["state"] == "SUPERSEDED"
    assert store.db.execute("SELECT count(*) FROM market_candles").fetchone()[0] == 0
    await radar.close()


async def test_stale_pending_binding_is_retired_without_reopening_terminal_job(store, monkeypatch):
    radar, recovery, h, _ = seed(store, monkeypatch, 1)
    key, job = h.items("analytics-gap:")[0]
    with store.db:
        h.put(key, {**job, "state": "SUPERSEDED", "next_attempt_ms": NOW + 20 * MINUTE})
    await recovery.work(NOW + 10 * MINUTE)
    radar._get.assert_not_awaited()
    assert h.count("analytics-gap-pending:") == 0 and h.get(key)["state"] == "SUPERSEDED"
    await radar.close()


def test_repack_finishes_captured_range_despite_new_writes_and_keeps_progress(store):
    enable(store.db)
    store.bounded = True
    original = encode({"metrics": {"price_return_pct": -0.0}, "source": "retained"})
    with store.db:
        for i in range(8):
            store.db.execute(
                "INSERT INTO runtime VALUES (?,?)", (f"radar:v1:fixture:{i}", pack_binary_v2(original))
            )
    first = work(store, NOW, batch=1, seconds=1)
    assert not first["records_complete"]
    upper = first["records_upper"]
    with closing(Store(store.path, store.owner_id)) as reopened:
        for i in range(30):
            with reopened.db:
                History(reopened).put(f"zzz-new:{i:04}", {"new": i})
            state = work(reopened, NOW + i, batch=1, seconds=1)
            assert state["records_upper"] == upper and state["records"] >= first["records"]
            if state.get("records_complete"):
                break
        assert state["records_complete"] and state["records"] == 8
        assert [encode(reopened.get_runtime(f"radar:v1:fixture:{i}")) for i in range(8)] == [original] * 8
        assert History(reopened).get(f"zzz-new:{i:04}") == {"new": i}


def test_repack_validates_typed_envelopes_without_recompressing_them(store):
    enable(store.db)
    store.bounded = True
    raw = encode({"source_inputs": [], "settings_revision": 1, "metrics": {"price_return_pct": 1.25}})
    packed = pack_binary(raw)
    assert packed.startswith(TYPED_MAGIC)
    with store.db:
        store.db.execute("INSERT INTO runtime VALUES (?,?)", ("radar:v1:fixture", packed))
    with patch("cryptowatcher.record_codec.pack_binary", side_effect=AssertionError("Recompressed target")):
        assert work(store, NOW, seconds=1)["records_complete"]
    assert (
        store.db.execute("SELECT payload_json FROM runtime WHERE key='radar:v1:fixture'").fetchone()[0]
        == packed
    )
    with store.db:
        store.db.execute("DELETE FROM runtime WHERE key='storage_repack'")
        store.db.execute("UPDATE runtime SET payload_json=? WHERE key='radar:v1:fixture'", (packed[:-1],))
    with pytest.raises(StateError):
        work(store, NOW, seconds=1)
    assert store.get_runtime("storage_repack") is None


def test_slow_repack_phase_cannot_starve_later_phases_across_checkpoints(store):
    enable(store.db)
    store.bounded = True
    with store.db:
        for i in range(8):
            History(store).put(f"fixture:{i}", {"source": "retained", "metrics": {"v": i}})
        store.put_runtime("storage_repack", {"next_phase": 1})
    clock = [0.0]

    def slow_decisions(store, state, batch, deadline):
        clock[0] += 1  # One SQL operation overruns a40ms slice.

    with (
        patch("cryptowatcher.storage_repack.time.monotonic", side_effect=lambda: clock[0]),
        patch("cryptowatcher.storage_repack.decision_heads", side_effect=slow_decisions),
    ):
        first = work(store, NOW)
        assert first["records"] == 0
        with closing(Store(store.path, store.owner_id)) as reopened:
            second = work(reopened, NOW + 1)
            assert second["records"] == 8 and second["records_complete"]
            assert History(reopened).get("fixture:7") == {"source": "retained", "metrics": {"v": 7}}


async def test_maintenance_and_heartbeat_continue_while_analytics_is_waiting(
    store, infrastructure, monkeypatch
):
    radar, _, h, _ = seed(store, monkeypatch, 3)
    controller = Controller(infrastructure, store, radar)
    ready_ingestion(store)
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_outcomes(clock):
        entered.set()
        await release.wait()
        return 0

    monkeypatch.setattr(controller.outcomes, "async_work", slow_outcomes)
    with patch("cryptowatcher.bot.ComparisonWorker.async_work", new=AsyncMock(return_value=0)):
        task = asyncio.create_task(controller.analytics_job(None))
        await entered.wait()
        try:
            await controller.repack_job(None)
            await controller.backfill_job(None)
            await controller.heartbeat(None)
            assert not task.done() and controller.analytics_lock.locked()
            assert h.count("analytics-gap-pending:") == 0
            assert store.get_runtime("repack_worker")["state"] == "OK"
            assert store.get_runtime("heartbeat")["components_healthy"]
        finally:
            release.set()
            await task
    app = application(controller)
    callbacks = {job.callback for job in app.job_queue.jobs()}
    assert {controller.repack_job, controller.backfill_job, controller.analytics_job} <= callbacks
    await radar.close()


def ready_ingestion(store):
    value = {
        "at": time.time(),
        "bootstrap_pending_streams": 0,
        "blocked_streams": 0,
        "recovery_pending": 0,
        "recovery_working": 0,
    }
    with store.db:
        store.put_runtime("ingestion", value)
        store.put_runtime(
            "last_poll_status", {"state": "OK", "at": time.time(), "available": 1, "requested": 1}
        )
    return value


async def test_usable_degraded_poll_does_not_starve_maintenance(store, infrastructure, monkeypatch):
    radar, _, h, _ = seed(store, monkeypatch, 1)
    controller = Controller(infrastructure, store, radar)
    ready_ingestion(store)
    with store.db:
        store.put_runtime(
            "last_poll_status",
            {
                "state": "DEGRADED",
                "at": time.time(),
                "available": 1149,
                "requested": 1152,
                "errors": {
                    f"linear/NEW{i}USDT/15m": "insufficient_history_or_zero_baseline" for i in range(3)
                },
            },
        )
    await controller.repack_job(None)
    await controller.backfill_job(None)
    assert store.get_runtime("repack_worker")["state"] == "OK"
    assert h.count("analytics-gap-pending:") == 0
    # Degraded without usable data is not market readiness.
    with store.db:
        store.put_runtime(
            "last_poll_status", {"state": "DEGRADED", "at": time.time(), "available": 0, "requested": 10}
        )
    assert not controller.maintenance_ready()
    await radar.close()


async def test_warmup_refreshes_cached_readiness_before_normal_poll_and_stops(
    store, infrastructure, monkeypatch
):
    radar, _, _, _ = seed(store, monkeypatch, 0)
    controller = Controller(infrastructure, store, radar)
    before = store.snapshot()
    clock = [time.time()]
    monkeypatch.setattr("cryptowatcher.bot.time.time", lambda: clock[0])
    radar.snapshot = AsyncMock(return_value=RadarResult({"spot/BTCUSDT/1m": {}}, {}, 1))
    radar.delivery_ready = Mock(side_effect=[False, True])
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    await controller.poll(context)
    assert store.get_runtime("last_poll_status")["state"] == "WARMING"
    clock[0] += 6
    await controller.warmup_poll(context)
    assert radar.snapshot.await_count == 2 and store.get_runtime("last_poll_status")["state"] == "OK"
    assert controller.first_ready_poll
    clock[0] += 6
    await controller.warmup_poll(context)
    await controller.poll(context)
    assert radar.snapshot.await_count == 2  # Normal60s owner cadence is unchanged.
    assert store.snapshot() == before
    radar._get.assert_not_awaited()
    context.bot.send_message.assert_not_awaited()
    await radar.close()


async def test_idle_live_recovery_keeps_progress_when_cached_status_returns_to_warming(
    store, infrastructure, monkeypatch
):
    radar, _, h, _ = seed(store, monkeypatch, 1)
    controller = Controller(infrastructure, store, radar)
    ready_ingestion(store)
    assert controller.maintenance_ready()  # Valid ready evidence of this process.
    with store.db:
        store.put_runtime(
            "last_poll_status", {"state": "WARMING", "at": time.time(), "available": 1, "requested": 2}
        )
    await controller.repack_job(None)
    await controller.backfill_job(None)
    assert h.count("analytics-gap-pending:") == 0
    assert store.get_runtime("repack_worker")["state"] == "OK"
    with store.db:
        store.put_runtime("ingestion", {**store.get_runtime("ingestion"), "recovery_pending": 1})
    assert not controller.maintenance_ready()  # Actual live work still preempts maintenance.
    await radar.close()


async def test_maintenance_misfires_coalesce_without_parallel_replays(store, infrastructure, monkeypatch):
    radar, _, _, _ = seed(store, monkeypatch, 0)
    controller = Controller(infrastructure, store, radar)
    app = application(controller)
    selected = {
        controller.poll,
        controller.warmup_poll,
        controller.heartbeat,
        controller.deliver,
        controller.analytics_job,
        controller.repack_job,
        controller.backfill_job,
        controller.enrichment_job,
        controller.agent_job,
    }
    jobs = [j for j in app.job_queue.jobs() if j.callback in selected]
    assert len(jobs) == len(app.job_queue.jobs()) == 9
    for job in jobs:
        assert job.job.max_instances == 1 and job.job.coalesce
        assert job.job.misfire_grace_time == 30
    await radar.close()


async def test_real_scheduler_executes_briefly_late_heartbeat_and_rejects_expired_run(
    store, infrastructure, monkeypatch
):
    from datetime import datetime, timedelta, timezone

    from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_MISSED
    from apscheduler.executors.asyncio import run_coroutine_job

    radar, _, _, _ = seed(store, monkeypatch, 0)
    controller = Controller(infrastructure, store, radar)
    app = application(controller)
    scheduler = app.job_queue.scheduler
    scheduler.start(paused=True)  # Resolve real scheduler defaults without running other callbacks.
    job = next(j.job for j in app.job_queue.jobs() if j.callback == controller.heartbeat)
    calls = []

    async def heartbeat():
        await controller.heartbeat(None)
        calls.append(store.get_runtime("heartbeat"))

    job.modify(func=heartbeat, args=(), kwargs={})
    try:
        recent = datetime.now(timezone.utc) - timedelta(seconds=4)
        events = await run_coroutine_job(job, "default", [recent], "test.scheduler")
        assert [e.code for e in events] == [EVENT_JOB_EXECUTED]
        assert len(calls) == 1 and calls[0]["components_healthy"] is True
        expired = datetime.now(timezone.utc) - timedelta(seconds=31)
        events = await run_coroutine_job(job, "default", [expired], "test.scheduler")
        assert [e.code for e in events] == [EVENT_JOB_MISSED]
        assert len(calls) == 1  # The finite grace is not permission to run arbitrary old work.
    finally:
        scheduler.shutdown(wait=False)
        await asyncio.sleep(0)
        await radar.close()


@pytest.mark.parametrize(
    "busy",
    [
        "bootstrap_pending_streams",
        "blocked_streams",
        "recovery_pending",
        "recovery_working",
        "stale",
        "warming",
        "prior_runtime_poll",
    ],
)
async def test_maintenance_yields_disk_to_startup_and_live_recovery(store, infrastructure, monkeypatch, busy):
    radar, _, _, _ = seed(store, monkeypatch, 1)
    controller = Controller(infrastructure, store, radar)
    ingestion = ready_ingestion(store)
    if busy == "stale":
        ingestion["at"] -= 31
    elif busy not in ("warming", "prior_runtime_poll"):
        ingestion[busy] = 1
    with store.db:
        store.put_runtime("ingestion", ingestion)
        store.put_runtime(
            "last_poll_status",
            {
                "state": "WARMING" if busy == "warming" else "OK",
                "at": controller.maintenance_started_at - 1 if busy == "prior_runtime_poll" else time.time(),
            },
        )
    before = store.db.total_changes
    with (
        patch(
            "cryptowatcher.storage_repack.async_work", new=AsyncMock(return_value={"state": "IN_PROGRESS"})
        ) as repack,
        patch.object(controller.recovery, "work", new=AsyncMock()) as backfill,
    ):
        await controller.repack_job(None)
        await controller.backfill_job(None)
        repack.assert_not_awaited()
        backfill.assert_not_awaited()
    assert store.db.total_changes == before
    ready_ingestion(store)
    with (
        patch(
            "cryptowatcher.storage_repack.async_work", new=AsyncMock(return_value={"state": "IN_PROGRESS"})
        ) as repack,
        patch.object(controller.recovery, "work", new=AsyncMock()) as backfill,
    ):
        await controller.repack_job(None)
        await controller.backfill_job(None)
        repack.assert_awaited_once_with(store, radar.server_now(), slices=1, measure=radar._measure)
        backfill.assert_awaited_once()
    await radar.close()


def steady_repack(store, infrastructure, monkeypatch):
    radar, _, _, _ = seed(store, monkeypatch, 0)
    controller = Controller(infrastructure, store, radar)
    wall = radar.server_now() / 1000
    monkeypatch.setattr("cryptowatcher.bot.time.time", lambda: wall)
    controller.maintenance_started_at = wall - 601
    ready_ingestion(store)
    store.put_runtime(
        "ingestion",
        {
            **store.get_runtime("ingestion"),
            "server_ms": radar.server_now(),
            "active_streams": 1,
            "confirmed_sources": {
                "state": "MEASURED",
                "streams": 1,
                "unknown_streams": 0,
                "late_streams": 0,
                "earliest_deadline_ms": radar.server_now() + 30000,
                "grace_ms": 30000,
            },
        },
    )
    store.put_runtime("analytics", {"state": "OK", "at_ms": radar.server_now()})
    assert controller.maintenance_ready()
    return controller, radar, wall


@pytest.mark.parametrize(
    "remaining,deferred", [(0, False), (None, False), (-1, False), (True, False), ("1", False), (1, True)]
)
async def test_successful_expiry_only_defers_packing_for_a_real_remaining_due_batch(
    store, infrastructure, monkeypatch, remaining, deferred
):
    controller, radar, _ = steady_repack(store, infrastructure, monkeypatch)
    store.put_runtime("storage_repack", {"initial_reclaim_complete": True})
    store.put_runtime(
        "market_retention",
        {"state": "OK", "at_ms": radar.server_now(), "expired_events": 1, "remaining_due_batch": remaining},
    )
    assert controller.retention_backlog_current()
    assert controller.retention_pressure_current() is deferred
    with (
        patch.object(controller, "retention_priority", new=AsyncMock(return_value=0)),
        patch(
            "cryptowatcher.storage_repack.async_work", new=AsyncMock(return_value={"state": "COMPLETE"})
        ) as repack,
    ):
        await controller.repack_job(None)
    assert repack.await_args.kwargs == (
        {"slices": 1, "defer_compaction": True, "measure": radar._measure}
        if deferred
        else {"slices": 1, "measure": radar._measure}
    )
    await radar.close()


@pytest.mark.parametrize("stop,expected", [("burst", 4), ("deadline", 3), ("empty", 1), ("live", 1)])
async def test_existing_maintenance_prioritizes_expiry_yields_and_keeps_other_work_bounded(
    store, infrastructure, monkeypatch, stop, expected
):
    controller, radar, _ = steady_repack(store, infrastructure, monkeypatch)
    store.put_runtime("storage_repack", {"initial_reclaim_complete": True})
    pending = {"state": "OK", "at_ms": radar.server_now(), "expired_events": 1, "remaining_due_batch": 50}
    store.put_runtime("market_retention", pending)
    clock = [0.0]
    entered = asyncio.Event()
    turns = []

    def expire(now):
        if turns:
            assert entered.is_set()
        turns.append(now)
        if stop == "deadline":
            clock[0] += 0.201
        store.put_runtime(
            "market_retention",
            {**pending, "expired_events": 0, "remaining_due_batch": 0} if stop == "empty" else pending,
        )

    async def another_turn():
        while not turns:
            await asyncio.sleep(0)
        entered.set()
        if stop == "live":
            radar._pending["spot/NEWUSDT/1m"] = (0, 0)

    task = asyncio.create_task(another_turn())
    with (
        patch("cryptowatcher.bot.time.monotonic", side_effect=lambda: clock[0]),
        patch.object(controller.retention, "work", side_effect=expire),
        patch(
            "cryptowatcher.storage_repack.async_work", new=AsyncMock(return_value={"state": "COMPLETE"})
        ) as repack,
    ):
        await controller.repack_job(None)
        await task
    assert len(turns) == expected
    telemetry = store.get_runtime("retention_worker")
    assert telemetry["state"] == "OK" and telemetry["slices"] == expected
    assert telemetry["expired_events"] == (0 if stop == "empty" else expected)
    assert repack.await_count == (0 if stop == "live" else 1)
    if repack.await_count:
        assert repack.await_args.kwargs == (
            {"slices": 1, "measure": radar._measure}
            if stop == "empty"
            else {"slices": 1, "defer_compaction": True, "measure": radar._measure}
        )
    assert not controller.retention_lock.locked()
    await radar.close()


@pytest.mark.parametrize("condition", ["initial", "stale", "failed", "analytics", "retention", "startup"])
async def test_expiry_priority_preserves_initial_freshness_failure_and_lock_guards(
    store, infrastructure, monkeypatch, condition
):
    controller, radar, wall = steady_repack(store, infrastructure, monkeypatch)
    store.put_runtime("storage_repack", {"initial_reclaim_complete": condition != "initial"})
    pending = {"state": "OK", "at_ms": radar.server_now(), "expired_events": 1, "remaining_due_batch": 50}
    if condition == "stale":
        pending["at_ms"] -= 60001
    store.put_runtime("market_retention", pending)
    if condition == "failed":
        store.put_runtime("retention_worker", {"state": "FAILED", "at_ms": radar.server_now()})
    if condition == "startup":
        controller.maintenance_started_at = wall - 599
    lock = controller.analytics_lock if condition == "analytics" else controller.retention_lock
    if condition in ("analytics", "retention"):
        await lock.acquire()
    try:
        with patch.object(controller.retention, "work") as expire:
            assert await controller.retention_priority() == 0
            expire.assert_not_called()
    finally:
        if condition in ("analytics", "retention"):
            lock.release()
        await radar.close()


async def test_expiry_failure_is_sanitized_and_regular_success_allows_priority_again(
    store, infrastructure, monkeypatch
):
    controller, radar, _ = steady_repack(store, infrastructure, monkeypatch)
    store.put_runtime("storage_repack", {"initial_reclaim_complete": True})
    pending = {"state": "OK", "at_ms": radar.server_now(), "expired_events": 1, "remaining_due_batch": 50}
    store.put_runtime("market_retention", pending)
    with patch.object(controller.retention, "work", side_effect=RuntimeError("private fixture error body")):
        await controller.repack_job(None)
    assert store.get_runtime("retention_worker")["state"] == "FAILED"
    assert "private fixture error body" not in encode(store.get_runtime("retention_worker"))
    with patch.object(controller.retention, "work") as expire:
        assert await controller.retention_priority() == 0
        expire.assert_not_called()
        controller.retention_once(radar.server_now())
        assert store.get_runtime("retention_worker")["state"] == "OK"
    await radar.close()


async def test_expiry_priority_yields_to_analytics_and_serializes_the_regular_call(
    store, infrastructure, monkeypatch
):
    controller, radar, _ = steady_repack(store, infrastructure, monkeypatch)
    store.put_runtime("storage_repack", {"initial_reclaim_complete": True})
    pending = {"state": "OK", "at_ms": radar.server_now(), "expired_events": 1, "remaining_due_batch": 10}
    store.put_runtime("market_retention", pending)
    entered = asyncio.Event()
    calls = []

    def expire(now):
        calls.append("regular" if controller.analytics_lock.locked() else "priority")
        assert controller.retention_lock.locked()
        store.put_runtime(
            "market_retention",
            pending if len(calls) == 1 else {**pending, "expired_events": 0, "remaining_due_batch": 0},
        )
        entered.set()

    async def analytics_arrives():
        await entered.wait()
        await controller.analytics_job(None)

    task = asyncio.create_task(analytics_arrives())
    with (
        patch.object(controller.retention, "work", side_effect=expire),
        patch.object(controller.outcomes, "async_work", new=AsyncMock(return_value=0)),
        patch("cryptowatcher.bot.ComparisonWorker.async_work", new=AsyncMock(return_value=0)),
        patch("cryptowatcher.storage_repack.async_work", new=AsyncMock(return_value={"state": "COMPLETE"})),
    ):
        await controller.repack_job(None)
        await task
    assert calls == ["priority", "regular"]
    assert not controller.retention_lock.locked() and not controller.analytics_lock.locked()
    assert store.get_runtime("retention_worker")["state"] == "OK"
    await radar.close()


async def test_cancelling_expiry_priority_releases_locks_without_another_expiry_call(
    store, infrastructure, monkeypatch
):
    controller, radar, _ = steady_repack(store, infrastructure, monkeypatch)
    store.put_runtime("storage_repack", {"initial_reclaim_complete": True})
    pending = {"state": "OK", "at_ms": radar.server_now(), "expired_events": 1, "remaining_due_batch": 10}
    store.put_runtime("market_retention", pending)
    entered = asyncio.Event()

    def expire(now):
        store.put_runtime("market_retention", pending)
        entered.set()

    with patch.object(controller.retention, "work", side_effect=expire) as expiry:
        task = asyncio.create_task(controller.repack_job(None))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert expiry.call_count == 1
    assert not controller.retention_lock.locked() and not controller.repack_lock.locked()
    await radar.close()


@pytest.mark.parametrize(
    "condition",
    [
        "ready",
        "warming",
        "boundary",
        "cold",
        "coverage_boundary",
        "coverage",
        "old_poll",
        "old_ingestion",
        "old_analytics",
        "live",
        "working",
        "unknown",
        "late_sources",
        "unknown_sources",
        "missing_sources",
        "changed_source_scope",
        "source_clock_mismatch",
        "source_deadline",
        "source_deadline_boundary",
    ],
)
async def test_repack_boost_keeps_startup_freshness_and_coverage_guards(
    store, infrastructure, monkeypatch, condition
):
    controller, radar, wall = steady_repack(store, infrastructure, monkeypatch)
    if condition == "warming":
        store.put_runtime(
            "last_poll_status", {"state": "WARMING", "at": wall, "available": 1142, "requested": 1144}
        )
    elif condition == "cold":
        controller.maintenance_started_at = wall - 599
    elif condition == "boundary":
        controller.maintenance_started_at = wall - 600
    elif condition == "coverage_boundary":
        store.put_runtime("last_poll_status", {"state": "OK", "at": wall, "available": 95, "requested": 100})
    elif condition == "coverage":
        store.put_runtime("last_poll_status", {"state": "OK", "at": wall, "available": 94, "requested": 100})
    elif condition == "old_poll":
        store.put_runtime(
            "last_poll_status", {"state": "OK", "at": wall - 121, "available": 1, "requested": 1}
        )
    elif condition == "old_ingestion":
        store.put_runtime("ingestion", {**store.get_runtime("ingestion"), "at": wall - 6})
    elif condition == "old_analytics":
        store.put_runtime("analytics", {"state": "OK", "at_ms": radar.server_now() - 61000})
    elif condition == "live":
        radar._pending["spot/NEWUSDT/1m"] = (0, 0)
    elif condition == "working":
        radar._working.add("spot/NEWUSDT/1m")
    elif condition == "unknown":
        store.put_runtime("last_poll_status", {"state": "OK", "at": wall, "available": 1})
    elif condition in (
        "late_sources",
        "unknown_sources",
        "missing_sources",
        "changed_source_scope",
        "source_clock_mismatch",
        "source_deadline",
        "source_deadline_boundary",
    ):
        ingestion = store.get_runtime("ingestion")
        confirmed = ingestion["confirmed_sources"]
        if condition == "late_sources":
            confirmed["late_streams"] = 1
        elif condition == "unknown_sources":
            confirmed["unknown_streams"] = 1
        elif condition == "missing_sources":
            ingestion.pop("confirmed_sources")
        elif condition == "changed_source_scope":
            ingestion["active_streams"] = 2
        elif condition == "source_clock_mismatch":
            ingestion["server_ms"] -= 5001
        else:
            # The diagnostic itself remains within its5s freshness budget.
            # Its original earliest deadline must still expire between samples.
            confirmed["earliest_deadline_ms"] = radar.server_now() + 1000
            monkeypatch.setattr(
                "cryptowatcher.bot.time.time",
                lambda: wall + (2 if condition == "source_deadline" else 1),
            )
        store.put_runtime("ingestion", ingestion)
    with (
        patch("cryptowatcher.bot.time.monotonic", return_value=0),
        patch(
            "cryptowatcher.storage_repack.async_work", new=AsyncMock(return_value={"state": "IN_PROGRESS"})
        ) as repack,
    ):
        await controller.repack_job(None)
        assert repack.await_count == (
            4
            if condition in ("ready", "warming", "boundary", "coverage_boundary", "source_deadline_boundary")
            else 1
        )
        assert all(call.kwargs == {"slices": 1, "measure": radar._measure} for call in repack.await_args_list)
        assert store.get_runtime("repack_worker")["compaction_slices"] == repack.await_count
    assert store.get_runtime("repack_worker")["state"] == "OK"
    await radar.close()


async def test_steady_repack_yields_and_new_live_work_preempts_extra_slices(
    store, infrastructure, monkeypatch
):
    controller, radar, _ = steady_repack(store, infrastructure, monkeypatch)
    entered = asyncio.Event()

    def one_slice(*args):
        entered.set()
        return {"state": "IN_PROGRESS"}

    async def live_arrives():
        await entered.wait()
        radar._pending["spot/NEWUSDT/1m"] = (0, 0)

    task = asyncio.create_task(live_arrives())
    with patch("cryptowatcher.storage_repack.work", side_effect=one_slice) as repack:
        await controller.repack_job(None)
        await task
        assert repack.call_count == 1
    await radar.close()


@pytest.mark.parametrize("stop", ["deadline", "complete", "recovery", "coverage", "stale", "source_lag"])
async def test_steady_repack_stops_burst_on_deadline_completion_or_lost_readiness(
    store, infrastructure, monkeypatch, stop
):
    controller, radar, wall = steady_repack(store, infrastructure, monkeypatch)
    clock = [0.0]

    def one_slice(*args):
        if stop == "deadline":
            clock[0] += 0.201
        elif stop == "recovery":
            store.put_runtime("ingestion", {**store.get_runtime("ingestion"), "recovery_pending": 1})
        elif stop == "coverage":
            store.put_runtime(
                "last_poll_status", {"state": "OK", "at": wall, "available": 90, "requested": 100}
            )
        elif stop == "stale":
            store.put_runtime("ingestion", {**store.get_runtime("ingestion"), "at": wall - 31})
        elif stop == "source_lag":
            ingestion = store.get_runtime("ingestion")
            ingestion["confirmed_sources"]["late_streams"] = 1
            store.put_runtime("ingestion", ingestion)
        return {"state": "COMPLETE" if stop == "complete" else "IN_PROGRESS"}

    with (
        patch("cryptowatcher.bot.time.monotonic", side_effect=lambda: clock[0]),
        patch("cryptowatcher.storage_repack.work", side_effect=one_slice) as repack,
    ):
        await controller.repack_job(None)
        assert repack.call_count == 1
    await radar.close()


@pytest.mark.asyncio
async def test_completed_initial_repack_continues_pending_live_work_in_the_existing_worker(
    store, infrastructure, monkeypatch
):
    controller, radar, _ = steady_repack(store, infrastructure, monkeypatch)
    pending = {"state": "COMPLETE", "catalogue_maintenance": {"pending_work": True}}
    idle = {"state": "COMPLETE", "catalogue_maintenance": {"pending_work": False}}
    with patch("cryptowatcher.storage_repack.work", side_effect=[pending, idle]) as repack:
        await controller.repack_job(None)
        assert repack.call_count == 2
    await radar.close()
