"""Direct SQLite prototype: correctness before the one full capped Linux run."""

import json
from contextlib import closing

import pytest

from cryptowatcher.config import OwnerSettings
from cryptowatcher.events import PREFIX, EventEngine
from cryptowatcher.market_storage import enable
from cryptowatcher.outcomes import OutcomeWorker
from cryptowatcher.sources import Sources
from cryptowatcher.storage import RUNTIME_UPSERT, StateError, Store, encode
from tests.processing_replay import candle, replay
from tests.sqlite_repository import History, install, maintenance, repository_context, stats
from tests.test_events import NOW, window
from tests.test_processing_replay import assert_joint_result


@pytest.fixture
def core(tmp_path):
    with repository_context(True), closing(Store(tmp_path / "core.sqlite3", 233382575)) as store:
        store.initialize(OwnerSettings())
        enable(store.db)
        store.bounded = True
        store.clock_ms = lambda: NOW
        install(store)
        yield store


def observe(store):
    worker = OutcomeWorker(store)
    engine = EventEngine(store, on_event=worker.admit)
    engine.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True)
    return engine, worker


def test_direct_revisions_are_immutable_reference_locked_and_transactional(core):
    engine, _ = observe(core)
    event = engine.history.events()[0]
    assert isinstance(engine.history, History)
    key = f"event:{event['event_id']}:0000000001"
    assert encode(engine.history.get(key)) == encode(event)
    before = list(core.db.iterdump())
    with core.db, pytest.raises(StateError, match="Conflicting immutable"):
        engine.history.version("event", {**event, "strength": event["strength"] + 1})
    assert list(core.db.iterdump()) == before
    episode = engine.history.get("episode-head:" + event["episode_id"])
    with core.db, pytest.raises(StateError, match="reference is immutable"):
        engine.history.version(
            "episode", {**episode, "revision": episode["revision"] + 1, "reference_price": 1}
        )
    assert list(core.db.iterdump()) == before
    # A failure after snapshot admission rolls back all newly retained rows.
    with core.db:
        core.db.execute(
            "CREATE TRIGGER fail_new_episode BEFORE INSERT ON prototype_episode_revisions BEGIN SELECT RAISE(ABORT,'injected failure'); END"
        )
    before = list(core.db.iterdump())
    with core.db, pytest.raises(Exception, match="injected failure"):
        engine.observe("linear/ETHUSDT/1m", window(), 1e6, 0.01, NOW, live=True)
    assert list(core.db.iterdump()) == before
    assert stats(core)["foreign_key_violations"] == []


def test_fresh_head_and_snapshot_corruption_cannot_use_previous_good_read(core):
    engine, _ = observe(core)
    event = engine.history.events()[0]
    key = "event-head:" + event["event_id"]
    assert engine.history.get(key) == event
    original = core.get_runtime(PREFIX + key)
    with core.db:
        core.db.execute(RUNTIME_UPSERT, (PREFIX + key, encode({**original, "strength": 999})))
    with pytest.raises(StateError, match="head projection"):
        engine.history.get(key)
    with pytest.raises(StateError, match="head projection"), core.db:
        engine.history.version("event", {**event, "revision": 2})
    with core.db:
        core.db.execute(RUNTIME_UPSERT, (PREFIX + key, encode(original)))
        core.db.execute("DROP TRIGGER prototype_snapshot_immutable")
        digest = core.db.execute(
            "SELECT digest FROM prototype_links WHERE owner=? AND field='source_inputs'",
            (f"event:{event['event_id']}:0000000001",),
        ).fetchone()[0]
        core.db.execute("UPDATE prototype_snapshots SET payload='[]' WHERE digest=?", (digest,))
    with pytest.raises(StateError, match="corrupt direct snapshot"):
        engine.history.get(key)


def capture(path, direct):
    with repository_context(direct), closing(Store(path, 233382575)) as store:
        store.initialize(OwnerSettings())
        enable(store.db)
        store.bounded = True
        store.clock_ms = lambda: NOW
        if direct:
            install(store)
        engine, worker = observe(store)
        # Unchanged repeat, strengthening, second TF and second market.
        for tf, at, price, market in [
            ("1m", NOW + 60000, 102, "spot"),
            ("1m", NOW + 120000, 110, "spot"),
            ("3m", NOW + 180000, 104, "spot"),
            ("5m", NOW, 104, "linear"),
        ]:
            engine.observe(f"{market}/BTCUSDT/{tf}", window(at, tf, price), 1e6, 0.01, at, live=True)
        source = Sources(store)
        for market in ("spot", "linear"):
            source.put(
                f"{market}/BTCUSDT/1m", [candle(NOW + i * 60000, "1m") for i in range(1, 65)], recovery=True
            )
        worker.work(NOW + 65 * 60000, batch=256)
        values = {
            prefix: [(k, v) for k, v in engine.history.items(prefix)]
            for prefix in ("event:", "episode:", "outcome:", "outcome-job:", "comparison-job:")
        }
        return encode(values)


def test_direct_full_public_facts_match_existing_formulas_origin_updates_and_float_types(tmp_path):
    expected = capture(tmp_path / "reference.sqlite3", False)
    actual = capture(tmp_path / "direct.sqlite3", True)
    assert actual == expected
    assert json.loads(actual)["comparison-job:"] == []


async def test_direct_real_pipeline_to_both_transports_and_storage_maintenance(tmp_path):
    result = await replay(tmp_path / "paced.sqlite3", instruments=2, seconds=12, direct_sqlite=True)
    assert_joint_result(result)
    assert result["direct_sqlite"]["foreign_key_violations"] == []
    assert result["direct_sqlite"]["revisions"]["event"] > 0
    assert result["direct_sqlite"]["revisions"]["outcome"] > 0
    assert result["repack"]["state"] == "DIRECT_SQLITE_CORE"


def test_full_source_retention_annual_facts_and_real_gc_keep_numeric_results(core):
    from cryptowatcher.analytics import cohorts
    from cryptowatcher.outcomes import DAY, Retention

    engine, worker = observe(core)
    source = Sources(core)
    source.put("spot/BTCUSDT/1m", [candle(NOW + i * 60000, "1m") for i in range(1, 65)], recovery=True)
    worker.work(NOW + 65 * 60000, batch=256)
    before = cohorts(core, ("detector", "timeframe"))["cohorts"]
    assert before and all(x["sample_size"] == 1 for x in before)
    retention = Retention(core)
    for _ in range(30):
        retention.work(NOW + 31 * DAY, batch=20)
        maintenance(core, NOW + 31 * DAY)
        if not engine.history.events(1):
            break
    assert not engine.history.events(1)
    assert cohorts(core, ("detector", "timeframe"))["cohorts"] == before
    assert stats(core)["revisions"]["event"] == 0
    assert stats(core)["foreign_key_violations"] == []


async def test_existing_ambiguous_agent_ack_and_restart_contract_on_real_direct_storage(core):
    from tests.test_agent_delivery import test_restart_ambiguous_ack_reuses_identical_bytes_and_success_state

    await test_restart_ambiguous_ack_reuses_identical_bytes_and_success_state(core)


def test_existing_publication_failure_rolls_back_real_source_episode_and_jobs(core):
    from tests.test_agent_delivery import (
        test_atomic_callback_failure_rolls_back_all_source_episode_and_outcome_admission,
    )

    test_atomic_callback_failure_rolls_back_all_source_episode_and_outcome_admission(core)
