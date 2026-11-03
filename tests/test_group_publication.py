"""Group publication integrity on disposable real SQLite; no capacity claim."""

import json
import sqlite3
from contextlib import closing

import pytest

from cryptowatcher.domain_repository import History, install
from cryptowatcher.events import EventEngine
from cryptowatcher.storage import StateError, encode
from cryptowatcher.writer_memory import source_group, transaction
from tests.test_events import NOW, window
from tests.test_runtime_repository import opened


def test_group_reuses_context_and_publishes_final_state_at_read_barrier(tmp_path):
    with closing(opened(tmp_path / "group.sqlite3", True)) as store:
        install(store)
        history = History(store)
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            with source_group(store) as group:
                for value in (1, 2):
                    with transaction(history) as nested:
                        assert nested is group
                        history.put("active:fixture", {"marker": value})
                        assert history.get("active:fixture") == {"marker": value}
                assert group.stats["domain_buffered"] == 2
                assert group.stats["domain_flushed"] == 0
                # Even direct SQL is a visibility barrier for mutable domain rows.
                row = store.db.execute(
                    "SELECT payload FROM domain_state WHERE key=?", ("active:fixture",)
                ).fetchone()
                assert json.loads(row[0]) == {"marker": 2}
                assert group.stats["domain_flushed"] == 1
            assert store.db.hot is None and store._writer_batch is None
        assert history.get("active:fixture") == {"marker": 2}


@pytest.mark.parametrize("abort", [False, True])
def test_group_savepoint_rollback_and_outer_failure_preserve_state(tmp_path, abort):
    with closing(opened(tmp_path / "savepoint.sqlite3", True)) as store:
        install(store)
        history = History(store)
        before = list(store.db.iterdump())
        try:
            with store.db:
                store.db.execute("BEGIN IMMEDIATE")
                with source_group(store):
                    history.put("active:fixture", {"marker": 1})
                    store.db.execute("SAVEPOINT fixture")
                    history.put("active:fixture", {"marker": 2})
                    history.put("agent:intent:fixture", {"marker": 2})
                    store.db.execute("ROLLBACK TO fixture")
                    store.db.execute("RELEASE fixture")
                    assert history.get("active:fixture") == {"marker": 1}
                    assert history.get("agent:intent:fixture") is None
                    if abort:
                        raise RuntimeError("abort complete group")
        except RuntimeError:
            assert abort
        assert store.db.hot is None and store._writer_batch is None
        if abort:
            assert list(store.db.iterdump()) == before
        else:
            assert history.get("active:fixture") == {"marker": 1}


def test_group_authorizer_change_cannot_reuse_deferred_read(tmp_path):
    with closing(opened(tmp_path / "authority.sqlite3", True)) as store:
        install(store)
        history = History(store)
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            with source_group(store):
                history.put("active:fixture", {"marker": 1})

                def deny(action, table, column, database, context):
                    return (
                        sqlite3.SQLITE_DENY
                        if action == sqlite3.SQLITE_READ and table == "domain_state"
                        else sqlite3.SQLITE_OK
                    )

                store.db.set_authorizer(deny)
                try:
                    with pytest.raises(sqlite3.DatabaseError):
                        history.get("active:fixture")
                finally:
                    store.db.set_authorizer(None)
        assert history.get("active:fixture") == {"marker": 1}


@pytest.mark.parametrize("change", ["owner", "readonly"])
def test_changed_authority_rolls_back_group_and_discards_context(tmp_path, change):
    with closing(opened(tmp_path / "owner.sqlite3", True)) as store:
        install(store)
        history = History(store)
        owner = store.owner_id
        before = list(store.db.iterdump())
        try:
            with pytest.raises(StateError, match="owner or transaction"), store.db:
                store.db.execute("BEGIN IMMEDIATE")
                with source_group(store):
                    history.put("active:fixture", {"marker": 1})
                    if change == "owner":
                        store.owner_id += 1
                    else:
                        store.readonly = True
        finally:
            store.owner_id, store.readonly = owner, False
        assert store.db.hot is None and store._writer_batch is None
        assert list(store.db.iterdump()) == before


def test_group_exact_revisions_match_individual_publications(tmp_path):
    def run(path, grouped):
        with closing(opened(path, True)) as store:
            install(store)
            engine = EventEngine(store)
            points = [
                ("spot/BTCUSDT/1m", NOW, "1m", 104),
                ("spot/BTCUSDT/3m", NOW + 180000, "3m", 110),
                ("linear/ETHUSDT/5m", NOW + 300000, "5m", 108),
            ]
            if grouped:
                with store.db:
                    store.db.execute("BEGIN IMMEDIATE")
                    with source_group(store):
                        for stream, at, tf, price in points:
                            engine.observe(
                                stream, window(at, tf, price), 1e6, 0.01, at, live=True, commit=False
                            )
            else:
                for stream, at, tf, price in points:
                    engine.observe(stream, window(at, tf, price), 1e6, 0.01, at, live=True)
            facts = {
                p: engine.history.items(p)
                for p in ("event:", "episode:", "history:", "outbox:telegram:", "pending:telegram:")
            }
            assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            return encode(facts)

    assert run(tmp_path / "individual.sqlite3", False) == run(tmp_path / "grouped.sqlite3", True)


def test_snapshot_reuse_still_checks_the_actual_stored_bytes(tmp_path):
    with closing(opened(tmp_path / "snapshot.sqlite3", True)) as store:
        install(store)
        engine = EventEngine(store)
        engine.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True)
        event = engine.history.items("event:")[0][1]
        row = store.db.execute(
            "SELECT digest FROM direct_links WHERE owner=? LIMIT 1",
            (f"event:{event['event_id']}:{event['revision']:010d}",),
        ).fetchone()
        assert row is not None
        triggers = store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='direct_snapshots'"
        ).fetchall()
        for trigger in triggers:
            store.db.execute('DROP TRIGGER "' + trigger[0].replace('"', '""') + '"')
        store.db.execute("UPDATE direct_snapshots SET payload='{}' WHERE digest=?", (row[0],))
        store.db.commit()
        before = list(store.db.iterdump())
        with pytest.raises(StateError, match="snapshot"), store.db:
            store.db.execute("BEGIN IMMEDIATE")
            with source_group(store):
                engine.history.version("event", {**event, "event_id": "new-test-identity"})
        assert list(store.db.iterdump()) == before


def test_finite_group_buffer_flushes_instead_of_dropping_updates(tmp_path):
    with closing(opened(tmp_path / "finite.sqlite3", True)) as store:
        install(store)
        history = History(store)
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            with source_group(store) as group:
                for index in range(140):
                    history.put(f"active:fixture-{index}", {"index": index})
                    assert len(group.direct) <= 128
                assert history.get("active:fixture-0") == {"index": 0}
        assert len(history.items("active:fixture-")) == 140
        assert store.db.hot is None


def test_group_metadata_rechecks_trigger_side_settings_mutation(tmp_path):
    with closing(opened(tmp_path / "trigger-settings.sqlite3", True)) as store:
        install(store)
        engine = EventEngine(store)
        store.db.execute("""CREATE TRIGGER fixture_settings_change AFTER INSERT
            ON direct_event_revisions BEGIN
            UPDATE owner_settings SET revision=revision+1; END""")
        store.db.commit()
        before = list(store.db.iterdump())
        with pytest.raises(StateError, match="settings"), store.db:
            store.db.execute("BEGIN IMMEDIATE")
            with source_group(store):
                engine.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True, commit=False)
        assert list(store.db.iterdump()) == before
        assert store.snapshot().revision == 1
