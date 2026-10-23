import sqlite3
from contextlib import closing

import pytest

from cryptowatcher import candle_block_codec as codec
from cryptowatcher import candle_blocks as blocks
from cryptowatcher.market_storage import stream_id
from cryptowatcher.storage import Store, encode
from tests.test_bounded_storage import bounded
from tests.test_candle_block_codec import fixture

DAY = 86400000


def prepare(store):
    bounded(store)
    blocks.install(store.db, store.cached_text, store.cached_bytes)
    with store.db:
        sid = stream_id(store.db, "spot/BTCUSDT/1m", create=True)
    tile, slots = fixture()
    values = [codec.public(tile, 60000, slot, slots[slot][0][3]) for slot in (0, 7, 31)]
    return sid, tile, values


def test_source_batches_reuse_exact_revision_keep_corrections_lease_types_snapshot_and_reopen(store):
    sid, tile, values = prepare(store)
    with store.db:
        first = blocks.admit_many(store.db, sid, values)
    assert first == [(sid, value["start"], 0) for value in values]
    assert blocks.summary(store.db)["versions"] == 3
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        reader.db.execute("BEGIN")
        old = blocks.tile(reader.db, sid, tile, 60000)
        corrected = {**values[0], "close": 102.0}
        with store.db:
            actual = blocks.admit_many(store.db, sid, [corrected, values[0]], tile + 40 * DAY)
        assert actual == [(sid, tile, 1), (sid, tile, 0)]
        assert codec.point(blocks.tile(reader.db, sid, tile, 60000), sid, tile, 1) is None
        reader.db.rollback()
        new = blocks.tile(reader.db, sid, tile, 60000)
        assert new != old
        assert encode(codec.point(new, sid, tile, 0)[0]) == encode(values[0])
        assert encode(codec.point(new, sid, tile, 1)[0]) == encode(corrected)
        assert codec.point(new, sid, tile, 0)[1:] == (tile + 40 * DAY, tile + 70 * DAY)
    with closing(Store(store.path, store.owner_id)) as writer:
        with writer.db:
            assert blocks.admit_many(writer.db, sid, [corrected]) == [(sid, tile, 1)]
        assert blocks.summary(writer.db)["versions"] == 4


def test_source_cas_immutable_values_stream_binding_and_unknown_deletion_fail_closed(store):
    sid, tile, values = prepare(store)
    with store.db:
        blocks.admit_many(store.db, sid, values)
    before = list(store.db.iterdump())
    old = blocks.tile(store.db, sid, tile, 60000)
    changed = codec.unpack(old)[3]
    changed[0][0][3][3] = 102.0
    bad = codec.pack(sid, tile, 60000, changed)
    with pytest.raises(sqlite3.OperationalError), store.db:
        blocks.put(store.db, bad, old)
    assert list(store.db.iterdump()) == before
    with pytest.raises(sqlite3.IntegrityError), store.db:
        store.db.execute("UPDATE market_streams SET name='linear/ETHUSDT/1m' WHERE id=?", (sid,))
    with pytest.raises(sqlite3.OperationalError), store.db:
        store.db.execute("DELETE FROM market_candle_blocks")
    with store.db:
        blocks.admit_many(store.db, sid, [values[0]], tile + 10 * DAY)
    current = blocks.tile(store.db, sid, tile, 60000)
    with pytest.raises(ValueError, match="revision changed"), store.db:
        blocks.put(store.db, old, old)
    assert blocks.tile(store.db, sid, tile, 60000) == current


def test_source_rollback_missing_counter_and_metadata_mismatch_preserve_all_candles(store):
    sid, tile, values = prepare(store)
    with store.db:
        blocks.admit_many(store.db, sid, values)
    original = blocks.tile(store.db, sid, tile, 60000)
    before = list(store.db.iterdump())
    with pytest.raises(RuntimeError), store.db:
        blocks.admit_many(store.db, sid, [{**values[0], "close": 102.0}])
        raise RuntimeError("interrupted source correction")
    assert list(store.db.iterdump()) == before
    with pytest.raises(sqlite3.IntegrityError, match="metadata mismatch"), store.db:
        store.db.execute("UPDATE market_candle_blocks SET row_count=row_count+1")
    with store.db:
        store.db.execute("DELETE FROM market_counts WHERE category='source_blocks'")
    with pytest.raises(ValueError, match="counter"):
        blocks.summary(store.db)
    with pytest.raises(sqlite3.IntegrityError, match="counter"), store.db:
        blocks.admit_many(store.db, sid, [values[0]], tile + 50 * DAY)
    assert blocks.tile(store.db, sid, tile, 60000) == original


