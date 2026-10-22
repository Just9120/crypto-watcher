import hashlib
import random
import struct
import uuid

import pytest
import zstandard

from cryptowatcher import indexed_frame
from cryptowatcher import record_blocks as blocks
from cryptowatcher import shared_body_frame as shared
from cryptowatcher.storage import encode
from tests.test_record_blocks import entries


def sealed(checked):
    return shared.MAGIC + hashlib.sha256(checked).digest() + checked


def packet_entries(number):
    result = []
    for coordinate, body, head in entries():
        newer = blocks.Coordinate(
            coordinate.kind,
            str(uuid.UUID(int=number * 100 + int(uuid.UUID(coordinate.identity)))),
            coordinate.revision,
        )
        body = {**body, coordinate.field: newer.identity, "unknown_packet_field": number}
        head = None if head is None else {**head, coordinate.field: newer.identity, "revision_key": newer.key}
        result.append((newer, body, head))
    return result


def test_shared_body_keeps_all2048_typed_rows_exact_without_widening_original_frame_format(store):
    frames = []
    values = []
    for packet in range(32):
        rows = [
            {"ordinal": packet * 64 + slot, "future": [-0.0, 0.0, 0, True, None, "тест", 2**100]}
            for slot in range(64)
        ]
        frames.append(indexed_frame.pack(rows))
        values.extend(rows)
    frame = shared.pack_frames(frames)
    assert len(shared.layout(frame)[0]) == 2049
    for first in range(0, 2048, 64):
        view = shared.view(frame, first, 64)
        for slot in range(64):
            assert encode(shared.select(view, slot, store.cached_bytes)) == encode(values[first + slot])
    selected = shared.select(shared.view(frame, 0, 64), 0, store.cached_bytes)
    selected["future"][0] = 99
    assert encode(shared.select(shared.view(frame, 0, 64), 0, store.cached_bytes)) == encode(values[0])
    assert store.reference_cache_bytes <= 1024 * 1024
    assert indexed_frame.MAX_RECORDS == blocks.MAX_RECORDS == 64
    with pytest.raises(ValueError):
        indexed_frame.layout(frame)


def test_all_domain_kinds_keep_coordinates_projections_unknown_fields_and_numeric_bits(store):
    values = [packet_entries(number) for number in range(1, 17)]
    packets = [blocks.pack(rows) for rows in values]
    frame = shared.pack_frames([blocks.frames(packet)[1] for packet in packets])
    for number, (original, rows) in enumerate(zip(packets, values)):
        packet = blocks.with_shared_body(original, frame, number * len(rows))
        assert packet.startswith(blocks.SHARED_MAGIC)
        assert blocks.frames(packet)[0] == blocks.frames(original)[0]
        assert [encode(row) for row in blocks.bodies(packet)] == [
            encode(row) for row in blocks.bodies(original)
        ]
        for slot, (coordinate, value, head) in enumerate(rows):
            assert encode(blocks.read(packet, slot, coordinate, store.cached_bytes)) == encode(value)
            assert encode(blocks.checked_projected(packet, slot, coordinate, store.cached_bytes)) == encode(
                head
            )
        assert blocks.with_shared_body(packet, frame, number * len(rows)) == packet
    with pytest.raises(ValueError, match="changed immutable"):
        blocks.with_shared_body(packets[0], frame, len(values[0]))
    with pytest.raises(ValueError, match="identity"):
        blocks.read(blocks.with_shared_body(packets[0], frame, 0), 0, values[1][0][0])


def test_large_frame_reuses_only_the_selected_view_and_rechecks_current_bytes(store, monkeypatch):
    frames = [
        indexed_frame.pack([{"sample": "x" * 700, "n": i + group * 64} for i in range(64)])
        for group in range(32)
    ]
    frame = shared.pack_frames(frames)
    assert len(shared.data(frame)[1]) > 1024 * 1024
    view = shared.view(frame, 128, 64)
    constructor = zstandard.ZstdDecompressor
    calls = []

    def decompress(*args, **kwargs):
        calls.append(1)
        return constructor(*args, **kwargs)

    monkeypatch.setattr(zstandard, "ZstdDecompressor", decompress)
    for slot in range(64):
        assert shared.select(view, slot, store.cached_bytes)["n"] == 128 + slot
    assert len(calls) == 1
    assert store.reference_cache_bytes <= 1024 * 1024
    damaged = view[:-1] + bytes([view[-1] ^ 1])
    with pytest.raises(ValueError, match="checksum"):
        shared.select(damaged, 0, store.cached_bytes)
    assert len(calls) == 1


