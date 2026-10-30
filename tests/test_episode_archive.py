"""Episode summaries survive staggered detail expiry without double counting."""

import json
from contextlib import closing

import pytest

from cryptowatcher import outcomes
from cryptowatcher.analytics import cohorts
from cryptowatcher.events import EventEngine, History
from cryptowatcher.export import Selection, ndjson
from cryptowatcher.market_storage import enable
from cryptowatcher.outcomes import DAY, MINUTE, OutcomeWorker, Retention
from cryptowatcher.quality_summary import archive_facts, month_boundary, prepare_event_facts
from cryptowatcher.sources import Candle, Sources
from cryptowatcher.storage import StateError, Store

from .test_events import NOW, observe, selected


def populate(store):
    enable(store.db)
    store.bounded = True
    store.clock_ms = lambda: NOW
    worker = OutcomeWorker(store)
    engine = EventEngine(store, selected("price", "composite"), on_event=worker.admit)
    observe(engine)
    observe(engine, NOW + MINUTE)
    observe(engine, NOW + 2 * MINUTE)
    observe(engine, NOW + 3 * MINUTE, tf="3m")
    end = NOW + 60 * MINUTE
    sources = Sources(store)
    stream = "spot/BTCUSDT/1m"
    sources.load(stream)
    sources.put(
        stream,
        [
            sources.windows[stream].get(t)
            or Candle.parse([t, 102, 103, 101, 102, 1, 400], "1m", "rest", end, end)
            for t in range(NOW, end, MINUTE)
        ],
    )
    worker.work(end)
    assert len(engine.history.events()) == 4
    assert all(r["state"] == "COMPLETE" for _, r in engine.history.items("outcome-head:"))
    return engine.history


