from contextlib import closing

import pytest

from cryptowatcher.analytics import ComparisonWorker, as_of, cohorts
from cryptowatcher.digest import aggregate
from cryptowatcher.events import History
from cryptowatcher.export import Selection, csv_summary, ndjson
from cryptowatcher.outcomes import OutcomeWorker
from cryptowatcher.sources import Sources
from cryptowatcher.storage import Store
from tests.catalogue_fixture import move_events
from tests.test_analytics import series
from tests.test_bounded_storage import bounded
from tests.test_outcomes import DAY, MINUTE, NOW, future, observe


def test_bootstrap_reads_all_cold_history_pages_and_preserves_asof_revisions(store):
    bounded(store)
    worker = OutcomeWorker(store)
    observe(store, worker)
    history = History(store)
    expected_jobs = sorted(history.items("outcome-job:"))
    expected_events = history.events(100)
    assert len(expected_events) > 1
    with store.db:
        for prefix in ("outcome-job:", "outcome-pending:"):
            for key, _ in history.items(prefix):
                history.delete(key)
        history.delete("outcome-admission")
    move_events(store)
    assert history.events(100) == expected_events
    for _ in range(100):
        worker.bootstrap(batch=1)
        if history.get("outcome-admission")["complete"]:
            break
    assert history.get("outcome-admission")["complete"]
    assert sorted(history.items("outcome-job:")) == expected_jobs
    old = expected_events[0]
    with store.db:
        newer = {**old, "revision": old["revision"] + 1, "available_at_ms": old["available_at_ms"] + 10000}
        history.version("event", newer)
    assert as_of(history, "event", old["event_id"], old["available_at_ms"] - 1) is None
    assert as_of(history, "event", old["event_id"], old["available_at_ms"] + 5000) == old
    assert as_of(history, "event", old["event_id"], newer["available_at_ms"]) == newer


def test_bootstrap_failure_rolls_back_one_batch_without_committing_the_callers_transaction(
    store, monkeypatch
):
    bounded(store)
    worker = OutcomeWorker(store)
    observe(store, worker)
    history = History(store)
    with store.db:
        for prefix in ("outcome-job:", "outcome-pending:"):
            for key, _ in history.items(prefix):
                history.delete(key)
        history.delete("outcome-admission")
    move_events(store)
    actual = worker.admit
    calls = 0

    def failing_admit(event):
        nonlocal calls
        calls += 1
        actual(event)
        if calls == 2:
            raise RuntimeError("injected admission failure")

    monkeypatch.setattr(worker, "admit", failing_admit)
    with store.db:
        history.put("caller-state", {"pending": True})
        assert store.db.in_transaction
        with pytest.raises(RuntimeError, match="admission failure"):
            worker.bootstrap(batch=100)
        assert store.db.in_transaction
        assert history.count("outcome-job:") == 0
        assert history.count("outcome-pending:") == 0
        assert history.get("outcome-admission") is None
        assert history.get("caller-state") == {"pending": True}
        with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
            assert History(reader).get("caller-state") is None
        # Pure SQL reports must preserve this same caller transaction too.
        aggregate(store, NOW - DAY, NOW + DAY, NOW)
        cohorts(store)
        assert store.db.in_transaction
    assert history.get("caller-state") == {"pending": True}


def test_all_domain_sql_consumers_keep_exact_exports_digest_cohorts_and_asof_after_transfer(
    store, monkeypatch
):
    import cryptowatcher.export as exports

    monkeypatch.setattr(exports.time, "time", lambda: (NOW + 10 * MINUTE) / 1000)
    bounded(store)
    series(store, days=2)
    worker = OutcomeWorker(store)
    observe(store, worker)
    Sources(store).put("spot/BTCUSDT/1m", future(), recovery=True)
    worker.work(NOW + 6 * MINUTE)
    history = History(store)
    event = history.events(1)[0]
    comparisons = ComparisonWorker(store)
    comparisons.requeue(event["event_id"], NOW + 7 * MINUTE)
    for _ in range(20):
        comparisons.work(NOW + 8 * MINUTE)
        if history.get("comparison-job:" + event["event_id"]) is None:
            break
    assert any(value.get("sample_size", 0) > 0 for _, value in history.items("comparison-head:"))
    selections = [
        Selection(NOW - DAY, NOW + DAY),
        Selection(NOW - 1, NOW + 1, "price", "1", "spot"),
        Selection(NOW - 1, NOW + 1, market="linear"),
    ]
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        before_exports = [
            (list(ndjson(reader, selection)), list(csv_summary(reader, selection)))
            for selection in selections
        ]
        assert not reader.db.in_transaction
    before_cohorts = cohorts(store)
    before_cutoff = cohorts(store, cutoff_ms=NOW + 6 * MINUTE)
    before_digest = aggregate(store, NOW - DAY, NOW + DAY, NOW + 8 * MINUTE)
    assert not store.db.in_transaction
    move_events(store, ("event", "outcome", "episode", "comparison"))
    assert cohorts(store) == before_cohorts
    assert cohorts(store, cutoff_ms=NOW + 6 * MINUTE) == before_cutoff
    assert aggregate(store, NOW - DAY, NOW + DAY, NOW + 8 * MINUTE) == before_digest
    assert not store.db.in_transaction
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        for selection, expected in zip(selections, before_exports, strict=True):
            assert (list(ndjson(reader, selection)), list(csv_summary(reader, selection))) == expected
        assert not reader.db.in_transaction