def test_legacy_tile_transfer_preserves_public_and_lease_rows_is_atomic_and_idempotent(store):
    from cryptowatcher.market_storage import admit, public

    sid, tile, values = prepare(store)
    with store.db:
        for value in values:
            admit(store.db, "spot/BTCUSDT/1m", value, needed_until=tile + 40 * DAY)
    original = [
        (r[0], r[1], r[2], r[4], r[5], public(bytes(r[3])))
        for r in store.db.execute("SELECT * FROM market_candles ORDER BY stream,start,revision")
    ]
    with store.db:
        assert blocks.import_tile(store.db, sid, tile) == 3
    assert store.db.execute("SELECT 1 FROM market_candles LIMIT 1").fetchone() is None
    assert encode(codec.rows(blocks.tile(store.db, sid, tile, 60000))) == encode(original)
    before = list(store.db.iterdump())
    with store.db:
        assert blocks.import_tile(store.db, sid, tile) == 0
    assert list(store.db.iterdump()) == before
    assert blocks.summary(store.db)["versions"] == 3


def test_legacy_transfer_rejects_missing_counter_without_leaving_partial_source_tile(store):
    from cryptowatcher.market_storage import admit

    sid, tile, values = prepare(store)
    with store.db:
        admit(store.db, "spot/BTCUSDT/1m", values[0])
        store.db.execute("DELETE FROM market_counts WHERE category='candles'")
    before = list(store.db.iterdump())
    with pytest.raises(ValueError, match="legacy source counter"), store.db:
        blocks.import_tile(store.db, sid, tile)
    assert list(store.db.iterdump()) == before
    assert blocks.tile(store.db, sid, tile, 60000) is None


def test_tile_expiry_evaluates_each_revision_and_preserves_holds_and_renewed_detail_lease(store):
    sid, tile, values = prepare(store)
    with store.db:
        blocks.admit_many(store.db, sid, values)
    at = tile + 36 * DAY
    with store.db:
        store.db.execute(
            "INSERT INTO market_holds VALUES(?,?,?,?,?)",
            ("source-fixture", sid, tile, tile + 60000, at + 7200000),
        )
        assert blocks.gc(store.db, at, seconds=5) == 2
    raw = blocks.tile(store.db, sid, tile, 60000)
    assert codec.point(raw, sid, tile, 0) is not None
    assert codec.point(raw, sid, tile + 7 * 60000, 0) is None
    assert codec.point(raw, sid, tile, 0)[2] == at + 3600000
    with store.db:
        store.db.execute("DELETE FROM market_holds")
        blocks.admit_many(store.db, sid, [values[0]], tile + 10 * DAY)
        assert blocks.gc(store.db, tile + 37 * DAY, seconds=5) == 0
    with pytest.raises(sqlite3.OperationalError), store.db:
        store.db.execute("INSERT INTO market_candle_gc VALUES(1,?)", (tile + 37 * DAY,))
        store.db.execute("DELETE FROM market_candle_blocks")
    assert store.db.execute("SELECT 1 FROM market_candle_gc").fetchone() is None
    with store.db:
        assert blocks.gc(store.db, tile + 40 * DAY, seconds=5) == 1
    assert blocks.summary(store.db) == {
        "blocks": 0,
        "versions": 0,
        "payload_bytes": 0,
        "fallback_tiles": 0,
        "index_bytes": 0,
    }


def test_failed_collection_rolls_back_every_tile_counter_and_ephemeral_expiry_authority(store):
    sid, tile, values = prepare(store)
    with store.db:
        blocks.admit_many(store.db, sid, values)
    store.db.execute(
        "CREATE TRIGGER source_fixture_failure AFTER DELETE ON market_candle_blocks BEGIN SELECT RAISE(ABORT,'fixture failure'); END"
    )
    before = list(store.db.iterdump())
    with pytest.raises(sqlite3.IntegrityError, match="fixture failure"), store.db:
        blocks.gc(store.db, tile + 100 * DAY, seconds=5)
    assert list(store.db.iterdump()) == before
    assert blocks.summary(store.db)["versions"] == 3
    assert store.db.execute("SELECT 1 FROM market_candle_gc").fetchone() is None
