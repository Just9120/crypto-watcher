import sqlite3
import sys
import time
from contextlib import closing

import apsw
import pytest

from cryptowatcher import compressed_validation as cv
from cryptowatcher.native_validation import NativeVFS, available

from .test_history_rebuild import closed_backup

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Native Linux validation helper")


@pytest.mark.parametrize("disk", [False, True])
def test_native_full_validation_preserves_all_types_and_rejects_writes_and_early_release(tmp_path, disk):
    assert available(), "Build canonical native helper before Linux tests"
    source, _ = closed_backup(tmp_path)
    original = source.read_bytes()
    image = cv.Image(
        source,
        limit=8 * 1024**2 if disk else cv.MAX_COMPRESSED_BYTES,
        deadline=time.monotonic() + 30,
        backing=tmp_path / "source.frames" if disk else None,
    )
    sentinel = tmp_path / "sentinel.sqlite3"
    sentinel.touch(mode=0o600)
    vfs = cv.frozen_vfs(image, sentinel)
    assert isinstance(vfs, NativeVFS)
    db = apsw.Connection(str(sentinel), flags=apsw.SQLITE_OPEN_READWRITE, vfs=vfs.name)
    try:
        db.execute("PRAGMA query_only=ON")
        cv.configure_cache(db, image)
        cv.full_check(db, image.deadline)
        assert not db.readonly("main")  # SQLite3.53 RO metadata skips CHECK verification.
        with closing(sqlite3.connect(source)) as raw:
            for table in ("owner_settings", "delivery_preferences", "mutes", "runtime", "legacy_import"):
                assert list(db.execute(f"SELECT * FROM {table} ORDER BY rowid")) == list(
                    raw.execute(f"SELECT * FROM {table} ORDER BY rowid")
                )
        with pytest.raises(cv.QualificationError, match="NATIVE_VFS_HANDLES_OPEN"):
            vfs.unregister()
        with pytest.raises(apsw.ReadOnlyError):
            db.execute("DELETE FROM runtime")
        stats = vfs.stats()
        assert stats["reads"] > 0
        assert stats["disk_prefetch"] is disk
        if disk:
            assert stats["prefetches"] > 0 and stats["prefetch_failures"] == 0
            assert stats["encoded_bytes"] > 0
    finally:
        db.close()
        vfs.unregister()
    assert source.read_bytes() == original
    assert image.reconstructed_hash() == image.sha256
    with pytest.raises(cv.QualificationError, match="NATIVE_VFS_CONTEXT_CLOSED"):
        vfs.stats()
    image.blocks.close()


@pytest.mark.parametrize("damage", ["check", "index"])
def test_native_full_check_rejects_constraint_or_index_corruption_without_changing_source(tmp_path, damage):
    assert available()
    source, _ = closed_backup(tmp_path)
    with closing(sqlite3.connect(source)) as db:
        if damage == "check":
            db.execute("PRAGMA ignore_check_constraints=ON")
            db.execute("UPDATE owner_settings SET revision=0")
            db.commit()
        else:
            page = db.execute(
                "SELECT rootpage FROM sqlite_master WHERE type='index' AND tbl_name='runtime'"
            ).fetchone()[0]
            size = db.execute("PRAGMA page_size").fetchone()[0]
    if damage == "index":
        with source.open("r+b") as output:
            output.seek((page - 1) * size)
            output.write(b"\xff")
    original = source.read_bytes()
    with (
        pytest.raises(cv.QualificationError, match="DATABASE_CORRUPT"),
        cv.classified_errors(time.monotonic() + 30),
    ):
        cv.validate_closed(source, directory=tmp_path, deadline=time.monotonic() + 30)
    assert source.read_bytes() == original


def test_native_closed_wal_header_and_corrupt_frame_controls(tmp_path):
    assert available()
    source, _ = closed_backup(tmp_path)
    with closing(sqlite3.connect(source)) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        db.execute("UPDATE owner_settings SET revision=revision")
        db.commit()
    original = source.read_bytes()
    assert original[18:20] == b"\x02\x02"
    result = cv.validate_closed(source, directory=tmp_path, deadline=time.monotonic() + 30)
    assert result["bytes"] == len(original) and source.read_bytes() == original
    image = cv.Image(source, limit=cv.MAX_COMPRESSED_BYTES, deadline=time.monotonic() + 30)
    image.blocks[0] = b"broken"
    sentinel = tmp_path / "broken.sqlite3"
    sentinel.touch(mode=0o600)
    vfs = cv.frozen_vfs(image, sentinel)
    try:
        with pytest.raises(apsw.IOError):
            with closing(
                apsw.Connection(str(sentinel), flags=apsw.SQLITE_OPEN_READWRITE, vfs=vfs.name)
            ) as db:
                cv.full_check(db, image.deadline)
    finally:
        vfs.unregister()


def test_native_frame_bounds_and_absolute_deadline_refuse(tmp_path):
    assert available()
    source, _ = closed_backup(tmp_path)
    image = cv.Image(source, limit=cv.MAX_COMPRESSED_BYTES, deadline=time.monotonic() + 30)
    image.blocks.offsets[0] = image.blocks.end + 1
    with pytest.raises(cv.QualificationError, match="NATIVE_VFS_REGISTRATION_REFUSED"):
        cv.frozen_vfs(image, tmp_path / "bounds.sqlite3")
    image = cv.Image(source, limit=cv.MAX_COMPRESSED_BYTES, deadline=time.monotonic() + 30)
    image.deadline = time.monotonic() + 0.5
    sentinel = tmp_path / "deadline.sqlite3"
    sentinel.touch(mode=0o600)
    vfs = cv.frozen_vfs(image, sentinel)
    try:
        with closing(apsw.Connection(str(sentinel), flags=apsw.SQLITE_OPEN_READWRITE, vfs=vfs.name)) as db:
            time.sleep(max(0, image.deadline - time.monotonic()) + 0.01)
            with pytest.raises(apsw.IOError):
                db.execute("SELECT count(*) FROM runtime").fetchone()
    finally:
        vfs.unregister()
