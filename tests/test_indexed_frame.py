import hashlib

import pytest
import zstandard

from cryptowatcher import indexed_frame
from cryptowatcher.storage import encode


def checked(header):
    return indexed_frame.MAGIC + hashlib.sha256(header).digest() + header


def test_selected_record_keeps_float_bits_types_unknown_fields_and_materializes_only_its_slice(monkeypatch):
    values = [
        {
            "index": i,
            "text": "русский" * 100,
            "bits": [-0.0, 0, 0.0, 1.0000000000000002, 2**100],
            "future": {"x": None},
        }
        for i in range(64)
    ]
    frame = indexed_frame.pack(values)
    offsets, raw = indexed_frame.data(frame)
    original = indexed_frame.typed_unpack
    sizes = []

    def selected(data, maximum):
        sizes.append(len(data))
        return original(data, maximum)

    monkeypatch.setattr(indexed_frame, "typed_unpack", selected)
    assert encode(indexed_frame.select(frame, 17)) == encode(values[17])
    assert sizes == [offsets[18] - offsets[17]]
    assert sizes[0] < len(raw) // 32


def test_index_bounds_and_checksum_are_checked_before_decompression(monkeypatch):
    frame = indexed_frame.pack([{"one": 1}, {"two": 2}])
    damaged = frame[36:38] + (indexed_frame.MAX_BYTES + 1).to_bytes(4, "big") + frame[42:]
    invalid_offset = frame[36:42] + (1).to_bytes(4, "big") + frame[46:]
    monkeypatch.setattr(
        indexed_frame.zstandard,
        "ZstdDecompressor",
        lambda **kwargs: pytest.fail("allocated beyond validated bound"),
    )
    for raw in (frame[:-1], frame + b"extra", checked(damaged), checked(invalid_offset)):
        with pytest.raises(ValueError):
            indexed_frame.select(raw, 0)


def test_pure_binary_and_text_cache_share_existing_one_mib_and_return_fresh_objects(store):
    frame = indexed_frame.pack([{"x": [1, 2, 3]}])
    first = indexed_frame.select(frame, 0, store.cached_bytes)
    first["x"].append(4)
    assert indexed_frame.select(frame, 0, store.cached_bytes) == {"x": [1, 2, 3]}
    store.cached_bytes("large", "", b"input", lambda: bytes(700000))
    store.cached_text("unicode", "", "text", lambda: "界" * 300000)
    assert 0 < store.reference_cache_bytes <= 1024 * 1024
    assert sum(size for _, size in store.reference_cache.values()) == store.reference_cache_bytes
    damaged = bytearray(frame)
    damaged[-1] ^= 1
    with pytest.raises(ValueError, match="checksum"):
        indexed_frame.select(bytes(damaged), 0, store.cached_bytes)


def test_trailing_frames_bad_slots_and_oversized_population_are_rejected():
    frame = indexed_frame.pack([None])
    trailing = checked(frame[36:] + b"second frame")
    with pytest.raises((ValueError, zstandard.ZstdError)):
        indexed_frame.select(trailing, 0)
    for slot in (True, -1, 1):
        with pytest.raises(ValueError):
            indexed_frame.select(frame, slot)
    with pytest.raises(ValueError):
        indexed_frame.pack([None] * 65)
    with pytest.raises(ValueError):
        indexed_frame.pack(["x" * (indexed_frame.MAX_BYTES + 1)])
