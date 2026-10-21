import hashlib
import random
from unittest.mock import patch

import pytest
import zstandard

from cryptowatcher import head_codec, record_codec
from cryptowatcher.events import HEAD_FIELDS, HEAD_SCHEMA, History, identity
from cryptowatcher.storage import StateError, encode, json_payload


def head(kind="event", revision=1, strength=-0.0):
    value = {
        "event_id": identity("head-event"),
        "episode_id": identity("head-episode"),
        "outcome_id": identity("head-outcome"),
        "comparison_id": identity("head-comparison"),
        "revision": revision,
        "event_revision": 1,
        "market_observation_ms": 1800000000000,
        "available_at_ms": 1800000000123,
        "market_type": "spot",
        "detector_id": "price",
        "detector_version": "1",
        "symbol": "BTCUSDT",
        "timeframe": "3m",
        "direction": "UP",
        "strength": strength,
        "state": "COMPLETE",
        "horizon_min": 15,
        "methodology_version": "1",
        "classification": "CONTINUATION",
        "noise_threshold_pct": 0.1,
        "metrics": {"price_return_pct": 2.0},
        "instrument": "spot/BTCUSDT",
        "started_ms": 1800000000000,
        "last_observation_ms": 1800000000100,
        "longest_tf_ms": 180000,
    }
    key = f"{kind}:{value[kind + '_id']}:{revision:010d}"
    return History._head(kind, value, key, bounded=True)


def test_projected_templates_keep_exact_values_types_defaults_and_all_query_fields():
    assert head_codec.SCHEMA == HEAD_SCHEMA
    rng = random.Random(972)
    for kind, fields in zip(head_codec.KINDS, head_codec.FIELDS):
        assert tuple(HEAD_FIELDS[kind]) == fields  # Future additions require a fallback/new template.
        for index in range(100):
            value = head(
                kind,
                revision=1 if index % 2 else index + 2,
                strength=rng.choice((-0.0, 0, 2.0, rng.random())),
            )
            if "symbol" in value:
                value["symbol"] = "Монета🌍" + str(index)
            raw = head_codec.pack(value)
            assert raw is not None
            assert encode(head_codec.unpack(raw)) == encode(json_payload(raw)) == encode(value)
            assert record_codec.binary_payload(raw) == encode(value)
    expanded = {**head(), "future_projection": {"kept": True}}
    assert head_codec.pack(expanded) is None
    assert encode(json_payload(record_codec.pack_binary(encode(expanded)))) == encode(expanded)


def test_projected_head_damage_and_unrecognized_masks_fail_closed():
    from cryptowatcher.compact_json import pack

    original = head()
    raw = head_codec.pack(original)
    assert raw is not None
    invalid = [
        raw[:-1],
        raw + b"trailing",
        raw[:8] + bytes([raw[8] ^ 1]) + raw[9:],
        raw[:4] + b"\x00" * 4 + raw[8:],
    ]
    for body in (
        [True, 3, 0, []],
        [0, 0, 0, []],
        [0, 3, 1, ["id"]],
        [0, 3, 0, ["id", 1.0]],
        [0, 1 << 40, 0, []],
    ):
        invalid.append(raw[:40] + pack(body))
    for damaged in invalid:
        with pytest.raises(StateError):
            json_payload(damaged)
        with pytest.raises(StateError):
            record_codec.binary_payload(damaged)


def test_sql_heads_mixed_cw3_ch1_and_joint_pure_cache_budget(store):
    from cryptowatcher.compact_json import pack

    value = head()
    public = encode(value).encode()
    frame = zstandard.ZstdCompressor(level=3, write_checksum=True).compress(pack(value))
    old = b"CW3\0" + len(public).to_bytes(4, "big") + hashlib.sha256(public).digest() + frame
    new = record_codec.pack_binary(public.decode())
    assert new.startswith(head_codec.MAGIC) and len(new) < len(old)
    for raw in (old, new):
        expected = store.db.execute("SELECT json_extract(cw_record_json(?),'$.strength')", (raw,)).fetchone()[
            0
        ]
        assert expected == value["strength"]
    with patch.object(record_codec, "sql_payload", wraps=record_codec.sql_payload) as decoder:
        for _ in range(12):
            store.db.execute("SELECT cw_record_json(?),cw_record_json(?)", (new, new)).fetchone()
        assert decoder.call_count == 0  # Same immutable bytes were already validated above.
        changed = record_codec.pack_binary(encode(head(revision=2)))
        store.db.execute("SELECT cw_record_json(?)", (changed,)).fetchone()
        assert decoder.call_count == 1
    for i in range(2500):
        value = head(revision=i + 3)
        value["symbol"] = "ASCII" * 50 + "🌍" + str(i)
        store.cached_record_json(record_codec.pack_binary(encode(value)))
    assert store.reference_cache_bytes <= 1024**2 and len(store.reference_cache) < 2500
    assert sum(size for _, size in store.reference_cache.values()) == store.reference_cache_bytes
    # A different damaged frame is never served from a previously valid cache entry.
    with pytest.raises(StateError):
        store.cached_record_json(new[:-1])
