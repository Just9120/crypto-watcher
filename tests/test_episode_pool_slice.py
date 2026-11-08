"""One-slot boundary probe with production ingestion, queues and bounded SQLite."""

import json
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from cryptowatcher.agent_delivery import AgentConfig, AgentDelivery
from cryptowatcher.bot import Controller
from cryptowatcher.episode_pool import EpisodePool, selection
from cryptowatcher.expiry_index import expires, renew
from cryptowatcher.ingestion import BybitRadar
from cryptowatcher.outcomes import MINUTE
from cryptowatcher.sources import Candle
from cryptowatcher.storage import Store
from cryptowatcher.universe import classify

from .test_agent_delivery import ENV, ack
from .test_bounded_storage import bounded
from .test_events import NOW, window
from .test_ingestion import message
from .test_universe import metadata


async def test_full_pool_keeps_global_delivery_origin_and_historical_retry(store, infrastructure):
    bounded(store)
    settings = store.snapshot()
    store.update(
        settings.revision, settings.settings.changed(active_detectors=("price",), alert_detectors=("price",))
    )
    origin = NOW + 2 * MINUTE
    now = origin
    store.clock_ms = lambda: now
    telegram = AsyncMock(return_value=SimpleNamespace(message_id=42))
    context = SimpleNamespace(bot=SimpleNamespace(send_message=telegram))
    requests = []
    retry_bytes = []

    def receive(request):
        response = ack(request)  # Verify HMAC over the actual bytes and build the exact ACK.
        payload = json.loads(request.content)
        requests.append(payload)
        if payload["delivery_kind"] == "HISTORICAL_OUTCOME_UPDATE" and any(
            value["horizon_minutes"] == 5 for value in payload["historical_outcomes"]
        ):
            retry_bytes.append((request.content, request.headers["x-cryptowatcher-signature"]))
            if len(retry_bytes) == 1:
                return httpx.Response(503)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        market = BybitRadar(store, client)
        market.server_now = lambda: now
        controller = Controller(infrastructure, store, market)
        controller.agent.config = AgentConfig.from_env(ENV)
        controller.agent.enable(True)
        pool = EpisodePool(controller.outcomes, capacity=1)
        controller.episode_pool = pool
        history = controller.history

        def observe(market_type, tf, price):
            instrument, rejection = classify(market_type, metadata(market=market_type))
            assert instrument is not None, rejection
            stream = f"{instrument.key}/{tf}"
            market.instruments[instrument.key] = instrument
            market.tickers[instrument.key] = {"turnover24h": "2000000", "lastPrice": str(price)}
            market.streams.add(stream)
            market.sources.put(stream, window(now=now, tf=tf)[:-1], recovery=True)
            frame = message(tf, start=now - int(tf[:-1]) * MINUTE, close=str(price), high=str(price + 1))
            market.accept_message(market_type, frame, market.streams, received_ms=now)
            return next(event for event in history.events() if event["market_type"] == market_type)

        selected = observe("spot", "1m", 106)
        pool.advance(now)
        assert pool.selected(selected["episode_id"])
        outside = observe("linear", "1m", 106)
        assert outside["episode_id"] != selected["episode_id"]
        pool.advance(now)
        assert selection(history, outside)["state"] == "WAITING"
        assert history.get("event-head:" + outside["event_id"]) == outside

        now += 2000
        await controller.deliver(context)
        await controller.agent_job(context)
        await controller.agent_job(context)
        outbox = next(
            record
            for _, record in history.items("outbox:telegram:")
            if record["event_id"] == outside["event_id"]
        )
        assert outbox["state"] == "DELIVERED"
        assert outbox["attempts"][0]["message_id"] == 42
        assert any(
            "BTCUSDT" in call.args[1] and "Perpetual" in call.args[1] for call in telegram.await_args_list
        )
        assert any(
            payload["event_id"] == outside["event_id"] and payload["delivery_kind"] == "LIVE_EVENT"
            for payload in requests
        )
        assert history.get("agent:successful:" + outside["episode_id"]) is not None

        now = origin + MINUTE
        confirmation = observe("spot", "3m", 112)
        assert confirmation["timeframe"] == "3m" and confirmation["event_id"] != selected["event_id"]
        assert confirmation["episode_id"] == selected["episode_id"]
        assert confirmation["reference_time_ms"] == origin
        assert confirmation["reference_price"] == 106
        now += 2000
        await controller.deliver(context)
        await controller.agent_job(context)

        def supply_outcome_candles(start, end):
            market.sources.put(
                "spot/BTCUSDT/1m",
                [
                    Candle.parse([t, 106, 110, 104, 109, 1, 400], "1m", "rest", end, end)
                    for t in range(start, end, MINUTE)
                ],
                recovery=True,
            )

        first_horizon = origin + 5 * MINUTE
        now = first_horizon
        supply_outcome_candles(origin, now)
        await controller.analytics_job(context)
        assert store.get_runtime("analytics")["state"] == "OK"
        jobs = [job for _, job in history.items("outcome-job:")]
        assert len(jobs) == 3 and {job["horizon_min"] for job in jobs} == {15, 30, 60}
        first = [result for _, result in history.items("outcome-head:")]
        assert len(first) == 1 and first[0]["horizon_min"] == 5 and first[0]["state"] == "COMPLETE"
        assert {job["event_id"] for job in jobs} == {selected["event_id"]}
        await controller.agent_job(context)
        assert len(retry_bytes) == 1
        failed_key, failed = next(
            (key, record) for key, record in history.items("agent:outbox:") if record["reason"] == "HTTP_503"
        )
        assert failed["kind"] == "HISTORICAL_OUTCOME_UPDATE"
        assert failed["expires_ms"] < now < failed["next_ms"]
        # Model a pending outbox already indexed by the deployed pre-pool release.
        with store.db:
            renew(store.db, "radar:v1:" + failed_key, failed["expires_ms"], shorten=True)
        now += 500
        controller.retention_once(now)
        assert history.get(failed_key) == failed, "Cleanup dropped the pending historical retry"
        detail_expiry = expires(store.db, "radar:v1:event-head:" + selected["event_id"])[0]
        assert expires(store.db, "radar:v1:" + failed_key)[0] == detail_expiry

        now = failed["next_ms"]
        with closing(Store(store.path, store.owner_id)) as reopened:
            reopened.clock_ms = lambda: now
            restarted = AgentDelivery(reopened, client, AgentConfig.from_env(ENV))
            await restarted.work(now)
            assert restarted.history.get(failed_key)["state"] == "DELIVERED"
        assert len(retry_bytes) == 2 and retry_bytes[0] == retry_bytes[1]
        live = next(payload for payload in requests if payload["event_id"] == selected["event_id"])
        historical = json.loads(retry_bytes[0][0])
        for field in ("event_id", "market_observed_at", "fresh_until", "reference_price"):
            assert historical[field] == live[field]

        now = origin + 60 * MINUTE
        supply_outcome_candles(first_horizon, now)
        await controller.analytics_job(context)
        assert store.get_runtime("analytics")["state"] == "OK"
        results = [outcome for _, outcome in history.items("outcome-head:")]
        assert len(results) == 4 and {outcome["horizon_min"] for outcome in results} == {5, 15, 30, 60}
        for outcome in results:
            assert outcome["event_id"] == selected["event_id"] and outcome["state"] == "COMPLETE"
            assert outcome["window_start_ms"] == origin
            assert outcome["window_end_ms"] == origin + outcome["horizon_min"] * MINUTE
            assert outcome["metrics"]["price_return_pct"] == pytest.approx(100 * (109 / 106 - 1))
            assert outcome["metrics"]["mfe_pct"] == pytest.approx(100 * (110 / 106 - 1))
            assert outcome["metrics"]["mae_pct"] == pytest.approx(100 * (104 / 106 - 1))

        now = detail_expiry + 1
        for _ in range(5):
            controller.retention_once(now)
            if history.metadata("event-head:" + outside["event_id"]) is None:
                break
        assert history.metadata("event-head:" + outside["event_id"]) is None
        assert history.get(failed_key) is None
        archived = store.db.execute(
            "SELECT horizon,fact FROM market_episode_results WHERE episode_id=?", (outside["episode_id"],)
        ).fetchall()
        assert len(archived) == 1 and archived[0][0] == 0
        assert json.loads(archived[0][1])["state"] == "NOT_SELECTED"


