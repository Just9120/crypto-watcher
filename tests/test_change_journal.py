import hashlib
import json
import sqlite3
import time
from contextlib import closing

import pytest
import zstandard

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


def rearm(store, tmp_path, **overrides):
    args = dict(
        prior_epoch=EPOCH,
        epoch="b" * 32,
        owner=store.owner_id,
        archive_directory=(tmp_path / "retired").resolve(),
        expires=int(time.time()) + 3600,
        deadline=time.monotonic() + 20,
        no_prior_capture=True,
    )
    args.update(overrides)
    return journal.rearm_before_initial_snapshot(store.db, **args)


def application_rows(store):
    return {
        name: [tuple(row) for row in store.db.execute(f"SELECT * FROM {name} ORDER BY 1")]
        for name in journal.TABLES
    }


def test_initial_rearm_archives_invalid_keys_preserves_gap_and_tracks_new_writes(store, tmp_path):
    install(store, max_keys=2)
    store.put_runtime("retained", {"history": True})
    store.put_runtime("renamed-before-snapshot", 1)
    with store.db:
        store.db.execute("UPDATE runtime SET key='renamed' WHERE key='renamed-before-snapshot'")
    store.put_runtime("after-journal-overflow", {"not_lost": True})
    assert store.db.execute(f"SELECT valid FROM {journal.META}").fetchone()[0] == 0
    before = application_rows(store)
    keys = [tuple(row) for row in store.db.execute(f"SELECT * FROM {journal.KEYS} ORDER BY 1,2")]
    result = rearm(store, tmp_path)
    assert result["state"] == "JOURNAL_REARMED_BEFORE_INITIAL_SNAPSHOT"
    assert result["application_rows_deleted"] == 0
    assert application_rows(store) == before
    directory = tmp_path / "retired"
    encoded = (directory / "journal.zst").read_bytes()
    with zstandard.ZstdDecompressor().stream_reader(encoded) as reader:
        raw = reader.read()
    lines = [json.loads(line) for line in raw.splitlines()]
    assert [tuple(row) for row in lines[1:-1]] == keys
    proof = json.loads((directory / "archive.json").read_text())
    assert proof["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert proof["archive_sha256"] == hashlib.sha256(encoded).hexdigest()
    assert proof["prior"]["valid"] == 0
    current = journal.state(store.db, "b" * 32, store.owner_id)
    assert current["last_seq"] == current["key_count"] == current["key_bytes"] == 0
    # Gap writes belong to the forthcoming full snapshot; only subsequent writes
    # belong to the new journal. Never reuse an earlier captured source point.
    store.put_runtime("post-snapshot", 2)
    with journal.changes(
        store.db, epoch="b" * 32, owner=store.owner_id, after=0, deadline=time.monotonic() + 10
    ) as (mark, changed):
        assert [r["key"] for r in changed] == ["post-snapshot"]
        assert mark["last_seq"] == 1


@pytest.mark.parametrize(
    "options",
    [
        {"no_prior_capture": False},
        {"epoch": EPOCH},
        {"prior_epoch": "c" * 32},
        {"owner": 1},
        {"expires": int(time.time()) + 100},
        {"deadline": float("inf")},
    ],
)
def test_rearm_refuses_unsafe_identity_or_tail_reuse(store, tmp_path, options):
    install(store)
    store.put_runtime("useful", 1)
    before = application_rows(store)
    with pytest.raises(StateError):
        rearm(store, tmp_path, **options)
    assert application_rows(store) == before
    assert journal.state(store.db, EPOCH, store.owner_id)["key_count"] == 1
    assert not store.db.in_transaction


@pytest.mark.parametrize("fault", ["archive_write", "archive_identity", "counters", "ddl"])
def test_rearm_failure_never_clears_old_keys_or_application_rows(store, tmp_path, monkeypatch, fault):
    from cryptowatcher import disk_backup, validation

    install(store)
    store.put_runtime("useful", {"history": True})
    if fault == "counters":
        with store.db:
            store.db.execute(f"UPDATE {journal.META} SET key_count=key_count+1")
    if fault == "ddl":
        with store.db:
            store.db.execute(
                "CREATE TRIGGER unknown AFTER DELETE ON _cw_catchup_keys BEGIN DELETE FROM runtime; END"
            )
    before = application_rows(store)
    old_keys = [tuple(r) for r in store.db.execute(f"SELECT * FROM {journal.KEYS}")]
    old_state = tuple(store.db.execute(f"SELECT * FROM {journal.META}").fetchone())
    if fault == "archive_write":

        def refused(*args):
            raise OSError("disk full")

        monkeypatch.setattr(disk_backup.ArchiveSink, "write", refused)
    if fault == "archive_identity":
        monkeypatch.setattr(validation, "fingerprint", lambda *args: ("0" * 64, 123))
    with pytest.raises((StateError, OSError)):
        rearm(store, tmp_path)
    assert application_rows(store) == before
    assert [tuple(r) for r in store.db.execute(f"SELECT * FROM {journal.KEYS}")] == old_keys
    assert tuple(store.db.execute(f"SELECT * FROM {journal.META}").fetchone()) == old_state
    assert not store.db.in_transaction


def test_rearm_deadline_after_key_mutation_rolls_back_before_commit(store, tmp_path, monkeypatch):
    from cryptowatcher import compressed_validation

    install(store)
    store.put_runtime("useful", 1)
    before = application_rows(store)

    def budget(deadline):
        if (
            store.db.in_transaction
            and store.db.execute(f"SELECT COUNT(*) FROM {journal.KEYS}").fetchone()[0] == 0
        ):
            raise StateError("time budget exhausted")

    monkeypatch.setattr(compressed_validation, "budget", budget)
    with pytest.raises(StateError, match="time budget"):
        rearm(store, tmp_path)
    assert application_rows(store) == before
    assert journal.state(store.db, EPOCH, store.owner_id)["key_count"] == 1
    assert not store.db.in_transaction
    store.put_runtime("after-failed-maintenance", 2)
    assert journal.state(store.db, EPOCH, store.owner_id)["key_count"] == 2
