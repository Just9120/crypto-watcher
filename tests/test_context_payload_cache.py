import sqlite3
import struct
from dataclasses import replace

import pytest

from cryptowatcher import context_tile_codec as codec
from cryptowatcher import context_tiles as tiles
from cryptowatcher import indexed_frame, numeric_block_codec, owner_slots
from cryptowatcher.market_storage import DAY
from tests.test_context_tile_codec import BASE, point
from tests.test_context_tiles import setup


def test_hot_guard_and_cold_lookup_share_only_the_authenticated_immutable_timestamp_index(store, monkeypatch):
    keys, _, _ = setup(store, 64, freeze=False)
    tiles.publish(store.db, keys)
    payload = bytes(store.db.execute("SELECT payload FROM market_context_tiles WHERE id=1").fetchone()[0])
    store.reference_cache.clear()
    store.reference_cache_bytes = 0
    decoded = 0
    original = codec.typed_unpack

    def observed(*args):
        nonlocal decoded
        decoded += 1
        return original(*args)

    monkeypatch.setattr(codec, "typed_unpack", observed)
    monkeypatch.setattr(indexed_frame, "typed_unpack", observed)
    for at in (keys[0][2], keys[-1][2], keys[-1][2] + 1, keys[0][2] - 1):
        assert store.db.execute("SELECT cw_context_tile_contains(?,?)", (payload, at)).fetchone()[0] == int(
            at in {key[2] for key in keys}
        )
    assert decoded == 64
    assert tiles.cold(store.db, *keys[0]) is not None
    assert decoded == 64
    assert not any(key[0] == "bytes/context-tile-values" for key in store.reference_cache)
    assert store.reference_cache_bytes <= 1024 * 1024
    corrupted = payload[:-1] + bytes([payload[-1] ^ 1])
    with pytest.raises(sqlite3.OperationalError):
        store.db.execute("SELECT cw_context_tile_contains(?,?)", (corrupted, keys[0][2])).fetchone()
    assert store.db.execute("SELECT cw_context_tile_contains(?,?)", (payload, keys[0][2])).fetchone()[0] == 1


def test_shared_guard_keeps_exact_misses_nearest_lookup_and_hot_points_between_retained_times(store):
    from cryptowatcher.market_storage import save_context
    from tests.test_context_tiles import values

    keys, _, _ = setup(store, 8)
    tiles.publish(store.db, keys[::2])
    assert tiles.cold(store.db, *keys[-1]) is None
    nearest = tiles.lookup(store.db, *keys[-1], exact=False)
    assert nearest.at == keys[-1][2]
    assert tiles.cold(store.db, *keys[1]) is None
    assert tiles.lookup(store.db, *keys[1]).at == keys[1][2]
    with store.db:
        save_context(store.db, "ticker", "spot/BTCUSDT", values(9), BASE)
    assert (
        tiles.lookup(store.db, keys[0][0], "ticker", values(9)["available_at_ms"]).at
        == values(9)["available_at_ms"]
    )
    assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("invalid", ("order", "timing", "mixed-stream"))
def test_hot_ownership_guard_rejects_a_new_malformed_payload_after_a_warm_index(store, invalid):
    tile = codec.pack(19, [point(0), point(1)])
    store.db.execute("SELECT cw_context_tile_contains(?,?)", (tile.payload, point(0).at)).fetchone()
    values = [codec._value(point(0)), codec._value(point(1))]
    if invalid == "order":
        values.reverse()
    elif invalid == "timing":
        values[1][4]["available_at_ms"] += 1
    else:
        values[1][0] += 1
    with pytest.raises(sqlite3.OperationalError):
        store.db.execute(
            "SELECT cw_context_tile_contains(?,?)", (indexed_frame.pack(values), point(0).at)
        ).fetchone()


