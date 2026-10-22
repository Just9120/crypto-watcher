import hashlib
import random
import sqlite3
import struct
import uuid
from contextlib import closing

import pytest
import zstandard

from cryptowatcher import (
    domain_packets,
    indexed_frame,
    packet_bundles,
    packet_store,
    projection_recipes,
    record_blocks,
    scalar_units,
    shared_records,
)
from cryptowatcher import scalar_body_frame as scalar
from cryptowatcher import shared_body_frame as shared
from cryptowatcher.compact_json import integer, pack, unpack
from cryptowatcher.storage import Store
from tests.test_record_blocks import entries
from tests.test_shared_records import equal, setup


def rows(count=128):
    rng = random.Random(861)
    return [
        {
            "times": [1700000040000 + rng.randrange(100000) * 60000 for _ in range(80)],
            "slot": i,
            "unknown": [None, -0.0, 0.0, True, -(2**100), "\x0cтест", "a" * 64],
        }
        for i in range(count)
    ]


def seal(checked):
    return scalar.MAGIC + hashlib.sha256(checked).digest() + checked


def declared(stored, deltas):
    offsets = [0]
    for part in stored:
        offsets.append(offsets[-1] + len(part))
    delta_frame = zstandard.ZstdCompressor(write_checksum=True).compress(
        struct.pack(">" + "I" * len(deltas), *deltas)
    )
    checked = struct.pack(">HIIH", len(stored), offsets[-1] + sum(deltas), offsets[-1], len(delta_frame))
    checked += struct.pack(">" + "I" * len(offsets), *offsets) + delta_frame
    checked += zstandard.ZstdCompressor(write_checksum=True).compress(b"".join(stored))
    return seal(checked)


def test_unit_transform_preserves_every_typed_byte_and_unknown_fields():
    values = rows(20) + [{"v": (2**8100 * 60000), "uuid": "0c000000-0000-0000-0000-00000000000c"}]
    for value in values:
        original = pack(value)
        encoded = scalar_units.transform(original, shared.MAX_BYTES)
        assert len(encoded) <= len(original)
        restored = scalar_units.transform(encoded, len(original), inverse=True)
        assert restored == original and pack(unpack(restored, shared.MAX_BYTES)) == original
    raw = pack(1700000040000)
    noncanonical = raw[:-1] + bytes([raw[-1] | 128, 0])
    assert scalar_units.transform(noncanonical, 100) == noncanonical
    assert scalar_units.transform(noncanonical, 100, inverse=True) == noncanonical


@pytest.mark.parametrize(
    "raw,inverse",
    [
        (b"\x0c" + integer(30_000_000), False),
        (b"\x0c\x01", True),
        (b"\x0c\x80\x80\x80\x80\x00", True),
        (b"\x04short", True),
        (b"\x05\x7fshort", False),
        (b"\x06\x01" * 66 + b"\0", True),
        (b"\x0d", True),
        (b"\0\0", True),
        (b"\x0b\x01\x06\x00", True),
    ],
)
def test_unit_transform_rejects_malformed_or_noncanonical_new_tokens(raw, inverse):
    with pytest.raises(ValueError):
        scalar_units.transform(raw, shared.MAX_BYTES, inverse=inverse)


def test_unit_expansion_checks_budget_before_append():
    original = pack(1700000040000)
    encoded = scalar_units.transform(original, 100)
    assert len(encoded) < len(original)
    with pytest.raises(ValueError, match="expansion bound"):
        scalar_units.transform(encoded, len(encoded), inverse=True)


def test_sb2_authenticates_original_sizes_and_preserves_all_data_views_and_fresh_objects(store):
    values = rows()
    originals = [pack(value) for value in values]
    frame = scalar.pack(originals)
    offsets, raw = shared.data(frame, store.cached_bytes)
    assert raw == b"".join(originals) and offsets[-1] == len(raw)
    assert shared.layout(frame)[0] == offsets
    for first in (0, 64):
        view = shared.view(frame, first, 64)
        assert shared.parts(view, store.cached_bytes) == originals[first : first + 64]
        for slot in (0, 1, 32, 63):
            assert pack(shared.select(view, slot, store.cached_bytes)) == originals[first + slot]
    changed = shared.select(shared.view(frame, 0, 1), 0, store.cached_bytes)
    changed["times"].clear()
    assert pack(shared.select(shared.view(frame, 0, 1), 0, store.cached_bytes)) == originals[0]
    assert store.reference_cache_bytes <= 1024**2
    damaged = frame[:-1] + bytes([frame[-1] ^ 1])
    with pytest.raises(ValueError, match="checksum"):
        shared.select(shared.VIEW_MAGIC + struct.pack(">HH", 0, 1) + damaged, 0, store.cached_bytes)


