"""Published snapshots keep immutable identity, callback isolation and fresh reads."""

from unittest.mock import patch

import pytest

from cryptowatcher import record_codec
from cryptowatcher.events import PREFIX, EventEngine, History, identity
from cryptowatcher.market_storage import enable
from cryptowatcher.outcomes import MINUTE, OutcomeWorker
from cryptowatcher.sources import Candle, Sources
from cryptowatcher.storage import RUNTIME_UPSERT, StateError, encode

from .test_events import NOW, observe, selected


def test_fresh_runtime_reads_reuse_only_validated_bytes_and_return_independent_objects(store):
    enable(store.db)
    store.bounded = True
    key = "fixture:pure-runtime"
    value = {"schema": "fixture", "nested": [{"price": 1.25, "note": "same" * 50}]}
    raw = record_codec.pack_binary(encode(value))
    with store.db:
        store.db.execute(RUNTIME_UPSERT, (key, raw))
    store.cached_record_json(raw)
    with patch.object(record_codec, "sql_payload", wraps=record_codec.sql_payload) as decoder:
        first = store.get_runtime(key)
        first["nested"][0]["price"] = 999
        assert store.get_runtime(key) == value
        assert decoder.call_count == 0
    newer = {**value, "revision": 2}
    with store.db:
        store.db.execute(RUNTIME_UPSERT, (key, record_codec.pack_binary(encode(newer))))
    assert store.get_runtime(key) == newer
    assert dict(store.scan_runtime("fixture:pure-")) == {key: newer}
    store.db.execute("BEGIN")
    store.db.execute("DELETE FROM runtime WHERE key=?", (key,))
    assert store.get_runtime(key) is None
    store.db.rollback()
    assert store.get_runtime(key) == newer
    with store.db:
        store.db.execute("DELETE FROM runtime WHERE key=?", (key,))
    assert store.get_runtime(key) is None
    assert store.reference_cache_bytes <= 1024 * 1024
    with pytest.raises(StateError):
        store.decode_runtime(raw[:-1])


def test_schema_less_episode_reference_still_checks_saved_body_and_existence(store):
    enable(store.db)
    store.bounded = True
    history = History(store)
    episode = {
        "episode_id": identity("published-episode"),
        "revision": 1,
        "state": "ACTIVE",
        "instrument": "spot/BTCUSDT",
        "direction": "UP",
        "started_ms": NOW,
        "last_observation_ms": NOW,
        "available_at_ms": NOW,
        "longest_tf_ms": MINUTE,
        "strength": 2.0,
        "confirmations": {},
        "conditions": [],
    }
    revision = f"episode:{episode['episode_id']}:0000000001"
    with store.db:
        history.version("episode", episode)
        history.put("agent:fixture", {"episode": history.revision_reference("episode", episode)})
    assert history.get("agent:fixture")["episode"] == episode
    stored = store.get_runtime(PREFIX + "agent:fixture")
    assert set(stored["episode"]) == {"_cw_revision", "sha256"}
    with store.db:
        history._put_raw(revision, {**episode, "strength": 3.0})
    with pytest.raises(StateError, match="hash mismatch"):
        history.get("agent:fixture")
    with store.db:
        history._put_raw(revision, episode)
    assert history.get("agent:fixture")["episode"] == episode
    with store.db:
        history.delete(revision)
    with pytest.raises(StateError, match="Missing retained service revision"):
        history.get("agent:fixture")


def test_paired_producer_consumer_and_replaced_legacy_callback_have_distinct_boundaries(store):
    saved, legacy = [], []

    def original(event):
        legacy.append(event["event_id"])

    def published(event, episode):
        assert store.db.in_transaction
        history = History(store)
        assert history.get("episode-head:" + episode["episode_id"]) == episode
        assert history.metadata("event-head:" + event["event_id"])["episode_id"] == episode["episode_id"]
        saved.append(event["event_id"])

    engine = EventEngine(store, selected("price"), on_event=original, on_event_snapshot=published)
    observe(engine)
    assert len(saved) == 1 and legacy == []
    engine.on_event = lambda event: legacy.append(event["event_id"])
    observe(engine, now=NOW + 3 * MINUTE, tf="3m")
    assert len(saved) == 1 and legacy


