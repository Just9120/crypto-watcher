"""Joined mutable-state integrity and delivery fairness, before capped qualification."""

import json
from contextlib import closing

import httpx
import pytest

from cryptowatcher.agent_delivery import AgentConfig, AgentDelivery
from cryptowatcher.config import OwnerSettings
from cryptowatcher.events import EventEngine
from cryptowatcher.market_storage import enable
from cryptowatcher.sources import Sources
from cryptowatcher.storage import StateError, Store
from tests.hot_state import Batch
from tests.hot_state import installed as hot_context
from tests.processing_replay import replay
from tests.sqlite_repository import History, install, repository_context
from tests.test_agent_delivery import ENV, ack, engine
from tests.test_events import NOW, window


async def test_joined_hot_state_preserves_the_full_small_flow(tmp_path):
    result = await replay(
        tmp_path / "flow.sqlite3",
        instruments=3,
        seconds=8,
        direct_sqlite=True,
        bounded_pool=True,
        hot_state=True,
    )
    hot, pool = result["hot_state"], result["bounded_pool"]
    assert hot["observations"] == result["accepted_closed"] == result["offered_closed"] == 24
    assert 0 < hot["flushed_mutations"] < hot["buffered_mutations"]
    assert hot["metadata_hits"] > hot["metadata_misses"] > 0 and hot["peak_keys"] <= 128
    assert pool["global_expected_sha256"] == pool["global_actual_sha256"]
    assert pool["selected_outcome_ids"] == pool["actual_outcome_ids"]
    assert result["completed"] > 0 and result["signed_agent_receipts"] > 0
    assert result["producer_errors"] == result["worker_errors"] == result["receiver_errors"] == []
    assert result["direct_sqlite"]["foreign_key_violations"] == []
    assert json.loads(json.dumps(hot)) == hot


@pytest.fixture
def hot_store(tmp_path):
    with (
        repository_context(True),
        hot_context(True) as stats,
        closing(Store(tmp_path / "hot.sqlite3", 233382575)) as store,
    ):
        store.initialize(OwnerSettings())
        enable(store.db)
        store.bounded = True
        store.clock_ms = lambda: NOW
        install(store)
        yield store, stats


def test_flush_failure_rolls_back_source_events_and_mutable_state(hot_store):
    store, _ = hot_store
    source = Sources(store)
    radar = EventEngine(store, on_event=lambda event: None, on_event_snapshot=lambda event, episode: None)
    with store.db:
        store.db.execute(
            "CREATE TRIGGER reject_hot_flush BEFORE INSERT ON runtime WHEN NEW.key LIKE 'radar:v1:active:%' BEGIN SELECT RAISE(ABORT,'hot flush failure'); END"
        )
    before = list(store.db.iterdump())

    def consumer(candidate, advanced):
        radar.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True, commit=False)

    with pytest.raises(Exception, match="hot flush failure"):
        source.put("spot/BTCUSDT/1m", window(), live=True, after_write=consumer)
    assert list(store.db.iterdump()) == before
    assert source.windows["spot/BTCUSDT/1m"] == {} and source.live == {}
    assert store.db.hot is None and History(store).events() == []


def test_header_memo_observes_owner_changes_and_savepoint_rollback(hot_store):
    store, stats = hot_store
    history = History(store)
    sql = "SELECT revision,settings_json FROM owner_settings WHERE owner_id=?"
    batch = Batch(history, History._put_raw, History._delete_raw, stats)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        store.db.hot = batch
        try:
            first = store.db.execute(sql, (store.owner_id,)).fetchone()[0]
            assert store.db.execute(sql, (store.owner_id,)).fetchone()[0] == first
            store.db.execute("SAVEPOINT changed_owner")
            store.db.cursor().execute("UPDATE owner_settings SET revision=revision+1")
            assert store.db.execute(sql, (store.owner_id,)).fetchone()[0] == first + 1
            store.db.execute("ROLLBACK TO changed_owner")
            store.db.execute("RELEASE changed_owner")
            assert store.db.execute(sql, (store.owner_id,)).fetchone()[0] == first
            with pytest.raises(StateError, match="cannot commit"):
                store.db.commit()
            with pytest.raises(StateError, match="cannot end"):
                store.db.execute("COMMIT")
            with pytest.raises(StateError, match="cannot roll back"):
                store.db.rollback()
        finally:
            store.db.hot = None
    assert store.snapshot().revision == first