def test_optional_writer_keeps_sb1_when_no_saving_and_uses_smaller_sb2():
    old = shared.pack_frames([indexed_frame.pack([None])])
    assert shared.pack_frames([indexed_frame.pack([None])], scalar_units=True) == old
    values = rows()
    frames = [indexed_frame.pack(values[start : start + 64]) for start in (0, 64)]
    baseline = shared.pack_frames(frames)
    candidate = shared.pack_frames(frames, scalar_units=True)
    assert candidate.startswith(scalar.MAGIC) and len(candidate) < len(baseline)
    assert shared.data(candidate) == shared.data(baseline)


def test_sb2_full2048slots_preserve_old_limits_and_cannot_change_frozen_rp1_format(store):
    originals = [pack({"at": 1700000040000 + i * 60000}) for i in range(shared.MAX_RECORDS)]
    frame = scalar.pack(originals)
    assert len(shared.layout(frame)[0]) == 2049
    for first in (0, 1024, 1984):
        view = shared.view(frame, first, 64)
        assert shared.parts(view, store.cached_bytes) == originals[first : first + 64]
    with pytest.raises(ValueError, match="projection recipe version"):
        projection_recipes.layout(projection_recipes.MAGIC + frame)
    assert store.reference_cache_bytes <= 1024**2


def test_bad_restoration_declaration_fails_after_warm_cache_and_allocation_precedes_decompression(
    store, monkeypatch
):
    original = pack(1700000040000)
    encoded = scalar_units.transform(original, 100)
    frame = declared([encoded], [len(original) - len(encoded)])
    assert shared.select(shared.view(frame, 0, 1), 0, store.cached_bytes) == 1700000040000
    malformed = declared([encoded], [len(original) - len(encoded) + 1])
    with pytest.raises(ValueError, match="size mismatch"):
        shared.select(shared.view(malformed, 0, 1), 0, store.cached_bytes)
    monkeypatch.setattr(zstandard, "ZstdDecompressor", lambda **kwargs: pytest.fail("unbounded allocation"))
    for checked in (
        struct.pack(">H", shared.MAX_RECORDS + 1) + frame[38:],
        frame[36:38] + struct.pack(">I", shared.MAX_BYTES + 1) + frame[42:],
    ):
        with pytest.raises(ValueError, match="allocation bound"):
            shared.layout(seal(checked))


@pytest.mark.parametrize("invalid", ["trailing", "missing", "checksum", "size", "delta"])
def test_bounded_metadata_and_zstd_checks_are_not_skipped(invalid):
    frame = scalar.pack([pack(value) for value in rows(2)])
    count, original, stored, size = struct.unpack(">HIIH", frame[36:48])
    offset_end = 48 + (count + 1) * 4
    delta_end = offset_end + size
    if invalid == "trailing":
        damaged = seal(frame[36:] + b"extra")
    elif invalid == "missing":
        damaged = seal(frame[36:offset_end])
    elif invalid == "checksum":
        data = frame[offset_end:delta_end]
        damaged = seal(frame[36:offset_end] + data[:-1] + bytes([data[-1] ^ 1]) + frame[delta_end:])
    elif invalid == "size":
        damaged = seal(struct.pack(">HIIH", count, original + 1, stored, size) + frame[48:])
    else:
        delta = zstandard.ZstdCompressor(write_checksum=True).compress(struct.pack(">II", 2**32 - 1, 0))
        damaged = seal(
            struct.pack(">HIIH", count, original, stored, len(delta))
            + frame[48:offset_end]
            + delta
            + frame[delta_end:]
        )
    with pytest.raises(ValueError):
        shared.select(shared.VIEW_MAGIC + struct.pack(">HH", 0, 1) + damaged, 0)


