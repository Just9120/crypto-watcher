import hashlib
import hmac
import json

import httpx
import pytest

from cryptowatcher.agent_contract import envelope
from cryptowatcher.agent_delivery import AgentConfig, AgentDelivery
from cryptowatcher.config import OwnerSettings
from cryptowatcher.events import EventEngine, History
from cryptowatcher.outcomes import OutcomeWorker
from cryptowatcher.sources import Candle, Sources
from cryptowatcher.storage import StateError, Store, encode

from .test_events import NOW, observe, selected, window

SECRET = "fixture_not_a_credential_" * 3
ENV = {
    "AGENT_EVENT_URL": "https://agent.example/integrations/cryptowatcher/events",
    "AGENT_PRODUCER_REF": "test-radar",
    "AGENT_HMAC_SECRET": SECRET,
}
SOURCE = "a" * 40


def ack(request):
    payload = json.loads(request.content)
    assert (
        request.headers["x-cryptowatcher-signature"]
        == hmac.new(SECRET.encode(), request.content, hashlib.sha256).hexdigest()
    )
    assert payload["trading_authorized"] is False and payload["model_invoked"] is False
    return httpx.Response(
        200,
        json={
            "receipt_id": "f" * 64,
            "event_id": payload["event_id"],
            "revision": payload["revision"],
            "trading_authorized": False,
            "model_invoked": False,
        },
    )


def engine(store, delivery, *families):
    return EventEngine(
        store,
        selected(*(families or ("price", "composite"))),
        source_revision=SOURCE,
        on_event=delivery.admit,
        on_episode=delivery.episode_closed,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"AGENT_HMAC_SECRET": "short"},
        {"AGENT_EVENT_URL": "http://agent.example/integrations/cryptowatcher/events"},
        {"AGENT_EVENT_URL": "https://user:pass@agent.example/integrations/cryptowatcher/events"},
        {"AGENT_EVENT_URL": "https://agent.example/integrations/cryptowatcher/events?secret=bad"},
        {"AGENT_ACCESS_CLIENT_ID": "unpaired"},
        {"AGENT_ACCESS_CLIENT_SECRET": "bad\nheader"},
        {"AGENT_PRODUCER_REF": "bad /identity"},
    ],
)
def test_invalid_transport_is_isolated_and_never_echoes_values(changes):
    config = AgentConfig.from_env({**ENV, **changes})
    assert config.state == "BLOCKED"
    assert SECRET not in repr(config) and "user:pass" not in repr(config)
    assert AgentConfig.from_env({}).state == "UNSET"


async def test_atomic_admission_coalescing_and_telegram_independence(store):
    requests = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: requests.append(r) or ack(r))
    ) as client:
        delivery = AgentDelivery(store, client, AgentConfig.from_env(ENV))
        before = store.snapshot()
        delivery.enable(True)
        assert store.snapshot() == before
        store.set_enabled(False)
        store.mute("spot/BTCUSDT", (NOW + 999999) / 1000)
        radar = engine(store, delivery)
        observe(radar)
        assert delivery.diagnostics()["intents"] == 1
        assert delivery.history.count("event-head:") == 2
        await delivery.work(NOW + 1999)
        assert not requests
        await delivery.work(NOW + 2000)
        payload = json.loads(requests[0].content)
        assert len(payload["episode"]["confirmations"]) == 2
        assert payload["market_observed_at"] < payload["produced_at"]
        assert delivery.history.count("agent:successful:") == 1
        assert payload["reference_price"] == 102
        assert SECRET not in encode(delivery.history.items("agent:"))


