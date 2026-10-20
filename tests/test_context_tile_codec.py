from dataclasses import replace

import pytest

from cryptowatcher import context_tile_codec as codec
from cryptowatcher import indexed_frame, numeric_block_codec, owner_slots
from cryptowatcher.market_storage import DAY
from tests.test_outcomes import NOW

BASE = NOW // DAY * DAY + 600000


def point(index=0, *, frozen=False):
    at = BASE + index * 60000
    value = codec.Point(
        7,
        "ticker",
        at,
        {
            "values": {"symbol": "BTCUSDT", "lastPrice": str(100 + index), "zero": -0.0},
            "extra": ["тест", None, False, 0, 0.0, True, {"version": 1}],
        },
        {"available_at_ms": at, "market_observation_ms": at - 13},
        0,
        at + 35 * DAY,
    )
    return replace(value, integrity=codec.digest(value)) if frozen else value


def test_context_tile_exact_values_frozen_hashes_and_leases_share_existing_bounded_cache(store):
    expected = [point(index, frozen=index % 3 == 0) for index in range(64)]
    tile = codec.pack(17, expected)
    assert codec.signature(codec.points(tile, store.cached_bytes)) == codec.signature(expected)
    assert codec.stats(tile) == {
        "identity": 17,
        "stream": 7,
        "kind": "ticker",
        "day": BASE // DAY,
        "count": 64,
        "first_at": BASE,
        "last_at": BASE + 63 * 60000,
        "first_due": BASE + 35 * DAY,
        "frozen": 22,
    }
    assert tile.leases.startswith((b"NR1\x00", b"NR2\x00"))
    selected = codec.get(tile, 0, store.cached_bytes)
    selected.stable["values"]["zero"] = 0
    assert codec.signature([codec.get(tile, 0, store.cached_bytes)]) == codec.signature(expected[:1])
    assert store.reference_cache_bytes <= 1024 * 1024
    damaged = replace(tile, payload=tile.payload[:-1] + bytes([tile.payload[-1] ^ 1]))
    with pytest.raises(ValueError, match="checksum"):
        codec.get(damaged, 0, store.cached_bytes)


def test_bulk_validation_decodes_each_lease_vector_and_frame_once(monkeypatch):
    tile = codec.pack(1, [point(index, frozen=True) for index in range(64)])
    leases, data = numeric_block_codec.leases, indexed_frame.data
    calls = {"leases": 0, "frame": 0}

    def read_leases(*args):
        calls["leases"] += 1
        return leases(*args)

    def read_frame(*args):
        calls["frame"] += 1
        return data(*args)

    monkeypatch.setattr(numeric_block_codec, "leases", read_leases)
    monkeypatch.setattr(indexed_frame, "data", read_frame)
    expected = [point(index, frozen=True) for index in range(64)]
    assert codec.signature(codec.points(tile)) == codec.signature(expected)
    assert calls == {"leases": 1, "frame": 1}


def test_late_anchor_and_purge_renewal_are_monotonic_without_changing_immutable_frame():
    tile = codec.pack(1, [point(0, frozen=True), point(1)])
    later = BASE + 28 * DAY
    updated = codec.renew(tile, 0, anchor=later, purge=later + 30 * DAY)
    assert updated.payload is tile.payload and updated.frozen == tile.frozen and updated.seal == tile.seal
    assert codec.signature(codec.points(updated)) == codec.signature(
        [
            replace(point(0, frozen=True), anchor=later, purge=later + 30 * DAY),
            point(1),
        ]
    )
    assert codec.renew(updated, 0, anchor=0, purge=0) is updated
    with pytest.raises(ValueError):
        codec.renew(updated, 0, anchor=True, purge=0)
    assert codec.get(tile, 0).anchor == 0


def test_late_freeze_preserves_values_and_old_hash_and_rejects_wrong_expected_hash():
    tile = codec.pack(2, [point(), point(1, frozen=True)])
    with pytest.raises(ValueError, match="freeze hash"):
        codec.freeze(tile, 0, bytes(32))
    updated = codec.freeze(tile, 0, codec.digest(point()))
    assert updated.payload is tile.payload and updated.leases is tile.leases
    assert codec.signature(codec.points(updated)) == codec.signature(
        [point(frozen=True), point(1, frozen=True)]
    )
    assert codec.freeze(updated, 0, codec.digest(point())) is updated
    assert codec.get(tile, 0).integrity is None


