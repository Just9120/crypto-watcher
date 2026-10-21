import gzip
import hashlib
import os
import random
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

import apsw
import pytest
import zstandard

from cryptowatcher.compressed_validation import (
    BLOCK,
    ArchiveWriter,
    Image,
    QualificationError,
    classified_errors,
    configure_cache,
    snapshot,
    validate_closed,
)


def test_coalesced_partial_writes_sparse_holes_truncate_and_freeze():
    image = Image(None, limit=2**20, deadline=time.monotonic() + 10)
    reference = bytearray()
    writes = [(17, b"first"), (BLOCK - 3, b"boundary-value"), (BLOCK * 12 + 29, b"far")]
    for offset, value in writes:
        reference.extend(bytes(max(0, offset + len(value) - len(reference))))
        reference[offset : offset + len(value)] = value
        image.write(value, offset)
        assert image.read(len(reference), 0) == bytes(reference)
    image.truncate(BLOCK + 7)
    reference = reference[: BLOCK + 7]
    image.write(b"new", BLOCK + 19)
    reference.extend(bytes(BLOCK + 22 - len(reference)))
    reference[BLOCK + 19 : BLOCK + 22] = b"new"
    image.freeze()
    assert image.read(len(reference), 0) == bytes(reference)
    assert image.reconstructed_hash() == hashlib.sha256(reference).hexdigest()
    with pytest.raises(apsw.ReadOnlyError):
        image.write(b"modified", 0)


def test_coalesced_truncate_before_buffer_flush_discards_removed_bytes():
    image = Image(None, limit=2**20, deadline=time.monotonic() + 10)
    image.write(b"original", 0)
    with pytest.raises(QualificationError):
        image.reconstructed_hash()
    image.truncate(0)
    image.write(b"new", BLOCK + 5)
    image.freeze()
    expected = bytes(BLOCK + 5) + b"new"
    assert image.read(len(expected), 0) == expected
    assert image.reconstructed_hash() == hashlib.sha256(expected).hexdigest()


def test_capture_frames_survive_repeated_growth_shrink_and_tail_reuse():
    image = Image(None, limit=2**20, deadline=time.monotonic() + 10)
    expected = bytearray(BLOCK * 20)
    randomizer = random.Random(321)
    for index in range(20):
        page = randomizer.randbytes(BLOCK)
        image.write(page, index * BLOCK)
        expected[index * BLOCK : (index + 1) * BLOCK] = page
    image.write(bytes(BLOCK), 0)
    expected[:BLOCK] = bytes(BLOCK)
    replacement = randomizer.randbytes(BLOCK)
    image.write(replacement, 0)
    expected[:BLOCK] = replacement
    image.truncate(BLOCK * 10)
    del expected[BLOCK * 10 :]
    image.write(b"tail", BLOCK * 12 + 5)
    expected.extend(bytes(BLOCK * 12 + 9 - len(expected)))
    expected[-4:] = b"tail"
    image.freeze()
    assert image.reconstructed_hash() == hashlib.sha256(expected).hexdigest()
    assert b"".join(image.iter_bytes()) == expected


def test_capture_arena_capacity_failure_keeps_existing_frame_bytes():
    from cryptowatcher.compressed_validation import _FrameArena

    arena = _FrameArena(32)
    arena.append(b"first-frame")
    arena.append(b"second")
    before = list(arena)
    with pytest.raises(QualificationError, match="COMPRESSED_MEMORY_LIMIT"):
        arena[0] = b"x" * 32
    assert list(arena) == before
    with pytest.raises(QualificationError, match="COMPRESSED_MEMORY_LIMIT"):
        arena.append(b"y" * 32)
    assert list(arena) == before


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
    for n, a in (
        (0, 0),
        (0, BLOCK),
        (4096, BLOCK),
        (BLOCK, BLOCK - 1),
        (4096, len(data) - 17),
        (1, len(data)),
        (4096, len(data) + BLOCK),
    ):
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
    with pytest.raises(zstandard.ZstdError):
        image.reconstructed_hash()


def test_gzip_archive_full_check_and_exact_restored_identity(database):
    original = database.read_bytes()
    archive = database.with_suffix(".sqlite3.gz")
    archive.write_bytes(gzip.compress(original))
    before = archive.read_bytes()
    proof = validate_closed(
        archive, directory=database.parent, deadline=time.monotonic() + 30, archive_encoding="GZIP"
    )
    assert proof["bytes"] == len(original) and proof["sha256"] == hashlib.sha256(original).hexdigest()
    assert archive.read_bytes() == before and database.read_bytes() == original


