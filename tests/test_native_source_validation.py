"""Real parent-VFS locking, source immutability and bounded sequential read-ahead."""

import hashlib
import sqlite3
import time
from contextlib import closing

import apsw
import pytest

from cryptowatcher import native_validation
from cryptowatcher.compressed_validation import QualificationError
from cryptowatcher.source_validation import SourceVFS, open_source

pytestmark = pytest.mark.skipif(not native_validation.available(), reason="Built native helper required")


def prepare(store):
    store.db.execute("CREATE TABLE source_probe(seq INTEGER PRIMARY KEY,value BLOB NOT NULL)")
    expected = [(seq, bytes([seq % 251]) * 4096) for seq in range(1024)]
    with store.db:
        store.db.executemany("INSERT INTO source_probe VALUES(?,?)", expected)
    assert store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    return expected


def test_native_source_reads_every_original_row_with_fewer_parent_calls(store):
    expected = prepare(store)
    original = hashlib.sha256(store.path.read_bytes()).hexdigest()
    reader, vfs = open_source(store.path, time.monotonic() + 20)
    assert vfs is not None
    try:
        reader.execute("BEGIN")
        assert list(reader.execute("SELECT seq,value FROM source_probe ORDER BY seq")) == expected
        stats = vfs.stats()
        assert stats["sqlite_reads"] > 1024
        assert stats["parent_reads"] * 8 < stats["sqlite_reads"]
        assert stats["buffer_bytes"] == 1024 * 1024
        # SQLite rereads the header across opening/read-lock transitions. Every
        # real parent read still stays inside the one-window byte bound.
        assert 0 < stats["fetched_bytes"] <= stats["parent_reads"] * stats["buffer_bytes"]
    finally:
        reader.close()
        vfs.unregister()
    assert hashlib.sha256(store.path.read_bytes()).hexdigest() == original


def test_pinned_wal_snapshot_survives_writer_and_passive_checkpoint(store):
    expected = prepare(store)
    reader, vfs = open_source(store.path, time.monotonic() + 20)
    assert vfs is not None
    try:
        reader.execute("PRAGMA cache_size=1")
        reader.execute("BEGIN")
        assert reader.execute("SELECT value FROM source_probe WHERE seq=0").fetchone()[0] == expected[0][1]
        with store.db:
            store.db.execute("UPDATE source_probe SET value=? WHERE seq IN(100,900)", (b"new" * 1500,))
        store.db.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall()
        assert list(reader.execute("SELECT seq,value FROM source_probe ORDER BY seq")) == expected
        reader.close()
        assert store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        with closing(
            apsw.Connection(
                store.path.as_uri() + "?mode=ro",
                flags=apsw.SQLITE_OPEN_READONLY | apsw.SQLITE_OPEN_URI,
                vfs=vfs.name,
            )
        ) as fresh:
            assert list(fresh.execute("SELECT value FROM source_probe WHERE seq IN(100,900)")) == [
                (b"new" * 1500,),
                (b"new" * 1500,),
            ]
    finally:
        reader.close()
        vfs.unregister()


def test_autocommit_transactions_do_not_reuse_a_previous_main_file_window(store):
    expected = prepare(store)
    reader, vfs = open_source(store.path, time.monotonic() + 20)
    assert vfs is not None
    try:
        reader.execute("PRAGMA cache_size=1")
        assert reader.execute("SELECT value FROM source_probe WHERE seq=0").fetchone()[0] == expected[0][1]
        with store.db:
            store.db.execute("UPDATE source_probe SET value=? WHERE seq=100", (b"changed",))
        assert store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        assert reader.execute("SELECT value FROM source_probe WHERE seq=100").fetchone()[0] == b"changed"
    finally:
        reader.close()
        vfs.unregister()


def test_source_cannot_write_open_another_database_or_outlive_its_handle(store, tmp_path):
    prepare(store)
    original = store.path.read_bytes()
    vfs = SourceVFS(store.path, time.monotonic() + 20)
    reader = None
    other = tmp_path / "other.sqlite3"
    with closing(sqlite3.connect(other)) as db:
        db.execute("CREATE TABLE other(value INTEGER)")
        db.commit()
    other_before = other.read_bytes()
    try:
        with pytest.raises(apsw.CantOpenError):
            apsw.Connection(str(store.path), flags=apsw.SQLITE_OPEN_READWRITE, vfs=vfs.name)
        with pytest.raises(apsw.CantOpenError):
            apsw.Connection(str(other), flags=apsw.SQLITE_OPEN_READONLY, vfs=vfs.name)
        reader = apsw.Connection(str(store.path), flags=apsw.SQLITE_OPEN_READONLY, vfs=vfs.name)
        with pytest.raises(apsw.ReadOnlyError):
            reader.execute("CREATE TABLE forbidden(value INTEGER)")
        with pytest.raises(QualificationError, match="NATIVE_VFS_HANDLES_OPEN"):
            vfs.unregister()
        assert reader.execute("SELECT count(*) FROM source_probe").fetchone()[0] == 1024
    finally:
        if reader is not None:
            reader.close()
        vfs.unregister()
    assert store.path.read_bytes() == original and other.read_bytes() == other_before


def test_expired_source_read_fails_and_can_still_release_every_handle(store):
    prepare(store)
    reader, vfs = open_source(store.path, time.monotonic() + 0.2)
    assert vfs is not None
    try:
        reader.execute("PRAGMA cache_size=1")
        reader.execute("BEGIN")
        reader.execute("SELECT value FROM source_probe WHERE seq=0").fetchone()
        time.sleep(0.25)
        with pytest.raises(apsw.IOError):
            reader.execute("SELECT value FROM source_probe WHERE seq=900").fetchone()
    finally:
        reader.close()
        vfs.unregister()


def test_installed_broken_source_helper_never_falls_back(store, tmp_path, monkeypatch):
    broken = tmp_path / "compressed_vfs.so"
    broken.write_bytes(b"not a native library")
    monkeypatch.setattr(native_validation, "LIBRARY", broken)
    with pytest.raises(QualificationError, match="NATIVE_VFS_LOAD_FAILED"):
        open_source(store.path, time.monotonic() + 20)