def test_partial_expiry_preserves_other_frozen_points_and_extended_dependency_then_releases_all():
    late = replace(point(2, frozen=True), anchor=BASE + 37 * DAY, purge=BASE + 68 * DAY)
    tile = codec.pack(3, [point(0, frozen=True), point(1), late])
    updated = codec.expire(tile, BASE + 35 * DAY, candle_days=35, detail_days=30)
    assert updated is not None and updated.identity == tile.identity
    assert codec.signature(codec.points(updated)) == codec.signature([point(1), late])
    assert codec.stats(updated)["frozen"] == 1 and codec.stats(tile)["count"] == 3
    assert codec.expire(updated, BASE + 70 * DAY, candle_days=35, detail_days=30) is None


def test_retention_increase_renews_due_boundary_and_does_not_shorten_existing_longer_lease():
    tile = codec.pack(4, [point(0, frozen=True), point(1)])
    assert codec.expire(tile, BASE + 34 * DAY, candle_days=35, detail_days=30) is tile
    updated = codec.expire(tile, BASE + 36 * DAY, candle_days=40, detail_days=30)
    assert updated is not None and updated.payload == tile.payload
    assert codec.get(updated, 0).purge == BASE + 40 * DAY
    assert codec.expire(updated, BASE + 36 * DAY, candle_days=35, detail_days=30) is updated


@pytest.mark.parametrize("change", ["identity", "stream", "kind", "day", "seal", "mask", "hidden", "leases"])
def test_bad_header_or_lease_cannot_be_read_as_another_point(change):
    tile = codec.pack(5, [point(0, frozen=True)])
    changes = {
        "identity": {"identity": 6},
        "stream": {"stream": 9},
        "kind": {"kind": "instrument"},
        "day": {"day": tile.day + 1},
        "seal": {"seal": bytes(32)},
        "mask": {
            "frozen": owner_slots.mask([63]),
            "seal": owner_slots.signature(7, 5, owner_slots.mask([63])),
        },
        "hidden": {"leases": numeric_block_codec.counts(5, [0, 1] + [0] * 62, [point().purge] + [0] * 63)},
        "leases": {"leases": tile.leases[:-1] + bytes([tile.leases[-1] ^ 1])},
    }
    with pytest.raises(ValueError):
        codec.get(replace(tile, **changes[change]), 0)


@pytest.mark.parametrize("slot", [-1, 1, 64, True, "0"])
def test_invalid_slot_cannot_alias_retained_context(slot):
    with pytest.raises(ValueError):
        codec.get(codec.pack(6, [point()]), slot)


@pytest.mark.parametrize("change", ["duplicate", "order", "stream", "kind", "day", "hash", "time"])
def test_pack_rejects_unsupported_or_conflicting_points_without_mutating_input(change):
    rows = [point(), point(1, frozen=True)]
    if change == "duplicate":
        rows[1] = rows[0]
    elif change == "order":
        rows.reverse()
    else:
        differences = {
            "stream": {"stream": 8},
            "kind": {"kind": "instrument"},
            "day": {"at": rows[1].at + DAY},
            "hash": {"integrity": bytes(32)},
            "time": {"timing": {"available_at_ms": True}},
        }
        rows[1] = replace(rows[1], **differences[change])
    original = codec.signature(rows)
    with pytest.raises(ValueError):
        codec.pack(7, rows)
    assert codec.signature(rows) == original


def test_expanded_admission_bound_and_all_slot_order_are_checked_before_publication():
    with pytest.raises(indexed_frame.FrameSizeError):
        codec.pack(8, [replace(point(), stable={"value": "x" * codec.MAX_EXPANDED})])
    tile = codec.pack(8, [point(), point(1)])
    values = indexed_frame.all_values(tile.payload)
    forged = replace(tile, payload=indexed_frame.pack([values[1], values[0]]))
    with pytest.raises(ValueError, match="Unordered"):
        codec.stats(forged)