def test_hot_ownership_guard_also_works_without_an_optional_cache(store):
    keys, _, _ = setup(store, 8)
    tiles.publish(store.db, keys)
    tiles.install_functions(store.db)
    payload = bytes(store.db.execute("SELECT payload FROM market_context_tiles WHERE id=1").fetchone()[0])
    assert store.db.execute("SELECT cw_context_tile_contains(?,?)", (payload, keys[0][2])).fetchone()[0] == 1
    assert (
        store.db.execute("SELECT cw_context_tile_contains(?,?)", (payload, keys[-1][2] + 1)).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize("field", ("payload", "leases"))
def test_exact_miss_still_checks_current_frame_and_lease_bytes_after_a_warm_read(store, field):
    keys, _, _ = setup(store, 8)
    tiles.publish(store.db, keys)
    assert tiles.cold(store.db, *keys[0]) is not None
    original = bytes(store.db.execute(f"SELECT {field} FROM market_context_tiles WHERE id=1").fetchone()[0])
    changes = store.db.total_changes
    with store.db.blobopen("market_context_tiles", field, 1) as blob:
        blob.write(original[:-1] + bytes([original[-1] ^ 1]))
    assert store.db.total_changes == changes
    try:
        with pytest.raises(ValueError):
            tiles.cold(store.db, keys[0][0], "ticker", keys[-1][2] + 1)
    finally:
        with store.db.blobopen("market_context_tiles", field, 1) as blob:
            blob.write(original)
    assert tiles.cold(store.db, keys[0][0], "ticker", keys[-1][2] + 1) is None
    assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_pure_payload_reuse_keeps_current_leases_masks_and_independent_returned_values(store, monkeypatch):
    expected = [point(i, frozen=i % 3 == 0) for i in range(64)]
    tile = codec.pack(19, expected)
    count = 0
    unpack = codec.typed_unpack

    def observed(*args):
        nonlocal count
        count += 1
        return unpack(*args)

    monkeypatch.setattr(codec, "typed_unpack", observed)
    assert codec.stats(tile, store.cached_bytes)["count"] == 64
    assert codec.times(tile, store.cached_bytes) == [p.at for p in expected]
    copy = codec.points(tile, store.cached_bytes)
    copy[0].stable["values"]["zero"] = 42
    assert codec.signature(codec.points(tile, store.cached_bytes)) == codec.signature(expected)
    assert count == 64
    renewed = codec.renew(tile, 0, anchor=BASE + DAY, purge=BASE + 40 * DAY)
    frozen = codec.freeze(renewed, 1, codec.digest(point(1)))
    assert codec.monotonic(tile, frozen, store.cached_bytes)
    assert not codec.monotonic(frozen, tile, store.cached_bytes)
    assert codec.stats(frozen, store.cached_bytes)["frozen"] == 23
    assert codec.stats(frozen, store.cached_bytes)["first_due"] == expected[1].purge
    assert codec.get(frozen, 0).anchor == BASE + DAY
    assert count == 64 and store.reference_cache_bytes <= 1024 * 1024


def test_times_only_cache_keeps_a_small_index_without_admitting_full_json_values(store, monkeypatch):
    expected = [point(i, frozen=i % 3 == 0) for i in range(64)]
    tile = codec.pack(19, expected)
    decoded = 0
    original = codec.typed_unpack

    def observed(*args):
        nonlocal decoded
        decoded += 1
        return original(*args)

    monkeypatch.setattr(codec, "typed_unpack", observed)
    times = codec.times(tile, store.cached_bytes)
    times[0] = -1
    for _ in range(3):
        assert codec.times(tile, store.cached_bytes) == [p.at for p in expected]
    assert decoded == 64
    assert not any(key[0] == "bytes/context-tile-values" for key in store.reference_cache)
    entries = [
        value for key, value in store.reference_cache.items() if key[0] == "bytes/context-tile-times-only-v1"
    ]
    assert len(entries) == 1 and len(entries[0][0]) == 9 + 64 * 8
    assert store.reference_cache_bytes <= 1024 * 1024


@pytest.mark.parametrize("field", ("identity", "stream", "kind", "day", "seal", "leases"))
def test_warm_payload_does_not_hide_changed_current_header_or_lease(store, field):
    tile = codec.pack(19, [point(0, frozen=True), point(1)])
    codec.stats(tile, store.cached_bytes)
    codec.times(tile, store.cached_bytes)
    changes = {
        "identity": {"identity": 20},
        "stream": {"stream": 8, "seal": owner_slots.signature(8, 19, tile.frozen)},
        "kind": {"kind": "instrument"},
        "day": {"day": tile.day + 1},
        "seal": {"seal": bytes(32)},
        "leases": {"leases": tile.leases[:-1] + bytes([tile.leases[-1] ^ 1])},
    }
    with pytest.raises(ValueError):
        codec.times(replace(tile, **changes[field]), store.cached_bytes)


def test_times_probe_never_converts_an_invalid_cached_value_into_bytes():
    tile = codec.pack(19, [point(0)])

    def invalid(kind, key, raw, read):
        return 5 if kind == "context-tile-values" else read()

    with pytest.raises(ValueError, match="pure context tile cache result"):
        codec.times(tile, invalid)


@pytest.mark.parametrize("invalid", ("order", "timing", "mixed-stream"))
def test_new_validly_checksummed_payload_cannot_reuse_old_semantic_validation(store, invalid):
    tile = codec.pack(19, [point(0), point(1)])
    codec.stats(tile, store.cached_bytes)
    codec.times(tile, store.cached_bytes)
    values = [codec._value(point(0)), codec._value(point(1))]
    if invalid == "order":
        values.reverse()
    elif invalid == "timing":
        values[1][4]["available_at_ms"] += 1
    else:
        values[1][0] += 1
    payload = indexed_frame.pack(values)
    with pytest.raises(ValueError):
        codec.stats(replace(tile, payload=payload), store.cached_bytes)
    with pytest.raises(ValueError):
        codec.times(replace(tile, payload=payload), store.cached_bytes)


def test_blobwrite_without_change_counter_is_checked_and_lease_rollback_remains_visible(store):
    keys, _, _ = setup(store, 8)
    tiles.publish(store.db, keys)
    before = tiles.lookup(store.db, *keys[0])
    store.db.execute("BEGIN")
    found = tiles.cold(store.db, *keys[0])
    tiles.renew(store.db, found, anchor=BASE + DAY, purge=BASE + 40 * DAY)
    assert tiles.lookup(store.db, *keys[0]).anchor == BASE + DAY
    store.db.rollback()
    assert tiles.lookup(store.db, *keys[0]) == before
    original = bytes(store.db.execute("SELECT payload FROM market_context_tiles WHERE id=1").fetchone()[0])
    changes = store.db.total_changes
    with store.db.blobopen("market_context_tiles", "payload", 1) as blob:
        blob.write(original[:-1] + bytes([original[-1] ^ 1]))
    assert store.db.total_changes == changes
    try:
        with pytest.raises(ValueError):
            tiles.lookup(store.db, *keys[0])
    finally:
        with store.db.blobopen("market_context_tiles", "payload", 1) as blob:
            blob.write(original)
    assert tiles.lookup(store.db, *keys[0]) == before
    assert store.reference_cache_bytes <= 1024 * 1024


def test_warm_payload_still_checks_new_lease_identity_and_noncanonical_varints(store):
    tile = codec.pack(19, [point(0), point(1)])
    codec.stats(tile, store.cached_bytes)
    invalid = numeric_block_codec.seal(b"NR1\0", struct.pack(">Q", 19) + b"\x80\0" + bytes(127))
    with pytest.raises(ValueError, match="Noncanonical"):
        codec.stats(replace(tile, leases=invalid), store.cached_bytes)
    with pytest.raises(ValueError, match="identity"):
        codec.stats(
            replace(tile, identity=20, seal=owner_slots.signature(7, 20, tile.frozen)), store.cached_bytes
        )
    assert codec.stats(tile, store.cached_bytes)["count"] == 2


def test_projection_keeps_sqlite_deadline_interrupt_distinct_from_bad_source(store):
    tile = codec.pack(19, [point(0)])
    store.db.set_progress_handler(lambda: 1, 1)
    try:
        with pytest.raises(sqlite3.OperationalError) as error:
            tiles._derived(store.db, "index", tile)
        assert error.value.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT
    finally:
        store.db.set_progress_handler(None, 0)
    assert tiles._derived(store.db, "index", tile) == [point(0).at]
