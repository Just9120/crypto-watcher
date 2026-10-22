import hashlib
import struct
import uuid

import pytest

from cryptowatcher import record_blocks as blocks
from cryptowatcher.storage import encode


def entries():
    result = []
    for index, kind in enumerate(blocks.KINDS):
        coordinate = blocks.Coordinate(kind, str(uuid.UUID(int=index + 1)), index + 1)
        value = {
            coordinate.field: coordinate.identity,
            "revision": coordinate.revision,
            "schema": "cryptowatcher." + kind + ".v1",
            "metrics": {"one": -0.0, "two": 0, "three": 0.0, "four": 1.0000000000000002},
            "future": {"unexpected": [True, None, "Русский текст", 2**100]},
        }
        head = (
            None
            if kind == "decision"
            else {
                coordinate.field: coordinate.identity,
                "revision": coordinate.revision,
                "schema": blocks.HEAD_SCHEMA,
                "revision_key": coordinate.key,
                "strength": -0.0,
                "future_head_field": 0,
            }
        )
        result.append((coordinate, value, head))
    return result


@pytest.mark.parametrize("legacy", [False, True])
def test_checked_projection_cache_never_shares_mutable_heads_or_skips_current_bytes(store, legacy):
    values = entries()
    raw = blocks.pack(values, legacy=legacy)
    coordinate, _, expected = values[0]
    first = blocks.checked_projected(raw, 0, coordinate, store.cached_bytes)
    assert encode(first) == encode(expected)
    first["local_mutation"] = ["must not be cached"]
    assert encode(blocks.checked_projected(raw, 0, coordinate, store.cached_bytes)) == encode(expected)
    damaged = raw[:-1] + bytes((raw[-1] ^ 1,))
    with pytest.raises(ValueError):
        blocks.checked_projected(damaged, 0, coordinate, store.cached_bytes)
    assert store.reference_cache_bytes <= 1024 * 1024


def test_checked_projection_revalidates_changed_nonselected_coordinates_after_cache_hit(store, monkeypatch):
    values = entries()
    raw = blocks.pack(values)
    coordinate = values[0][0]
    count = 0
    original = blocks.projections

    def counted(packet):
        nonlocal count
        count += 1
        return original(packet)

    monkeypatch.setattr(blocks, "projections", counted)
    blocks.checked_projected(raw, 0, coordinate, store.cached_bytes)
    blocks.checked_projected(raw, 1, values[1][0], store.cached_bytes)
    assert count == 1
    projection, body = blocks.frames(raw)
    rows = blocks.indexed_frame.all_values(projection)
    rows[-1] = rows[0]
    projection = blocks.indexed_frame.pack(rows)
    frames = struct.pack(">I", len(projection)) + projection + body
    changed = blocks.MAGIC + hashlib.sha256(frames).digest() + frames
    with pytest.raises(ValueError, match="Duplicate domain projection"):
        blocks.checked_projected(changed, 0, coordinate, store.cached_bytes)
    assert count == 2


@pytest.mark.parametrize("legacy", [False, True])
def test_block_preserves_all_kinds_unknown_fields_exact_numbers_and_projection_without_body_decode(
    monkeypatch,
    legacy,
):
    values = entries()
    raw = blocks.pack(values, legacy=legacy)
    for slot, (coordinate, value, _) in enumerate(values):
        assert encode(blocks.read(raw, slot, coordinate)) == encode(value)
    projection, body = blocks.frames(raw)
    original = blocks.parse

    def projected_only(frame):
        assert frame != body
        return original(frame)

    monkeypatch.setattr(blocks, "parse", projected_only)
    original_data = blocks.indexed_frame.data

    def projected_data(frame, *args, **kwargs):
        assert frame != body
        return original_data(frame, *args, **kwargs)

    monkeypatch.setattr(blocks.indexed_frame, "data", projected_data)
    assert [(c, encode(h)) for c, h in blocks.projections(raw)] == [(c, encode(h)) for c, _, h in values]
    assert projection != body


def test_block_rejects_stale_slot_tampering_trailing_bytes_and_invalid_coordinates():
    values = entries()
    raw = blocks.pack(values)
    for invalid in (raw[:-1], raw + b"extra", raw[:57] + bytes([raw[57] ^ 1]) + raw[58:]):
        with pytest.raises(ValueError, match="checksum"):
            blocks.projections(invalid)
    with pytest.raises(ValueError, match="identity"):
        blocks.read(raw, 1, values[0][0])
    with pytest.raises(ValueError):
        blocks.read(raw, True, values[1][0])
    with pytest.raises(ValueError, match="Repeated"):
        blocks.pack([values[0], values[0]])
    for kind, identity, revision in (
        ("future", str(uuid.uuid4()), 1),
        ("event", "invalid", 1),
        ("event", str(uuid.uuid4()), True),
    ):
        with pytest.raises(ValueError):
            blocks.Coordinate(kind, identity, revision).validate()


