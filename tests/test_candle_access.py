import sqlite3
from contextlib import closing

import pytest

from cryptowatcher import candle_access as access
from cryptowatcher import candle_block_codec as codec
from cryptowatcher import candle_blocks as blocks
from cryptowatcher import market_storage as market
from cryptowatcher.storage import Store, encode
from tests.test_bounded_storage import bounded
from tests.test_candle_block_codec import fixture


def writer(store):
    blocks.install(store.db, store.cached_text)
    with store.db:
        store.db.execute("INSERT INTO market_meta VALUES('candle_block_runtime','1')")


@pytest.mark.parametrize("stream", ["spot/BTCUSDT/1m", "linear/ETHUSDT/15m"])
def test_mixed_sources_ranges_latest_and_frozen_window_preserve_order_gaps_revisions_and_bits(store, stream):
    from cryptowatcher.history_codec import digest

    bounded(store)
    step = int(stream.rsplit("/", 1)[1][:-1]) * 60000
    tile, slots = fixture(step)
    values = [codec.public(tile, step, slot, slots[slot][0][3]) for slot in (0, 7, 31)]
    values += [{**values[0], "start": tile + step * 64, "server_ms": tile + step * 100}]
    corrected = {**values[0], "close": 102.0}
    with store.db:
        points = [market.admit(store.db, stream, v) for v in values]
        newer = market.admit(store.db, stream, corrected)
    frozen = [values[3], values[0], values[2], values[0], corrected]
    ref = market.compact_reference(
        points[0][0],
        [[v[1], v[2]] for v in [points[3], points[0], points[2], points[0], newer]],
        digest(encode(frozen).encode()),
    )
    expected = encode(market.read(store.db, stream))
    blocks.install(store.db, store.cached_text)
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        reader.db.execute("BEGIN")
        assert encode(market.read(reader.db, stream)) == expected
        with store.db:
            assert blocks.import_tile(store.db, points[0][0], tile) == 4
        # A live reader retains one coherent WAL snapshot across the transfer.
        assert encode(market.read(reader.db, stream)) == expected
        reader.db.rollback()
        assert encode(market.read(reader.db, stream, cache=reader.cached_bytes)) == expected
        assert market.restore_window(reader.db, ref, cache=reader.cached_bytes) == encode(frozen).encode()
        assert encode(market.read(reader.db, stream, limit=2)) == encode([values[3], values[2]])
        assert market.read(reader.db, stream, tile + step, tile + 7 * step) == []
        assert encode(market.read(reader.db, stream, tile + step, tile + 32 * step)) == encode(values[1:3])
        assert reader.reference_cache_bytes <= 1024 * 1024
    assert store.db.execute("SELECT COUNT(*) FROM market_candles").fetchone()[0] == 1


def test_point_and_range_use_global_latest_and_reject_conflicting_duplicate_revisions(store):
    bounded(store)
    tile, slots = fixture()
    value = codec.public(tile, 60000, 0, slots[0][0][3])
    with store.db:
        sid, _, _ = market.admit(store.db, "spot/BTCUSDT/1m", value)
    blocks.install(store.db, store.cached_text)
    with store.db:
        blocks.import_tile(store.db, sid, tile)
        # Old readers/writers can coexist until activation; equal revisions must agree.
        market.admit(store.db, "spot/BTCUSDT/1m", value)
    assert encode(market.read(store.db, "spot/BTCUSDT/1m")) == encode([value])
    corrected = {**value, "close": 102.0}
    with store.db:
        market.admit(store.db, "spot/BTCUSDT/1m", corrected)
    assert encode(market.read(store.db, "spot/BTCUSDT/1m")) == encode([corrected])
    assert encode(access.Lookup(store.db, sid).point(tile, 0)) == encode(value)
    with store.db:
        store.db.execute(
            "UPDATE market_candles SET payload=? WHERE revision=0",
            (market.encoded([corrected[k] for k in market.FIELDS]),),
        )
    with pytest.raises(ValueError, match="Conflicting"):
        access.Lookup(store.db, sid).point(tile, 0)
    with store.db:
        store.db.execute("DELETE FROM market_candles WHERE revision=1")
    with pytest.raises(ValueError, match="Conflicting"):
        market.read(store.db, "spot/BTCUSDT/1m")