async def test_restart_ambiguous_ack_reuses_identical_bytes_and_success_state(store):
    calls = []

    def handler(request):
        calls.append(request.content)
        if len(calls) == 1:
            raise httpx.ReadTimeout("sensitive endpoint response " + SECRET)
        return ack(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        delivery = AgentDelivery(store, client, AgentConfig.from_env(ENV))
        delivery.enable(True)
        observe(engine(store, delivery, "price"))
        await delivery.work(NOW + 2000)
        assert delivery.history.count("agent:successful:") == 0
        reopened = Store(store.path, store.owner_id)
        try:
            restored = AgentDelivery(reopened, client, AgentConfig.from_env(ENV))
            await restored.work(NOW + 3000)
            assert len(calls) == 2 and calls[0] == calls[1]
            assert restored.history.count("agent:successful:") == 1
            records = encode(restored.history.items("agent:"))
            assert SECRET not in records and "ReadTimeout" not in records
        finally:
            reopened.close()


@pytest.mark.parametrize("status", [200, 301, 401, 409, 429, 503])
async def test_invalid_ack_redirect_permanent_and_transient_failures(store, status):
    def handler(request):
        return httpx.Response(
            status, json={"event_id": "wrong", "revision": 1}, headers={"Retry-After": "60"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        delivery = AgentDelivery(store, client, AgentConfig.from_env(ENV))
        delivery.enable(True)
        observe(engine(store, delivery, "price"))
        await delivery.work(NOW + 2000)
        record = delivery.history.items("agent:outbox:")[0][1]
        assert record["state"] == ("BLOCKED" if status in (301, 401, 409) else "PENDING")
        assert delivery.history.count("agent:successful:") == 0
        if status == 429:
            assert record["next_ms"] == NOW + 62000
        if status == 401:
            assert delivery.retry(NOW + 4000) == 1


async def test_expired_retry_is_explicit_historical_revision_without_freshening(store):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(503) if len(calls) == 1 else ack(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        delivery = AgentDelivery(store, client, AgentConfig.from_env(ENV))
        delivery.enable(True)
        observe(engine(store, delivery, "price"))
        await delivery.work(NOW + 2000)
        await delivery.work(NOW + 60001)
        await delivery.work(NOW + 60002)
        assert len(calls) == 2
        old, new = calls
        assert old["delivery_kind"] == "LIVE_EVENT"
        assert new["delivery_kind"] == "HISTORICAL_OUTCOME_UPDATE"
        assert new["revision"] == 2 and new["market_observed_at"] == old["market_observed_at"]
        assert new["fresh_until"] == old["fresh_until"] and new["reference_price"] == old["reference_price"]
        assert delivery.history.count("agent:successful:") == 0
        assert {v["state"] for _, v in delivery.history.items("agent:outbox:")} == {"SUPERSEDED", "DELIVERED"}


async def test_config_race_and_future_clock_do_not_publish_live_old_revision(store):
    calls = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: calls.append(json.loads(r.content)) or ack(r))
    ) as client:
        delivery = AgentDelivery(store, client, AgentConfig.from_env(ENV))
        delivery.enable(True)
        observe(engine(store, delivery, "price"))
        before = store.snapshot()
        store.update(before.revision, before.settings.changed(price_move_min=4))
        await delivery.work(NOW + 2000)
        assert calls[0]["delivery_kind"] == "HISTORICAL_OUTCOME_UPDATE"
        assert calls[0]["settings_snapshot"]["price_move_min"] == 2
        delivery.replay(calls[0]["event_id"], NOW + 3000)
        await delivery.work(NOW - 1)
        assert len(calls) == 1


async def test_episode_closure_and_late_outcome_keep_original_context(store):
    calls = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: calls.append(json.loads(r.content)) or ack(r))
    ) as client:
        delivery = AgentDelivery(store, client, AgentConfig.from_env(ENV))
        delivery.enable(True)
        outcomes = OutcomeWorker(store, on_outcome=delivery.outcome)
        radar = EventEngine(
            store,
            selected("price"),
            source_revision=SOURCE,
            on_event=lambda event: (outcomes.admit(event), delivery.admit(event)),
            on_episode=delivery.episode_closed,
        )
        observe(radar)
        await delivery.work(NOW + 2000)
        radar.tick(NOW + 120000)
        await delivery.work(NOW + 120000)
        assert calls[-1]["episode"]["state"] == "CLOSED"
        assert calls[-1]["delivery_kind"] == "HISTORICAL_OUTCOME_UPDATE"
        outcomes.work(NOW + 300000)
        await delivery.work(NOW + 300000)
        assert calls[-1]["historical_outcomes"][0]["completeness"] == "INCOMPLETE"
        assert calls[-1]["historical_outcomes"][0]["metrics"] == {}
        assert calls[-1]["market_observed_at"] == calls[0]["market_observed_at"]