def test_hot_buffer_has_a_finite_budget_and_copies_inputs():
    stats = dict.fromkeys(("buffered_mutations", "flushed_mutations", "peak_keys"), 0)
    batch = Batch(None, None, None, stats)
    value = {"nested": [1]}
    batch.write("active:test", value)
    value["nested"].append(2)
    assert batch.writes["active:test"] == {"nested": [1]}
    with pytest.raises(StateError, match="budget"):
        batch.write("active:large", {"data": "x" * 1024**2})
    assert list(batch.writes) == ["active:test"]


async def test_agent_fresh_work_precedes_seed_history_without_starving_it(store):
    requests = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: requests.append(r) or ack(r))
    ) as client:
        delivery = AgentDelivery(store, client, AgentConfig.from_env(ENV))
        delivery.enable(True)
        radar = engine(store, delivery, "price")
        for i in range(5):
            radar.observe(f"spot/OLD{i}USDT/1m", window(NOW - 300000), 1e6, 0.01, NOW - 300000, live=True)
        radar.tick(NOW)
        for i in range(5):
            radar.observe(f"spot/NEW{i}USDT/1m", window(), 1e6, 0.01, NOW, live=True)
        for _ in range(4):
            await delivery.work(NOW + 2000)
        kinds = [json.loads(r.content)["delivery_kind"] for r in requests]
        assert kinds == ["LIVE_EVENT"] * 3 + ["HISTORICAL_OUTCOME_UPDATE"]
        await delivery.run_ready(lambda: NOW + 3000)
        assert len(requests) == 10
        assert delivery.diagnostics()["intents"] == delivery.diagnostics()["pending"] == 0


async def test_agent_recovers_canonical_queue_and_unknown_ack_does_not_amplify(store):
    requests = []

    def target(request):
        requests.append(request)
        return httpx.Response(200, json={"wrong": "ack"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(target)) as client:
        delivery = AgentDelivery(store, client, AgentConfig.from_env(ENV))
        delivery.enable(True)
        radar = engine(store, delivery, "price")
        radar.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True)
        with store.db:
            for key, value in delivery.history.items("agent:ready:"):
                delivery.history.delete(key)
        restored = AgentDelivery(store, client, AgentConfig.from_env(ENV))
        await restored.run_ready(lambda: NOW + 2000)
        assert len(requests) == 1
        body = requests[0].content
        assert restored.diagnostics()["target"]["reason"] == "AMBIGUOUS_ACK"
        await restored.run_ready(lambda: NOW + 3000)
        assert len(requests) == 2 and requests[1].content == body
        assert store.snapshot().revision == 1


async def test_expired_canonical_recovery_keeps_compact_immutable_references(hot_store):
    store, _ = hot_store
    requests = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: requests.append(r) or ack(r))
    ) as client:
        delivery = AgentDelivery(store, client, AgentConfig.from_env(ENV))
        delivery.enable(True)
        radar = engine(store, delivery, "price")
        radar.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True)
        intent_key = delivery.history.items("agent:intent:")[0][0]
        with store.db:
            for key, value in delivery.history.items("agent:ready:"):
                delivery.history.delete(key)
        restored = AgentDelivery(store, client, AgentConfig.from_env(ENV))
        restored.ready.recover("intent", NOW + 120000)
        raw = store.get_runtime("radar:v1:" + intent_key)
        assert raw["historical"] is True
        assert set(raw["episode"]) == {"_cw_revision", "sha256"}
        assert restored.history.get(intent_key)["episode"]["reference_time_ms"] == NOW
        await restored.work(NOW + 120000)
        assert (
            len(requests) == 1
            and json.loads(requests[0].content)["delivery_kind"] == "HISTORICAL_OUTCOME_UPDATE"
        )
