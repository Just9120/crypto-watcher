"""Observation preserves real callbacks and verifies committed evidence."""

from contextlib import closing

import pytest

from cryptowatcher.bot import Controller
from cryptowatcher.ingestion import BybitRadar
from cryptowatcher.storage import encode
from tests.light_replay import ENV
from tests.processing_observation import Observation
from tests.processing_replay import replay
from tests.test_events import NOW, window
from tests.test_processing_replay import assert_joint_result
from tests.test_runtime_repository import opened


def test_collect_keeps_runtime_consumer_identity_and_queue_buffering(tmp_path, infrastructure, monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    with closing(opened(tmp_path / "observer.sqlite3", True)) as store:
        controller = Controller(infrastructure, store, BybitRadar(store))
        controller.agent.enable(True)
        engine = controller.engine
        observer = Observation(engine)
        original = engine.on_event_snapshot
        engine.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True)
        assert engine.on_event_snapshot is original
        assert any(original is consumer for consumer in engine._publication_consumers)
        events, proof = observer.collect(controller.history)
        assert events and proof["events_read"] == engine.writer_stats["facts"]
        assert proof["consumer_wiring_unchanged"] is True
        assert controller.history.items("agent:intent:")
        before = encode(controller.history.items("event:"))
        events[0]["reference_price"] = "changed returned copy"
        assert encode(controller.history.items("event:")) == before
        engine.on_event_snapshot = lambda event, episode: original(event, episode)
        with pytest.raises(AssertionError, match="binding"):
            observer.collect(controller.history)


def test_collect_rejects_a_missing_persisted_fact_or_open_transaction(tmp_path):
    with closing(opened(tmp_path / "missing.sqlite3", True)) as store:
        from cryptowatcher.domain_repository import install
        from cryptowatcher.events import EventEngine

        install(store)
        engine = EventEngine(store)
        observer = Observation(engine)
        engine.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True)
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            with pytest.raises(AssertionError, match="committed"):
                observer.collect(engine.history)
        engine.writer_stats["facts"] += 1
        with pytest.raises(AssertionError, match="fact count"):
            observer.collect(engine.history)


async def test_clean_replay_proves_unmodified_consumers_and_exact_persisted_oracle(tmp_path):
    result = await replay(tmp_path / "clean.sqlite3", instruments=2, seconds=12)
    assert_joint_result(result)
    pool = result["bounded_pool"]
    proof = pool["observation"]
    assert proof["mode"] == "PERSISTED_REVISIONS_AFTER_PHASE"
    assert proof["consumer_wiring_unchanged"] is True
    assert proof["events_read"] == proof["producer_facts"] == pool["global_actual"]
    assert pool["global_expected_sha256"] == pool["global_actual_sha256"]
    assert pool["global_difference"]["missing_count"] == pool["global_difference"]["extra_count"] == 0
    assert result["signed_agent_receipts"] > 0 and result["telegram_attempts"] > 0
    assert not any(queue["pending_total"] for queue in result["delivery_queues_at_endpoint"].values())
