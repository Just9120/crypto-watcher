"""Direct repository compatibility using real temporary SQLite, no runtime activation."""

import sqlite3
from contextlib import closing

import pytest

from cryptowatcher.config import OwnerSettings
from cryptowatcher.domain_repository import History, install
from cryptowatcher.events import EventEngine
from cryptowatcher.market_storage import enable
from cryptowatcher.storage import StateError, Store, encode
from tests.test_events import NOW, window


def opened(path, writer_connection=False):
    store = Store(path, 233382575, writer_connection=writer_connection)
    store.initialize(OwnerSettings())
    if not store.bounded:
        enable(store.db)
        store.bounded = True
    store.clock_ms = lambda: NOW
    return store


@pytest.mark.parametrize("writer_connection", [False, True])
def test_additive_repository_preserves_legacy_records_and_new_public_numbers(tmp_path, writer_connection):
    path = tmp_path / "mixed.sqlite3"
    with closing(opened(path, writer_connection)) as store:
        engine = EventEngine(store)
        engine.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True)
        legacy = encode(engine.history.items("event:"))
        previous = engine.history.items("episode:")[0][1]
        store.db.commit()
        install(store)
        engine.history = History(store)
        assert encode(engine.history.items("event:")) == legacy
        engine.observe("linear/ETHUSDT/1m", window(), 1e6, 0.01, NOW, live=True)
        assert store.db.execute("SELECT COUNT(*) FROM direct_event_revisions").fetchone()[0] > 0
        assert engine.history.get(f"episode:{previous['episode_id']}:{previous['revision']:010d}") == previous
        engine.observe("spot/BTCUSDT/1m", window(NOW + 60000, "1m", 110), 1e6, 0.01, NOW + 60000, live=True)
        assert store.get_runtime("radar:v1:condition-state:spot/BTCUSDT/1m") is None
        engine.tick(NOW + 3600000)
        assert engine.history.get("episode-head:" + previous["episode_id"])["state"] == "CLOSED"
        saved = encode({kind: engine.history.items(kind) for kind in ("event:", "episode:")})
        assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
    with closing(opened(path, writer_connection)) as store:
        history = History(store)
        assert encode({kind: history.items(kind) for kind in ("event:", "episode:")}) == saved
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with closing(Store(path, 233382575, readonly=True)) as store:
        history = History(store)
        assert encode({kind: history.items(kind) for kind in ("event:", "episode:")}) == saved


@pytest.mark.parametrize("writer_connection", [False, True])
def test_repository_rejects_corruption_and_rolls_back_new_publications(tmp_path, writer_connection):
    with closing(opened(tmp_path / "rollback.sqlite3", writer_connection)) as store:
        install(store)
        engine = EventEngine(store)
        engine.history = History(store)
        engine.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True)
        before = list(store.db.iterdump())
        with pytest.raises(RuntimeError), store.db:
            engine.observe("linear/ETHUSDT/1m", window(), 1e6, 0.01, NOW, live=True, commit=False)
            raise RuntimeError("abort parent")
        assert list(store.db.iterdump()) == before
        # Updating immutable payload bytes must fail at the SQLite boundary.
        with pytest.raises(sqlite3.IntegrityError, match="Immutable"):
            store.db.execute("UPDATE direct_snapshots SET payload='{}'")
        row = store.db.execute("SELECT id,revision FROM direct_episode_revisions LIMIT 1").fetchone()
        store.db.execute("DELETE FROM domain_members WHERE id=?", (row[0],))
        with pytest.raises(StateError, match="members"):
            engine.history.get(f"episode:{row[0]}:{row[1]:010d}")


@pytest.mark.parametrize("writer_connection", [False, True])
def test_direct_head_remains_checked_after_existing_head_repacking(tmp_path, writer_connection):
    from cryptowatcher.head_codec import pack
    from cryptowatcher.storage import json_payload

    with closing(opened(tmp_path / "repacked-head.sqlite3", writer_connection)) as store:
        install(store)
        engine = EventEngine(store)
        engine.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True)
        key, value = engine.history.items("event-head:")[0]
        row = store.db.execute(
            "SELECT payload_json FROM runtime WHERE key=?", ("radar:v1:" + key,)
        ).fetchone()
        compressed = pack(json_payload(row[0]))
        assert compressed is not None
        store.db.execute("UPDATE runtime SET payload_json=? WHERE key=?", (compressed, "radar:v1:" + key))
        assert engine.history.get(key) == value


def test_runtime_writer_invalidates_metadata_when_authorizer_changes(tmp_path):
    with closing(opened(tmp_path / "authorizer.sqlite3", True)) as store:
        install(store)

        def captured(event, episode):
            store.snapshot()

            def deny(action, name, field, database, context):
                return (
                    sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_READ and name == "owner_settings"
                    else sqlite3.SQLITE_OK
                )

            store.db.set_authorizer(deny)
            try:
                with pytest.raises(sqlite3.DatabaseError):
                    store.snapshot()
            finally:
                store.db.set_authorizer(None)
            assert store.snapshot().revision == 1

        engine = EventEngine(store, on_event_snapshot=captured)
        engine.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True)
        assert engine.writer_stats["failed"] == 0
        with pytest.raises(StateError, match="owner"):
            Store(store.path, 233382576, readonly=True)


def test_runtime_cte_settings_change_invalidates_cache_and_aborts_whole_observation(tmp_path):
    with closing(opened(tmp_path / "cte-cas.sqlite3", True)) as store:
        install(store)

        def changed(event, episode):
            assert store.snapshot().revision == 1
            store.db.execute(
                "WITH c AS (SELECT 1) UPDATE owner_settings SET revision=revision+1 WHERE owner_id=?",
                (store.owner_id,),
            )
            assert store.snapshot().revision == 2

        engine = EventEngine(store, on_event_snapshot=changed)
        before = list(store.db.iterdump())
        with pytest.raises(StateError, match="settings"):
            engine.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True)
        assert list(store.db.iterdump()) == before
        assert store.snapshot().revision == 1


def test_repeated_revision_requires_identical_canonical_numeric_bytes(tmp_path):
    with closing(opened(tmp_path / "canonical.sqlite3", True)) as store:
        install(store)
        engine = EventEngine(store)
        engine.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True)
        event = engine.history.items("event:")[0][1]
        assert isinstance(event["reference_price"], float) and event["reference_price"].is_integer()
        altered = {**event, "reference_price": int(event["reference_price"])}
        with pytest.raises(StateError, match="immutable"), store.db:
            engine.history.version("event", altered)
        assert encode(engine.history.get(f"event:{event['event_id']}:{event['revision']:010d}")) == encode(
            event
        )
