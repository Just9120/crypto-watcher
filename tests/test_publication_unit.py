"""Publication-unit correctness, authority, rollback and the real small pipeline."""

import copy
import json
import sqlite3
from contextlib import closing

import pytest

from cryptowatcher.config import OwnerSettings
from cryptowatcher.events import PREFIX, EventEngine
from cryptowatcher.market_storage import enable
from cryptowatcher.outcomes import OutcomeWorker
from cryptowatcher.sources import Sources
from cryptowatcher.storage import RUNTIME_UPSERT, StateError, Store, encode
from tests import publication_unit as unit
from tests.processing_replay import replay
from tests.sqlite_repository import install, maintenance, repository_context
from tests.test_events import NOW, window
from tests.test_sqlite_repository import core as core


def producer(store, callback=None):
    worker = OutcomeWorker(store)

    def published(event, episode):
        worker.admit(event, episode=episode)
        if callback:
            callback(event, episode)

    return EventEngine(store, on_event=lambda event: None, on_event_snapshot=published)


def advance(store, radar, market="spot", tf="1m", at=NOW, price=104):
    source = Sources(store)
    candles = window(at, tf, price)
    stream = f"{market}/BTCUSDT/{tf}"
    source.load(stream)
    incoming = [c for c in candles if c.start not in source.windows[stream]]
    source.put(
        stream,
        incoming,
        live=True,
        after_write=lambda candidate, live: radar.observe(
            stream,
            sorted(candidate.windows[stream].values(), key=lambda c: c.start),
            1e6,
            0.01,
            at,
            live=live,
            commit=False,
        ),
    )
    return source


def facts(path, enabled):
    with repository_context(True), unit.installed(enabled) as stats, closing(Store(path, 233382575)) as store:
        store.initialize(OwnerSettings())
        enable(store.db)
        store.bounded = True
        store.clock_ms = lambda: NOW
        install(store)
        radar = producer(store)
        for market, tf, at, price in [
            ("spot", "1m", NOW, 104),
            ("spot", "1m", NOW + 60000, 102),
            ("spot", "1m", NOW + 120000, 110),
            ("spot", "3m", NOW + 180000, 104),
            ("linear", "5m", NOW, 104),
        ]:
            advance(store, radar, market, tf, at, price)
        assert maintenance(store, NOW)["state"] == "DIRECT_SQLITE_CORE"
        result = encode(
            {
                p: radar.history.items(p)
                for p in (
                    "event:",
                    "episode:",
                    "outcome-job:",
                    "outcome-pending:",
                    "comparison-job:",
                    "history:",
                    "notification:",
                )
            }
        )
        assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
        return result, dict(stats)


def test_publication_preserves_exact_origin_updates_both_markets_and_tfs(tmp_path):
    expected, _ = facts(tmp_path / "old.sqlite3", False)
    actual, stats = facts(tmp_path / "new.sqlite3", True)
    assert actual == expected
    assert stats["observations"] == stats["completed_units"] == 5
    assert stats["failed_units"] == 0
    assert stats["prepared_revisions"] == stats["body_encodes"] > 0
    assert stats["reused_records"] > 0
    assert 0 < stats["peak_keys"] <= 128 and 0 < stats["peak_serialized_bytes"] <= 1024**2


@pytest.fixture
def published(core):
    with unit.installed() as stats:
        radar = producer(core)
        advance(core, radar)
        yield core, radar, stats


def active(store, history, stats):
    store.db.execute("BEGIN IMMEDIATE")
    current = unit.Publication(history, stats)
    return current, unit.ACTIVE.set(current)


def test_prepared_read_copies_and_observes_raw_head_and_snapshot_corruption(published):
    store, radar, stats = published
    event = radar.history.events()[0]
    key = "event-head:" + event["event_id"]
    current, token = active(store, radar.history, stats)
    try:
        first = radar.history.get(key)
        first["source_inputs"].clear()
        assert radar.history.get(key)["source_inputs"] == event["source_inputs"]
        assert stats["reused_records"] > 0
        before = store.get_runtime(PREFIX + key)
        store.db.execute(RUNTIME_UPSERT, (PREFIX + key, encode({**before, "strength": 999})))
        with pytest.raises(StateError, match="head projection"):
            radar.history.get(key)
        store.db.execute(RUNTIME_UPSERT, (PREFIX + key, encode(before)))
        assert radar.history.get(key) == event
        store.db.execute("SAVEPOINT damage")
        store.db.execute("DROP TRIGGER prototype_snapshot_immutable")
        digest = store.db.execute(
            "SELECT digest FROM prototype_links WHERE owner=? AND field='source_inputs'",
            (f"event:{event['event_id']}:0000000001",),
        ).fetchone()[0]
        store.db.execute("UPDATE prototype_snapshots SET payload='[]' WHERE digest=?", (digest,))
        with pytest.raises(StateError, match="corrupt direct snapshot"):
            radar.history.get(key)
        store.db.execute("ROLLBACK TO damage")
        store.db.execute("RELEASE damage")
        assert radar.history.get(key) == event
    finally:
        unit.ACTIVE.reset(token)
        store.db.rollback()


