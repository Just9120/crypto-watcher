import hashlib
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


def test_block_preserves_all_kinds_unknown_fields_exact_numbers_and_projection_without_body_decode(
    monkeypatch,
):
    values = entries()
    raw = blocks.pack(values)
    for slot, (coordinate, value, _) in enumerate(values):
        assert encode(blocks.read(raw, slot, coordinate)) == encode(value)
    projection, body = blocks.frames(raw)
    original = blocks.parse

    def projected_only(frame):
        assert frame != body
        return original(frame)

    monkeypatch.setattr(blocks, "parse", projected_only)
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
    raw = blocks.pack([(coordinate, value, head)])
    projection, body = blocks.frames(raw)
    # A plausible full checksum does not bypass the inner allocation bound.
    projection = projection[:4] + (blocks.MAX_EXPANDED + 1).to_bytes(4, "big") + projection[8:]
    frames = len(projection).to_bytes(4, "big") + projection + body
    tampered = blocks.MAGIC + hashlib.sha256(frames).digest() + frames
    monkeypatch.setattr(blocks, "sql_payload", lambda frame: pytest.fail("decompressed beyond bound"))
    with pytest.raises(ValueError, match="bound"):
        blocks.projections(tampered)


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
