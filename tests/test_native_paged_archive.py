"""Native page frames preserve64KiB index compatibility and fail closed."""

import hashlib
import math
import sys
import time

import pytest
import zstandard

from cryptowatcher import compressed_validation as cv
from cryptowatcher.native_validation import NativeVFS, available
from cryptowatcher.validation_archive import BLOCK, ArchiveArena

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Required native Linux helper")


@pytest.mark.parametrize("page_bytes", [4096, 8192, 16384, 65536])
def test_actual_native_snapshot_decodes_pages_and_checks_all_bytes(store, tmp_path, monkeypatch, page_bytes):
    assert available()
    store.db.execute("CREATE TABLE page_probe(payload BLOB)")
    with store.db:
        store.db.executemany("INSERT INTO page_probe VALUES(zeroblob(4096))", [()] * 256)
    assert store.db.execute("PRAGMA freelist_count").fetchone()[0] == 0
    size = store.db.execute("PRAGMA page_count").fetchone()[0] * 4096
    snapshot = store.snapshot()
    target = ArchiveArena(
        tmp_path / "paged.zst",
        max_raw_bytes=size,
        max_archive_bytes=size + 2 * 1024**2,
        deadline=time.monotonic() + 20,
    )
    target.page_bytes = page_bytes
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    contexts = []

    def frozen(image, path):
        context = NativeVFS(image, path)
        contexts.append(context)
        return context

    monkeypatch.setattr(cv, "frozen_vfs", frozen)
    try:
        with cv.snapshot_image(store.path, scratch, deadline=target.deadline, archive_store=target) as (
            database,
            image,
        ):
            assert database.execute("SELECT COUNT(*),SUM(length(payload)) FROM page_probe").fetchone() == (
                256,
                256 * 4096,
            )
            assert image.reconstructed_hash() == target.raw_sha256
            assert contexts[0].stats()["decodes"] >= math.ceil(size / page_bytes)
    finally:
        target.close()
    assert store.snapshot() == snapshot


@pytest.mark.parametrize("damage", ["short", "mixed", "extra"])
def test_native_malformed_page_packet_refuses_without_retaining_borrow(tmp_path, damage):
    assert available()
    compressor = zstandard.ZstdCompressor(write_checksum=True)
    pages = [compressor.compress(bytes(4096)) for _ in range(16)]
    if damage == "mixed":
        pages[1:3] = [compressor.compress(bytes(8192))]
    packet = b"".join(pages)
    if damage == "short":
        packet = packet[:-1]
    if damage == "extra":
        packet += compressor.compress(bytes(4096))
    target = ArchiveArena(
        tmp_path / "invalid.zst",
        max_raw_bytes=BLOCK,
        max_archive_bytes=4 * BLOCK,
        deadline=time.monotonic() + 20,
    )
    try:
        target.append(packet)
        target.finish(BLOCK, hashlib.sha256(bytes(BLOCK)).hexdigest())
        image = cv.Image.from_archive_store(target)
        with pytest.raises(cv.QualificationError, match="NATIVE_VFS_REGISTRATION_REFUSED"):
            NativeVFS(image, tmp_path / "sentinel.sqlite3")
    finally:
        # A retained ctypes borrow would raise BufferError instead of closing.
        target.close()


def test_expired_native_registration_refusal_keeps_timeout_and_releases_borrow(tmp_path, monkeypatch):
    assert available()
    source = tmp_path / "raw.bin"
    source.write_bytes(bytes(BLOCK))
    image = cv.Image(source, limit=1024**2, deadline=time.monotonic() + 10)
    image.blocks.offsets[0] = image.blocks.end + 1
    image.deadline = 1.0
    calls = iter((0.0, 2.0))
    monkeypatch.setattr(cv.time, "monotonic", lambda: next(calls))
    try:
        with pytest.raises(cv.QualificationError, match="TIME_BUDGET_EXCEEDED"):
            NativeVFS(image, tmp_path / "sentinel.sqlite3")
    finally:
        image.blocks.data.close()
