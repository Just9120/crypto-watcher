"""Any meaningful confirmation reads/exports/replays the same frozen episode result."""

import json
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from cryptowatcher.agent_delivery import AgentConfig, AgentDelivery
from cryptowatcher.bot import Controller
from cryptowatcher.events import EventEngine
from cryptowatcher.export import Selection, ndjson
from cryptowatcher.market_storage import enable
from cryptowatcher.outcomes import MINUTE, episode_results
from cryptowatcher.sources import Candle, Sources
from cryptowatcher.storage import Store

from .conftest import update
from .test_agent_delivery import ENV, SOURCE, ack
from .test_events import NOW, observe, selected


@pytest.mark.parametrize("bounded", [False, True])
async def test_later_confirmation_uses_origin_horizons_in_ui_export_and_signed_replay(
    store, infrastructure, bounded
):
    if bounded:
        enable(store.db)
        store.bounded = True
    store.clock_ms = lambda: NOW
    controller = Controller(infrastructure, store, SimpleNamespace(close=AsyncMock()))
    engine = EventEngine(store, selected("price"), source_revision=SOURCE, on_event=controller.outcomes.admit)
    observe(engine)
    origin = engine.history.events()[0]
    snapshot = store.snapshot()
    store.update(snapshot.revision, snapshot.settings.changed(outcome_horizons=(1, 2)))
    observe(engine, NOW + MINUTE)
    observe(engine, NOW + 2 * MINUTE)
    observe(engine, NOW + 3 * MINUTE, tf="3m")
    later = next(e for e in engine.history.events() if e["event_id"] != origin["event_id"])
    assert later["settings_snapshot"]["outcome_horizons"] == [1, 2]
    end = NOW + 60 * MINUTE
    Sources(store).put(
        "spot/BTCUSDT/1m",
        [
            Candle.parse([t, 102, 103, 101, 102, 1, 400], "1m", "rest", end, end)
            for t in range(NOW, end, MINUTE)
        ],
    )
    assert len(engine.history.items("outcome-job:")) == 4
    controller.outcomes.work(end)
    frozen, horizons, results = episode_results(engine.history, later)
    assert frozen == origin
    assert horizons == (5, 15, 30, 60)
    assert len(results) == 4 and all(x["state"] == "COMPLETE" for x in results)
    assert all(x["event_id"] == origin["event_id"] for x in results)
    assert len(engine.history.items("outcome-job:")) == (0 if bounded else 4)

    request = update(callback="outcomes:" + later["event_id"])
    await controller.handle(request, None)
    body = "\n".join(call.args[0] for call in request.effective_message.reply_text.call_args_list)
    assert "5 мин · готово" in body and "60 мин · готово" in body and "1 мин ·" not in body

    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        records = [json.loads(line) for line in ndjson(reader, Selection(NOW + MINUTE, NOW + 4 * MINUTE))]
        assert reader.db.total_changes == 0
    manifest = records[0]
    assert manifest["selected_events"] == 1
    assert manifest["outcome_units_by_method"]["2"] == "EPISODE_ORIGIN"
    exported = [r["record"] for r in records if r["type"] == "outcome"]
    assert {x["outcome_id"] for x in exported} == {x["outcome_id"] for x in results}
    episodes = [r["record"] for r in records if r["type"] == "episode"]
    assert episodes and all(x["outcome_origin"] == origin for x in episodes)
    assert {r["record"]["event_id"] for r in records if r["type"] == "event"} == {later["event_id"]}

    calls = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: calls.append(json.loads(request.content)) or ack(request)
        )
    ) as client:
        agent = AgentDelivery(store, client, AgentConfig.from_env(ENV))
        agent.enable(True)
        agent.replay(later["event_id"], end)
        await agent.work(end)
    assert len(calls) == 1
    assert calls[0]["schema_version"] == "cryptowatcher.market_event.v2"
    assert calls[0]["event_id"] == origin["event_id"]
    assert calls[0]["reference_price"] == origin["reference_price"]
    assert len(calls[0]["historical_outcomes"]) == 4

    # Simulate expiry of the first observation while a later confirmation is still visible.
    with store.db:
        engine.history.delete("event-head:" + origin["event_id"])
        engine.history.delete(f"event:{origin['event_id']}:0000000001")
    expired = update(callback="outcomes:" + later["event_id"])
    await controller.handle(expired, None)
    unavailable = "\\n".join(call.args[0] for call in expired.effective_message.reply_text.call_args_list)
    assert "недоступны" in unavailable
    assert "ожидается срок" not in unavailable