def test_actual_shared_publication_and_reopen_keep_original_bytes_and_ownership(store):
    import time

    from cryptowatcher.storage import IntegrityError, check_integrity

    setup(store)
    data = rows(16)
    values = []
    for i, (coordinate, value, head) in enumerate(entries()):
        values.append((coordinate, {**value, "retained_unknown": data}, head))
    original = record_blocks.pack(values)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        packet = packet_store.append(store.db, "detail_packets", 131, original)
        packet_store.retain(store.db, packet)
    store.db.execute("BEGIN")
    try:
        prepared = shared_records.prepare(store.db, [packet], scalar_units=True)
    finally:
        store.db.rollback()
    assert prepared.body.startswith(scalar.MAGIC)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        group = shared_records.publish(store.db, prepared, store.cached_bytes)
    equal(original, domain_packets.read(store.db, packet, store.cached_bytes))
    with closing(Store(store.path, store.owner_id, readonly=True)) as reopened:
        equal(original, domain_packets.read(reopened.db, packet, reopened.cached_bytes))
    backup_path = store.path.with_name("scalar-backup.sqlite3")
    assert store.backup(backup_path, timeout_seconds=10)["phase"] == "COMPLETE"
    with closing(Store(backup_path, store.owner_id)) as backup:
        body = backup.db.execute("SELECT body FROM market_shared_records WHERE id=?", (group,)).fetchone()[0]
        with backup.db, backup.db.blobopen("market_shared_records", "body", group) as blob:
            blob.seek(len(body) - 1)
            blob.write(bytes([body[-1] ^ 1]))
    with closing(sqlite3.connect(backup_path)) as checked:
        with pytest.raises(IntegrityError) as failure:
            check_integrity(checked, deadline=time.monotonic() + 10)
        assert failure.value.code == "DATABASE_CORRUPT"
    with store.db:
        packet_store.release(store.db, packet)
        packet_store.collect(store.db)
        assert shared_records.collect(store.db) == 1
    assert store.db.execute("SELECT 1 FROM market_shared_records WHERE id=?", (group,)).fetchone() is None


def test_partial_sb2_publication_gc_and_pinned_reader_preserve_original_values(store):
    from cryptowatcher import shared_record_work as work
    from cryptowatcher.reset_history import private_state, signature

    old_packets = setup(store)
    private = signature(private_state(store))
    packet_bundles.install(store.db)
    values = [
        (coordinate, {**value, "retained_unknown": rows(16)}, head) for coordinate, value, head in entries()
    ]
    originals = []
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        for selected in (values[:2], values[2:]):
            raw = record_blocks.pack(selected)
            packet = packet_store.append(store.db, "detail_packets", 132, raw)
            packet_store.retain(store.db, packet)
            originals.append((packet, raw))
        for packet, _ in old_packets + originals:
            packet_bundles.checked(store.db, packet)
    store.clock_ms = lambda: 1_800_000_000_000
    work.install(store.db, store.clock_ms(), store.cached_bytes)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        work.scan(store.db, store.clock_ms())
    with store.db:
        store.db.execute("BEGIN")
        prepared = shared_records.prepare(store.db, [packet for packet, _ in originals], scalar_units=True)
    assert prepared.body.startswith(scalar.MAGIC)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        group = work.stage(store.db, prepared)
        assert work.advance(store.db, store.cached_bytes) == originals[0][0]
    with closing(Store(store.path, store.owner_id, readonly=True)) as snapshot:
        snapshot.db.execute("BEGIN")
        equal(originals[0][1], domain_packets.read(snapshot.db, originals[0][0], snapshot.cached_bytes))
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            packet_store.release(store.db, originals[1][0])
            packet_store.collect(store.db)
            assert shared_records.collect(store.db) == 0
        assert work.header(store.db)["pending"] == 0
        assert tuple(
            store.db.execute("SELECT kind FROM market_shared_reclaim WHERE group_id=?", (group,)).fetchone()
        ) == ("sparse",)
        equal(originals[0][1], domain_packets.read(store.db, originals[0][0], store.cached_bytes))
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            packet_store.release(store.db, originals[0][0])
            packet_store.collect(store.db)
            assert shared_records.collect(store.db) == 1
        # The snapshot owns its old bytes and old SQL bindings through both
        # pending-member retirement and final group collection.
        for packet, original in originals:
            equal(original, domain_packets.read(snapshot.db, packet, snapshot.cached_bytes))
        assert store.path.with_name(store.path.name + "-wal").stat().st_size > 0
    assert signature(private_state(store)) == private
    assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()
    for packet, original in old_packets:
        equal(original, domain_packets.read(store.db, packet, store.cached_bytes))
    assert store.db.execute("SELECT 1 FROM market_shared_records WHERE id=?", (group,)).fetchone() is None