def test_outcome_consumer_receives_frozen_event_and_replacement_keeps_legacy_signature(store):
    paired, legacy = [], []

    def original(result):
        legacy.append(result["horizon_min"])

    def published(result, event):
        assert result["event_id"] == event["event_id"]
        assert result["event_revision"] == event["revision"]
        assert event["reference_time_ms"] == NOW
        paired.append(result["horizon_min"])

    worker = OutcomeWorker(store, on_outcome=original, on_outcome_event=published)
    engine = EventEngine(store, selected("price"), on_event=worker.admit)
    observe(engine)
    sources = Sources(store)

    def supply(first, last):
        end = NOW + last * MINUTE
        sources.put(
            "spot/BTCUSDT/1m",
            [
                Candle.parse([NOW + i * MINUTE, 102, 104, 101, 103, 1, 400], "1m", "rest", end, end)
                for i in range(first, last)
            ],
        )

    supply(0, 5)
    worker.work(NOW + 5 * MINUTE)
    assert paired == [5] and legacy == []
    worker.on_outcome = lambda result: legacy.append(result["horizon_min"])
    supply(5, 15)
    worker.work(NOW + 15 * MINUTE)
    assert paired == [5] and legacy == [15]


async def test_complete_publication_group_commits_four_results_and_times_after_commit(tmp_path):
    from .test_async_runtime import seed

    store, _worker, history = seed(tmp_path, count=8)
    worker = OutcomeWorker(store, publication_batch_size=4)
    worker.bootstrap(20)
    clock = [NOW + 5 * MINUTE]
    commits = []

    def trace(sql):
        if sql == "COMMIT":
            commits.append(sql)
            clock[0] += 1000

    store.db.set_trace_callback(trace)
    try:
        assert await worker.async_work(lambda: clock[0], batch=8) == 8
        assert history.count("outcome-pending:") == 0
        assert history.count("outcome-head:") == 8
        progress = store.get_runtime("analytics_progress:outcome")
        assert progress["apply_calls"] == 8
        # Two result commits, then the telemetry commit. The latency includes
        # each result group's commit rather than its pre-commit apply call.
        assert len(commits) == 3
        assert progress["max_completed_due_lag_ms"] == 2000
        assert not store.db.in_transaction
    finally:
        store.db.set_trace_callback(None)
        store.close()


async def test_failed_second_result_rolls_back_whole_group_and_remains_retryable(tmp_path, monkeypatch):
    from .test_async_runtime import seed

    store, _worker, history = seed(tmp_path, count=8)
    worker = OutcomeWorker(store, publication_batch_size=4)
    worker.bootstrap(20)
    second = history.items("outcome-pending:", 2)[1][1]["outcome_id"]
    calculate = worker.calculate

    def invalid(readonly, job, event):
        result = calculate(readonly, job, event)
        if job["outcome_id"] == second:
            result["metrics"]["price_return_pct"] = float("nan")
        return result

    monkeypatch.setattr(worker, "calculate", invalid)
    try:
        with pytest.raises(ValueError):
            await worker.async_work(lambda: NOW + 5 * MINUTE, batch=8)
        assert not store.db.in_transaction
        assert history.count("outcome-head:") == 0
        assert history.count("outcome-pending:") == 8
        monkeypatch.setattr(worker, "calculate", calculate)
        assert await worker.async_work(lambda: NOW + 5 * MINUTE, batch=8) == 8
        assert history.count("outcome-head:") == 8
        assert history.count("outcome-pending:") == 0
    finally:
        store.close()


async def test_cancellation_keeps_whole_committed_group_and_retries_remaining_jobs(tmp_path):
    import asyncio

    from .test_async_runtime import seed

    store, _worker, history = seed(tmp_path, count=8)
    calls = []
    task = None

    def legacy(_result):
        raise AssertionError("Registered independent consumer should receive its snapshot")

    def published(result, _event):
        calls.append(result["outcome_id"])
        if len(calls) == 1:
            task.cancel()

    worker = OutcomeWorker(store, publication_batch_size=4, on_outcome=legacy, on_outcome_event=published)
    task = asyncio.create_task(worker.async_work(lambda: NOW + 5 * MINUTE, batch=8))
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not store.db.in_transaction
        assert history.count("outcome-head:") == 4
        assert history.count("outcome-pending:") == 4
        worker.on_outcome_event = lambda *_args: None
        assert await worker.async_work(lambda: NOW + 5 * MINUTE, batch=8) == 4
        assert history.count("outcome-head:") == 8
        assert history.count("outcome-pending:") == 0
    finally:
        store.close()


