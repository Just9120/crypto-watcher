import hashlib
import sqlite3
import time
from contextlib import closing

import apsw
import pytest

from cryptowatcher import compressed_validation as cv


def test_disk_arena_partial_writes_and_byte_identity(tmp_path):
    backing = (tmp_path / "frames.bin").resolve()
    if not cv.disk_backing(backing):
        pytest.skip("Disk-backed arena requires a disk filesystem")
    image = cv.Image(None, limit=4 * 1024**2, deadline=time.monotonic() + 15, backing=backing)
    expected = bytearray(cv.BLOCK * 12 + 7)
    for offset, value in ((13, b"first"), (cv.BLOCK - 3, b"boundary"), (cv.BLOCK * 12, b"lastone")):
        image.write(value, offset)
        expected[offset : offset + len(value)] = value
    image.freeze()
    assert image.reconstructed_hash() == hashlib.sha256(expected).hexdigest()
    assert image.read(len(expected), 0) == bytes(expected)
    assert image.blocks.backing == backing
    with pytest.raises(apsw.ReadOnlyError):
        image.write(b"change", 0)
    image.blocks.close()
    with pytest.raises(FileExistsError):
        cv.Image(None, limit=1024, deadline=time.monotonic() + 5, backing=backing)


def test_disk_snapshot_full_validation_preserves_source_and_owner(tmp_path):
    directory = tmp_path.resolve()
    backing = directory / "snapshot.frames"
    if not cv.disk_backing(backing):
        pytest.skip("Disk-backed snapshot requires a disk filesystem")
    source = directory / "source.sqlite3"
    with closing(sqlite3.connect(source)) as db:
        db.execute("CREATE TABLE owner_settings(owner_id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO owner_settings VALUES(233382575)")
        db.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY, value INTEGER CHECK(value>0))")
        db.execute("INSERT INTO evidence VALUES(1,7)")
        db.commit()
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    with cv.snapshot_image(
        source, directory, deadline=time.monotonic() + 15, backing=backing, encoded_limit=4 * 1024**2
    ) as (db, image):
        assert list(db.execute("SELECT * FROM evidence")) == [(1, 7)]
        assert image.reconstructed_hash()
        assert db.execute("PRAGMA query_only").fetchone() == (1,)
        with pytest.raises(apsw.ReadOnlyError):
            db.execute("UPDATE evidence SET value=8")
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original


def test_disk_snapshot_keeps_full_check_constraints_gate(tmp_path):
    directory = tmp_path.resolve()
    backing = directory / "damaged.frames"
    if not cv.disk_backing(backing):
        pytest.skip("Disk-backed snapshot requires a disk filesystem")
    source = directory / "damaged.sqlite3"
    with closing(sqlite3.connect(source)) as db:
        db.executescript(
            "CREATE TABLE owner_settings(owner_id INTEGER); INSERT INTO owner_settings VALUES(233382575); CREATE TABLE t(x INTEGER CHECK(x>0)); PRAGMA ignore_check_constraints=ON; INSERT INTO t VALUES(-1);"
        )
        db.commit()
    with pytest.raises(cv.QualificationError, match="DATABASE_CORRUPT"):
        with cv.snapshot_image(
            source, directory, deadline=time.monotonic() + 15, backing=backing, encoded_limit=4 * 1024**2
        ):
            pytest.fail("Corrupt CHECK constraint must not reach the consumer")


def test_disk_arena_bounds_and_unknown_filesystem_refuse(tmp_path, monkeypatch):
    backing = (tmp_path / "refused.frames").resolve()
    monkeypatch.setattr(cv, "disk_backing", lambda path: False)
    with pytest.raises(cv.QualificationError, match="DISK_BACKING_REQUIRED"):
        cv.Image(None, limit=1024, deadline=time.monotonic() + 5, backing=backing)
    assert not backing.exists()


def test_anonymous_default_cannot_expand_its_memory_limit(tmp_path):
    with pytest.raises(cv.QualificationError, match="DEFAULT_MEMORY_LIMIT_REQUIRED"):
        with cv.snapshot_image(
            tmp_path / "unused.sqlite3",
            tmp_path,
            deadline=time.monotonic() + 5,
            encoded_limit=cv.MAX_COMPRESSED_BYTES + 1,
        ):
            pytest.fail("The default memory gate remains unchanged")


def test_disk_arena_refuses_insufficient_space_before_creation(tmp_path, monkeypatch):
    from types import SimpleNamespace

    backing = (tmp_path / "no-space.frames").resolve()
    monkeypatch.setattr(cv, "disk_backing", lambda path: True)
    monkeypatch.setattr(cv.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    with pytest.raises(cv.QualificationError, match="DISK_RESERVE_EXHAUSTED"):
        cv.Image(None, limit=1024, deadline=time.monotonic() + 5, backing=backing)
    assert not backing.exists()


def test_deadline_diagnostic_never_claims_completed_corruption_scan(monkeypatch):
    now = [0.0]

    class Database:
        def set_progress_handler(self, *args):
            pass

        def execute(self, sql):
            if sql == "PRAGMA integrity_check":
                now[0] = 6.0
                return [("bounded VFS read stopped",)]
            return []

    monkeypatch.setattr(cv.time, "monotonic", lambda: now[0])
    with pytest.raises(cv.QualificationError) as error:
        cv.full_check(Database(), 5.0)
    assert error.value.code == "TIME_BUDGET_EXCEEDED"