def test_latest_small_window_reads_bounded_tiles_and_rechecks_actual_bytes_after_cache(store, monkeypatch):
    bounded(store)
    blocks.install(store.db, store.cached_text)
    tile, slots = fixture()
    base = codec.public(tile, 60000, 0, slots[0][0][3])
    with store.db:
        sid = market.stream_id(store.db, "spot/BTCUSDT/1m", create=True)
        blocks.admit_many(
            store.db,
            sid,
            [{**base, "start": tile + i * 60000, "server_ms": tile + 2000 * 60000} for i in range(1024)],
        )
    calls = []
    original = codec.latest

    def tracked(raw, *args, **kwargs):
        calls.append(len(raw))
        return original(raw, *args, **kwargs)

    monkeypatch.setattr(codec, "latest", tracked)
    assert len(market.read(store.db, "spot/BTCUSDT/1m", limit=4, cache=store.cached_bytes)) == 4
    assert len(calls) == 1
    with store.db:
        store.db.execute("DROP TRIGGER market_candle_block_update_guard")
        row = store.db.execute(
            "SELECT id,payload FROM market_candle_blocks ORDER BY tile DESC LIMIT 1"
        ).fetchone()
        bad = bytes(row[1][:-1]) + bytes([row[1][-1] ^ 1])
        store.db.execute("UPDATE market_candle_blocks SET payload=? WHERE id=?", (bad, row[0]))
    with pytest.raises(ValueError, match="checksum"):
        market.read(store.db, "spot/BTCUSDT/1m", limit=4, cache=store.cached_bytes)