@pytest.mark.parametrize("damage", ["truncated", "crc"])
def test_invalid_gzip_archive_refuses_qualification(database, damage):
    archive = database.with_suffix(".sqlite3.gz")
    content = gzip.compress(database.read_bytes())
    broken = content[:-7] if damage == "truncated" else content[:-8] + bytes(8)
    archive.write_bytes(broken)
    deadline = time.monotonic() + 30
    with pytest.raises(QualificationError, match="VALIDATION_IO_ERROR"), classified_errors(deadline):
        validate_closed(archive, directory=database.parent, deadline=deadline, archive_encoding="GZIP")
    assert archive.read_bytes() == broken


def test_zstd_frame_checksum_rejects_changed_content(database):
    image = Image(database, limit=10 * 1024 * 1024, deadline=time.monotonic() + 30)
    blob = image.blocks[0]
    image.blocks[0] = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(zstandard.ZstdError):
        image.reconstructed_hash()


def test_native_cache_uses_only_remaining_cap_and_never_disables_integrity(database):
    from types import SimpleNamespace

    from cryptowatcher import compressed_validation as cv

    blocks = []
    with apsw.Connection(":memory:") as db:
        for retained in (0, 2 * 1024**3, cv.MAX_COMPRESSED_BYTES - sys.getsizeof(blocks)):
            image = SimpleNamespace(
                mutable=False, retained=retained, blocks=blocks, deadline=time.monotonic() + 30
            )
            kib = configure_cache(db, image)
            assert 512 * 1024 <= kib <= 1024 * 1024
            assert db.execute("PRAGMA cache_size").fetchone()[0] == -kib
            assert (
                retained + sys.getsizeof(blocks) + kib * 1024 + cv.VALIDATION_OVERHEAD_BYTES
                <= cv.VALIDATION_MEMORY_BYTES
            )
        # An accounting overflow must reject, rather than allocating above the cap.
        image.retained += 1024
        with pytest.raises(QualificationError, match="COMPRESSED_MEMORY_LIMIT"):
            configure_cache(db, image)
    proof = validate(database, limit=10 * 1024 * 1024, deadline=time.monotonic() + 30)
    assert proof["sqlite_cache_kib"] == 1024 * 1024


def test_native_cache_accounts_for_resident_memory_beyond_compressed_payload(monkeypatch):
    from types import SimpleNamespace

    from cryptowatcher import compressed_validation as cv

    resident = 2816 * 1024**2
    monkeypatch.setattr(cv, "resident_memory_bytes", lambda: resident)
    image = SimpleNamespace(mutable=False, retained=2 * 1024**3, blocks=[], deadline=time.monotonic() + 30)
    with apsw.Connection(":memory:") as db:
        kib = configure_cache(db, image)
        assert 512 * 1024 <= kib < 1024 * 1024
        assert resident + kib * 1024 + cv.VALIDATION_OVERHEAD_BYTES <= cv.VALIDATION_MEMORY_BYTES
        assert db.execute("PRAGMA cache_size").fetchone()[0] == -kib


def test_native_cache_refuses_existing_heap_pressure_without_allocating_or_weakening_checks(monkeypatch):
    from types import SimpleNamespace

    from cryptowatcher import compressed_validation as cv

    monkeypatch.setattr(cv, "resident_memory_bytes", lambda: 3328 * 1024**2)
    image = SimpleNamespace(mutable=False, retained=2 * 1024**3, blocks=[], deadline=time.monotonic() + 30)
    with apsw.Connection(":memory:") as db:
        before = db.execute("PRAGMA cache_size").fetchone()[0]
        with pytest.raises(QualificationError, match="COMPRESSED_MEMORY_LIMIT"):
            configure_cache(db, image)
        assert db.execute("PRAGMA cache_size").fetchone()[0] == before
        assert db.execute("PRAGMA ignore_check_constraints").fetchone()[0] == 0


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


