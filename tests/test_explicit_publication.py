"""Integrity of explicit domain writes and targeted metadata invalidation."""

from contextlib import closing

import pytest

from cryptowatcher.domain_publication import publication
from cryptowatcher.domain_repository import History, install
from cryptowatcher.events import EventEngine
from cryptowatcher.storage import StateError, encode
from tests.test_events import NOW, window
from tests.test_runtime_repository import opened


def test_ordinary_market_writes_do_not_discard_verified_configuration(tmp_path):
    with closing(opened(tmp_path / "metadata.sqlite3", True)) as store:
        install(store)
        history = History(store)
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            with publication(history) as unit:
                expected = store.snapshot()
                misses = unit.batch.stats["metadata_misses"]
                for index in range(10):
                    history.put("diagnostic:fixture", {"index": index})
                    assert store.snapshot() == expected
                assert unit.batch.stats["metadata_misses"] == misses
                assert unit.batch.stats["metadata_hits"] >= 10


def test_temporary_metadata_guards_are_connection_local_and_fail_closed_if_changed(tmp_path):
    path = tmp_path / "guards.sqlite3"
    with closing(opened(path, True)) as store:
        install(store)
        assert (
            store.db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'cw_pub_%'").fetchone()[0]
            == 0
        )
        assert (
            store.db.execute("SELECT COUNT(*) FROM sqlite_temp_master WHERE name LIKE 'cw_pub_%'").fetchone()[
                0
            ]
            == 6
        )
        store.db.execute("DROP TRIGGER temp.cw_pub_owner_settings_update")
        before = list(store.db.iterdump())
        with pytest.raises(StateError, match="guards"), store.db:
            store.db.execute("BEGIN IMMEDIATE")
            with publication(History(store)):
                pass
        assert list(store.db.iterdump()) == before
    with closing(opened(path, True)) as store:
        install(store)
        assert store.snapshot().revision == 1
        assert (
            store.db.execute("SELECT COUNT(*) FROM sqlite_temp_master WHERE name LIKE 'cw_pub_%'").fetchone()[
                0
            ]
            == 6
        )


def test_callback_savepoint_rollback_cannot_leave_a_queued_agent_intent(tmp_path):
    with closing(opened(tmp_path / "callback.sqlite3", True)) as store:
        install(store)
        history = History(store)

        def callback(event, episode):
            store.db.execute("SAVEPOINT callback")
            history.put("agent:intent:must-not-survive", {"event": event["event_id"]})
            store.db.execute("ROLLBACK TO callback")
            store.db.execute("RELEASE callback")
            assert history.get("agent:intent:must-not-survive") is None

        engine = EventEngine(store, on_event_snapshot=callback)
        engine.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True)
        assert engine.history.items("event:")
        assert history.get("agent:intent:must-not-survive") is None
        assert store.db.hot is None


def test_prepared_write_capability_expires_and_cannot_cross_store(tmp_path):
    with (
        closing(opened(tmp_path / "first.sqlite3", True)) as first,
        closing(opened(tmp_path / "second.sqlite3", True)) as second,
    ):
        install(first)
        install(second)
        history = History(first)
        with first.db:
            first.db.execute("BEGIN IMMEDIATE")
            with publication(history) as unit:
                with pytest.raises(StateError, match="owner"):
                    History(second).publish_prepared(unit, "event", {})
        with pytest.raises(StateError, match="owner or transaction"):
            history.publish_prepared(unit, "event", {})


def test_ordinary_version_calls_keep_their_caught_error_savepoint(tmp_path):
    with closing(opened(tmp_path / "caught.sqlite3", True)) as store:
        install(store)
        history = History(store)

        def callback(event, episode):
            with pytest.raises(StateError, match="immutable"):
                history.version("event", {**event, "reference_price": "conflicting"})
            assert encode(history.get(f"event:{event['event_id']}:{event['revision']:010d}")) == encode(event)

        engine = EventEngine(store, on_event_snapshot=callback)
        engine.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True)
        assert engine.history.items("episode:")
        assert engine.writer_stats["publication_units"] == engine.writer_stats["observations"] == 1


def test_callback_barriers_preserve_every_detector_condition_and_rearm(tmp_path):
    with closing(opened(tmp_path / "condition-overlay.sqlite3", True)) as store:
        install(store)
        snapshots = []
        engine = EventEngine(
            store, on_event_snapshot=lambda event, episode: snapshots.append(event["event_id"])
        )
        engine.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True)
        initial = engine.history.condition_group("spot/BTCUSDT/1m")
        assert len(snapshots) > 1
        assert initial and all("last_ms" in item for item in initial.values())
        engine.tick(NOW + 5 * 60000)
        closed = engine.history.condition_group("spot/BTCUSDT/1m")
        assert set(closed) == set(initial)
        assert all("last_ms" in item for item in closed.values())
        engine.observe(
            "spot/BTCUSDT/1m", window(NOW + 6 * 60000, "1m", 110), 1e6, 0.01, NOW + 6 * 60000, live=True
        )
        assert engine.writer_stats["failed"] == 0


def test_callback_cannot_end_the_source_transaction_while_buffers_are_suspended(tmp_path):
    with closing(opened(tmp_path / "commit-guard.sqlite3", True)) as store:
        install(store)
        history = History(store)
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            with publication(history) as unit:
                with unit.callback():
                    with pytest.raises(StateError, match="commit"):
                        store.db.commit()
                    with pytest.raises(StateError, match="end"):
                        store.db.execute("COMMIT")
                    assert store.db.in_transaction
        assert store.db.publication_depth == 0


def test_configuration_is_checked_after_buffered_consumer_writes_are_flushed(tmp_path):
    with closing(opened(tmp_path / "final-settings.sqlite3", True)) as store:
        install(store)
        history = History(store)
        store.db.execute("""CREATE TRIGGER fixture_settings_on_intent AFTER INSERT
            ON runtime WHEN NEW.key LIKE 'radar:v1:agent:intent:%' BEGIN
            UPDATE owner_settings SET revision=revision+1; END""")
        store.db.commit()
        before = list(store.db.iterdump())
        with pytest.raises(StateError, match="settings"), store.db:
            store.db.execute("BEGIN IMMEDIATE")
            with publication(history):
                history.put("agent:intent:late", {"marker": "buffered"})
        assert list(store.db.iterdump()) == before
        assert store.snapshot().revision == 1