def test_original_ceiling_singleton_preserves_exact_packet_publication_backup_and_gc(store):
    from cryptowatcher import shared_record_work as work
    from cryptowatcher.shared_record_worker import read_batch

    setup(store)
    packet_bundles.install(store.db)
    coordinate, value, head = entries()[-1]
    entropy = random.Random(9947).randbytes(700000)
    low, high = 100000, len(entropy)
    original = None
    while low <= high:
        size = (low + high) // 2
        try:
            raw = record_blocks.pack(
                [(coordinate, {**value, "retained_unknown": entropy[:size].hex()}, head)]
            )
        except indexed_frame.FrameSizeError:
            high = size - 1
        else:
            original = raw
            low = size + 1
    assert original is not None and len(original) > record_blocks.MAX_PACKET - 256
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        packet = packet_store.append(store.db, "detail_packets", 132, original)
        packet_store.retain(store.db, packet)
        packet_bundles.checked(store.db, packet)
    now = 1_800_000_000_000
    store.clock_ms = lambda: now
    work.install(store.db, now, store.cached_bytes)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        work.scan(store.db, now)
    prepared = read_batch(store.path, store.owner_id, [packet], now, scalar_units=True)
    assert prepared is not None
    assert prepared.body.startswith(indexed_frame.MAGIC) and prepared.projection.startswith(
        indexed_frame.MAGIC
    )
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        group = work.stage(store.db, prepared)
        assert group is not None and work.advance(store.db, store.cached_bytes) == packet
    assert domain_packets.read(store.db, packet, store.cached_bytes) == original
    with closing(Store(store.path, store.owner_id, readonly=True)) as reopened:
        assert domain_packets.read(reopened.db, packet, reopened.cached_bytes) == original
    backup = store.path.with_name("ceiling-backup.sqlite3")
    assert store.backup(backup, timeout_seconds=10)["phase"] == "COMPLETE"
    with closing(Store(backup, store.owner_id, readonly=True)) as restored:
        assert domain_packets.read(restored.db, packet, restored.cached_bytes) == original
    with store.db:
        packet_store.release(store.db, packet)
        packet_store.collect(store.db)
        assert shared_records.collect(store.db) == 1
    assert store.db.execute("SELECT 1 FROM market_shared_records WHERE id=?", (group,)).fetchone() is None


@pytest.mark.parametrize("damage", ["multiple_members", "body_only", "projection_only"])
def test_original_frame_fallback_requires_one_member_and_both_original_frames(damage):
    raw = record_blocks.pack(entries()[:2])
    projection, body = record_blocks.frames(raw)
    initial = 1
    if damage == "multiple_members":
        initial = 2
    elif damage == "body_only":
        projection = projection_recipes.pack_frames([raw])
    else:
        body = shared.pack_frames([body])
    with pytest.raises(ValueError, match="paired singleton"):
        shared_records.signature("detail_packets", 132, body, projection, initial)


def test_original_singleton_blob_damage_is_detected_after_warm_cache_without_sql_changes(store):
    originals = setup(store)
    packet, original = originals[0]
    with store.db:
        store.db.execute("BEGIN")
        captured = shared_records.prepare(store.db, [packet])
    projection, body = record_blocks.frames(original)
    prepared = shared_records.Prepared(captured.members, body, projection)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        group = shared_records.publish(store.db, prepared, store.cached_bytes)
    assert domain_packets.read(store.db, packet, store.cached_bytes) == original
    changes = store.db.total_changes
    with store.db.blobopen("market_shared_records", "body", group) as blob:
        blob.seek(4)
        before = blob.read(1)
        blob.seek(4)
        blob.write(bytes([before[0] ^ 1]))
    assert store.db.total_changes == changes
    with pytest.raises(ValueError):
        domain_packets.read(store.db, packet, store.cached_bytes)


def test_readonly_preparation_preflights_full_logical_packet_bound_before_main_publication(store):
    from cryptowatcher.shared_record_worker import read_batch

    setup(store)
    packet_bundles.install(store.db)
    rng = random.Random(771)
    _, original_value, original_head = entries()[0]
    originals = []
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        for i in range(3):
            coordinate = record_blocks.Coordinate("event", str(uuid.UUID(int=9000 + i)), 1)
            value = {
                **original_value,
                coordinate.field: coordinate.identity,
                "unknown_body": rng.randbytes(150000).hex(),
            }
            head = {**original_head, coordinate.field: coordinate.identity, "revision_key": coordinate.key}
            if i == 0:
                head["unknown_head"] = rng.randbytes(150000).hex()
            raw = record_blocks.pack([(coordinate, value, head)])
            identity = packet_store.append(store.db, "detail_packets", 131, raw)
            packet_store.retain(store.db, identity)
            packet_bundles.checked(store.db, identity)
            originals.append((identity, raw))
    changes = store.db.total_changes
    body = shared.pack_frames([record_blocks.frames(raw)[1] for _, raw in originals])
    projection = projection_recipes.pack_frames([raw for _, raw in originals])
    with pytest.raises(indexed_frame.FrameSizeError, match="shared domain block bound"):
        record_blocks.shared_packet(projection_recipes.restore(projection, 0, 1), body, 0, 1)
    prepared = read_batch(store.path, store.owner_id, [i for i, _ in originals], 1800000000000)
    assert prepared is not None and len(prepared.members) < len(originals)
    assert store.db.total_changes == changes
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        shared_records.publish(store.db, prepared, store.cached_bytes)
    for identity, raw in originals:
        equal(raw, domain_packets.read(store.db, identity, store.cached_bytes))
