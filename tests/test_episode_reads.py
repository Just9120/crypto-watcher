"""Episode reuse cannot hide a changed, missing or unreadable SQLite record."""

import sqlite3

import pytest

from cryptowatcher.events import PREFIX, EventEngine
from cryptowatcher.storage import RUNTIME_UPSERT, StateError, encode
from tests.hot_state import Batch
from tests.sqlite_repository import History
from tests.test_events import NOW, window
from tests.test_hot_agent import hot_store as hot_store


def prepared(store):
    engine = EventEngine(store)
    engine.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True)
    event = engine.history.events()[0]
    key = "episode-head:" + event["episode_id"]
    return engine.history, key


def enter(store, history, stats):
    store.db.execute("BEGIN IMMEDIATE")
    batch = Batch(history, History._put_raw, History._delete_raw, stats)
    store.db.hot = batch
    return batch


def test_episode_memo_copies_and_sees_new_revision_rollback_corruption_and_deletion(hot_store):
    store, stats = hot_store
    history, key = prepared(store)
    with store.db:
        batch = enter(store, history, stats)
        try:
            first = history.get(key)
            returned = history.get(key)
            returned["confirmations"].clear()
            assert history.get(key) == first
            assert stats["episode_record_hits"] >= 2
            # The same connection cannot borrow the memo under another owner.
            owner, hits = store.owner_id, stats["episode_record_hits"]
            try:
                store.owner_id = owner + 1
                assert history.get(key) == first
                assert stats["episode_record_hits"] == hits
            finally:
                store.owner_id = owner
            store.db.execute("SAVEPOINT changed_episode")
            changed = {**first, "revision": first["revision"] + 1, "strength": first["strength"] + 1}
            history.version("episode", changed)
            assert history.get(key) == changed
            store.db.execute("ROLLBACK TO changed_episode")
            store.db.execute("RELEASE changed_episode")
            assert history.get(key) == first
            # Metadata after a full read must still omit private input fields;
            # a prior metadata read must not strip a subsequent public body.
            small = {**first, "revision": first["revision"] + 1, "source_context": {"x": 1}}
            history.version("episode", small)
            assert history.get(key) == small
            assert "source_context" not in history.metadata(key)
            assert history.get(key) == small
            original = store.get_runtime(PREFIX + key)
            store.db.execute(RUNTIME_UPSERT, (PREFIX + key, encode({**original, "strength": 999})))
            with pytest.raises(StateError, match="head projection"):
                history.get(key)
            store.db.execute(RUNTIME_UPSERT, (PREFIX + key, encode(original)))
            assert history.get(key) == small
            history._delete_raw(key)
            assert history.get(key) is None
            assert batch.episodes.bytes <= 1024**2
        finally:
            store.db.hot = None
    assert history.get(key) is None


def test_episode_memo_does_not_hide_sqlite_access_denial_or_linked_corruption(hot_store):
    store, stats = hot_store
    history, key = prepared(store)
    with store.db:
        enter(store, history, stats)
        try:
            first = history.get(key)
            assert history.get(key) == first
            store.db.set_authorizer(
                lambda action, table, *_: (
                    sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_READ and table == "prototype_episode_revisions"
                    else sqlite3.SQLITE_OK
                )
            )
            with pytest.raises(sqlite3.DatabaseError):
                history.get(key)
            store.db.set_authorizer(None)
            linked = {**first, "revision": first["revision"] + 1, "source_context": {"x": "a" * 512}}
            history.version("episode", linked)
            assert history.get(key) == linked
            digest = store.db.execute(
                "SELECT digest FROM prototype_links WHERE owner=? AND field='source_context'",
                (f"episode:{first['episode_id']}:{linked['revision']:010d}",),
            ).fetchone()[0]
            store.db.execute("DROP TRIGGER prototype_snapshot_immutable")
            store.db.execute("UPDATE prototype_snapshots SET payload='{}' WHERE digest=?", (digest,))
            with pytest.raises(StateError, match="corrupt direct snapshot"):
                history.get(key)
        finally:
            store.db.set_authorizer(None)
            store.db.hot = None


def test_episode_memo_is_bounded_and_not_shared_with_the_next_operation(hot_store):
    store, stats = hot_store
    history, key = prepared(store)
    with store.db:
        first = enter(store, history, stats)
        try:
            history.get(key)
            assert first.episodes.entries
            for i in range(80):
                first.episodes.remember(str(i), ("x" * 16000,))
            assert len(first.episodes.entries) <= 32
            assert first.episodes.bytes <= 1024**2
            first.episodes.remember("too-large", ("x" * 1024**2,))
            assert len(first.episodes.entries) <= 32
        finally:
            store.db.hot = None
    with store.db:
        second = enter(store, history, stats)
        try:
            before = stats["episode_row_misses"]
            history.get(key)
            assert stats["episode_row_misses"] == before + 1
            assert second.episodes is not first.episodes
        finally:
            store.db.hot = None
