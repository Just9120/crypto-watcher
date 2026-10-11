import asyncio
import sqlite3
import threading

import pytest

from cryptowatcher.analytics import ComparisonWorker, as_of
from cryptowatcher.analytics_worker import read_snapshot
from cryptowatcher.config import OwnerSettings
from cryptowatcher.detector_config import effective_parameters
from cryptowatcher.diagnostics import snapshot
from cryptowatcher.events import History
from cryptowatcher.ingestion import BybitRadar
from cryptowatcher.outcomes import MINUTE, OutcomeWorker
from cryptowatcher.sources import Candle, Sources
from cryptowatcher.storage import Store

NOW = 1_800_000_000_000


def seed(tmp_path, count=1):
    store = Store(tmp_path / "state.sqlite", 233382575)
    store.initialize(OwnerSettings(outcome_horizons=(5,)))
    outcomes = OutcomeWorker(store)
    history = History(store)
    candles = [
        Candle.parse(
            [NOW + i * MINUTE, 100, 102, 99, 101, 1, 100], "1m", "rest", NOW + 5 * MINUTE, NOW + 5 * MINUTE
        )
        for i in range(5)
    ]
    Sources(store).put("spot/BTCUSDT/1m", candles)
    with store.db:
        for i in range(count):
            event = {
                "event_id": str(i),
                "revision": 1,
                "episode_id": "episode",
                "available_at_ms": NOW,
                "reference_time_ms": NOW,
                "reference_price": 100,
                "reference_atr14": 2,
                "direction": "UP",
                "continuation_threshold_pct": 0.1,
                "timeframe": "1m",
                "symbol": "BTCUSDT",
                "market_type": "spot",
                "detector_id": "price",
                "detector_version": "1",
                "source_inputs": [],
                "settings_snapshot": store.snapshot().settings.payload(),
                "effective_parameters": effective_parameters(store.snapshot().settings, "spot/BTCUSDT"),
            }
            history.version("event", event)
            outcomes.admit(event)
    return store, outcomes, history


@pytest.mark.asyncio
async def test_snapshot_calculation_yields_and_publishes_only_at_completion(tmp_path, monkeypatch):
    store, worker, history = seed(tmp_path)
    entered, release = threading.Event(), threading.Event()
    calculate = worker.calculate
    clock = [NOW + 5 * MINUTE]

    def blocked(readonly, job, event):
        entered.set()
        assert release.wait(3)
        return calculate(readonly, job, event)

    monkeypatch.setattr(worker, "calculate", blocked)
    task = asyncio.create_task(worker.async_work(lambda: clock[0]))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        # This writer and heartbeat must remain available while the reader is busy.
        store.put_runtime("heartbeat", {"at": 123})
        assert history.count("outcome-head:") == 0
        clock[0] += 1000
    finally:
        release.set()
    assert await task == 1
    outcome = history.items("outcome-head:")[0][1]
    assert outcome["available_at_ms"] == clock[0]
    assert as_of(history, "outcome", outcome["outcome_id"], clock[0] - 1) is None
    assert outcome["metrics"]["price_return_pct"] == pytest.approx(1)
    assert store.get_runtime("heartbeat") == {"at": 123}
    store.close()


@pytest.mark.asyncio
async def test_cancelled_read_keeps_pending_for_the_next_worker(tmp_path, monkeypatch):
    store, worker, history = seed(tmp_path)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calculate = worker.calculate

    def blocked(readonly, job, event):
        entered.set()
        try:
            assert release.wait(3)
            return calculate(readonly, job, event)
        finally:
            finished.set()

    monkeypatch.setattr(worker, "calculate", blocked)
    task = asyncio.create_task(worker.async_work(lambda: NOW + 5 * MINUTE))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert history.count("outcome-pending:") == 1 and history.count("outcome-head:") == 0
        store.put_runtime("heartbeat", {"at": 123})
    finally:
        release.set()
    assert await asyncio.to_thread(finished.wait, 2)
    assert await OutcomeWorker(store).async_work(lambda: NOW + 5 * MINUTE) == 1
    assert history.count("outcome-pending:") == 0 and history.count("outcome-head:") == 1
    store.close()


