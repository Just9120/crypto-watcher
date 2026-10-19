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


def test_native_full_validation_includes_source_tiles_cells_and_their_counters(store, tmp_path):
    from cryptowatcher.market_storage import enable
    from cryptowatcher.outcomes import OutcomeWorker
    from cryptowatcher.sources import Sources
    from cryptowatcher.storage_repack import work
    from tests.test_numeric_price_blocks import activate, population
    from tests.test_outcomes import MINUTE, NOW, future, observe

    assert available(), "Build canonical native helper before Linux tests"
    enable(store.db)
    store.bounded = True
    worker = OutcomeWorker(store)
    observe(store, worker)
    Sources(store).put("spot/BTCUSDT/1m", future())
    worker.work(NOW + 6 * MINUTE)
    for tick in range(100):
        state = work(store, NOW + tick, seconds=1)
        if state["state"] == "COMPLETE":
            break
    assert state["state"] == "COMPLETE" and state["version"] == 11
    assert store.db.execute("SELECT COUNT(*) FROM market_candle_blocks").fetchone()[0] > 0
    from cryptowatcher import metric_codec, metric_price_codec, numeric_block_codec, numeric_blocks

    metric_ids, metric_values, candidates = population(store)
    activate(store)
    with store.db:
        accepted = sum(
            numeric_blocks.offer_price(store.db, i, c) for i, c in zip(metric_ids, candidates, strict=True)
        )
    assert accepted > 0
    wanted = {i: metric_codec.pack(v) for i, v in zip(metric_ids, metric_values, strict=True)}
    store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    original = store.path.read_bytes()
    image = cv.Image(store.path, limit=cv.MAX_COMPRESSED_BYTES, deadline=time.monotonic() + 30)
    sentinel = tmp_path / "source-tiles-native.sqlite3"
    sentinel.touch(mode=0o600)
    vfs = cv.frozen_vfs(image, sentinel)
    try:
        with closing(apsw.Connection(str(sentinel), flags=apsw.SQLITE_OPEN_READWRITE, vfs=vfs.name)) as db:
            cv.configure_cache(db, image)
            cv.full_check(db, image.deadline)
            actual_metrics = {}
            for block, payload in db.execute("SELECT id,payload FROM market_numeric_blocks"):
                for slot, frame in enumerate(numeric_block_codec.unpack(bytes(payload), block)):
                    identity_value = block * 64 + slot + 1
                    if identity_value in wanted:
                        actual_metrics[identity_value] = metric_price_codec.frame(frame)
            assert actual_metrics == wanted
            for table in (
                "market_candle_blocks",
                "market_candle_block_totals",
                "market_candle_fallbacks",
                "market_candle_gc",
                "market_numeric_blocks",
                "market_numeric_block_refs",
                "market_numeric_block_lookup",
                "market_numeric_totals",
                "market_numeric_transfer",
                "market_numeric_gc",
                "market_counts",
                "runtime",
            ):
                actual = list(db.execute(f"SELECT * FROM {table} ORDER BY 1,2"))
                expected = [tuple(row) for row in store.db.execute(f"SELECT * FROM {table} ORDER BY 1,2")]
                assert actual == expected
    finally:
        vfs.unregister()
    assert image.reconstructed_hash() == image.sha256
    assert store.path.read_bytes() == original


def test_native_full_validation_preserves_all_types_and_rejects_writes_and_early_release(tmp_path):
    assert available(), "Build canonical native helper before Linux tests"
    source, _ = closed_backup(tmp_path)
    original = source.read_bytes()
    image = cv.Image(source, limit=cv.MAX_COMPRESSED_BYTES, deadline=time.monotonic() + 30)
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
        assert vfs.stats()["reads"] > 0
    finally:
        db.close()
        vfs.unregister()
    assert source.read_bytes() == original
    assert image.reconstructed_hash() == image.sha256
    with pytest.raises(cv.QualificationError, match="NATIVE_VFS_CONTEXT_CLOSED"):
        vfs.stats()


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
