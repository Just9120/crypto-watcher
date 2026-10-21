"""A disk frame index never changes standard ZSTD restoration or resource bounds."""

import hashlib
import io
import time
from types import SimpleNamespace

import pytest
import zstandard

from cryptowatcher.compressed_validation import QualificationError
from cryptowatcher.validation_archive import BLOCK, ArchiveArena


def arena(path, *, raw=3 * BLOCK):
    return ArchiveArena(path, max_raw_bytes=raw, max_archive_bytes=8 * BLOCK, deadline=time.monotonic() + 20)


def test_reserved_header_delayed_frames_last_partial_standard_restore_and_reopen(tmp_path):
    path = tmp_path / "snapshot.zst"
    compressor = zstandard.ZstdCompressor(write_checksum=True)
    raw = [b"old header" + bytes(BLOCK - 10), b"body" * (BLOCK // 4), b"last partial"]
    target = arena(path)
    try:
        for _ in raw:
            target.append(b"")
        target[0] = compressor.compress(raw[0])
        target[1] = compressor.compress(raw[1])
        target[2] = compressor.compress(raw[2])
        raw[0] = b"new header" + bytes(BLOCK - 10)
        target[0] = compressor.compress(raw[0])
        joined = b"".join(raw)
        digest = hashlib.sha256(joined).hexdigest()
        target.finish(len(joined), digest)
        assert target.sealed and target.data is not None
        with pytest.raises(QualificationError, match="INVALID_ARCHIVE_WRITE"):
            target[0] = b"overwrite"
    finally:
        target.close()
    original = path.read_bytes()
    restored = io.BytesIO()
    zstandard.ZstdDecompressor().copy_stream(io.BytesIO(original), restored)
    assert restored.getvalue() == joined
    reopened = ArchiveArena.open(
        path, max_raw_bytes=len(joined), max_archive_bytes=8 * BLOCK, deadline=time.monotonic() + 20
    )
    try:
        assert b"".join(zstandard.ZstdDecompressor().decompress(value) for value in reopened) == joined
        assert reopened.raw_sha256 == digest
        assert reopened.raw_bytes == len(joined)
        from cryptowatcher.compressed_validation import Image

        image = Image.from_archive_store(reopened)
        assert image.reconstructed_hash() == digest and not image.mutable
        reopened.assert_unchanged()
    finally:
        reopened.close()
    assert path.read_bytes() == original


def test_framed_tail_cannot_hide_extra_standard_restore_bytes(tmp_path):
    from cryptowatcher.compressed_validation import Image

    path = tmp_path / "padded-tail.zst"
    target = arena(path, raw=BLOCK)
    try:
        target.append(zstandard.ZstdCompressor().compress(bytes(BLOCK)))
        # A forged index claims one byte although a standard restore emits64KiB.
        target.finish(1, hashlib.sha256(bytes(1)).hexdigest())
    finally:
        target.close()
    reopened = ArchiveArena.open(
        path, max_raw_bytes=BLOCK, max_archive_bytes=8 * BLOCK, deadline=time.monotonic() + 20
    )
    try:
        image = Image.from_archive_store(reopened)
        with pytest.raises(QualificationError, match="BYTE_IDENTITY_MISMATCH"):
            image.reconstructed_hash()
        with pytest.raises(QualificationError, match="BYTE_IDENTITY_MISMATCH"):
            image.read(1, 0)
    finally:
        reopened.close()


def test_independent_page_packets_consume_every_frame_and_refuse_hidden_tail(tmp_path):
    from cryptowatcher.compressed_validation import Image

    target = arena(tmp_path / "pages.zst", raw=BLOCK)
    image = Image(None, limit=target.limit, deadline=target.deadline, archive_store=target)
    raw = b"".join(bytes([page]) * 4096 for page in range(16))
    try:
        image.set_block(0, raw)
        packet = target[0]
        assert zstandard.frame_content_size(packet) == 4096
        assert image.unpack_block(packet) == raw
        with pytest.raises(QualificationError, match="BYTE_IDENTITY_MISMATCH"):
            image.unpack_block(packet + image.compressor.compress(b"hidden"))
        damaged = packet[:-1] + bytes([packet[-1] ^ 1])
        with pytest.raises(zstandard.ZstdError):
            image.unpack_block(damaged)
    finally:
        target.close()


def test_page_decoder_refuses_an_oversized_stream_window(tmp_path):
    from cryptowatcher.compressed_validation import Image

    target = arena(tmp_path / "bounded-window.zst", raw=BLOCK)
    image = Image(None, limit=target.limit, deadline=target.deadline, archive_store=target)
    oversized = zstandard.ZstdCompressor(write_content_size=False).compress(bytes(2 * BLOCK))
    assert zstandard.get_frame_parameters(oversized).window_size > BLOCK
    try:
        with pytest.raises(zstandard.ZstdError):
            image.unpack_block(oversized)
    finally:
        target.close()


def test_nonsequential_change_refuses_before_write_and_identical_retry_is_safe(tmp_path):
    target = arena(tmp_path / "snapshot.zst")
    try:
        for _ in range(3):
            target.append(b"")
        target[0], target[1], target[2] = b"header", b"one", b"two"
        original = target.path.read_bytes()
        target[1] = b"one"
        with pytest.raises(QualificationError, match="NONSEQUENTIAL"):
            target[1] = b"changed"
        assert target.path.read_bytes() == original
    finally:
        target.close()


def test_last_frame_rewrite_and_truncation_leave_no_unindexed_tail(tmp_path):
    target = arena(tmp_path / "snapshot.zst")
    try:
        for _ in range(3):
            target.append(b"")
        target[0], target[1], target[2] = b"header", b"one", b"much longer"
        target[2] = b"two"
        assert target.path.stat().st_size == target.end
        assert target.pop() == b"two"
        assert target.path.stat().st_size == target.end == target.offsets[1] + target.lengths[1]
        target[1] = b"longer one"
        assert target.path.stat().st_size == target.end
    finally:
        target.close()


@pytest.mark.parametrize("change", ["checksum", "truncated", "marker"])
def test_incomplete_or_changed_footer_never_reopens(tmp_path, change):
    path = tmp_path / "snapshot.zst"
    target = arena(path, raw=BLOCK)
    value = bytes(BLOCK)
    try:
        target.append(zstandard.ZstdCompressor().compress(value))
        target.finish(BLOCK, hashlib.sha256(value).hexdigest())
    finally:
        target.close()
    data = bytearray(path.read_bytes())
    if change == "checksum":
        data[-25] ^= 1
    elif change == "marker":
        data[-1] ^= 1
    else:
        del data[-8:]
    path.write_bytes(data)
    with pytest.raises(QualificationError, match="INVALID_ARCHIVE_INDEX"):
        ArchiveArena.open(
            path, max_raw_bytes=BLOCK, max_archive_bytes=8 * BLOCK, deadline=time.monotonic() + 20
        )


def test_disk_reserve_deadline_and_capacity_refuse_without_touching_source(tmp_path, monkeypatch):
    from cryptowatcher import validation_archive as module

    path = tmp_path / "not-created.zst"
    monkeypatch.setattr(module.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    with pytest.raises(QualificationError, match="ARCHIVE_DISK_RESERVE"):
        arena(path)
    assert not path.exists()
    monkeypatch.setattr(module.shutil, "disk_usage", lambda path: SimpleNamespace(free=10**12))
    with pytest.raises(QualificationError, match="TIME_BUDGET"):
        ArchiveArena(path, max_raw_bytes=BLOCK, max_archive_bytes=8 * BLOCK, deadline=time.monotonic() - 1)
    assert not path.exists()
    with pytest.raises(QualificationError, match="ARCHIVE_SIZE_LIMIT"):
        ArchiveArena(
            path, max_raw_bytes=BLOCK, max_archive_bytes=2 * BLOCK + 1, deadline=time.monotonic() + 20
        )
    assert not path.exists()


def test_later_file_mutation_invalidates_open_readback(tmp_path):
    path = tmp_path / "snapshot.zst"
    target = arena(path, raw=BLOCK)
    value = bytes(BLOCK)
    try:
        target.append(zstandard.ZstdCompressor().compress(value))
        target.finish(BLOCK, hashlib.sha256(value).hexdigest())
    finally:
        target.close()
    reopened = ArchiveArena.open(
        path, max_raw_bytes=BLOCK, max_archive_bytes=8 * BLOCK, deadline=time.monotonic() + 20
    )
    try:
        with path.open("ab") as changed:
            changed.write(b"x")
        with pytest.raises(QualificationError, match="SOURCE_CHANGED"):
            reopened.assert_unchanged()
    finally:
        reopened.close()


def test_actual_live_sqlite_snapshot_is_standard_archive_with_exact_rows_and_private(store, tmp_path):
    import sqlite3
    from contextlib import closing

    import apsw

    from cryptowatcher.compressed_validation import snapshot_image
    from cryptowatcher.reset_history import private_state, signature

    store.put_runtime("test-retained-source", {"value": "original", "rows": list(range(100))})
    original = list(store.db.iterdump())
    private = signature(private_state(store))
    size = (
        store.db.execute("PRAGMA page_count").fetchone()[0]
        * store.db.execute("PRAGMA page_size").fetchone()[0]
    )
    path = tmp_path / "actual-snapshot.zst"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    target = arena(path, raw=size)
    pinned = apsw.Connection(
        store.path.resolve().as_uri() + "?mode=ro",
        flags=apsw.SQLITE_OPEN_READONLY | apsw.SQLITE_OPEN_URI,
    )
    pinned.execute("BEGIN")
    pinned.execute("SELECT owner_id FROM owner_settings").fetchone()
    try:
        with snapshot_image(
            store.path, scratch, deadline=time.monotonic() + 20, archive_store=target, source_snapshot=pinned
        ) as (
            database,
            image,
        ):
            assert database.execute("SELECT owner_id FROM owner_settings").fetchone()[0] == store.owner_id
            assert image.reconstructed_hash() == target.raw_sha256
            assert target.raw_bytes == image.size
            with pytest.raises(apsw.ConnectionClosedError):
                pinned.txn_state()
    finally:
        target.close()
    restored = tmp_path / "restored.db"
    with path.open("rb") as source, restored.open("xb") as destination:
        zstandard.ZstdDecompressor().copy_stream(source, destination)
    assert hashlib.sha256(restored.read_bytes()).hexdigest() == target.raw_sha256
    with closing(sqlite3.connect(restored)) as reader:
        assert list(reader.iterdump()) == original
        assert reader.execute("PRAGMA foreign_key_check").fetchall() == []
    assert signature(private_state(store)) == private
    assert list(store.db.iterdump()) == original and list(scratch.iterdir()) == []


@pytest.mark.parametrize("invalid", ["not_pinned", "writable", "wrong_source", "too_small"])
def test_snapshot_requires_exact_readonly_pinned_source_and_admitted_size(store, tmp_path, invalid):
    import sqlite3
    from contextlib import closing

    import apsw

    from cryptowatcher.compressed_validation import snapshot_image

    original = list(store.db.iterdump())
    source = store.path
    if invalid == "wrong_source":
        source = tmp_path / "other.db"
        with closing(sqlite3.connect(source)) as other:
            other.execute("CREATE TABLE other(value)")
    flags = apsw.SQLITE_OPEN_READWRITE if invalid == "writable" else apsw.SQLITE_OPEN_READONLY
    reader = apsw.Connection(
        source.resolve().as_uri() + "?mode=" + ("rw" if invalid == "writable" else "ro"),
        flags=flags | apsw.SQLITE_OPEN_URI,
    )
    if invalid != "not_pinned":
        reader.execute("BEGIN")
        reader.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
    size = (
        store.db.execute("PRAGMA page_count").fetchone()[0]
        * store.db.execute("PRAGMA page_size").fetchone()[0]
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    target = arena(tmp_path / "unqualified.zst", raw=size - 1 if invalid == "too_small" else size)
    try:
        expected = "ARCHIVE_SIZE_LIMIT" if invalid == "too_small" else "PINNED_SOURCE_REQUIRED"
        with pytest.raises(QualificationError, match=expected):
            with snapshot_image(
                store.path,
                scratch,
                deadline=time.monotonic() + 20,
                archive_store=target,
                source_snapshot=reader,
            ):
                raise AssertionError("An invalid snapshot was admitted")
        assert not target.sealed
        with pytest.raises(apsw.ConnectionClosedError):
            reader.txn_state()
    finally:
        target.close()
    assert list(store.db.iterdump()) == original


def test_full_check_constraints_still_reject_corrupt_captured_source(store, tmp_path):
    from cryptowatcher.compressed_validation import snapshot_image

    store.db.executescript(
        "CREATE TABLE proof_check(value INTEGER CHECK(value>0)); PRAGMA ignore_check_constraints=ON; INSERT INTO proof_check VALUES(-1); PRAGMA ignore_check_constraints=OFF;"
    )
    size = (
        store.db.execute("PRAGMA page_count").fetchone()[0]
        * store.db.execute("PRAGMA page_size").fetchone()[0]
    )
    path = tmp_path / "rejected.zst"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    target = arena(path, raw=size)
    try:
        with pytest.raises(QualificationError, match="DATABASE_CORRUPT"):
            with snapshot_image(store.path, scratch, deadline=time.monotonic() + 20, archive_store=target):
                raise AssertionError("Invalid snapshot passed its full CHECK gate")
    finally:
        target.close()
    assert path.exists()  # A failed capture never removes evidence or claims a usable backup.
