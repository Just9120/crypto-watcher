import hashlib
import os
import random
import sqlite3
import time
import zlib
from contextlib import closing
from pathlib import Path

import apsw
import pytest

from cryptowatcher.compressed_validation import (
    Image,
    QualificationError,
    classified_errors,
    snapshot,
    validate_closed,
)


def validate(path, *, limit, deadline, progress=lambda **kw: None):
    with classified_errors(deadline):
        result = validate_closed(
            path, directory=path.parent, limit=limit, deadline=deadline, progress=progress
        )
    return {**result, "state": "PASS"}


def validate_live(path, *, limit, deadline, directory, progress=lambda **kw: None):
    from cryptowatcher import compressed_validation as c

    original = c.full_check

    def checked(db, deadline):
        progress(phase="FULL_INTEGRITY")
        original(db, deadline)

    c.full_check = checked
    try:
        progress(phase="SNAPSHOT")
        with snapshot(path, directory, deadline=deadline) as db:
            return {
                "state": "PASS",
                "revision": db.execute("SELECT revision FROM owner_settings").fetchone()[0],
            }
    finally:
        c.full_check = original


@pytest.fixture
def database(tmp_path):
    p = tmp_path / "db.sqlite3"
    with closing(sqlite3.connect(p)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(
            "CREATE TABLE checked(id INTEGER PRIMARY KEY, key TEXT UNIQUE, payload TEXT CHECK(length(payload)>0))"
        )
        db.executemany(
            "INSERT INTO checked VALUES (?,?,?)",
            [(i, str(3000 - i), "data-" + str(i) * 100) for i in range(3000)],
        )
        db.commit()
    assert not Path(str(p) + "-wal").exists()
    return p


def test_exact_full_validation_of_closed_wal_file(database):
    original = database.read_bytes()
    r = validate(database, limit=10 * 1024 * 1024, deadline=time.monotonic() + 30)
    assert r["state"] == "PASS" and r["sha256"] == hashlib.sha256(original).hexdigest()
    assert database.read_bytes() == original and r["compressed_bytes"] < len(original)


def test_all_slice_reads_match_exact_source(database):
    data = database.read_bytes()
    image = Image(database, limit=10 * 1024 * 1024, deadline=time.monotonic() + 30)
    random.seed(5)
    for i in range(1000):
        a = random.randrange(len(data) + 1000)
        n = random.randrange(8000)
        assert image.read(n, a) == data[a : a + n]


def test_constraints_rejected(database):
    with closing(sqlite3.connect(database)) as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute("INSERT INTO checked VALUES(4000,'invalid','')")
        db.commit()
    before = database.read_bytes()
    with pytest.raises(QualificationError, match="DATABASE_CORRUPT"):
        validate(database, limit=10 * 1024 * 1024, deadline=time.monotonic() + 30)
    assert database.read_bytes() == before


def test_corrupt_header_rejected(database):
    with database.open("r+b") as f:
        f.write(b"corrupt database")
    with pytest.raises(QualificationError, match="DATABASE_CORRUPT"):
        validate(database, limit=10 * 1024 * 1024, deadline=time.monotonic() + 30)


def test_index_corruption_rejected(database):
    with closing(sqlite3.connect(database)) as db:
        page = db.execute("SELECT rootpage FROM sqlite_master WHERE type='index'").fetchone()[0]
        size = db.execute("PRAGMA page_size").fetchone()[0]
    with database.open("r+b") as f:
        f.seek((page - 1) * size)
        f.write(b"\xff")
    with pytest.raises(QualificationError, match="DATABASE_CORRUPT"):
        validate(database, limit=10 * 1024 * 1024, deadline=time.monotonic() + 30)


def test_memory_and_deadline_refused(database):
    before = database.read_bytes()
    with pytest.raises(QualificationError, match="COMPRESSED_MEMORY_LIMIT"):
        validate(database, limit=1024, deadline=time.monotonic() + 30)
    with pytest.raises(QualificationError, match="TIME_BUDGET_EXCEEDED"):
        validate(database, limit=10 * 1024 * 1024, deadline=0)
    assert database.read_bytes() == before


def test_sidecar_refused(database):
    Path(str(database) + "-wal").write_bytes(b"preserved")
    with pytest.raises(QualificationError, match="CLOSED_FILE_REQUIRED"):
        validate(database, limit=10 * 1024 * 1024, deadline=time.monotonic() + 30)


def test_changed_original_rejected(database):
    def change(**fields):
        if fields["phase"] == "BYTE_IDENTITY":
            with database.open("ab") as f:
                f.write(b"change")

    with pytest.raises(QualificationError, match="SOURCE_CHANGED|BYTE_IDENTITY_MISMATCH"):
        validate(database, limit=10 * 1024 * 1024, deadline=time.monotonic() + 30, progress=change)


def test_compacted_cache_does_not_hide_modified_pages(database):
    image = Image(database, limit=10 * 1024 * 1024, deadline=time.monotonic() + 30)
    image.cache.clear()
    image.blocks[3] = b"broken"
    with pytest.raises(zlib.error):
        image.reconstructed_hash()


def test_live_wal_snapshot_frozen_and_reader_released(database, monkeypatch):
    from cryptowatcher import compressed_validation as c

    with closing(sqlite3.connect(database)) as live:
        live.execute("CREATE TABLE owner_settings(owner_id INTEGER,revision INTEGER)")
        live.execute("INSERT INTO owner_settings VALUES(123,1)")
        live.commit()
        original = c.full_check

        def checked(db, deadline):
            live.execute("UPDATE owner_settings SET revision=2")
            live.commit()
            assert live.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
            original(db, deadline)

        monkeypatch.setattr(c, "full_check", checked)
        with snapshot(database, database.parent, deadline=time.monotonic() + 30) as db:
            assert db.execute("SELECT revision FROM owner_settings").fetchone()[0] == 1
        assert live.execute("SELECT revision FROM owner_settings").fetchone()[0] == 2


def test_live_constraint_corruption_rejected(database):
    with closing(sqlite3.connect(database)) as db:
        db.execute("CREATE TABLE owner_settings(owner_id INTEGER,revision INTEGER)")
        db.execute("INSERT INTO owner_settings VALUES(123,1)")
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute("INSERT INTO checked VALUES(4000,'invalid','')")
        db.commit()
    with pytest.raises(QualificationError, match="DATABASE_CORRUPT"):
        validate_live(
            database, limit=10 * 1024 * 1024, deadline=time.monotonic() + 30, directory=database.parent
        )


def test_incompressible_snapshot_limit_preserves_live_data_and_releases_reader(database, monkeypatch):
    from cryptowatcher import compressed_validation as c

    with closing(sqlite3.connect(database)) as db:
        db.execute("CREATE TABLE owner_settings(owner_id INTEGER,revision INTEGER)")
        db.execute("INSERT INTO owner_settings VALUES(123,1)")
        db.execute("CREATE TABLE random_bytes(payload BLOB)")
        db.execute("INSERT INTO random_bytes VALUES(?)", (os.urandom(200_000),))
        db.commit()
        monkeypatch.setattr(c, "MAX_COMPRESSED_BYTES", 40_000)
        with pytest.raises(QualificationError, match="COMPRESSED_MEMORY_LIMIT"):
            with snapshot(database, database.parent, deadline=time.monotonic() + 30):
                pytest.fail("Oversized compressed snapshot yielded")
        assert db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        assert db.execute("SELECT length(payload) FROM random_bytes").fetchone()[0] == 200_000
    assert not list(database.parent.glob("cw-cvfs-*"))


def test_snapshot_failure_after_copy_cleans_scratch_and_preserves_source(database, monkeypatch):
    from cryptowatcher import compressed_validation as c

    with closing(sqlite3.connect(database)) as db:
        db.execute("CREATE TABLE owner_settings(owner_id INTEGER,revision INTEGER)")
        db.execute("INSERT INTO owner_settings VALUES(123,1)")
        db.commit()
    original = database.read_bytes()

    def fail(db, deadline):
        raise QualificationError("TIME_BUDGET_EXCEEDED")

    monkeypatch.setattr(c, "full_check", fail)
    with pytest.raises(QualificationError, match="TIME_BUDGET_EXCEEDED"):
        with snapshot(database, database.parent, deadline=time.monotonic() + 30):
            pytest.fail("Failed snapshot yielded")
    assert database.read_bytes() == original and not list(database.parent.glob("cw-cvfs-*"))


def test_metadata_connection_cannot_modify_validated_snapshot(database):
    with closing(sqlite3.connect(database)) as db:
        db.execute("CREATE TABLE owner_settings(owner_id INTEGER,revision INTEGER)")
        db.execute("INSERT INTO owner_settings VALUES(123,1)")
        db.commit()
    original = database.read_bytes()
    with snapshot(database, database.parent, deadline=time.monotonic() + 30) as db:
        with pytest.raises(apsw.ReadOnlyError):
            db.execute("UPDATE owner_settings SET revision=99")
    assert database.read_bytes() == original


def test_compressed_backup_metrics_hash_and_native_sqlite_restore(store, tmp_path, monkeypatch):
    from cryptowatcher import validation
    from cryptowatcher.storage import Store

    monkeypatch.setattr(validation, "scratch_space", lambda *args: None)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    destination = tmp_path / "backup.sqlite3"
    store.put_runtime("evidence", {"source": "preserved", "revision": 1})
    proof = store.backup(destination, validation_directory=scratch, compressed_validation=True)
    assert proof["validation_mode"] == "COMPRESSED_RAM" and proof["phase"] == "COMPLETE"
    assert proof["bytes"] == destination.stat().st_size
    assert proof["sha256"] == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert proof["sqlite_version"] == apsw.sqlitelibversion()
    restored = Store(destination, store.owner_id, readonly=True)
    try:
        assert restored.snapshot() == store.snapshot()
        assert restored.get_runtime("evidence", {}) == {"source": "preserved", "revision": 1}
    finally:
        restored.close()
    assert list(scratch.iterdir()) == []
