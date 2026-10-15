import base64
import hashlib
import json
import math

import pytest
import zstandard

from cryptowatcher.history_codec import Codec
from cryptowatcher.record_codec import MARKER, SCHEMA, pack, unpack_bytes
from cryptowatcher.storage import StateError, encode


def test_record_preserves_exact_bytes_types_and_shared_fields(store):
    value = {
        "integer": 2**120,
        "real": 1.0,
        "negative_zero": -0.0,
        "tiny": 5e-324,
        "precision": 1.0000000000000002,
        "text": 'é 漢字 "\n\u0000',
        "flags": [True, False, None],
        "source_inputs": [[i, 1.0, 2.0, 0.5, 1.5, 120, 180] for i in range(256)],
    }
    codec = Codec(store.db)
    raw = encode(value)
    envelope = pack(raw)
    assert len(envelope) < len(raw)
    assert unpack_bytes(json.loads(envelope)) == raw.encode()
    assert encode(codec.read(envelope)) == raw
    assert math.copysign(1, codec.read(envelope)["negative_zero"]) == -1
    assert codec.canonical_json(json.loads(envelope)) == raw.encode()
    shared = codec.pack(value)
    assert encode(codec.read(pack(encode(shared)))) == raw
    assert codec.canonical_json(json.loads(pack(encode(shared)))) == raw.encode()


@pytest.mark.parametrize("change", ["schema", "boolean_size", "size", "hash", "base64", "extra", "root"])
def test_record_rejects_invalid_descriptor(change):
    value = json.loads(pack('{"record_id":"one"}'))
    descriptor = value[MARKER]
    if change == "schema":
        descriptor["schema"] = "future"
    elif change == "boolean_size":
        descriptor["bytes"] = True
    elif change == "size":
        descriptor["bytes"] += 1
    elif change == "hash":
        descriptor["sha256"] = "0" * 64
    elif change == "base64":
        descriptor["zstd"] = "invalid!"
    elif change == "extra":
        descriptor["extra"] = 1
    else:
        value["record_id"] = "spoof"
    with pytest.raises(StateError):
        unpack_bytes(value)


@pytest.mark.parametrize("fault", ["trailer", "second_frame", "checksum", "no_checksum"])
def test_record_rejects_bad_frames(fault):
    raw = b'{"data":"original"}'
    value = json.loads(pack(raw.decode()))
    data = base64.b64decode(value[MARKER]["zstd"])
    if fault == "trailer":
        data += b"trailer"
    elif fault == "second_frame":
        data += data
    elif fault == "checksum":
        data = data[:-1] + bytes([data[-1] ^ 1])
    else:
        data = zstandard.ZstdCompressor(write_checksum=False).compress(raw)
    value[MARKER]["zstd"] = base64.b64encode(data).decode()
    with pytest.raises(StateError):
        unpack_bytes(value)


def test_record_rejects_invalid_json_objects_and_nested_envelopes(store):
    codec = Codec(store.db)
    for raw in ("{broken", "[]", "null", pack('{"event_id":"one"}')):
        with pytest.raises(StateError):
            codec.read(pack(raw))
    with pytest.raises(StateError):
        codec.record_value(
            {
                MARKER: {
                    "schema": SCHEMA,
                    "bytes": 1,
                    "sha256": hashlib.sha256(b"\xff").hexdigest(),
                    "zstd": base64.b64encode(
                        zstandard.ZstdCompressor(write_checksum=True).compress(b"\xff")
                    ).decode(),
                }
            }
        )


def test_ordinary_records_and_literal_marker_text_remain_compatible(store):
    raw = encode({"event_id": "one", "notes": MARKER, "revision": 1})
    codec = Codec(store.db)
    assert unpack_bytes(json.loads(raw)) is None
    assert codec.read(raw) == json.loads(raw)


def test_framed_immutable_versions_remain_complete_through_heads_and_history(store):
    from cryptowatcher.events import HEAD_FIELDS, PREFIX, History
    from tests.test_history_heads import record

    history = History(store)
    for kind in HEAD_FIELDS:
        original = record(kind, "framed")
        original[kind + "_id"] = "framed"
        reference = f"{kind}:framed:0000000001"
        with store.db:
            history.version(kind, original)
            row = store.db.execute(
                "SELECT payload_json FROM runtime WHERE key=?", (PREFIX + reference,)
            ).fetchone()
            store.db.execute(
                "UPDATE runtime SET payload_json=? WHERE key=?", (pack(row[0]), PREFIX + reference)
            )
        assert history.get(reference) == original
        assert history.get(kind + "-head:framed") == original
        assert list(history.items(kind + ":"))[0][1] == original
    with pytest.raises(StateError, match="Reserved history storage marker"):
        history.codec.pack(json.loads(pack('{"event_id":"one"}')))


@pytest.mark.parametrize("legacy", [False, True])
def test_framed_database_version_is_explicit_and_survives_reopen(store, legacy):
    from cryptowatcher.storage import (
        COMPRESSED_RECORD_VERSION,
        LEGACY_RECORD_FORMAT,
        RECORD_FORMAT,
        RECORD_FORMAT_KEY,
        Store,
    )

    path = store.path
    with store.db:
        store.put_runtime(RECORD_FORMAT_KEY, LEGACY_RECORD_FORMAT if legacy else RECORD_FORMAT)
        store.db.execute(f"PRAGMA user_version={COMPRESSED_RECORD_VERSION}")
    store.close()
    reopened = Store(path, 233382575)
    assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == COMPRESSED_RECORD_VERSION
    assert reopened.snapshot().revision == 1
    assert reopened.compact_candles is (not legacy)
    reopened.close()


@pytest.mark.parametrize(
    "metadata", [None, {}, {"schema": "future"}, {"physical_schema": True, "schema": SCHEMA}]
)
def test_unknown_version_two_metadata_refuses_before_mutation(store, metadata):
    from cryptowatcher.storage import RECORD_FORMAT_KEY, Store

    with store.db:
        if metadata is not None:
            store.put_runtime(RECORD_FORMAT_KEY, metadata)
        store.db.execute("PRAGMA user_version=2")
    path = store.path
    store.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(StateError, match="Unsupported database schema"):
        Store(path, 233382575)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_compact_candle_exact_types_unknown_fields_and_corruption():
    from cryptowatcher.record_codec import CANDLE_FIELDS, CANDLE_MARKER, pack_candle, sql_payload
    from cryptowatcher.storage import encode

    value = {name: i for i, name in enumerate(CANDLE_FIELDS)}
    value.update(close=1.0, volume=-0.0, source="rest", turnover=5e-324, received_ms=2**100)
    raw = encode(value)
    packed = pack_candle(raw)
    assert sql_payload(packed) == raw
    assert type(json.loads(sql_payload(packed))["close"]) is float
    assert pack_candle(encode({**value, "future_field": True})) == encode({**value, "future_field": True})
    noncanonical = json.dumps(value)
    assert pack_candle(noncanonical) == noncanonical
    with pytest.raises(StateError):
        sql_payload(encode({CANDLE_MARKER: [1]}))
