import hashlib
import io
import json
import sqlite3
import time
from contextlib import closing

import pytest
import zstandard

from cryptowatcher import change_journal, journal_archive, online_pages
from cryptowatcher.storage import StateError

EPOCH = "b" * 32


def setup(store):
    change_journal.install(
        store.db,
        epoch=EPOCH,
        owner=store.owner_id,
        max_keys=10000,
        max_bytes=1024**2,
        expires=int(time.time()) + 3600,
    )
    store.put_runtime("old", {"useful": 1})
    store.put_runtime("unchanged", {"useful": 2})


def execute(store, tmp_path):
    with closing(sqlite3.connect(store.path, timeout=1)) as db:
        db.execute("PRAGMA synchronous=FULL")
        return journal_archive.archive_and_ack(
            db, (tmp_path / "archive").resolve(), epoch=EPOCH, owner=store.owner_id
        )


def test_durable_archive_precedes_ack_and_preserves_application_values(store, tmp_path):
    setup(store)
    before = [tuple(r) for r in store.db.execute("SELECT * FROM runtime ORDER BY key")]
    state = change_journal.state(store.db, EPOCH, store.owner_id)
    result = execute(store, tmp_path)
    assert result["state"] == "ARCHIVED_ACKNOWLEDGED_PREFIX" and result["application_rows_deleted"] == 0
    assert [tuple(r) for r in store.db.execute("SELECT * FROM runtime ORDER BY key")] == before
    current = change_journal.state(store.db, EPOCH, store.owner_id)
    assert current["last_seq"] == state["last_seq"] and current["key_count"] == current["key_bytes"] == 0
    for field in ("epoch", "expires", "max_keys", "max_bytes"):
        assert current[field] == state[field]
    encoded = (tmp_path / "archive/tail.zst").read_bytes()
    with zstandard.ZstdDecompressor().stream_reader(encoded) as reader:
        raw = reader.read()
    assert hashlib.sha256(encoded).hexdigest() == result["archive_sha256"]
    assert hashlib.sha256(raw).hexdigest() == result["raw_sha256"]
    entries = [json.loads(x) for x in raw.splitlines()]
    assert {r["key"] for r in entries[1:-1]} == {"old", "unchanged"}


def test_concurrent_updates_and_new_keys_are_not_acknowledged(store, tmp_path, monkeypatch):
    setup(store)
    original = journal_archive.ArchiveSink.write
    changed = False

    def write(sink, data):
        nonlocal changed
        if not changed:
            store.put_runtime("old", {"later": True})
            store.put_runtime("new", 3)
            changed = True
        return original(sink, data)

    monkeypatch.setattr(journal_archive.ArchiveSink, "write", write)
    result = execute(store, tmp_path)
    current = change_journal.state(store.db, EPOCH, store.owner_id)
    assert current["last_seq"] > result["through"] and current["key_count"] == 2
    with change_journal.changes(
        store.db, epoch=EPOCH, owner=store.owner_id, after=result["through"], deadline=time.monotonic() + 10
    ) as (_, rows):
        assert {r["key"] for r in rows} == {"old", "new"}
    assert store.get_runtime("old") == {"later": True} and store.get_runtime("new") == 3


def test_archive_failure_does_not_acknowledge_a_key(store, tmp_path, monkeypatch):
    setup(store)

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(journal_archive.ArchiveSink, "write", fail)
    with pytest.raises(OSError):
        execute(store, tmp_path)
    assert change_journal.state(store.db, EPOCH, store.owner_id)["key_count"] == 2
    assert store.get_runtime("old") == {"useful": 1}


def test_corrupt_counter_is_refused_before_ack(store, tmp_path):
    setup(store)
    with store.db:
        store.db.execute("UPDATE _cw_catchup_state SET key_count=999")
    with pytest.raises(StateError, match="counters"):
        execute(store, tmp_path)
    assert store.db.execute("SELECT COUNT(*) FROM _cw_catchup_keys").fetchone()[0] == 2


def test_initial_key_archive_requires_proof_and_fresh_snapshot_retains_all_values(store, tmp_path):
    setup(store)
    base = (tmp_path / "base.sqlite3").resolve()
    store.backup(base)
    index = (tmp_path / "index").resolve()
    online_pages.make_base(base, index, expected_sha256=hashlib.sha256(base.read_bytes()).hexdigest())
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("PRAGMA synchronous=FULL")
        with pytest.raises(StateError, match="initial source point"):
            journal_archive.archive_and_ack(
                db,
                (tmp_path / "refused").resolve(),
                epoch=EPOCH,
                owner=store.owner_id,
                key_only_before_first_snapshot=True,
            )
        proof = journal_archive.archive_and_ack(
            db,
            (tmp_path / "initial").resolve(),
            epoch=EPOCH,
            owner=store.owner_id,
            key_only_before_first_snapshot=True,
            no_prior_capture=True,
        )
    assert proof["state"] == "PREPARED_UNCONSUMED_JOURNAL_PREFIX"
    assert proof["requires_fresh_full_source_snapshot"] and proof["application_rows_deleted"] == 0
    assert store.get_runtime("old") == {"useful": 1} and store.get_runtime("unchanged") == {"useful": 2}
    store.put_runtime("after-preparation", 3)
    output = (tmp_path / "output").resolve()
    output.mkdir(mode=0o700)
    stream = io.BytesIO()
    result = online_pages.capture(
        store.path.resolve(),
        index / "base.json",
        output,
        stream,
        expected_baseline_sha256=hashlib.sha256((index / "base.json").read_bytes()).hexdigest(),
        epoch=EPOCH,
        owner=store.owner_id,
        maximum=1024**2,
    )
    (output / "pages.delta").write_bytes(stream.getvalue())
    work = (tmp_path / "working.sqlite3").resolve()
    work.write_bytes(base.read_bytes())
    online_pages.apply_to_working_copy(
        work,
        output / "snapshot.json",
        expected_descriptor_sha256=hashlib.sha256((output / "snapshot.json").read_bytes()).hexdigest(),
    )
    with closing(sqlite3.connect(work)) as db:
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert {row[0] for row in db.execute("SELECT key FROM runtime")} == {
            "old",
            "unchanged",
            "after-preparation",
        }
    assert result["journal_watermark"] > proof["through"]
