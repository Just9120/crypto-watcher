import sqlite3
import time
from contextlib import closing

import pytest

from cryptowatcher import change_journal as journal
from cryptowatcher.storage import StateError, Store

EPOCH = "a" * 32


def install(store, **overrides):
    options = dict(
        epoch=EPOCH, owner=store.owner_id, max_keys=1000, max_bytes=1024**2, expires=int(time.time()) + 3600
    )
    options.update(overrides)
    return journal.install(store.db, **options)


def exported(store, after=0):
    with closing(sqlite3.connect(store.path)) as reader:
        with journal.changes(
            reader, epoch=EPOCH, owner=store.owner_id, after=after, deadline=time.monotonic() + 10
        ) as (mark, rows):
            return mark, list(rows)


def test_all_writes_and_deletions_are_atomic_and_coalesced(store):
    install(store)
    store.put_runtime("old", {"before": True})
    with store.db:
        store.db.execute("UPDATE runtime SET key='renamed' WHERE key='old'")
        store.db.execute("INSERT INTO mutes VALUES('BTCUSDT',123)")
        store.db.execute("UPDATE owner_settings SET revision=revision+1")
        store.db.execute("UPDATE delivery_preferences SET enabled=0")
        store.db.execute("INSERT INTO legacy_import VALUES('hash','private','backup')")
    before = exported(store)[0]["last_seq"]
    store.db.execute("BEGIN")
    store.db.execute("INSERT INTO runtime VALUES('atomic','{}')")
    store.db.rollback()
    mark, rows = exported(store)
    assert mark["last_seq"] == before
    by_key = {(r["table"], r["key"]): r["values"] for r in rows}
    assert by_key[("runtime", "old")] is None
    assert by_key[("runtime", "renamed")] == ("renamed", '{"before":true}')
    assert ("runtime", "atomic") not in by_key
    assert by_key[("owner_settings", str(store.owner_id))][1] == 2
    assert by_key[("delivery_preferences", str(store.owner_id))][1] == 0
    assert by_key[("legacy_import", "hash")] == ("hash", "private", "backup")
    assert by_key[("mutes", "BTCUSDT")] == ("BTCUSDT", 123.0)
    store.put_runtime("renamed", {"after": True})
    later, changed = exported(store, mark["last_seq"])
    assert later["last_seq"] > mark["last_seq"]
    assert changed == [dict(table="runtime", key="renamed", values=("renamed", '{"after":true}'))]


def test_snapshot_watermark_never_acknowledges_later_writes(store):
    install(store)
    store.put_runtime("key", "first")
    with closing(sqlite3.connect(store.path)) as reader:
        with journal.changes(
            reader, epoch=EPOCH, owner=store.owner_id, after=0, deadline=time.monotonic() + 10
        ) as (mark, rows):
            store.put_runtime("key", "second")
            store.put_runtime("late", "new")
            assert list(rows) == [dict(table="runtime", key="key", values=("key", '"first"'))]
    later, rows = exported(store, mark["last_seq"])
    assert later["last_seq"] > mark["last_seq"]
    assert len(rows) == 2
    assert {r["key"] for r in rows} == {"key", "late"}


@pytest.mark.parametrize("options", [{"max_keys": 1}, {"max_bytes": 132}])
def test_overflow_blocks_promotion_and_preserves_app_writes(store, options):
    install(store, **options)
    store.put_runtime("one", 1)
    store.put_runtime("one", 2)
    assert exported(store)[0]["key_count"] == 1
    store.put_runtime("two", 3)
    store.put_runtime("still-running", 4)
    assert store.get_runtime("still-running") == 4
    with pytest.raises(StateError, match="completeness"):
        exported(store)
    assert store.db.execute(f"SELECT COUNT(*) FROM {journal.KEYS}").fetchone()[0] == 1


def test_expiry_and_null_key_do_not_break_application_write(store):
    install(store)
    with store.db:
        store.db.execute(f"UPDATE {journal.META} SET expires=1")
    store.put_runtime("after-expiry", True)
    assert store.get_runtime("after-expiry") is True
    with pytest.raises(StateError, match="completeness"):
        exported(store)


def test_invalid_source_key_is_diagnosed_without_rejecting_original_sql(store):
    install(store)
    with store.db:
        store.db.execute("INSERT INTO runtime VALUES(NULL,'{}')")
    assert store.db.execute("SELECT COUNT(*) FROM runtime WHERE key IS NULL").fetchone()[0] == 1
    with pytest.raises(StateError, match="completeness"):
        exported(store)


def test_restart_keeps_epoch_and_refuses_reset_or_changed_ddl(store):
    options = install(store)
    store.put_runtime("persisted", True)
    with closing(Store(store.path, store.owner_id)) as reopened:
        assert (
            journal.install(
                reopened.db,
                epoch=EPOCH,
                owner=store.owner_id,
                max_keys=1000,
                max_bytes=1024**2,
                expires=options["expires"],
            )["key_count"]
            == 1
        )
        for overrides in ({"epoch": "b" * 32}, {"max_keys": 1001}):
            with pytest.raises(StateError):
                install(reopened, expires=options["expires"], **overrides)
        with reopened.db:
            reopened.db.execute(f"DROP TRIGGER {journal.PREFIX}runtime_insert")
        with pytest.raises(StateError, match="schema"):
            install(reopened, expires=options["expires"])
        assert reopened.get_runtime("persisted") is True


def test_install_refuses_unknown_schema_or_owner_atomically(store):
    with pytest.raises(StateError, match="owner"):
        install(store, owner=store.owner_id + 1)
    assert not store.db.execute("SELECT name FROM sqlite_master WHERE name LIKE '_cw_catchup_%'").fetchall()
    with store.db:
        store.db.execute("CREATE TABLE unknown(id)")
    with pytest.raises(StateError, match="schema"):
        install(store)
    assert not store.db.execute("SELECT name FROM sqlite_master WHERE name=?", (journal.META,)).fetchall()


def test_future_cursor_and_deadline_are_refused_and_reader_released(store):
    install(store)
    store.put_runtime("test", 1)
    with pytest.raises(StateError, match="cursor"):
        exported(store, 10**6)
    with closing(sqlite3.connect(store.path)) as reader:
        with pytest.raises(StateError, match="time budget"):
            with journal.changes(
                reader, epoch=EPOCH, owner=store.owner_id, after=0, deadline=time.monotonic() - 1
            ) as (_, rows):
                list(rows)
        assert not reader.in_transaction
    assert store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
