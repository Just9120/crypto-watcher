import random
import struct

import pytest
import zstandard

from cryptowatcher import numeric_block_codec as codec


def raw_values(values, compressed):
    body = struct.pack(">Q", 73)
    if compressed:
        body += struct.pack(">H", len(values))
        body += zstandard.ZstdCompressor(write_checksum=True).compress(values)
    else:
        body += values
    return codec.seal(b"NR2\0" if compressed else b"NR1\0", body)


@pytest.mark.parametrize("compressed", [False, True])
def test_terminal_one_byte_counts_preserve_every_value_and_fresh_mutable_lists(compressed):
    values = bytes(range(128))
    raw = raw_values(values, compressed)
    assert codec.leases(raw, 73) == (list(range(64)), list(range(64, 128)))
    details, quality = codec.leases(raw, 73)
    details[0] = -1
    quality.append(999)
    assert codec.leases(raw, 73) == (list(range(64)), list(range(64, 128)))


@pytest.mark.parametrize("compressed", [False, True])
def test_multibyte_leases_keep_original_full_63_bit_range(compressed):
    details = [0, 127, 128, 16383, 16384, 2**63 - 1] + [1] * 58
    quality = details[::-1]
    assert codec.leases(codec.counts(73, details, quality, compressed=compressed), 73) == (
        details,
        quality,
    )


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize(
    "values",
    [
        bytes(127),
        bytes(129),
        bytes(127) + b"\x80",
        b"\x80\x00" + bytes(127),
        b"\x81\x00" + bytes(127),
        b"\xff" * 9 + b"\x00" + bytes(127),
    ],
    ids=["short", "trailing", "truncated", "nonminimal-zero", "nonminimal-one", "overflow"],
)
def test_common_count_shape_never_bypasses_legacy_length_or_canonical_guards(values, compressed):
    with pytest.raises(ValueError):
        codec.leases(raw_values(values, compressed), 73)


@pytest.mark.parametrize("compressed", [False, True])
def test_common_counts_still_reject_wrong_current_identity_and_checksum(compressed):
    raw = raw_values(bytes(range(128)), compressed)
    with pytest.raises(ValueError, match="identity"):
        codec.leases(raw, 74)
    damaged = raw[:-1] + bytes([raw[-1] ^ 1])
    with pytest.raises(ValueError, match="checksum"):
        codec.leases(damaged, 73)


@pytest.mark.parametrize("compressed", [False, True])
def test_repeated_timestamp_leases_keep_each_slot_and_independent_lists(compressed):
    details = [1_802_908_800_000 if i % 3 else 0 for i in range(64)]
    quality = [2**63 - 1 if i % 5 else 127 for i in range(64)]
    raw = codec.counts(73, details, quality, compressed=compressed)
    first = codec.leases(raw, 73)
    assert first == (details, quality)
    first[0][1] = 17
    first[1][2] = 19
    assert codec.leases(raw, 73) == (details, quality)


@pytest.mark.parametrize("compressed", [False, True])
def test_all_varint_widths_and_distinct_full_range_values_preserve_exact_ints(compressed):
    rng = random.Random(72193)
    values = [0, 127, 128, 2**63 - 1]
    for width in range(1, 10):
        values += [2 ** (7 * width) - 1, 2 ** (7 * (width - 1))]
    values += [rng.randrange(2**63) for _ in range(128 - len(values))]
    rng.shuffle(values)
    assert codec.leases(codec.counts(73, values[:64], values[64:], compressed=compressed), 73) == (
        values[:64],
        values[64:],
    )
