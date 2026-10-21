import copy

import pytest

from cryptowatcher import compact_json, indexed_frame, quality_vector
from cryptowatcher import quality_frame_codec as codec


def entries(count):
    return [
        None
        if index % 7 == 3
        else [
            20000 + index,
            2**52 + index,
            index,
            [
                [
                    [
                        2**53 + row * 300 + index,
                        2**53 + row * 300 + index + 1,
                        quality_vector.pack(
                            [2**53 + row * 5 + j for j in range(row % 21)], delta=row % 2 == 0
                        )
                        if row % 21
                        else None,
                        row % 5 == 0,
                        -0.0 if row % 3 == 0 else row / 10,
                        {"method": 2, "value": row},
                    ]
                    for row in range(128)
                ],
                None,
            ],
        ]
        for index in range(count)
    ]


@pytest.mark.parametrize("count", [1, 2, 64])
def test_columns_preserve_exact_typed_entries_order_vectors_high_ids_and_dead_slots(store, count):
    values = entries(count)
    original = copy.deepcopy(values)
    baseline = indexed_frame.pack(values)
    packed = codec.pack(values, use_columns=True)
    assert values == original and len(packed) <= len(baseline)
    assert packed.startswith(codec.MAGIC)
    assert compact_json.pack(codec.all_values(packed)) == compact_json.pack(values)
    assert codec.all_values(baseline) == values
    assert codec.pack(values, use_columns=False) == baseline
    for slot, value in enumerate(values):
        assert codec.entry_bytes(packed, slot, store.cached_bytes) == compact_json.pack(value)
    assert store.reference_cache_bytes <= 1024 * 1024
    changed = bytearray(packed)
    changed[-1] ^= 1
    with pytest.raises(ValueError, match="checksum"):
        codec.entry_bytes(bytes(changed), 0, store.cached_bytes)


@pytest.mark.parametrize("change", ["tag", "ragged", "type", "count", "coordinate", "old_rows"])
def test_checksums_do_not_make_malformed_column_entries_valid(change):
    value = codec.columns(entries(1)[0])
    if change == "tag":
        value[3][0] = "C2"
    elif change == "ragged":
        value[3][1][0].pop()
    elif change == "type":
        value[3][1][1] = {}
    elif change == "count":
        value[3][1].pop()
    elif change == "coordinate":
        value[0] = True
    else:
        value = entries(1)[0]
    raw = indexed_frame.pack([value])
    frame = codec.MAGIC + raw[4:]
    with pytest.raises(ValueError, match="column"):
        codec.all_values(frame)
    with pytest.raises(ValueError, match="column"):
        codec.entry_bytes(frame, 0)


def test_unknown_format_and_slot_rejected_and_empty_rows_have_six_empty_columns():
    values = [[0, 1, 0, [[], None]], None]
    raw = codec.pack(values, use_columns=True)
    assert codec.all_values(raw) == values
    for slot in (-1, 2, True):
        with pytest.raises(ValueError, match="slot"):
            codec.entry_bytes(raw, slot)
    with pytest.raises(ValueError, match="format"):
        codec.all_values(b"QC9\0" + raw[4:])
    assert codec.columns(values[0])[3] == [codec.TAG, [[], [], [], [], [], []]]


def test_column_expansion_failure_falls_back_to_the_already_valid_row_frame(monkeypatch):
    values = entries(1)
    original = indexed_frame.pack
    calls = 0

    def pack(rows):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise indexed_frame.FrameSizeError("larger transposed representation")
        return original(rows)

    expected = original(values)
    monkeypatch.setattr(indexed_frame, "pack", pack)
    assert codec.pack(values, use_columns=True) == expected