def test_archived_online_backup_exact_identity_native_restore_and_wal_race(store, tmp_path, monkeypatch):
    from cryptowatcher import compressed_validation as cv
    from cryptowatcher import validation
    from cryptowatcher.storage import Store

    monkeypatch.setattr(validation, "scratch_space", lambda *args: None)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    destination = tmp_path / "backup.sqlite3.zst"
    store.put_runtime("evidence", {"state": "before"})
    original_check = cv.full_check

    def checked(db, deadline):
        # The production reader must be released; its later state is excluded.
        store.put_runtime("evidence", {"state": "after"})
        assert store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        original_check(db, deadline)

    monkeypatch.setattr(cv, "full_check", checked)
    proof = store.backup(destination, validation_directory=scratch, compressed_validation=True, archive=True)
    assert proof["phase"] == "COMPLETE" and proof["archive_encoding"] == "ZSTD"
    assert proof["archive_sha256"] == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert proof["archive_bytes"] == destination.stat().st_size
    restored = tmp_path / "restored.sqlite3"
    with destination.open("rb") as source, restored.open("xb") as target:
        zstandard.ZstdDecompressor().copy_stream(source, target)
    assert restored.stat().st_size == proof["bytes"]
    assert hashlib.sha256(restored.read_bytes()).hexdigest() == proof["sha256"]
    with closing(sqlite3.connect(restored)) as db:
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        for name in ("owner_settings", "delivery_preferences", "mutes", "runtime", "legacy_import"):
            assert db.execute("SELECT count(*) FROM " + name).fetchone()[0] >= 0
    readback = Store(restored, store.owner_id, readonly=True)
    try:
        assert readback.snapshot() == store.snapshot()
        assert readback.get_runtime("evidence", {}) == {"state": "before"}
    finally:
        readback.close()
    assert store.get_runtime("evidence", {}) == {"state": "after"}
    original = destination.read_bytes()
    from cryptowatcher.storage import StateError

    with pytest.raises(StateError, match="destination must be new"):
        store.backup(destination, validation_directory=scratch, compressed_validation=True, archive=True)
    assert destination.read_bytes() == original and not list(scratch.iterdir())


def test_archive_write_limit_and_expired_deadline_preserve_outputs(tmp_path, monkeypatch):
    from cryptowatcher import compressed_validation as cv

    monkeypatch.setattr(cv, "MAX_COMPRESSED_BYTES", 16)
    destination = tmp_path / "partial"
    with destination.open("wb") as output:
        sink = ArchiveWriter(output, time.monotonic() + 30)
        assert sink.write(b"preserved") == 9
        with pytest.raises(QualificationError, match="COMPRESSED_MEMORY_LIMIT"):
            sink.write(b"x" * 16)
        sink.deadline = 0
        with pytest.raises(QualificationError, match="TIME_BUDGET_EXCEEDED"):
            sink.write(b"late")
    assert destination.read_bytes() == b"preserved"


def test_archive_persisted_corruption_never_passes(store, tmp_path, monkeypatch):
    from cryptowatcher import compressed_validation as cv
    from cryptowatcher import validation
    from cryptowatcher.storage import IntegrityError

    monkeypatch.setattr(validation, "scratch_space", lambda *args: None)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    destination = tmp_path / "bad.sqlite3.zst"
    original = cv.validate_closed

    def damaged(path, **kwargs):
        with path.open("r+b") as stream:
            stream.seek(-1, 2)
            stream.write(b"\x00")
        return original(path, **kwargs)

    monkeypatch.setattr(cv, "validate_closed", damaged)
    with pytest.raises(IntegrityError):
        store.backup(destination, validation_directory=scratch, compressed_validation=True, archive=True)
    assert destination.is_file() and store.snapshot().revision == 1
    assert not list(scratch.iterdir())


@pytest.mark.parametrize("stage", ["integrity", "heads"])
@pytest.mark.parametrize("valid", [False, True])
def test_late_sqlite_results_never_become_corruption_or_success(monkeypatch, stage, valid):
    from cryptowatcher import compressed_validation as cv
    from cryptowatcher import record_catalog

    clock = [0.0]
    monkeypatch.setattr(cv.time, "monotonic", lambda: clock[0])

    class Database:
        def set_progress_handler(self, *args):
            pass

        def create_scalar_function(self, *args, **kwargs):
            pass

        def execute(self, sql):
            if sql == "PRAGMA integrity_check":
                if stage == "integrity":
                    clock[0] = 2.0
                    return [("ok",)] if valid else [("native read failed after deadline",)]
                return [("ok",)]
            return []

    def heads(database):
        assert stage == "heads"
        clock[0] = 2.0
        return valid

    monkeypatch.setattr(record_catalog, "valid_heads", heads)
    with pytest.raises(QualificationError, match="TIME_BUDGET_EXCEEDED"):
        cv.full_check(Database(), 1.0)


def test_standard_page_stream_coalesces_short_reads_before_indexing(tmp_path):
    raw = b"a" * 4096 + b"b" * 4096 + b"last partial"
    compressor = zstandard.ZstdCompressor(write_checksum=True)
    encoded = b"".join(compressor.compress(raw[i : i + 4096]) for i in range(0, len(raw), 4096))
    path = tmp_path / "independent-pages.zst"
    path.write_bytes(encoded)
    image = Image(path, limit=1024**2, deadline=time.monotonic() + 10, archive_encoding="ZSTD")
    try:
        assert image.size == len(raw) and len(image.blocks) == 1
        assert image.reconstructed_hash() == hashlib.sha256(raw).hexdigest()
    finally:
        image.blocks.data.close()
