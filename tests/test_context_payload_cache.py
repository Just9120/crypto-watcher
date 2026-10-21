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


@pytest.mark.parametrize("field", ("identity", "stream", "kind", "day", "seal", "leases"))
def test_warm_payload_does_not_hide_changed_current_header_or_lease(store, field):
    tile = codec.pack(19, [point(0, frozen=True), point(1)])
    codec.stats(tile, store.cached_bytes)
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


@pytest.mark.parametrize("invalid", ("order", "timing", "mixed-stream"))
def test_new_validly_checksummed_payload_cannot_reuse_old_semantic_validation(store, invalid):
    tile = codec.pack(19, [point(0), point(1)])
    codec.stats(tile, store.cached_bytes)
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
