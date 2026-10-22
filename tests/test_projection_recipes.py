import copy
import hashlib
import struct
import uuid

import pytest

from cryptowatcher import indexed_frame, projection_recipes, record_blocks, shared_body_frame
from cryptowatcher.compact_json import pack as typed_pack
from cryptowatcher.events import identity
from cryptowatcher.storage import encode
from tests.test_record_blocks import entries

BASE = 1_800_000_060_000


def event_entry(number):
    head = {
        "market_type": "spot",
        "symbol": "BTCUSDT",
        "timeframe": "1m",
        "detector_id": "price",
        "detector_version": "frozen-old-definition",
        "market_observation_ms": BASE + number * 60000,
        "future_field": [-0.0, 0, 0.0, True, None, 2**100, "тест"],
    }
    identifier = identity(f"spot/BTCUSDT/1m/price/frozen-old-definition/7/{BASE + number * 60000 - 60000}")
    return entry("event", identifier, {**head, "settings_revision": 7}, head)


def entry(kind, identifier, body, head):
    coordinate = record_blocks.Coordinate(kind, identifier, 2)
    value = {coordinate.field: identifier, "revision": 2, **body}
    projected = (
        None
        if head is None
        else {
            **head,
            coordinate.field: identifier,
            "revision": 2,
            "schema": record_blocks.HEAD_SCHEMA,
            "revision_key": coordinate.key,
        }
    )
    return coordinate, value, projected


def mixed_entries():
    event = event_entry(0)
    parent = event[0].identity
    outcome_head = {
        "event_id": parent,
        "horizon_min": 15,
        "methodology_version": "frozen-old-outcomes",
        "future_field": [-0.0, 0.0, 1, True],
    }
    outcome = entry(
        "outcome", identity(f"outcome/{parent}/15/frozen-old-outcomes"), outcome_head, outcome_head
    )
    comparison_head = {"event_id": parent, "future_field": [-0.0, 0, None]}
    comparison = entry(
        "comparison",
        identity(f"comparison/{parent}/frozen-old-comparators"),
        {**comparison_head, "methodology_version": "frozen-old-comparators"},
        comparison_head,
    )
    return [event, outcome, comparison, entries()[2], entries()[4]]


def logical_projection(packet):
    return indexed_frame.all_values(record_blocks.frames(packet)[0])


def sealed_projection_rows(rows):
    return projection_recipes.MAGIC + shared_body_frame.pack_frames([indexed_frame.pack(rows)])


def test_frozen_recipes_restore_every_projection_and_body_through_existing_domain_reader(store, monkeypatch):
    values = mixed_entries()
    packet = record_blocks.pack(values)
    frame = projection_recipes.pack_frames([packet])
    folded = shared_body_frame.data(frame[4:])[1]
    assert len(frame) <= shared_body_frame.MAX_COMPRESSED and folded
    monkeypatch.setattr(
        "cryptowatcher.events.identity", lambda *args: pytest.fail("Mutable identity helper used")
    )
    projection = projection_recipes.restore(frame, 0, len(values), store.cached_bytes)
    assert [typed_pack(row) for row in indexed_frame.all_values(projection)] == [
        typed_pack(row) for row in logical_projection(packet)
    ]
    body = shared_body_frame.pack_frames([record_blocks.frames(packet)[1]])
    restored = record_blocks.shared_packet(projection, body, 0, len(values))
    for slot, (coordinate, value, head) in enumerate(values):
        actual = record_blocks.read(restored, slot, coordinate, store.cached_bytes)
        # Preserve the old reader's actual coordinate-expansion field order,
        # as well as the complete original canonical public value.
        assert typed_pack(actual) == typed_pack(record_blocks.read(packet, slot, coordinate))
        assert encode(actual) == encode(value)
        projected = record_blocks.checked_projected(restored, slot, coordinate, store.cached_bytes)
        assert typed_pack(projected) == typed_pack(record_blocks.projected(packet, slot, coordinate))
        assert encode(projected) == encode(head)
    returned = record_blocks.projected(restored, 0, values[0][0], store.cached_bytes)
    returned["future_field"][0] = 99
    assert typed_pack(record_blocks.projected(restored, 0, values[0][0], store.cached_bytes)) == typed_pack(
        record_blocks.projected(packet, 0, values[0][0])
    )
    assert store.reference_cache_bytes <= 1024 * 1024


def test_custom_identities_unknown_methods_and_future_fields_remain_literal():
    values = mixed_entries()
    changed = []
    for number, (coordinate, value, head) in enumerate(values):
        identifier = str(uuid.UUID(int=100 + number))
        changed.append(
            entry(
                coordinate.kind,
                identifier,
                {k: v for k, v in value.items() if k not in (coordinate.field, "revision")},
                None
                if head is None
                else {
                    k: v
                    for k, v in head.items()
                    if k not in (coordinate.field, "revision", "schema", "revision_key")
                },
            )
        )
    packet = record_blocks.pack(changed)
    frame = projection_recipes.pack_frames([packet])
    projection = projection_recipes.restore(frame, 0, len(changed))
    assert [typed_pack(row) for row in indexed_frame.all_values(projection)] == [
        typed_pack(row) for row in logical_projection(packet)
    ]


