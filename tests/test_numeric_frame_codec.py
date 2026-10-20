import hashlib
import random
import struct

import pytest
import zstandard

from cryptowatcher import metric_codec, metric_price_codec
from cryptowatcher import numeric_block_codec as blocks
from cryptowatcher import numeric_frame_codec as codec
from tests.test_quality_packets import metric


def values(count):
    rng = random.Random(29317)
    result = []
    for identity in range(count):
        frames = [b""] * 64
        for slot in range(64):
            if (identity + slot) % 7:
                frames[slot] = (
                    bytes.fromhex("1101e807142813")
                    if slot % 3 == 0
                    else metric_codec.pack(metric(rng.randrange(1000000)))
                )
        result.append((identity, blocks.pack(identity, frames, recipes=True)))
    return result


def forged(body, *, checksum=True, content_size=True):
    raw = zstandard.ZstdCompressor(write_checksum=checksum, write_content_size=content_size).compress(body)
    return codec.MAGIC + hashlib.sha256(codec.MAGIC + raw).digest() + raw


def row_body(entries):
    frames = [blocks.unpack(raw, identity) for identity, raw in entries]
    return (
        b"BR1\x00"
        + bytes([len(entries)])
        + b"".join(struct.pack(">Q", identity) for identity, _ in entries)
        + bytes(int(raw.startswith(b"NB2\x00")) for _, raw in entries)
        + bytes(len(frame) for group in frames for frame in group)
        + b"".join(frame for group in frames for frame in group)
    )


@pytest.mark.parametrize("count", [1, 2, 64])
def test_all_exact_original_block_bytes_raw_recipes_ids_and_slots_survive(count, store):
    expected = values(count)
    raw = codec.pack(expected)
    assert len(raw) <= codec.MAX_FRAME
    assert codec.all_values(raw, store.cached_bytes) == expected
    for slot, (identity, original) in enumerate(expected):
        assert codec.selected(raw, slot, store.cached_bytes) == (identity, original)
        for before, after in zip(
            blocks.unpack(original, identity),
            blocks.unpack(codec.selected(raw, slot, store.cached_bytes)[1], identity),
            strict=True,
        ):
            assert before == after
            if before:
                assert metric_price_codec.frame(before) == metric_price_codec.frame(after)
    assert store.reference_cache_bytes <= 1024 * 1024


def test_dead_slots_and_highest_private_ids_survive_without_reassigning_positions(store):
    frames = [b""] * 64
    frames[0] = metric_codec.pack(metric(7))
    identity = blocks.MAX_BLOCK
    original = blocks.pack(identity, frames)
    expected = [None, (identity, original), None]
    raw = codec.pack(expected)
    assert codec.all_values(raw, store.cached_bytes) == expected
    assert codec.selected(raw, 0) is codec.selected(raw, 2) is None


def test_valid_literal_only_nb2_and_alternate_compressed_source_preserve_exact_metric_frames():
    frames = [metric_codec.pack(metric(7))] * 64
    original = blocks.pack(0, frames)
    literal_nb2 = blocks.seal(b"NB2\x00", blocks.header(original))
    packed = codec.pack([(0, literal_nb2)])
    assert codec.selected(packed, 0) == (0, literal_nb2)
    body = blocks.header(literal_nb2)
    compressed = zstandard.ZstdCompressor(level=19, write_checksum=True).compress(b"".join(frames))
    alternate = blocks.seal(b"NB2\x00", body[:72] + compressed)
    actual = codec.selected(codec.pack([(0, alternate)]), 0)
    assert actual[1][:4] == b"NB2\x00"
    assert blocks.unpack(actual[1], actual[0]) == frames


@pytest.mark.parametrize("slot", [-1, 1, 64, True, 0.0])
def test_selected_slot_is_a_bounded_integer(slot):
    with pytest.raises(ValueError, match="slot"):
        codec.selected(codec.pack(values(1)), slot)


def test_checksum_authentication_precedes_the_pure_cache_even_after_a_warm_read(store):
    raw = codec.pack(values(2))
    expected = codec.selected(raw, 0, store.cached_bytes)
    corrupt = raw[:-1] + bytes([raw[-1] ^ 1])
    with pytest.raises(ValueError, match="checksum"):
        codec.selected(corrupt, 0, store.cached_bytes)
    assert codec.selected(raw, 0, store.cached_bytes) == expected


@pytest.mark.parametrize("kind", ["duplicate", "unknown", "size", "flag", "dead", "length"])
def test_validly_checksummed_invalid_shapes_never_reach_a_public_metric(kind, store):
    body = bytearray(row_body(values(2)))
    if kind == "duplicate":
        body[13:21] = body[5:13]
    elif kind == "unknown":
        body[:4] = b"BP9\x00"
    elif kind == "size":
        body.append(0)
    elif kind == "flag":
        body[21] = 2
    elif kind == "dead":
        body[5:13] = struct.pack(">Q", codec.DEAD)
    else:
        body[23 + 1] = 255
    with pytest.raises(ValueError):
        codec.all_values(forged(bytes(body)), store.cached_bytes)


@pytest.mark.parametrize("kind", ["content", "checksum", "too_large", "concatenated", "truncated"])
def test_compression_metadata_and_expansion_are_finite(kind):
    body = row_body(values(1))
    if kind == "content":
        raw = forged(body, content_size=False)
    elif kind == "checksum":
        raw = forged(body, checksum=False)
    elif kind == "too_large":
        raw = forged(b"x" * (codec.MAX_RAW + 1))
    elif kind == "concatenated":
        payload = forged(body)[36:] * 2
        raw = codec.MAGIC + hashlib.sha256(codec.MAGIC + payload).digest() + payload
    else:
        payload = forged(body)[36:-1]
        raw = codec.MAGIC + hashlib.sha256(codec.MAGIC + payload).digest() + payload
    with pytest.raises(ValueError):
        codec.all_values(raw)


@pytest.mark.parametrize("entries", [[], [None], [None] * 64, [None] * 65])
def test_publication_requires_one_to_64_distinct_nonempty_block_entries(entries):
    with pytest.raises(ValueError):
        codec.pack(entries)
    first = values(1)[0]
    with pytest.raises(ValueError, match="duplicate"):
        codec.pack([first, first])