async def test_manual_replay_resolves_complete_outcome_heads_and_signs_original_inputs(store):
    outcomes = OutcomeWorker(store)
    radar = EventEngine(store, selected("price"), source_revision=SOURCE, on_event=outcomes.admit)
    observe(radar)
    event = radar.history.events(1)[0]
    end = NOW + 60 * 60000
    candles = [
        Candle.parse([t, 102, 103, 101, 102, 1, 400], "1m", "rest", end, end) for t in range(NOW, end, 60000)
    ]
    Sources(store).put("spot/BTCUSDT/1m", candles)
    outcomes.work(end)
    expected = {v["outcome_id"]: v for _, v in radar.history.items("outcome-head:")}
    assert len(expected) == 4 and all(v["state"] == "COMPLETE" for v in expected.values())
    calls = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: calls.append(json.loads(r.content)) or ack(r))
    ) as client:
        delivery = AgentDelivery(store, client, AgentConfig.from_env(ENV))
        delivery.enable(True)
        delivery.replay(event["event_id"], end)
        await delivery.work(end)
    assert len(calls) == 1 and calls[0]["delivery_kind"] == "HISTORICAL_OUTCOME_UPDATE"
    received = calls[0]["historical_outcomes"]
    assert len(received) == 4
    for value in received:
        original = expected[value["outcome_id"]]
        assert value["completeness"] == "COMPLETE"
        assert (
            value["source_window_sha256"]
            == hashlib.sha256(encode(original["source_inputs"]).encode()).hexdigest()
        )
        assert value["metrics"] == original["metrics"]


def test_atomic_callback_failure_rolls_back_all_source_episode_and_outcome_admission(store):
    worker = OutcomeWorker(store)

    def admission(event):
        worker.admit(event)
        raise StateError("Controlled consumer admission failure")

    radar = EventEngine(store, selected("price"), source_revision=SOURCE, on_event=admission)
    # Persistence failures escape rather than becoming a false no-signal result.
    with pytest.raises(StateError, match="Controlled consumer admission"):
        observe(radar)
    assert History(store).count("event-head:") == 0
    assert History(store).count("episode-head:") == 0
    assert History(store).count("outcome-pending:") == 0


def test_peer_projection_preserves_legacy_snapshot_and_all_market_timeframe_units(store, tmp_path):
    snapshot = store.snapshot()
    store.update(snapshot.revision, OwnerSettings().with_profile("sensitive"))
    radar = EventEngine(store, source_revision=SOURCE)
    for market in ("spot", "linear"):
        for tf in ("1m", "3m", "5m", "15m"):
            radar.observe(
                f"{market}/BTCUSDT/{tf}",
                window(NOW, tf, close=120, turnover=10000),
                1e6,
                0.01,
                NOW,
                live=True,
            )
    samples = []
    for _, event in radar.history.items("event-head:"):
        episode = radar.history.get(f"episode:{event['episode_id']}:{event['episode_revision']:010d}")
        value = envelope(
            radar.history,
            event,
            episode,
            producer_ref="test-radar",
            revision=1,
            kind="LIVE_EVENT",
            now_ms=NOW + 2000,
        )
        assert (
            value["settings_sha256"]
            == hashlib.sha256(encode(event["settings_snapshot"]).encode()).hexdigest()
        )
        assert value["metric_units"] and len(encode(value).encode()) <= 65536
        samples.append(value)
    assert len(samples) == 40
    assert {v["detector_id"] for v in samples} == {"price", "turnover", "breakout", "volatility", "composite"}
    (tmp_path / "peer-payloads.json").write_text(encode(samples), encoding="utf-8")


async def test_interrupted_inflight_restores_attempt_and_manual_fresh_replay(store):
    calls = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: calls.append(r.content) or ack(r))
    ) as client:
        delivery = AgentDelivery(store, client, AgentConfig.from_env(ENV))
        delivery.enable(True)
        observe(engine(store, delivery, "price"))
        delivery._freeze(NOW + 2000)
        key, record = delivery.history.items("agent:outbox:")[0]
        with store.db:
            record.update(state="IN_FLIGHT", attempts=1)
            delivery.history.put(key, record)
        await AgentDelivery(store, client, AgentConfig.from_env(ENV)).work(NOW + 3000)
        attempts = delivery.history.items("agent:attempt:")
        assert len(attempts) == 2 and attempts[0][1]["reason"] == "INTERRUPTED_IN_FLIGHT"
        event_id = record["event_id"]
        delivery.replay(event_id, NOW + 4000)
        await delivery.work(NOW + 4000)
        assert len(calls) == 2
        assert json.loads(calls[-1])["delivery_kind"] == "EPISODE_UPDATE"
        assert json.loads(calls[-1])["revision"] == 2