@pytest.mark.parametrize("mode,expected", [("ok", 3), ("unknown", 1), ("owner_pause", 1)])
async def test_finite_agent_drain_preserves_singleton_ack_and_owner_stop_rules(store, mode, expected):
    import httpx

    from cryptowatcher.agent_delivery import AgentConfig, AgentDelivery

    from .test_agent_delivery import ENV, ack, engine
    from .test_events import window

    requests = []
    delivery = None

    def handler(request):
        requests.append(request.content)
        response = ack(request)
        if mode == "unknown":
            raise httpx.ReadTimeout("fixture unknown outcome")
        if mode == "owner_pause":
            delivery.enable(False)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        delivery = AgentDelivery(store, client, AgentConfig.from_env(ENV))
        delivery.enable(True)
        radar = engine(store, delivery, "price")
        for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
            radar.observe(f"spot/{symbol}/1m", window(), 1000000, 0.01, NOW, live=True)
        await delivery.drain(lambda: NOW + 2000, seconds=1)
        assert len(requests) == expected
        assert not store.db.in_transaction
        if mode == "ok":
            assert delivery.history.count("agent:intent-due:") == 0
            assert delivery.history.count("agent:send-due:") == 0
        else:
            assert delivery.history.count("agent:intent-due:") == 2
        if mode == "owner_pause":
            assert not delivery.enabled()


async def test_bounded_group_failure_does_not_leave_source_or_agent_reference_proofs(tmp_path, monkeypatch):
    from cryptowatcher.agent_delivery import AgentConfig, AgentDelivery
    from cryptowatcher.config import OwnerSettings
    from cryptowatcher.storage import Store

    from .test_events import window

    store = Store(tmp_path / "bounded.sqlite3", 233382575)
    store.initialize(OwnerSettings(outcome_horizons=(5,)))
    enable(store.db)
    store.bounded = True
    agent = AgentDelivery(store, None, config=AgentConfig.from_env({}))
    agent.enable(True)
    worker = OutcomeWorker(
        store, publication_batch_size=4, on_outcome=agent.outcome, on_outcome_event=agent.outcome
    )
    engine = EventEngine(store, selected("price"), on_event=worker.admit)
    for i in range(8):
        stream = f"spot/BOUND{i}USDT/1m"
        engine.observe(stream, window(), 1000000, 0.01, NOW, live=True)
        Sources(store).put(
            stream,
            [
                Candle.parse(
                    [NOW + j * MINUTE, 102, 104, 101, 103, 1, 400],
                    "1m",
                    "rest",
                    NOW + 5 * MINUTE,
                    NOW + 5 * MINUTE,
                )
                for j in range(5)
            ],
        )
    worker.bootstrap(20)
    second = engine.history.items("outcome-pending:", 2)[1][1]["outcome_id"]
    calculate = worker.calculate

    def invalid(readonly, job, event):
        result = calculate(readonly, job, event)
        if job["outcome_id"] == second:
            result["metrics"]["price_return_pct"] = float("nan")
        return result

    monkeypatch.setattr(worker, "calculate", invalid)
    try:
        with pytest.raises(ValueError):
            await worker.async_work(lambda: NOW + 5 * MINUTE, batch=8)
        assert not store.db.in_transaction
        assert engine.history.count("outcome-head:") == 0
        assert engine.history.count("outcome-pending:") == 8
        assert engine.history.count("agent:intent:") == 0
        monkeypatch.setattr(worker, "calculate", calculate)
        assert await worker.async_work(lambda: NOW + 5 * MINUTE, batch=8) == 8
        assert engine.history.count("outcome-pending:") == 0
        intents = engine.history.items("agent:intent:")
        assert len(intents) == 8
        assert all(value["outcomes"][0]["state"] == "COMPLETE" for _, value in intents)
    finally:
        store.close()