def test_untagged_v1_reader_keeps_its_own_reference_even_with_new_episode_head(store):
    from cryptowatcher.events import History, identity
    from cryptowatcher.outcomes import OutcomeWorker, episode_origin

    worker = OutcomeWorker(store)
    engine = EventEngine(store, selected("price"), on_event=worker.admit)
    observe(engine)
    first = engine.history.events()[0]
    legacy = {**first, "event_id": identity("old-v1-confirmation"), "reference_time_ms": NOW + MINUTE}
    legacy.pop("outcome_unit")
    legacy.pop("outcome_methodology_version")
    history = History(store)
    expected = []
    with store.db:
        for horizon in (5, 15, 30, 60):
            value = {
                "outcome_id": identity(f"outcome/{legacy['event_id']}/{horizon}/1"),
                "state": "COMPLETE",
                "methodology_version": "1",
                "metrics": {"price_return_pct": horizon / 7},
            }
            history.put("outcome-head:" + value["outcome_id"], value)
            expected.append(value)
    assert episode_origin(history, legacy) == legacy
    origin, horizons, actual = episode_results(history, legacy)
    assert origin == legacy and horizons == (5, 15, 30, 60) and actual == expected
    with store.db:
        history.delete("episode-head:" + first["episode_id"])
    assert episode_results(history, first) == (None, (), [])


def test_bootstrap_does_not_research_untagged_history_or_expand_its_expired_inputs(store, monkeypatch):
    from cryptowatcher.outcomes import OutcomeWorker
    from cryptowatcher.storage import encode

    worker = OutcomeWorker(store)
    engine = EventEngine(store, selected("price"), on_event=worker.admit)
    observe(engine)
    history = engine.history
    with store.db:
        for key, value in history.items("event:"):
            value.pop("outcome_unit")
            value.pop("outcome_methodology_version")
            store.db.execute(
                "UPDATE runtime SET payload_json=? WHERE key=?", (encode(value), "radar:v1:" + key)
            )
        for prefix in ("outcome-job:", "outcome-pending:"):
            for key, _ in history.items(prefix):
                history.delete(key)
        history.delete("outcome-admission")
    original = worker.history.get

    def get(key, *args, **kwargs):
        if key.startswith("event:"):
            raise AssertionError("Legacy source windows must not be expanded")
        return original(key, *args, **kwargs)

    monkeypatch.setattr(worker.history, "get", get)
    worker.bootstrap()
    assert history.count("outcome-job:") == history.count("outcome-pending:") == 0
    assert history.get("outcome-admission")["complete"] is True


def test_existing_v1_job_completes_on_current_worker_without_relabeling_reference_or_method(store):
    from cryptowatcher.events import identity
    from cryptowatcher.outcomes import OutcomeWorker

    worker = OutcomeWorker(store)
    engine = EventEngine(store, selected("price"), on_event=worker.admit)
    observe(engine)
    legacy = {**engine.history.events()[0], "event_id": identity("existing-v1-job")}
    legacy.pop("outcome_unit")
    legacy.pop("outcome_methodology_version")
    with store.db:
        engine.history.version("event", legacy)
        worker.admit(legacy)
    end = NOW + 60 * MINUTE
    Sources(store).put(
        "spot/BTCUSDT/1m",
        [
            Candle.parse([t, 102, 104, 100, 103, 1, 400], "1m", "rest", end, end)
            for t in range(NOW, end, MINUTE)
        ],
    )
    worker.work(end)
    origin, horizons, results = episode_results(engine.history, legacy)
    assert origin == legacy and horizons == (5, 15, 30, 60)
    assert all(r["state"] == "COMPLETE" and r["methodology_version"] == "1" for r in results)
    assert all(r["event_id"] == legacy["event_id"] and r["reference_time_ms"] == NOW for r in results)
    assert all(r["metrics"]["price_return_pct"] == pytest.approx((103 / 102 - 1) * 100) for r in results)
    assert (
        engine.history.get("outcome-method:1")["reference"]
        == "immutable event observation time and reference close"
    )