async def test_new_timeframe_escalation_has_separate_successful_baseline(store):
    calls = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: calls.append(json.loads(r.content)) or ack(r))
    ) as client:
        delivery = AgentDelivery(store, client, AgentConfig.from_env(ENV))
        delivery.enable(True)
        radar = engine(store, delivery, "price")
        observe(radar)
        await delivery.work(NOW + 2000)
        observe(radar, tf="3m", close=110)
        await delivery.work(NOW + 2000)
        assert len(calls) == 2 and calls[-1]["delivery_kind"] == "EPISODE_UPDATE"
        assert {c["timeframe"] for c in calls[-1]["episode"]["confirmations"]} == {"1m", "3m"}


def test_unknown_preference_preserved_fail_closed(store):
    delivery = AgentDelivery(store, None, AgentConfig.from_env(ENV))
    with store.db:
        delivery.history.put("agent:preference", {"schema": 99, "enabled": True})
    assert not delivery.enabled()
    assert delivery.diagnostics()["preference"] == "BLOCKED_UNKNOWN_PREFERENCE"
    with pytest.raises(StateError, match="Unknown agent preference"):
        delivery.enable(True)
    assert delivery.history.get("agent:preference")["schema"] == 99


async def test_service_headers_do_not_escape_shared_client_and_large_ack_is_ambiguous(store):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=b"x" * 4097)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cfg = AgentConfig.from_env(
            {
                **ENV,
                "AGENT_ACCESS_CLIENT_ID": "test-access-id",
                "AGENT_ACCESS_CLIENT_SECRET": "test-access-secret",
            }
        )
        delivery = AgentDelivery(store, client, cfg)
        delivery.enable(True)
        observe(engine(store, delivery, "price"))
        await delivery.work(NOW + 2000)
        assert delivery.history.items("agent:outbox:")[0][1]["state"] == "PENDING"
        assert requests[0].headers["CF-Access-Client-Id"] == "test-access-id"
        await client.get("https://api.bybit.com/v5/market/time")
        assert "CF-Access-Client-Id" not in requests[1].headers
        assert "CF-Access-Client-Secret" not in requests[1].headers
        assert "x-cryptowatcher-signature" not in requests[1].headers
        assert "test-access-secret" not in encode(delivery.history.items("agent:"))


async def test_unset_integration_does_not_mutate_radar_or_pending_history(store):
    delivery = AgentDelivery(store, None, AgentConfig.from_env({}))
    before = store.db.total_changes
    await delivery.work(NOW)
    assert store.db.total_changes == before and not delivery.enabled()


async def test_owner_toggle_is_independent_and_unknown_sender_cannot_replay(infrastructure, store):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from cryptowatcher.bot import Controller

    from .conftest import update

    controller = Controller(infrastructure, store, SimpleNamespace(snapshot=AsyncMock()))
    original = store.snapshot()
    await controller.handle(update("/agent on"), None)
    assert controller.agent.enabled() and store.snapshot() == original
    before = store.db.total_changes
    await controller.handle(update("/agent off", owner=False), None)
    assert store.db.total_changes == before and controller.agent.enabled()
    await controller.handle(update("/agent off"), None)
    assert not controller.agent.enabled() and store.snapshot() == original


async def test_invalid_outcome_is_delivered_incomplete_without_blocking_next_event(store):
    calls = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: calls.append(json.loads(r.content)) or ack(r))
    ) as client:
        delivery = AgentDelivery(store, client, AgentConfig.from_env(ENV))
        delivery.enable(True)
        radar = engine(store, delivery, "price")
        observe(radar)
        await delivery.work(NOW + 2000)
        event = delivery.history.events(1)[0]
        # Actual OutcomeWorker error branch deliberately omits calculated window/classification.
        invalid = {
            "schema": "cryptowatcher.outcome.v1",
            "outcome_id": "invalid-fixture",
            "revision": 1,
            "event_id": event["event_id"],
            "event_revision": event["revision"],
            "horizon_min": 5,
            "methodology_version": "1",
            "state": "INVALID_SOURCE",
            "metrics": {},
            "source_inputs": [],
            "reason": "StateError",
            "available_at_ms": NOW + 300000,
        }
        with store.db:
            delivery.outcome(invalid)
        await delivery.work(NOW + 300000)
        outcome = calls[-1]["historical_outcomes"][0]
        assert outcome["completeness"] == "INCOMPLETE" and outcome["metrics"] == {}
        assert outcome["classification"] == "N/A"
        assert outcome["target_at"] == outcome["available_at"]
        observe(radar, now=NOW + 300000, market="linear", close=110)
        await delivery.work(NOW + 302000)
        assert calls[-1]["delivery_kind"] == "LIVE_EVENT"
