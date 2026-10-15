import random
import sqlite3
import sys
import time
from array import array
from contextlib import closing

import apsw
import pytest
import zstandard

from cryptowatcher import compressed_validation as cv
from cryptowatcher.native_validation import NativeVFS, available

from .test_history_rebuild import closed_backup

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Native Linux validation helper")


def page_frames(raw):
    data = bytearray()
    pages, offsets, lengths = array("Q"), array("Q"), array("Q")
    compressor = zstandard.ZstdCompressor(level=1, write_checksum=True)
    for start in range(0, len(raw), 4096):
        encoded = compressor.compress(raw[start : start + 4096])
        pages.append(start // 4096)
        offsets.append(len(data))
        lengths.append(len(encoded))
        data.extend(encoded)
    return data, pages, offsets, lengths


def test_auxiliary_pages_preserve_source_and_full_sqlite_checks(tmp_path):
    source, _ = closed_backup(tmp_path)
    original = source.read_bytes()
    data, pages, offsets, lengths = page_frames(original)
    image = cv.Image(source, limit=cv.MAX_COMPRESSED_BYTES, deadline=time.monotonic() + 30)
    sentinel = tmp_path / "pinned.sqlite3"
    sentinel.touch(mode=0o600)
    vfs = cv.frozen_vfs(image, sentinel)
    try:
        pages, offsets, lengths = (a[::2] for a in (pages, offsets, lengths))
        vfs.pin_pages(data, len(data), pages, offsets, lengths)
        with pytest.raises(BufferError):
            data.extend(b"must stay pinned")
        file = apsw.VFSFile(
            vfs.name, str(sentinel), [apsw.SQLITE_OPEN_READWRITE | apsw.SQLITE_OPEN_MAIN_DB, 0]
        )
        try:
            assert file.xRead(8192, 4090) == original[4090:12282]
        finally:
            file.xClose()
        with closing(apsw.Connection(str(sentinel), flags=apsw.SQLITE_OPEN_READWRITE, vfs=vfs.name)) as db:
            db.execute("PRAGMA query_only=ON")
            cv.full_check(db, image.deadline)
            with closing(sqlite3.connect(source)) as raw:
                assert list(db.execute("SELECT * FROM runtime ORDER BY rowid")) == list(
                    raw.execute("SELECT * FROM runtime ORDER BY rowid")
                )
            with pytest.raises(cv.QualificationError, match="NATIVE_PAGE_CACHE_REFUSED"):
                vfs.pin_pages(data, len(data), pages, offsets, lengths)
            with pytest.raises(apsw.ReadOnlyError):
                db.execute("DELETE FROM runtime")
        stats = vfs.stats()
        assert stats["pinned_pages"] == len(pages) and stats["pinned_reads"] > 0
    finally:
        vfs.unregister()
        image.blocks.close()
    assert source.read_bytes() == original
    data.extend(b"released")


@pytest.mark.parametrize(
    "fault", ["different_page", "checksum", "trailer", "duplicate", "bounds", "open_handle"]
)
def test_auxiliary_cache_refusal_leaves_original_view_intact(tmp_path, fault):
    source, _ = closed_backup(tmp_path)
    original = source.read_bytes()
    data, pages, offsets, lengths = page_frames(original)
    if fault == "different_page":
        changed = bytearray(original)
        changed[200] ^= 1
        data, pages, offsets, lengths = page_frames(changed)
    elif fault == "checksum":
        data[lengths[0] - 1] ^= 1
    elif fault == "trailer":
        lengths[0] += 1
    elif fault == "duplicate":
        pages[1] = pages[0]
    elif fault == "bounds":
        offsets[0] = len(data)
    image = cv.Image(source, limit=cv.MAX_COMPRESSED_BYTES, deadline=time.monotonic() + 30)
    sentinel = tmp_path / "refused.sqlite3"
    sentinel.touch(mode=0o600)
    vfs = cv.frozen_vfs(image, sentinel)
    db = (
        apsw.Connection(str(sentinel), flags=apsw.SQLITE_OPEN_READWRITE, vfs=vfs.name)
        if fault == "open_handle"
        else None
    )
    try:
        with pytest.raises(cv.QualificationError, match="NATIVE_PAGE_CACHE_REFUSED"):
            vfs.pin_pages(data, len(data), pages, offsets, lengths)
        assert vfs.stats()["pinned_pages"] == 0
        if db is not None:
            db.close()
            db = None
        with closing(apsw.Connection(str(sentinel), flags=apsw.SQLITE_OPEN_READWRITE, vfs=vfs.name)) as db:
            db.execute("PRAGMA query_only=ON")
            cv.full_check(db, image.deadline)
    finally:
        if db is not None:
            db.close()
        vfs.unregister()
        image.blocks.close()
    assert source.read_bytes() == original


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


@pytest.mark.parametrize("order", [(0, 2, 1, 3), (7, 5, 6, 4, 3)])
def test_interleaved_disk_reads_coalesce_without_changing_bytes(tmp_path, order):
    source = tmp_path / "interleaved.bin"
    generator = random.Random(43015)
    original = b"".join(generator.randbytes(8192) + b"x" * (cv.BLOCK - 8192) for _ in range(8))
    source.write_bytes(original)
    image = cv.Image(
        source,
        limit=1024**2,
        deadline=time.monotonic() + 10,
        backing=tmp_path / "interleaved.frames",
    )
    sentinel = tmp_path / "interleaved.sqlite3"
    sentinel.touch(mode=0o600)
    vfs = cv.frozen_vfs(image, sentinel)
    file = apsw.VFSFile(vfs.name, str(sentinel), [apsw.SQLITE_OPEN_READWRITE | apsw.SQLITE_OPEN_MAIN_DB, 0])
    try:
        for block in order:
            offset = block * cv.BLOCK
            assert file.xRead(4096, offset) == original[offset : offset + 4096]
        stats = vfs.stats()
        assert stats["disk_prefetch"] and stats["wide_prefetches"] > 0
        assert stats["covered_prefetches"] > 0 and stats["prefetch_failures"] == 0
    finally:
        file.xClose()
        vfs.unregister()
        image.blocks.close()
    assert source.read_bytes() == original


@pytest.mark.parametrize("damage", ["check", "index"])
@pytest.mark.parametrize("pinned", [False, True])
def test_native_full_check_rejects_constraint_or_index_corruption_without_changing_source(
    tmp_path, damage, pinned
):
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
    if pinned:
        image = cv.Image(source, limit=cv.MAX_COMPRESSED_BYTES, deadline=time.monotonic() + 30)
        sentinel = tmp_path / "corrupt-pinned.sqlite3"
        sentinel.touch(mode=0o600)
        vfs = cv.frozen_vfs(image, sentinel)
        data, pages, offsets, lengths = page_frames(original)
        try:
            vfs.pin_pages(data, len(data), pages, offsets, lengths)
            with (
                closing(apsw.Connection(str(sentinel), flags=apsw.SQLITE_OPEN_READWRITE, vfs=vfs.name)) as db,
                pytest.raises(cv.QualificationError, match="DATABASE_CORRUPT"),
                cv.classified_errors(image.deadline),
            ):
                db.execute("PRAGMA query_only=ON")
                cv.full_check(db, image.deadline)
        finally:
            vfs.unregister()
            image.blocks.close()
    else:
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