def test_block_rejects_identity_or_head_mismatch_and_expansion_before_decode(monkeypatch):
    coordinate, value, head = entries()[0]
    with pytest.raises(ValueError):
        blocks.pack([(coordinate, {**value, "revision": 2}, head)])
    with pytest.raises(ValueError):
        blocks.pack([(coordinate, value, {**head, "revision_key": "event:wrong"})])
    raw = blocks.pack([(coordinate, value, head)], legacy=True)
    projection, body = blocks.frames(raw)
    # A plausible full checksum does not bypass the inner allocation bound.
    projection = projection[:4] + (blocks.MAX_EXPANDED + 1).to_bytes(4, "big") + projection[8:]
    frames = len(projection).to_bytes(4, "big") + projection + body
    tampered = blocks.LEGACY_MAGIC + hashlib.sha256(frames).digest() + frames
    monkeypatch.setattr(blocks, "sql_payload", lambda frame: pytest.fail("decompressed beyond bound"))
    with pytest.raises(ValueError, match="bound"):
        blocks.projections(tampered)


@pytest.mark.parametrize("legacy", [False, True])
def test_frozen_legacy_and_indexed_blocks_expose_same_public_values(legacy):
    values = entries()
    raw = blocks.pack(values, legacy=legacy)
    assert raw.startswith(blocks.LEGACY_MAGIC if legacy else blocks.MAGIC)
    for i, (coordinate, value, head) in enumerate(values):
        assert encode(blocks.read(raw, i, coordinate)) == encode(value)
        assert blocks.projections(raw)[i][0] == coordinate
        assert encode(blocks.projections(raw)[i][1]) == encode(head)


def test_batch_builder_bounds_expanded_records_before_compression_and_keeps_order():
    source = []
    for index in range(130):
        coordinate = blocks.Coordinate("event", str(uuid.UUID(int=index + 1)), 1)
        source.append(
            (coordinate, {"event_id": coordinate.identity, "revision": 1, "text": "q" * 20000}, None)
        )
    groups = list(blocks.batches(iter(source)))
    assert len(groups) > 2
    assert [entry[0] for group in groups for entry in group] == [v[0] for v in source]
    for group in groups:
        raw = blocks.pack(group)
        assert len(raw) <= blocks.MAX_PACKET
        assert len(group) <= blocks.MAX_RECORDS
    coordinate, value, _ = source[0]
    with pytest.raises(ValueError, match="legacy"):
        list(blocks.batches([(coordinate, {**value, "text": "q" * blocks.MAX_PACKET}, None)]))


@pytest.mark.parametrize("legacy", [False, True])
def test_selected_projection_checks_slot_identity_and_actual_frames_without_body_decode(monkeypatch, legacy):
    values = entries()
    raw = blocks.pack(values, legacy=legacy)
    p, b = blocks.frames(raw)
    original = blocks.parse
    select = blocks.indexed_frame.select
    calls = []

    def parse(frame):
        assert frame != b
        return original(frame)

    def selected(frame, slot, cache=None):
        assert frame == p
        calls.append(slot)
        return select(frame, slot, cache)

    monkeypatch.setattr(blocks, "parse", parse)
    monkeypatch.setattr(blocks.indexed_frame, "select", selected)
    if not legacy:
        monkeypatch.setattr(blocks.indexed_frame, "all_values", lambda _: pytest.fail("unselected records"))
    for slot, (coordinate, _, head) in enumerate(values):
        assert encode(blocks.projected(raw, slot, coordinate)) == encode(head)
    if not legacy:
        assert calls == list(range(len(values)))
    for slot in (-1, len(values), True):
        with pytest.raises(ValueError):
            blocks.projected(raw, slot, values[0][0])
    with pytest.raises(ValueError, match="identity"):
        blocks.projected(raw, 1, values[0][0])
    tampered = raw[:-1] + bytes((raw[-1] ^ 1,))
    with pytest.raises(ValueError, match="checksum"):
        blocks.projected(tampered, 0, values[0][0])
