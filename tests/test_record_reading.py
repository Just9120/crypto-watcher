"""Existing storage formats keep exact values and reject damaged inputs."""

import hashlib
import json
import struct
from contextlib import closing
from unittest.mock import patch

import pytest
import zstandard

from cryptowatcher import compact_json
from cryptowatcher.capacity import prospective_cohorts
from cryptowatcher.events import PREFIX, History, identity
from cryptowatcher.market_storage import enable
from cryptowatcher.record_codec import (
    TYPED_MAGIC,
    binary_payload,
    pack,
    pack_binary,
    pack_binary_v2,
    pack_candle,
    sql_payload,
)
from cryptowatcher.storage import StateError, Store, encode, json_payload


@pytest.mark.parametrize("writer", [str, pack, pack_binary, pack_binary_v2])
def test_direct_reader_matches_existing_json_interface_and_returns_fresh_values(writer):
    value = {
        "numbers": [2, 2.0, -0.0, 0.0175454454105349, 1 << 90, -(1 << 90)],
        "flags": [True, False, None],
        "text": "История 🌍",
        "rows": [{"revision": i, "symbol": "BTCUSDT", "strength": 1.5} for i in range(4)],
        "settings_snapshot": {"outcome_horizons": [5, 15, 30, 60], "profile": "custom"},
    }
    payload = writer(encode(value))
    expected = json.loads(sql_payload(payload))
    actual = json_payload(payload)
    assert encode(actual) == encode(expected) == encode(value)
    assert type(actual["numbers"][0]) is int and type(actual["numbers"][1]) is float
    assert struct.pack(">d", actual["numbers"][2]) == struct.pack(">d", -0.0)
    actual["rows"][0]["symbol"] = "CHANGED"
    assert json_payload(payload)["rows"][0]["symbol"] == "BTCUSDT"


def test_legacy_noncanonical_json_and_compact_candle_readers_remain_compatible():
    raw = '{ "strength": -0.0, "count":2, "count":3, "nested": [true, null] }'
    assert json_payload(pack_binary_v2(raw)) == json.loads(raw)
    candle = {
        "close": 1.1,
        "high": 1.2,
        "low": 0.9,
        "open": 1.0,
        "received_ms": 12,
        "server_ms": 11,
        "source": "rest",
        "start": 1,
        "step": 60000,
        "turnover": 1.5,
        "volume": 1,
    }
    payload = pack_candle(encode(candle))
    assert encode(json_payload(payload)) == encode(candle)


def test_typed_reader_avoids_json_reparse_but_still_checks_canonical_hash_and_size():
    payload = pack_binary(encode({"rows": [{"revision": i, "symbol": "BTCUSDT"} for i in range(8)]}))
    assert payload.startswith(TYPED_MAGIC)
    # This input needs no JSON parsing: typed decode and its existing hash check suffice.
    with patch("cryptowatcher.record_codec.json.loads", side_effect=AssertionError("Redundant parse")):
        assert len(json_payload(payload)["rows"]) == 8
    for damaged in (
        payload[:8] + bytes([payload[8] ^ 1]) + payload[9:],
        payload[:4] + (1).to_bytes(4, "big") + payload[8:],
        payload[:-1],
        payload + b"trailing",
        b"CW9\0" + payload[4:],
    ):
        with pytest.raises(StateError):
            json_payload(damaged)
        with pytest.raises(StateError):
            binary_payload(damaged)


@pytest.mark.parametrize("checksum", [False, True])
def test_valid_zstd_frame_cannot_hide_bad_typed_data(checksum):
    raw = encode({"revision": 1}).encode()
    # A valid frame/checksum never makes an invalid tag or unmatched identity acceptable.
    typed = compact_json.pack({"revision": 2}) if checksum else b"\xff"
    frame = zstandard.ZstdCompressor(write_checksum=checksum).compress(typed)
    payload = TYPED_MAGIC + len(raw).to_bytes(4, "big") + hashlib.sha256(raw).digest() + frame
    with pytest.raises(StateError):
        json_payload(payload)


def test_all_truncated_prefixes_and_incomplete_long_varints_still_fail():
    value = {
        "schema": "custom-schema",
        "numbers": [1 << 140, -(1 << 140), -0.0],
        "rows": [{"symbol": "BTCUSDT", "revision": i} for i in range(5)],
        "text": "История",
    }
    raw = compact_json.pack(value)
    assert encode(compact_json.unpack(raw, len(raw))) == encode(value)
    for size in range(len(raw)):
        with pytest.raises(ValueError):
            compact_json.unpack(raw[:size], len(raw))
    for tag in (3, 5, 6, 7, 8, 11):
        with pytest.raises(ValueError):
            compact_json.unpack(bytes([tag]) + b"\x80" * 1171, 2000)


def test_frozen_cohorts_match_json_roundtrip_across_revisions_and_mixed_formats(store):
    enable(store.db)
    store.bounded = True
    history = History(store)
    writers = (str, pack, pack_binary_v2, pack_binary)
    with store.db:
        history.put("outcome-method:1", {"version": "1"})
        for i in range(80):
            settings = store.snapshot().settings.payload()
            settings.update(profile=("custom", "normal")[i % 2], outcome_horizons=[5, 15, 30, 60])
            record = {
                "event_id": identity("cohort-reading-" + str(i // 2)),
                "revision": i % 2 + 1,
                "market_observation_ms": 1_800_000_000_000 + i // 40 * 86_400_000,
                "detector_id": ("price", "turnover", "breakout", "volatility", "composite")[i // 8 % 5],
                "detector_version": str(i // 40 + 1),
                "settings_revision": i % 2 + 1,
                "settings_snapshot": settings,
                "market_type": ("spot", "linear")[i % 2],
                "timeframe": ("1m", "3m", "5m", "15m")[i // 2 % 4],
                "symbol": "ASSET" + str(i % 3) + "USDT",
                "direction": ("UP", "DOWN", "NEUTRAL")[i % 3],
                "strength": i / 7 + 1,
            }
            payload = writers[i % len(writers)](encode(history.codec.pack(record)))
            store.db.execute(
                "INSERT INTO runtime(key,payload_json) VALUES(?,?)",
                (f"{PREFIX}event:{record['event_id']}:{record['revision']:010d}", payload),
            )
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        result = prospective_cohorts(reader)
        with patch("cryptowatcher.capacity.json_payload", side_effect=lambda p: json.loads(sql_payload(p))):
            reference = prospective_cohorts(reader)
        assert encode(result) == encode(reference)
        assert result["state"] == "MEASURED_PROSPECTIVE" and result["event_records"] == 80
        assert result["facts_if_archived"] == 320
        assert result["event_revisions"] == {1: 40, 2: 40}
        assert result["dimension_cardinality"]["detector"] == 5
        assert result["dimension_cardinality"]["market"] == 2
        assert result["dimension_cardinality"]["timeframe"] == 4
        partial = prospective_cohorts(reader, seconds=0)
        assert partial["state"] == "PARTIAL_BUDGET" and partial["event_records"] == 0