def test_source_writer_transfers_before_admission_batches_per_tile_and_preserves_old_window(
    store, monkeypatch
):
    bounded(store)
    tile, slots = fixture()
    value = codec.public(tile, 60000, 0, slots[0][0][3])
    with store.db:
        reference = market.window(store.db, "spot/BTCUSDT/1m", [value, value], tile)
    writer(store)
    calls = []
    original = blocks.put

    def tracked(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(blocks, "put", tracked)
    values = [{**value, "start": tile + i * 60000, "server_ms": tile + 1000 * 60000} for i in range(512)]
    with store.db:
        coordinates = market.admit_many(store.db, "spot/BTCUSDT/1m", values)
    assert len(calls) == 17  # one old tile transfer plus16 batch publications
    assert len(coordinates) == 512
    assert coordinates[0][2] == 1  # public timestamps differ from frozen old candle
    assert store.db.execute("SELECT COUNT(*) FROM market_candles").fetchone()[0] == 0
    assert (
        market.restore_window(store.db, reference, cache=store.cached_bytes)
        == encode([value, value]).encode()
    )
    assert len(market.read(store.db, "spot/BTCUSDT/1m")) == 512
    with closing(Store(store.path, store.owner_id)) as reopened:
        with reopened.db:
            assert market.admit(reopened.db, "spot/BTCUSDT/1m", value)[2] == 0


@pytest.mark.parametrize("legacy", [False, True])
def test_source_capacity_fallback_keeps_all_revisions_global_numbering_leases_and_public_values(
    store, legacy
):
    bounded(store)
    tile, slots = fixture()
    value = codec.public(tile, 60000, 0, slots[0][0][3])
    if not legacy:
        writer(store)
    values = [{**value, "close": 100.5 + i / 1000} for i in range(256)]
    with store.db:
        initial = market.admit_many(store.db, "spot/BTCUSDT/1m", values)
    if legacy:
        writer(store)
    next_value = {**value, "close": 101.5}
    with store.db:
        sid, _, revision = market.admit(store.db, "spot/BTCUSDT/1m", next_value)
        assert revision == 256
        assert market.admit(store.db, "spot/BTCUSDT/1m", next_value)[2] == 256
        assert market.admit(store.db, "spot/BTCUSDT/1m", values[0], tile + 40 * 86400000)[2] == 0
    assert encode(market.read(store.db, "spot/BTCUSDT/1m")) == encode([next_value])
    lookup = access.Lookup(store.db, sid, store.cached_bytes)
    for coordinate, expected in zip(initial, values, strict=True):
        assert encode(lookup.point(*coordinate[1:])) == encode(expected)
    assert encode(lookup.point(tile, 256)) == encode(next_value)
    assert store.db.execute("SELECT COUNT(*) FROM market_candle_fallbacks").fetchone()[0] == 1
    retained = (
        blocks.summary(store.db)["versions"]
        + store.db.execute("SELECT COUNT(*) FROM market_candles").fetchone()[0]
    )
    assert retained == 257
    old = blocks.tile(store.db, sid, tile, 60000)
    if old is None:
        lease = tuple(
            store.db.execute(
                "SELECT needed_until,purge_after FROM market_candles WHERE stream=? AND start=? AND revision=0",
                (sid, tile),
            ).fetchone()
        )
    else:
        lease = codec.point(old, sid, tile, 0)[1:]
    assert lease == (tile + 40 * 86400000, tile + 70 * 86400000)


def test_source_batch_rolls_back_all_tiles_after_late_failure_and_refuses_shortened_lease(store):
    bounded(store)
    writer(store)
    tile, slots = fixture()
    value = codec.public(tile, 60000, 0, slots[0][0][3])
    with store.db:
        store.db.execute(
            f"CREATE TRIGGER fail_second_tile BEFORE INSERT ON market_candle_blocks WHEN NEW.tile>{tile} BEGIN SELECT RAISE(ABORT,'late source failure'); END"
        )
    before = list(store.db.iterdump())
    with pytest.raises(sqlite3.IntegrityError, match="late source failure"), store.db:
        market.admit_many(store.db, "spot/BTCUSDT/1m", [value, {**value, "start": tile + 32 * 60000}])
    assert list(store.db.iterdump()) == before
    with store.db:
        sid, _, _ = market.admit(store.db, "spot/BTCUSDT/1m", value, tile)
    old = blocks.tile(store.db, sid, tile, 60000)
    items = codec.unpack(old)[3]
    items[0][0][1] = tile - 1
    with pytest.raises(sqlite3.OperationalError), store.db:
        blocks.put(store.db, codec.pack(sid, tile, 60000, items), old)
    assert blocks.tile(store.db, sid, tile, 60000) == old


def test_mixed_source_analytics_and_retention_equal_legacy_and_all_layout_counters_are_visible(
    store, tmp_path
):
    from cryptowatcher.analytics import cohorts
    from cryptowatcher.events import History
    from cryptowatcher.outcomes import OutcomeWorker, Retention
    from cryptowatcher.sources import Sources
    from cryptowatcher.storage_meter import report
    from tests.test_outcomes import DAY, MINUTE, NOW, future, observe

    bounded(store)
    observe(store, OutcomeWorker(store))
    path = tmp_path / "mixed.sqlite3"
    with closing(sqlite3.connect(path)) as destination:
        store.db.backup(destination)
    with closing(Store(path, store.owner_id)) as mixed:
        mixed.clock_ms = store.clock_ms
        writer(mixed)
        for instance in (store, mixed):
            Sources(instance).put("spot/BTCUSDT/1m", future())
            OutcomeWorker(instance).work(NOW + 6 * MINUTE)
        for kind in ("event:", "event-head:", "outcome:", "outcome-head:", "comparison:"):
            assert encode(History(mixed).items(kind)) == encode(History(store).items(kind))
        assert encode(cohorts(mixed)) == encode(cohorts(store))
        source_report = report(mixed, NOW)["source_layout"]
        assert (
            source_report["total_versions"]
            == store.db.execute("SELECT COUNT(*) FROM market_candles").fetchone()[0]
        )
        assert source_report["versions"] > 0
        assert mixed.db.execute("SELECT count(*) FROM market_candle_gc").fetchone()[0] == 0
        for days in (30 - 1 / DAY, 30 + 1 / DAY, 31, 36, 400):
            for instance in (store, mixed):
                Retention(instance).work(int(NOW + days * DAY))
            assert encode(cohorts(mixed)) == encode(cohorts(store))
            assert encode(History(mixed).events(100)) == encode(History(store).events(100))
        assert blocks.summary(mixed.db)["versions"] == 0
        assert report(mixed, NOW + 400 * DAY)["source_layout"]["total_versions"] == 0


def test_expired_fallback_metadata_is_collected_with_counters_but_retained_sources_are_kept(store):
    import time

    from cryptowatcher.outcomes import Retention

    bounded(store)
    writer(store)
    tile, slots = fixture()
    base = codec.public(tile, 60000, 0, slots[0][0][3])
    with store.db:
        market.admit_many(
            store.db, "spot/BTCUSDT/1m", [{**base, "close": 100.5 + i / 1000} for i in range(257)]
        )
    assert blocks.summary(store.db)["fallback_tiles"] == 1
    with store.db:
        assert blocks.collect_fallbacks(store.db, 4, time.monotonic() + 5) == 0
    assert blocks.summary(store.db)["fallback_tiles"] == 1
    Retention(store).work(tile + 40 * 86400000)
    assert blocks.summary(store.db)["fallback_tiles"] == 0
    assert store.db.execute("SELECT count(*) FROM market_candles").fetchone()[0] == 0
    with store.db:
        store.db.execute("DELETE FROM market_counts WHERE category='source_block_indexes'")
        store.db.execute("UPDATE market_candle_block_totals SET fallbacks=1,index_bytes=40")
    with pytest.raises(ValueError, match="fallback counter"):
        blocks.summary(store.db)


@pytest.mark.parametrize("damage", [{"close": 1000.0}, {"step": 300000}, {"unknown": "field"}])
def test_source_batch_validates_late_invalid_values_before_any_tile_or_fallback_write(store, damage):
    bounded(store)
    writer(store)
    tile, slots = fixture()
    value = codec.public(tile, 60000, 0, slots[0][0][3])
    with store.db:
        market.admit_many(
            store.db, "spot/BTCUSDT/1m", [{**value, "close": 100.5 + i / 1000} for i in range(256)]
        )
    before = list(store.db.iterdump())
    with pytest.raises((ValueError, TypeError)), store.db:
        market.admit_many(store.db, "spot/BTCUSDT/1m", [{**value, "close": 101.7}, {**value, **damage}])
    assert list(store.db.iterdump()) == before
    assert blocks.summary(store.db)["fallback_tiles"] == 0
