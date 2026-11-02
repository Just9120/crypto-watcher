"""Runtime pool correctness, with actual SQLite and unchanged outcome workers."""

import json

import pytest

from cryptowatcher.analytics_pool import Pool
from cryptowatcher.storage import StateError, encode


def test_policy_reference_and_rollback_are_durable(tmp_path):
    from contextlib import closing

    from cryptowatcher.config import OwnerSettings
    from cryptowatcher.events import EventEngine
    from cryptowatcher.outcomes import OutcomeWorker
    from cryptowatcher.storage import Store
    from tests.processing_replay import MINUTE, NOW, candle

    at = NOW - 5 * MINUTE
    with closing(Store(tmp_path / "pool.sqlite3", 233382575)) as store:
        store.initialize(OwnerSettings())
        store.clock_ms = lambda: at
        worker = OutcomeWorker(store)
        pool = Pool(worker)
        pool.activate()
        events = []

        def saved(event):
            events.append(event)
            worker.admit(event)

        engine = EventEngine(store, on_event=saved)
        rows = [candle(at - i * MINUTE, "1m") for i in range(24, -1, -1)]
        engine.observe("spot/BTCUSDT/1m", rows, 2000000, 0.01, at, live=True)
        point = store.snapshot()
        pool.tick(at)
        assert pool.report()["selected"]
        assert store.snapshot() == point  # Auto-selection never writes owner settings.
        duplicate = Pool(OutcomeWorker(store))
        duplicate.offer(events[0])
        before = store.db.execute("SELECT COUNT(*) FROM analytics_pool_choices").fetchone()[0]
        assert before == len({e["episode_id"] for e in events})
        corrupt = {**events[0], "reference_price": events[0]["reference_price"] + 1}
        with pytest.raises(StateError, match="reference"):
            duplicate.offer(corrupt)
        with pytest.raises(RuntimeError), store.db:
            store.db.execute("UPDATE analytics_pool_choices SET reason='UNCOMMITTED'")
            raise RuntimeError("rollback")
        assert not store.db.execute(
            "SELECT 1 FROM analytics_pool_choices WHERE reason='UNCOMMITTED'"
        ).fetchone()
        with pytest.raises(StateError, match="policy"):
            Pool(OutcomeWorker(store), manual=("spot/BTCUSDT",))
        assert json.loads(encode(pool.report())) == pool.report()


def test_existing_jobs_are_preserved_and_block_new_admissions(tmp_path):
    from contextlib import closing

    from cryptowatcher.config import OwnerSettings
    from cryptowatcher.events import EventEngine
    from cryptowatcher.outcomes import OutcomeWorker
    from cryptowatcher.storage import Store
    from tests.processing_replay import MINUTE, NOW, candle

    at = NOW - 5 * MINUTE
    with closing(Store(tmp_path / "legacy.sqlite3", 233382575)) as store:
        store.initialize(OwnerSettings())
        store.clock_ms = lambda: at
        worker = OutcomeWorker(store)
        engine = EventEngine(store, on_event=lambda event: worker.admit(event))
        rows = [candle(at - i * MINUTE, "1m") for i in range(24, -1, -1)]
        engine.observe("spot/BTCUSDT/1m", rows, 2000000, 0.01, at, live=True)
        before = encode(worker.history.items("outcome-job:"))
        pool = Pool(worker)
        pool.activate()
        engine.observe("linear/BTCUSDT/1m", rows, 2000000, 0.01, at, live=True)
        pool.tick(at)
        assert pool.report()["legacy_jobs"] > 0
        assert not pool.report()["selected"]
        assert encode(worker.history.items("outcome-job:")) == before


def test_saturated_pool_keeps_obligations_through_close_until_real_outcomes_finish(tmp_path):
    from contextlib import closing

    from cryptowatcher.config import OwnerSettings
    from cryptowatcher.events import EventEngine
    from cryptowatcher.market_storage import enable
    from cryptowatcher.outcomes import OutcomeWorker
    from cryptowatcher.sources import Sources
    from cryptowatcher.storage import Store
    from tests.processing_replay import MINUTE, NOW, candle

    clock = [NOW]
    with closing(Store(tmp_path / "saturated.sqlite3", 233382575)) as store:
        store.initialize(OwnerSettings())
        enable(store.db)
        store.bounded = True
        store.clock_ms = lambda: clock[0]
        worker = OutcomeWorker(store)
        pool = Pool(worker)
        pool.activate()
        engine = EventEngine(store, on_event=lambda event: worker.admit(event))
        for index in range(40):
            clock[0] = NOW + index * 15_000
            end = NOW + index // 4 * MINUTE
            rows = [candle(end - i * MINUTE, "1m") for i in range(24, -1, -1)]
            for category in ("spot", "linear"):
                engine.observe(f"{category}/SIM{index:03}USDT/1m", rows, 2000000, 0.01, clock[0], live=True)
            pool.tick(clock[0])
        assert pool.report()["peaks"]["obligations"] == 64
        assert pool.audit["full_checks"] > 0
        before = {row[0] for row in pool.report()["selected"]}
        assert len(before) == 64
        clock[0] = NOW + 75 * MINUTE
        engine.tick(clock[0])
        pool.tick(clock[0])
        assert {row[0] for row in pool.report()["selected"]} == before
        assert dict(store.db.execute("SELECT state,COUNT(*) FROM analytics_pool_slots GROUP BY state")) == {
            "DRAINING": 64
        }
        chosen = pool.report()["selected"][0]
        event = worker.history.get("event-head:" + chosen[1])
        stream = f"{event['market_type']}/{event['symbol']}/1m"
        reference = event["reference_time_ms"]
        Sources(store).put(stream, [candle(reference + i * MINUTE, "1m") for i in range(61)], recovery=True)
        for _, job in worker.history.items("outcome-job:"):
            if job["event_id"] != chosen[1]:
                continue
            result = worker.calculate(store, job, event)
            assert result["state"] == "COMPLETE"
            worker.apply(job, event, {"outcome_id": job["outcome_id"]}, result, clock[0])
        pool.tick(clock[0])
        assert store.db.execute("SELECT COUNT(*) FROM analytics_pool_slots").fetchone()[0] == 63
        assert pool.audit["releases"] == 1
        assert not worker.history.get("comparison-job:")


def test_worker_bootstrap_cannot_bypass_runtime_pool(tmp_path):
    from contextlib import closing

    from cryptowatcher.config import OwnerSettings
    from cryptowatcher.events import EventEngine
    from cryptowatcher.outcomes import OutcomeWorker
    from cryptowatcher.storage import Store
    from tests.test_events import NOW, window

    with closing(Store(tmp_path / "bootstrap.sqlite3", 233382575)) as store:
        store.initialize(OwnerSettings())
        store.clock_ms = lambda: NOW
        engine = EventEngine(store)
        engine.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True)
        worker = OutcomeWorker(store)
        pool = Pool(worker)
        pool.activate()
        before = encode(worker.history.items("event:"))
        worker.bootstrap()
        assert not worker.history.items("outcome-job:")
        assert pool.report()["waiting"] > 0
        pool.tick(NOW)
        assert worker.history.items("outcome-job:")
        assert encode(worker.history.items("event:")) == before
        legacy = {**worker.history.items("event:")[0][1], "outcome_unit": "EVENT_REFERENCE"}
        prior = encode(worker.history.items("outcome-job:"))
        worker.admit(legacy)
        assert encode(worker.history.items("outcome-job:")) == prior
        assert pool.audit["legacy_offers_not_admitted"] == 1