def test_permissions_owner_and_rollback_cannot_reuse_prepared_records(published):
    store, radar, stats = published
    event = radar.history.events()[0]
    key = "event-head:" + event["event_id"]
    current, token = active(store, radar.history, stats)
    owner = store.owner_id
    try:
        assert radar.history.get(key) == event
        store.db.set_authorizer(
            lambda action, table, *args: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and table == "prototype_event_revisions"
                else sqlite3.SQLITE_OK
            )
        )
        with pytest.raises(sqlite3.DatabaseError):
            radar.history.get(key)
        store.db.set_authorizer(None)
        store.owner_id += 1
        with pytest.raises(StateError, match="owner or write mode"):
            radar.history.get(key)
        store.owner_id = owner
        store.readonly = True
        with pytest.raises(StateError, match="owner or write mode"):
            radar.history.get(key)
        store.readonly = False
        store.db.execute("SAVEPOINT version")
        newer = {**event, "revision": 2, "strength": event["strength"] + 1}
        radar.history.version("event", newer)
        assert radar.history.get(key) == newer
        store.db.execute("ROLLBACK TO version")
        store.db.execute("RELEASE version")
        assert radar.history.get(key) == event
        assert radar.history.get(f"event:{event['event_id']}:0000000002") is None
    finally:
        store.db.set_authorizer(None)
        store.owner_id = owner
        store.readonly = False
        unit.ACTIVE.reset(token)
        store.db.rollback()


def test_publication_keeps_conflict_reference_and_caught_statement_rollback(published):
    store, radar, stats = published
    event = radar.history.events()[0]
    current, token = active(store, radar.history, stats)
    try:
        before = list(store.db.iterdump())
        with pytest.raises(StateError, match="Conflicting immutable"):
            radar.history.version("event", {**event, "strength": 999})
        assert list(store.db.iterdump()) == before
        with pytest.raises(StateError, match="reference is immutable"):
            radar.history.version("event", {**event, "revision": 2, "reference_price": 1})
        assert list(store.db.iterdump()) == before
        radar.history.version("event", event)
        assert list(store.db.iterdump()) == before
        store.db.execute(
            "CREATE TRIGGER reject_publication BEFORE INSERT ON prototype_event_revisions WHEN NEW.revision=2 BEGIN SELECT RAISE(ABORT,'publication failure'); END"
        )
        before = list(store.db.iterdump())
        with pytest.raises(sqlite3.DatabaseError, match="publication failure"):
            radar.history.version(
                "event",
                {
                    **event,
                    "revision": 2,
                    "extra_shared": {"not_used": True},
                    "source_context": {"new": "x" * 200},
                },
            )
        assert list(store.db.iterdump()) == before
    finally:
        unit.ACTIVE.reset(token)
        store.db.rollback()


def test_consumer_failure_rolls_back_source_publication_and_all_jobs(core):
    def fail(event, episode):
        raise RuntimeError("consumer failure")

    with unit.installed() as stats:
        radar = producer(core, fail)
        before = list(core.db.iterdump())
        with pytest.raises(RuntimeError, match="consumer failure"):
            advance(core, radar)
        assert list(core.db.iterdump()) == before
        assert radar.history.events() == [] and unit.ACTIVE.get() is None
        assert stats["failed_units"] == 1
        advance(core, producer(core))
        assert radar.history.events()


def test_publication_view_has_a_finite_budget_and_cannot_escape_transaction(published):
    store, radar, stats = published
    event = radar.history.events()[0]
    key = "event-head:" + event["event_id"]
    current, token = active(store, radar.history, stats)
    try:
        radar.history.get(key)
        saved = copy.deepcopy(current.records[key])
        saved.snapshots["large"] = "x" * 1024**2
        with pytest.raises(StateError, match="record budget"):
            current.remember(key, saved)
        store.db.rollback()
        with pytest.raises(StateError, match="same active source transaction"):
            radar.history.get(key)
    finally:
        unit.ACTIVE.reset(token)


async def test_small_full_pipeline_matches_reference_and_preserves_delivery_maintenance(tmp_path):
    values = []
    for name, enabled in (("reference", False), ("publication", True)):
        with unit.installed(enabled) as stats:
            result = await replay(
                tmp_path / (name + ".sqlite3"),
                instruments=3,
                seconds=8,
                direct_sqlite=True,
                bounded_pool=True,
                hot_state=True,
            )
            assert result["accepted_closed"] == result["offered_closed"] == 24
            assert result["producer_errors"] == result["worker_errors"] == result["receiver_errors"] == []
            assert (
                result["completed"] > 0
                and result["signed_agent_receipts"] > 0
                and result["telegram_attempts"] > 0
            )
            assert result["direct_sqlite"]["foreign_key_violations"] == []
            # In8s startup/recovery admission can defer maintenance. The full
            # 130s native gate still requires actual DIRECT_SQLITE_CORE work;
            # the golden facts test above also executes actual maintenance.
            assert result["scheduled_calls"]["repack"] > 0
            if enabled:
                assert stats["observations"] == stats["completed_units"] == 24
                assert stats["prepared_revisions"] > 0 and stats["failed_units"] == 0
            values.append((result["bounded_pool"]["global_actual_sha256"], result["numeric_sha256"]))
            json.dumps(stats)
    assert values[0] == values[1]


def test_qualification_rejects_an_incomplete_publication_unit():
    from scripts.qualify_processing import verdict

    result = {
        "accepted_closed": 24,
        "publication_unit": {
            "mode": "one-pass-v1",
            "observations": 24,
            "completed_units": 23,
            "failed_units": 1,
        },
    }
    assert "PUBLICATION_UNIT_EVIDENCE_INCOMPLETE" in verdict(result, direct_sqlite=True, bounded_pool=True)
