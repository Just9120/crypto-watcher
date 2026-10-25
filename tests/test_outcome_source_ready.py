"""Read-only snapshot races and bounded recovery completion must not wait a minute."""

import copy
from unittest.mock import AsyncMock

import pytest

from cryptowatcher.analytics_recovery import AnalyticsRecovery
from cryptowatcher.events import History, identity
from cryptowatcher.ingestion import BybitRadar
from cryptowatcher.market_storage import enable
from cryptowatcher.outcomes import MINUTE, OutcomeWorker, pending_key
from cryptowatcher.sources import Sources
from cryptowatcher.storage import Store
from tests.test_outcomes import NOW, future, observe


def prepare(store):
    enable(store.db)
    store.bounded = True
    now = NOW + 5 * MINUTE + 200
    store.clock_ms = lambda: now
    snapshot = store.snapshot()
    store.update(snapshot.revision, snapshot.settings.changed(outcome_horizons=(5,)))
    radar = BybitRadar(store)
    radar.server_now = lambda: now
    recovery = AnalyticsRecovery(store, radar)
    worker = OutcomeWorker(store, recovery.request)
    observe(store, worker)
    h = History(store)
    job = h.items("outcome-job:")[0][1]
    event = h.get(f"event:{job['event_id']}:{job['event_revision']:010d}")
    return now, radar, recovery, worker, job, event


async def test_source_arrived_between_snapshot_and_publication_requeues_without_second_calculation(store):
    now, radar, recovery, worker, job, event = prepare(store)
    try:
        stale = worker.calculate(store, job, event)
        assert stale["state"] == "INCOMPLETE"
        Sources(store).put(job["stream"], future())
        worker.apply(job, event, {"outcome_id": job["outcome_id"]}, stale, now)
        current = worker.history.get("outcome-job:" + job["outcome_id"])
        assert current["next_attempt_ms"] == now and current["due_ms"] == NOW + 5 * MINUTE
        assert worker.history.get("outcome-head:" + job["outcome_id"])["state"] == "INCOMPLETE"
        worker.work(now + 1000)
        assert worker.history.get("outcome-head:" + job["outcome_id"])["state"] == "COMPLETE"
    finally:
        await radar.close()


@pytest.mark.parametrize("transport", ["websocket", "rest"])
async def test_completed_source_gap_wakes_due_job_before_one_minute_retry(store, transport):
    now, radar, recovery, worker, job, event = prepare(store)
    try:
        stale = worker.calculate(store, job, event)
        worker.apply(job, event, {"outcome_id": job["outcome_id"]}, stale, now)
        current = worker.history.get("outcome-job:" + job["outcome_id"])
        assert current["next_attempt_ms"] == now + MINUTE
        admitted = store.get_runtime("analytics_progress:outcome")["admitted_total"]
        radar._get = AsyncMock(
            return_value={
                "time": now,
                "result": {"list": [c.row() for c in reversed(future())]},
            }
        )
        if transport == "websocket":
            Sources(store).put(job["stream"], future())
            await recovery.retire_satisfied(now + 1000)
            radar._get.assert_not_awaited()
        else:
            await recovery.work(now + 1000)
            radar._get.assert_awaited_once()
        current = worker.history.get("outcome-job:" + job["outcome_id"])
        assert current["next_attempt_ms"] <= now + 1000
        assert current["due_ms"] == NOW + 5 * MINUTE
        assert store.get_runtime("analytics_progress:outcome")["admitted_total"] == admitted
        worker.work(now + 2000)
        assert worker.history.get("outcome-head:" + job["outcome_id"])["state"] == "COMPLETE"
        assert recovery.history.count("analytics-gap-pending:") == 0
    finally:
        await radar.close()


async def test_large_owner_fanout_is_bounded_restartable_and_keeps_future_stale_and_other_owners(store):
    now, radar, recovery, worker, template, event = prepare(store)
    restored = other_radar = None
    try:
        h = worker.history
        jobs = []
        for index in range(140):
            job = copy.deepcopy(template)
            outcome_id = identity(f"wake-fanout/{index}")
            job.update(outcome_id=outcome_id, state="INCOMPLETE", next_attempt_ms=now + MINUTE)
            job["pending_key"] = pending_key(job["next_attempt_ms"], outcome_id)
            job["hold_key"] = f"hold:{job['stream']}:{outcome_id}"
            if index == 0:
                job["due_ms"] = now + 10 * MINUTE
            if index == 1:
                job["state"] = "COMPLETE"
            if index == 2:
                job["stream"] = "linear/OTHERUSDT/1m"
            with store.db:
                h.put("outcome-job:" + outcome_id, job)
                h.put(job["hold_key"], {"start": NOW, "end": job["due_ms"]})
                if index != 3:
                    h.put(job["pending_key"], {"outcome_id": outcome_id})
            jobs.append(job)
        assert recovery.request(template["stream"], NOW, NOW + 5 * MINUTE)
        Sources(store).put(template["stream"], future())
        await recovery.retire_satisfied(now + 1000)
        gap = h.items("analytics-gap:")[0][1]
        assert gap["state"] == "PENDING" and gap["closed_data_ready"] is True
        assert gap["outcome_wake_after"] and gap["cursor_end"] == gap["end"]
        changed = sum(h.get("outcome-job:" + j["outcome_id"])["next_attempt_ms"] <= now + 1000 for j in jobs)
        assert 0 < changed <= 64
        # Reconstruct on the same committed database; do not rely on process RAM.
        restored = Store(store.path, store.owner_id)
        restored.clock_ms = lambda: now + 1000
        other_radar = BybitRadar(restored)
        other_radar.server_now = lambda: now + 1000
        restarted = AnalyticsRecovery(restored, other_radar)
        for _ in range(160):
            await restarted.retire_satisfied(now + 1000)
            if restarted.history.count("analytics-gap-pending:") == 0:
                break
        assert restarted.history.count("analytics-gap-pending:") == 0
        for index, job in enumerate(jobs):
            current = restarted.history.get("outcome-job:" + job["outcome_id"])
            if index < 4:
                assert current == job
            else:
                assert current["next_attempt_ms"] == now + 1000
                assert current["due_ms"] == job["due_ms"] and current["state"] == "INCOMPLETE"
        assert restored.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not restored.db.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        if other_radar is not None:
            await other_radar.close()
        if restored is not None:
            restored.close()
        await radar.close()


async def test_recovery_owner_wakeup_rolls_back_pending_rekey_and_durable_cursor(store, monkeypatch):
    now, radar, recovery, worker, job, event = prepare(store)
    try:
        worker.apply(job, event, {"outcome_id": job["outcome_id"]}, worker.calculate(store, job, event), now)
        Sources(store).put(job["stream"], future())
        gap = recovery.history.items("analytics-gap:")[0][1]
        before = worker.history.get("outcome-job:" + job["outcome_id"])
        original = recovery.history.put

        def fail(key, value):
            if key.startswith("outcome-job:"):
                raise RuntimeError("Injected wakeup transaction failure")
            return original(key, value)

        with monkeypatch.context() as patch:
            patch.setattr(recovery.history, "put", fail)
            with pytest.raises(RuntimeError, match="transaction failure"):
                recovery.finish_local(copy.deepcopy(gap), gap["pending_key"], now + 1000)
        assert worker.history.get("outcome-job:" + job["outcome_id"]) == before
        assert worker.history.get(before["pending_key"]) == {"outcome_id": job["outcome_id"]}
        assert recovery.history.get("analytics-gap:" + gap["id"]) == gap
    finally:
        await radar.close()