def test_a_future_timeframe_with_a_valid_frozen_uuid_keeps_its_literal_identity():
    original = event_entry(0)
    body = {k: v for k, v in original[1].items() if k not in ("event_id", "revision")}
    body["timeframe"] = "30m"
    head = {
        k: v for k, v in original[2].items() if k not in ("event_id", "revision", "schema", "revision_key")
    }
    head["timeframe"] = "30m"
    identifier = identity(f"spot/BTCUSDT/30m/price/frozen-old-definition/7/{BASE - 1800000}")
    packet = record_blocks.pack([entry("event", identifier, body, head)])
    frame = projection_recipes.pack_frames([packet])
    projection = projection_recipes.restore(frame, 0, 1)
    assert [typed_pack(row) for row in indexed_frame.all_values(projection)] == [
        typed_pack(row) for row in logical_projection(packet)
    ]


def test_all2048_projection_rows_keep_original64slot_views_with_no_schema_or_writes(store):
    packets = [
        record_blocks.pack([event_entry(packet * 64 + slot) for slot in range(64)]) for packet in range(32)
    ]
    changes = store.db.total_changes
    frame = projection_recipes.pack_frames(packets)
    assert len(projection_recipes.layout(frame)[0]) == 2049
    for packet, original in enumerate(packets):
        restored = projection_recipes.restore(frame, packet * 64, 64, store.cached_bytes)
        assert [typed_pack(row) for row in indexed_frame.all_values(restored)] == [
            typed_pack(row) for row in logical_projection(original)
        ]
    assert store.db.total_changes == changes and store.reference_cache_bytes <= 1024 * 1024
    with pytest.raises(ValueError, match="bound"):
        projection_recipes.pack_frames(packets + packets[:1])


@pytest.mark.parametrize("first,count", [(-1, 1), (0, 0), (0, 65), (True, 1), (0, False), (5, 1), (4, 2)])
def test_warm_recipe_view_never_accepts_changed_or_oversized_coordinates(store, first, count):
    values = mixed_entries()
    frame = projection_recipes.pack_frames([record_blocks.pack(values)])
    projection_recipes.restore(frame, 0, len(values), store.cached_bytes)
    with pytest.raises(ValueError):
        projection_recipes.restore(frame, first, count, store.cached_bytes)


@pytest.mark.parametrize("fault", ["unknown", "truncated", "checksum", "allocation", "nonselected"])
def test_cached_recipe_checks_current_whole_frame_and_nonselected_rows(store, fault):
    values = mixed_entries()
    frame = projection_recipes.pack_frames([record_blocks.pack(values)])
    projection_recipes.restore(frame, 0, len(values), store.cached_bytes)
    if fault == "unknown":
        damaged = b"RP9\0" + frame[4:]
    elif fault == "truncated":
        damaged = frame[:-1]
    elif fault == "checksum":
        damaged = frame[:-1] + bytes([frame[-1] ^ 1])
    elif fault == "allocation":
        checked = struct.pack(">HI", 2049, 1) + frame[46:]
        damaged = (
            projection_recipes.MAGIC + shared_body_frame.MAGIC + hashlib.sha256(checked).digest() + checked
        )
    else:
        rows = logical_projection(record_blocks.pack(values))
        rows[-1][1] = None
        damaged = sealed_projection_rows(rows)
    with pytest.raises(ValueError):
        projection_recipes.restore(damaged, 0, len(values), store.cached_bytes)


@pytest.mark.parametrize(
    "fault", ["settings-bool", "timeframe", "head", "method", "arity", "parent", "duplicate"]
)
def test_validly_checksummed_invalid_recipe_cannot_bypass_semantic_checks(fault):
    rows = logical_projection(record_blocks.pack(mixed_entries()))
    row = copy.deepcopy(rows[0])
    row[1] = [7]
    if fault == "settings-bool":
        row[1] = [True]
    elif fault == "timeframe":
        row[3]["timeframe"] = "30m"
    elif fault == "head":
        row[3] = None
    elif fault == "method":
        row = [3, [False], 2, {"event_id": rows[0][1]}]
    elif fault == "arity":
        row[1] = [7, 8]
    elif fault == "parent":
        row = [1, None, 2, {"event_id": "bad", "horizon_min": 5, "methodology_version": "1"}]
    damaged = sealed_projection_rows([row, row] if fault == "duplicate" else [row])
    with pytest.raises(ValueError):
        projection_recipes.restore(damaged, 0, 2 if fault == "duplicate" else 1)


def test_recipe_publisher_keeps_legacy_and_oversized_shapes_out_of_new_format():
    packet = record_blocks.pack(mixed_entries(), legacy=True)
    with pytest.raises(ValueError):
        projection_recipes.pack_frames([packet])
    with pytest.raises(ValueError):
        projection_recipes.pack_frames([])