def test_partial_expiry_readonly_export_frozen_method_and_calendar_ttl(store, monkeypatch):
    history = populate(store)
    groups = ("detector", "timeframe", "strength", "settings")
    before = cohorts(store, groups)["cohorts"]
    assert len(before) == 16 and {r["sample_size"] for r in before} == {1}
    origin = history.get("episode-head:" + history.events()[0]["episode_id"])["origin_event_id"]
    monkeypatch.setattr(outcomes, "METHOD", "future-default")
    retention = Retention(store)
    for _ in range(20):
        retention.work(NOW + 30 * DAY + 1, batch=1)
        if history.metadata("event-head:" + origin) is None:
            break
    assert history.metadata("event-head:" + origin) is None
    assert history.events(1)  # Later confirmations have not reached 30 days.
    assert cohorts(store, groups)["cohorts"] == before
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        exported = [json.loads(line) for line in ndjson(reader, Selection(detector="composite"))]
        summaries = [r["record"] for r in exported if r["type"] == "quality_summary"]
        assert {r["dimensions"]["timeframe"] for r in summaries} == {"1m", "3m"}
        assert {r["dimensions"]["methodology_version"] for r in summaries} == {"2"}
        assert all(r["facts"][0]["metrics"] and not r["detail_available"] for r in summaries)
        assert all("source_inputs" not in r["facts"][0] for r in summaries)
        assert reader.db.total_changes == 0
    for _ in range(100):
        retention.work(NOW + 31 * DAY, batch=1)
        if not history.events(1):
            break
    assert not history.events(1)
    assert cohorts(store, groups)["cohorts"] == before
    assert store.db.execute("SELECT count(*) FROM market_episode_results").fetchone()[0] == 4
    assert store.db.execute("SELECT count(*) FROM market_episode_members").fetchone()[0] == 4
    counted = store.db.execute(
        "SELECT records,logical_bytes FROM market_counts WHERE category='quality_episodes'"
    ).fetchone()
    provenance_count = store.db.execute("SELECT count(*) FROM market_episode_provenance").fetchone()[0]
    assert provenance_count == 3  # Origin settings and two detector definitions, not a copy per result.
    assert (
        store.db.execute("SELECT count(DISTINCT provenance) FROM market_episode_results").fetchone()[0] == 1
    )
    assert counted[0] == 8 + provenance_count and counted[1] > 0
    retention.work(NOW + 32 * DAY)
    assert cohorts(store, groups)["cohorts"] == before
    expires = month_boundary(NOW // DAY, 12) * DAY
    retention.work(expires - 1)
    assert cohorts(store, groups)["cohorts"] == before
    retention.work(expires)
    assert cohorts(store, groups)["cohorts"] == []
    assert store.db.execute("SELECT count(*) FROM market_episode_members").fetchone()[0] == 0
    assert tuple(
        store.db.execute(
            "SELECT records,logical_bytes FROM market_counts WHERE category='quality_episodes'"
        ).fetchone()
    ) == (0, 0)


def test_summary_rollback_and_conflicting_numeric_result_preserve_details(store):
    history = populate(store)
    event = history.events()[0]
    prepared = prepare_event_facts(store, event)
    before = list(store.db.iterdump())
    with pytest.raises(RuntimeError), store.db:
        archive_facts(store, prepared)
        raise RuntimeError("interrupted expiry")
    assert list(store.db.iterdump()) == before
    with store.db:
        archive_facts(store, prepared)
    baseline = list(store.db.iterdump())
    changed = [(day, group, provenance, {**fact, "noise": 999}) for day, group, provenance, fact in prepared]
    with pytest.raises(StateError, match="Conflicting immutable"), store.db:
        archive_facts(store, changed)
    assert list(store.db.iterdump()) == baseline
    assert History(store).metadata("event-head:" + event["event_id"]) is not None


def test_archival_uses_saved_numbers_when_raw_source_windows_are_unavailable(store, monkeypatch):
    from cryptowatcher import market_storage

    history = populate(store)
    event = history.metadata("event-head:" + history.events()[0]["event_id"])

    def unavailable(*args, **kwargs):
        raise StateError("Raw source lease expired")

    monkeypatch.setattr(market_storage, "restore_window", unavailable)
    prepared = prepare_event_facts(store, event)
    assert len(prepared) == 4
    assert all(fact["state"] == "COMPLETE" and fact["metrics"] for _, _, _, fact in prepared)
    with store.db:
        archive_facts(store, prepared)
    assert store.db.execute("SELECT count(*) FROM market_episode_results").fetchone()[0] == 4


def test_long_downtime_gc_keeps_episode_origin_until_last_overdue_member_is_archived(store):
    history = populate(store)
    retention = Retention(store)
    now = NOW + 400 * DAY
    # One member per batch guarantees the root is retired before its later
    # confirmations. Annual GC must not remove the only shared frozen result
    # while those other overdue details still await cleanup.
    retention.work(now, batch=1)
    assert history.count("event-head:") == 3
    assert store.db.execute("SELECT count(*) FROM market_episode_results").fetchone()[0] == 4
    for _ in range(40):
        retention.work(now, batch=1)
        if (
            not history.count("event-head:")
            and not store.db.execute("SELECT 1 FROM market_episode_results").fetchone()
        ):
            break
    assert history.count("event-head:") == history.count("outcome-job:") == 0
    assert store.db.execute("SELECT count(*) FROM market_episode_results").fetchone()[0] == 0
    assert store.db.execute("SELECT count(*) FROM market_episode_members").fetchone()[0] == 0
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()


def test_many_same_timestamp_members_keep_origins_across_bounded_expiry_batches(tmp_path):
    from scripts.measure_market_storage import measure

    value = measure(tmp_path / "same-stamp.sqlite3", instruments=5)
    assert value["events"] == 200
    assert value["episodes"] == 20
    assert value["outcomes"] == 80 and value["all_outcomes_complete"]
    assert value["automatic_comparison_jobs"] == 0
    assert value["analytics_equality"] == "PASS"