def test_additive_cutover_and_rotation_preserve_old_jobs_and_frozen_origin(store):
    from cryptowatcher.events import EventEngine, History
    from cryptowatcher.outcomes import OutcomeWorker
    from cryptowatcher.sources import Sources

    bounded(store)
    settings = store.snapshot()
    store.update(settings.revision, settings.settings.changed(active_detectors=("price",)))
    clock = [NOW - 2 * MINUTE]
    store.clock_ms = lambda: clock[0]
    worker = OutcomeWorker(store)
    engine = EventEngine(store, on_event=worker.admit)
    engine.observe("linear/ETHUSDT/1m", window(now=clock[0]), 2e6, 0.01, clock[0], live=True)
    history = History(store)
    old_event = history.events()[0]
    old_jobs = history.items("outcome-job:")
    assert len(old_jobs) == 4
    assert selection(history, old_event) is None

    clock[0] = NOW
    with closing(Store(store.path, store.owner_id)) as upgraded:
        upgraded.clock_ms = lambda: clock[0]
        worker = OutcomeWorker(upgraded, replay_admission=False)
        pool = EpisodePool(worker, capacity=1)
        engine = EventEngine(upgraded, on_event=pool.offer)
        history = worker.history
        assert history.get("event-head:" + old_event["event_id"]) == old_event
        assert history.items("outcome-job:") == old_jobs
        assert selection(history, old_event)["state"] == "LEGACY"

        engine.observe("spot/BTCUSDT/1m", window(), 2e6, 0.01, NOW, live=True)
        first = history.events()[0]
        pool.advance(NOW)
        assert pool.selected(first["episode_id"])

    clock[0] = NOW + 60 * MINUTE
    with closing(Store(store.path, store.owner_id)) as restarted:
        restarted.clock_ms = lambda: clock[0]
        worker = OutcomeWorker(restarted, replay_admission=False)
        pool = EpisodePool(worker, capacity=1)
        engine = EventEngine(restarted, on_event=pool.offer)
        history = worker.history
        assert pool.selected(first["episode_id"])
        engine.tick(clock[0])
        engine.observe("spot/SOLUSDT/1m", window(now=clock[0]), 2e6, 0.01, clock[0], live=True)
        second = history.events()[0]
        pool.advance(clock[0])
        assert selection(history, second)["state"] == "WAITING"
        assert pool.selected(first["episode_id"])

        Sources(restarted).put(
            "spot/BTCUSDT/1m",
            [
                Candle.parse([t, 102, 104, 101, 103, 1, 400], "1m", "rest", clock[0], clock[0])
                for t in range(NOW, clock[0], MINUTE)
            ],
            recovery=True,
        )
        worker.work(clock[0])
        clock[0] += 1000
        pool.advance(clock[0])
        assert selection(history, first)["state"] == "FINISHED"
        assert selection(history, second)["selected_ms"] == clock[0]
        assert pool.selected(second["episode_id"])
        second_jobs = [
            job for _, job in history.items("outcome-job:") if job["event_id"] == second["event_id"]
        ]
        assert len(second_jobs) == 4
        assert all(
            job["due_ms"] == second["reference_time_ms"] + job["horizon_min"] * MINUTE for job in second_jobs
        )
        assert history.get("event-head:" + old_event["event_id"]) == old_event