def test_whole_body_replacement_checks_nonselected_original_fields_and_rejects_legacy_packet():
    values = packet_entries(1)
    original = blocks.pack(values)
    rows = indexed_frame.all_values(blocks.frames(original)[1])
    rows[-1]["unknown_packet_field"] = 99
    frame = shared.pack_frames([indexed_frame.pack(rows)])
    with pytest.raises(ValueError, match="changed immutable"):
        blocks.with_shared_body(original, frame, 0)
    with pytest.raises(ValueError, match="indexed domain"):
        blocks.with_shared_body(blocks.pack(values, legacy=True), frame, 0)


@pytest.mark.parametrize("first,count", [(-1, 1), (0, 0), (0, 65), (1, 2), (True, 1), (0, True), ("0", 1)])
def test_view_cannot_alias_other_slots_or_bypass64slot_owner_bound(first, count):
    frame = shared.pack_frames([indexed_frame.pack([{}, {}])])
    with pytest.raises(ValueError):
        shared.view(frame, first, count)


@pytest.mark.parametrize("slot", [-1, 1, True, "0"])
def test_select_rejects_noninteger_or_out_of_view_slot(slot):
    frame = shared.pack_frames([indexed_frame.pack([{}, {"next": True}])])
    view = shared.view(frame, 0, 1)
    with pytest.raises(ValueError):
        shared.select(view, slot)


@pytest.mark.parametrize(
    "change", ["checksum", "count", "size", "offset", "truncated", "compression", "extra"]
)
def test_current_frame_bytes_and_allocation_checks_apply_after_cache_hit(store, change):
    original = shared.pack_frames([indexed_frame.pack([{"x": -0.0}, {"x": 1.0}])])
    assert shared.select(shared.view(original, 0, 1), 0, store.cached_bytes) == {"x": -0.0}
    checked = original[36:]
    if change == "checksum":
        damaged = original[:-1] + bytes([original[-1] ^ 1])
    elif change == "count":
        damaged = sealed(struct.pack(">H", shared.MAX_RECORDS + 1) + checked[2:])
    elif change == "size":
        damaged = sealed(checked[:2] + struct.pack(">I", shared.MAX_BYTES + 1) + checked[6:])
    elif change == "offset":
        damaged = sealed(checked[:10] + bytes(4) + checked[14:])
    elif change == "truncated":
        damaged = sealed(checked[:8])
    elif change == "compression":
        damaged = sealed(checked[:18] + b"not a ZSTD frame")
    else:
        damaged = sealed(checked + b"extra")
    value = shared.VIEW_MAGIC + struct.pack(">HH", 0, 1) + damaged
    with pytest.raises(ValueError):
        shared.select(value, 0, store.cached_bytes)


def test_unchecked_zstd_frame_without_checksum_and_wrong_content_size_are_rejected():
    raw = b"\x00"
    header = struct.pack(">HI", 1, len(raw)) + struct.pack(">II", 0, len(raw))
    for compressed in (
        zstandard.ZstdCompressor(write_checksum=False).compress(raw),
        zstandard.ZstdCompressor(write_checksum=True).compress(raw + raw),
    ):
        with pytest.raises(ValueError, match="decompression bound"):
            shared.layout(sealed(header + compressed))


def test_expanded_compressed_and_packet_cardinality_bounds_preserve_inputs():
    source = indexed_frame.pack(["a" * 1100] * 64)
    with pytest.raises(ValueError, match="packet cardinality"):
        shared.pack_frames([source] * 33)
    with pytest.raises(indexed_frame.FrameSizeError, match="expanded"):
        shared.pack_frames([source] * 32)
    rng = random.Random(712)
    inputs = [indexed_frame.pack([rng.randbytes(700).hex() for _ in range(64)]) for _ in range(16)]
    hashes = [hashlib.sha256(frame).digest() for frame in inputs]
    with pytest.raises(indexed_frame.FrameSizeError, match="compressed"):
        shared.pack_frames(inputs)
    assert [hashlib.sha256(frame).digest() for frame in inputs] == hashes


def test_domain_packet_version_cannot_mislabel_a_shared_body():
    original = blocks.pack(packet_entries(1))
    frame = shared.pack_frames([blocks.frames(original)[1]])
    packet = blocks.with_shared_body(original, frame, 0)
    with pytest.raises(ValueError, match="body version"):
        blocks.frames(blocks.MAGIC + packet[4:])
    with pytest.raises(ValueError, match="body version"):
        blocks.frames(blocks.SHARED_MAGIC + original[4:])