@pytest.mark.asyncio
async def test_old_snapshot_cannot_overwrite_concurrent_job_change(tmp_path, monkeypatch):
    store, worker, history = seed(tmp_path)
    entered, release = threading.Event(), threading.Event()
    calculate = worker.calculate

    def blocked(readonly, job, event):
        entered.set()
        assert release.wait(3)
        return calculate(readonly, job, event)

    monkeypatch.setattr(worker, "calculate", blocked)
    task = asyncio.create_task(worker.async_work(lambda: NOW + 5 * MINUTE))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        key, job = history.items("outcome-job:")[0]
        with store.db:
            job["next_attempt_ms"] += 99
            history.put(key, job)
    finally:
        release.set()
    assert await task == 0
    assert history.count("outcome-head:") == 0
    assert history.get(key) == job
    store.close()


@pytest.mark.asyncio
async def test_backlog_batches_drain_and_restart_preserves_completed_sources(tmp_path):
    store, worker, history = seed(tmp_path, 40)
    assert await worker.async_work(lambda: NOW + 5 * MINUTE, batch=32) == 32
    assert await ComparisonWorker(store).async_work(lambda: NOW + 5 * MINUTE, batch=32) == 32
    assert history.count("outcome-pending:") == 8
    assert history.count("comparison-pending:") == 8
    before = history.items("outcome:")
    path = store.path
    store.close()
    store = Store(path, 233382575)
    history = History(store)
    assert await OutcomeWorker(store).async_work(lambda: NOW + 5 * MINUTE) == 8
    assert await ComparisonWorker(store).async_work(lambda: NOW + 5 * MINUTE) == 8
    assert history.count("outcome-pending:") == history.count("comparison-pending:") == 0
    assert all(history.get(k) == v for k, v in before)
    assert history.count("protected:") == 5
    assert history.count("comparison-head:") == 40
    store.close()


def test_readonly_worker_rejects_writes_and_diagnostics_do_not_fake_zero(tmp_path, monkeypatch):
    store, _, _ = seed(tmp_path)
    with pytest.raises(sqlite3.OperationalError):
        read_snapshot(store.path, store.owner_id, lambda reader: reader.db.execute("DELETE FROM runtime"))
    expected = store.db.execute("SELECT COUNT(*) FROM runtime").fetchone()[0]
    original = History.count

    def interrupted(history, prefix):
        if prefix == "outcome-pending:":
            exc = sqlite3.OperationalError("interrupted")
            exc.sqlite_errorcode = sqlite3.SQLITE_INTERRUPT
            raise exc
        return original(history, prefix)

    monkeypatch.setattr(History, "count", interrupted)
    report = snapshot(store)
    assert report["outcomes"]["pending"] == {"state": "PENDING", "reason": "DIAGNOSTIC_QUERY_BUDGET"}
    assert report["ingestion"]["diagnostic_state"] == "STALE"
    assert store.db.execute("SELECT COUNT(*) FROM runtime").fetchone()[0] == expected
    assert store.db.execute("SELECT 1").fetchone()[0] == 1
    store.close()


@pytest.mark.asyncio
async def test_ws_diagnostics_refresh_while_metadata_request_is_waiting(tmp_path, monkeypatch):
    store, _, _ = seed(tmp_path)
    radar = BybitRadar(store)
    entered = asyncio.Event()

    async def blocked_refresh(settings):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(radar, "refresh", blocked_refresh)
    manager = asyncio.create_task(radar._manager())
    await entered.wait()
    radar.connections["spot:0"] = {"state": "RECONNECTING", "requested": 4, "acknowledged": 0}
    reporter = asyncio.create_task(radar._diagnostic_loop())
    await asyncio.sleep(0)
    assert store.get_runtime("ingestion")["subscriptions"]["spot:0"]["state"] == "RECONNECTING"
    assert snapshot(store)["ingestion"]["diagnostic_state"] == "FRESH"
    manager.cancel()
    reporter.cancel()
    await asyncio.gather(manager, reporter, return_exceptions=True)
    await radar.close()
    store.close()
