import base64
import json
from contextlib import closing

import pytest
import zstandard

from cryptowatcher.analytics import as_of
from cryptowatcher.events import HEAD_SCHEMA, PREFIX, History
from cryptowatcher.history_codec import BLOB_PREFIX, BLOB_SCHEMA, CACHE_BYTES, MARKER, ZSTD_BLOB_SCHEMA, Codec
from cryptowatcher.storage import StateError, encode

from .test_history_heads import record


def test_mixed_zlib_zstd_bodies_preserve_types_identity_and_legacy_encoding(store):
    old, new = Codec(store.db), Codec(store.db, body_encoding="zstd")
    value = {"price": 0.0175454454105349, "count": 2, "real": 2.0, "notes": "история" * 80}
    with store.db:
        key = old.save(value)
        original = store.db.execute(
            "SELECT payload_json FROM runtime WHERE key=?", (BLOB_PREFIX + key,)
        ).fetchone()[0]
        assert new.save(value) == key
        second = new.save({**value, "count": 3})
    assert (
        store.db.execute("SELECT payload_json FROM runtime WHERE key=?", (BLOB_PREFIX + key,)).fetchone()[0]
        == original
    )
    assert json.loads(original)["schema"] == BLOB_SCHEMA
    assert store.get_runtime(BLOB_PREFIX + second)["schema"] == ZSTD_BLOB_SCHEMA
    assert encode(new.load(key)) == encode(value)
    assert encode(old.load(second)) == encode({**value, "count": 3})
    assert type(old.load(second)["count"]) is int and type(old.load(second)["real"]) is float


@pytest.mark.parametrize(
    "fault",
    [
        "size",
        "trailer1",
        "trailer4",
        "checksum",
        "extra_frame",
        "no_checksum",
        "unknown_size",
        "oversized_window",
        "extra_field",
        "future_schema",
    ],
)
def test_zstd_body_refuses_corruption_unbounded_or_future_format(store, monkeypatch, fault):
    codec = Codec(store.db, body_encoding="zstd")
    value = {"source": "candle" * 1000, "price": 1.0}
    raw = encode(value).encode()
    with store.db:
        key = codec.save(value)
        saved = store.get_runtime(BLOB_PREFIX + key)
        encoded = base64.b64decode(saved["zstd"])
        if fault == "size":
            saved["bytes"] -= 1
        elif fault.startswith("trailer"):
            encoded = encoded[: -int(fault[-1])]
        elif fault == "checksum":
            encoded = encoded[:-1] + bytes([encoded[-1] ^ 1])
        elif fault == "extra_frame":
            encoded += zstandard.ZstdCompressor(write_checksum=True).compress(b"extra")
        elif fault == "no_checksum":
            encoded = zstandard.ZstdCompressor(write_checksum=False).compress(raw)
        elif fault == "unknown_size":
            encoded = zstandard.ZstdCompressor(write_checksum=True, write_content_size=False).compress(raw)
        elif fault == "oversized_window":
            assert encoded[4] & 32 and encoded[4] >> 6 > 0
            encoded = encoded[:4] + bytes([encoded[4] & ~32, 0xA0]) + encoded[5:]
            assert zstandard.get_frame_parameters(encoded).content_size == len(raw)
            assert zstandard.get_frame_parameters(encoded).window_size > 32 * 1024**2
        elif fault == "extra_field":
            saved["zlib"] = saved["zstd"]
        else:
            saved["schema"] = "cryptowatcher.shared-json.v999"
        saved["zstd"] = base64.b64encode(encoded).decode()
        store.db.execute("UPDATE runtime SET payload_json=? WHERE key=?", (encode(saved), BLOB_PREFIX + key))
    if fault in ("size", "unknown_size", "oversized_window"):

        def forbidden(*args, **kw):
            pytest.fail("Malformed frame identity/window must be rejected before decoder allocation")

        monkeypatch.setattr(zstandard, "ZstdDecompressor", forbidden)
    with pytest.raises(StateError):
        Codec(store.db).load(key)


def test_zstd_body_encoding_rollback_does_not_admit_a_cached_dangling_body(store):
    codec = Codec(store.db, body_encoding="zstd")
    value = {"source": "frozen" * 100}
    with pytest.raises(RuntimeError), store.db:
        key = codec.save(value)
        raise RuntimeError("rollback")
    with pytest.raises(StateError, match="Missing shared snapshot"):
        Codec(store.db).load(key)
    with store.db:
        assert codec.save(value) == key
    assert Codec(store.db).load(key) == value


def large_record(kind, name="one", revision=1):
    value = record(kind, name, revision)
    value["settings_snapshot"] = {
        "profile": "custom",
        "alert_universe_mode": "top",
        "thresholds": {"price": 2.0, "spike": 3},
        "notes": "shared" * 200,
    }
    value["source_inputs"] = [
        {
            "start": i * 60000,
            "step": 60000,
            "close": 100.0 + i / 10,
            "open": 100,
            "high": 102.0,
            "low": 98.0,
            "received_ms": i * 60000 + 60000,
            "source": "rest",
            "turnover": 100000.0,
        }
        for i in range(48)
    ]
    value["source_context"] = {"instrument": {"symbol": "BTCUSDT", "attributes": "source" * 100}}
    return value


def test_overlapping_windows_and_revisions_share_bodies_and_keep_public_types(store):
    history = History(store)
    first = large_record("event")
    second = large_record("event", revision=2)
    second["source_inputs"] = second["source_inputs"][16:]
    with store.db:
        history.version("event", first)
        history.version("event", second)
        history.version("outcome", large_record("outcome"))
    assert encode(history.get("event:one:0000000001")) == encode(first)
    assert encode(history.get("event-head:one")) == encode(second)
    assert type(history.get("event-head:one")["settings_snapshot"]["thresholds"]["price"]) is float
    assert type(history.get("event-head:one")["settings_snapshot"]["thresholds"]["spike"]) is int
    assert as_of(history, "event", "one", first["available_at_ms"]) == first
    # All three records use the same three UTC-aligned blocks, settings and context.
    assert (
        store.db.execute(
            "SELECT COUNT(*) FROM runtime WHERE key>=? AND key<?", (BLOB_PREFIX, BLOB_PREFIX + "~")
        ).fetchone()[0]
        == 5
    )
    total = store.db.execute(
        "SELECT SUM(length(payload_json)) FROM runtime WHERE key>=? AND key<?", (PREFIX, PREFIX + "~")
    ).fetchone()[0]
    assert total < (len(encode(first)) + len(encode(second))) * 0.5
    assert MARKER not in history.get("event-head:one")


def test_new_writes_restart_on_legacy_database_without_rebuild(store):
    """New producers share snapshots while retaining old bytes on the same DB."""
    from cryptowatcher.storage import Store

    history = History(store)
    legacy = large_record("event", "legacy")
    private_bytes = '{ "body": "unchanged private transport", "revision": 1.00 }'
    with store.db:
        history.put("event:legacy:0000000001", legacy)
        history.put("event-head:legacy", legacy)
        store.db.execute("INSERT INTO runtime VALUES (?,?)", ("private-transport", private_bytes))
    preserved_keys = (PREFIX + "event:legacy:0000000001", PREFIX + "event-head:legacy", "private-transport")
    before = dict(
        store.db.execute("SELECT key,payload_json FROM runtime WHERE key IN (?,?,?)", preserved_keys)
    )
    settings_before = store.snapshot()
    schema_before = store.db.execute("PRAGMA user_version").fetchone()[0]
    body_count = []
    for name in ("new-one", "new-two"):
        with store.db:
            for kind in ("event", "outcome"):
                history.version(kind, large_record(kind, name))
        body_count.append(
            store.db.execute("SELECT COUNT(*) FROM runtime WHERE key LIKE 'historyblob:v1:%'").fetchone()[0]
        )
    assert body_count[0] > 0 and body_count[0] == body_count[1]
    with store.db:
        history.version("event", large_record("event", "legacy", revision=2))
    # Only the mutable legacy head advances; the historical revision and private
    # serialized request remain exact, without converting any prior record.
    assert store.get_runtime(PREFIX + "event-head:legacy")["schema"] == HEAD_SCHEMA
    assert dict(
        store.db.execute(
            "SELECT key,payload_json FROM runtime WHERE key IN (?,?)", (preserved_keys[0], preserved_keys[2])
        )
    ) == {key: before[key] for key in (preserved_keys[0], preserved_keys[2])}
    with closing(Store(store.path, store.owner_id, readonly=True)) as reopened:
        reader = History(reopened)
        assert reader.get("event:legacy:0000000001") == legacy
        assert reader.get("event-head:legacy") == large_record("event", "legacy", revision=2)
        for name in ("new-one", "new-two"):
            for kind in ("event", "outcome"):
                assert reader.get(f"{kind}-head:{name}") == large_record(kind, name)
        assert reopened.snapshot() == settings_before
        assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == schema_before


@pytest.mark.parametrize("kind", ["episode", "comparison"])
def test_episode_and_comparison_heads_are_references_with_complete_readback(store, kind):
    history = History(store)
    value = large_record(kind)
    value[f"{kind}_id"] = "one"
    value["samples"] = [
        {
            "source_inputs": value["source_inputs"],
            "future_inputs": value["source_inputs"],
            "outcomes": {"5": {"metrics": {"price_return_pct": 1.2}}},
        }
    ]
    with store.db:
        history.version(kind, value)
    saved = store.get_runtime(PREFIX + f"{kind}-head:one")
    assert saved["schema"] == HEAD_SCHEMA and "samples" not in saved
    assert encode(history.get(f"{kind}-head:one")) == encode(value)


@pytest.mark.parametrize(
    "fault", ["missing_body", "corrupt_bytes", "size", "projection", "order", "unknown_schema"]
)
def test_corrupt_or_missing_shared_data_never_returns_partial_history(store, fault):
    history = History(store)
    value = large_record("event")
    with store.db:
        history.version("event", value)
        key = PREFIX + "event:one:0000000001"
        saved = store.get_runtime(key)
        fields = saved[MARKER]["fields"]
        body_key = BLOB_PREFIX + fields["source_inputs"]["ids"][0]
        if fault == "missing_body":
            store.db.execute("DELETE FROM runtime WHERE key=?", (body_key,))
        elif fault in ("corrupt_bytes", "size"):
            body = store.get_runtime(body_key)
            body["zlib"] = "not base64" if fault == "corrupt_bytes" else body["zlib"]
            if fault == "size":
                body["bytes"] -= 1
            store.db.execute("UPDATE runtime SET payload_json=? WHERE key=?", (encode(body), body_key))
        elif fault == "projection":
            saved["settings_snapshot"]["profile"] = "sensitive"
        elif fault == "order":
            fields["source_inputs"]["ids"].reverse()
        else:
            saved[MARKER]["schema"] = "future-schema"
        if fault in ("projection", "order", "unknown_schema"):
            store.db.execute("UPDATE runtime SET payload_json=? WHERE key=?", (encode(saved), key))
    with pytest.raises(StateError):
        history.get("event-head:one")
    assert (
        json.loads(store.db.execute("SELECT payload_json FROM runtime WHERE key=?", (key,)).fetchone()[0])
        == saved
    )


def test_blob_collision_is_detected_and_new_references_rollback_atomically(store):
    codec = Codec(store.db)
    payload = {"context": "shared" * 100}
    with store.db:
        key = codec.save(payload)
    with store.db:
        store.db.execute(
            "UPDATE runtime SET payload_json=? WHERE key=?",
            (encode({"schema": "corrupt"}), BLOB_PREFIX + key),
        )
    with pytest.raises(StateError), store.db:
        codec.save(payload)
    before = store.db.execute("SELECT COUNT(*) FROM runtime").fetchone()[0]
    with pytest.raises(RuntimeError), store.db:
        History(store).version("event", large_record("event"))
        raise RuntimeError("simulated transaction abort")
    assert store.db.execute("SELECT COUNT(*) FROM runtime").fetchone()[0] == before


def test_immutable_revision_keeps_json_numeric_types(store):
    history = History(store)
    original = large_record("event")
    with store.db:
        history.version("event", original)
    with pytest.raises(StateError, match="Conflicting immutable"), store.db:
        history.version("event", {**original, "reference_price": 100.0})
    assert type(history.get("event:one:0000000001")["reference_price"]) is int


def test_cached_encoding_does_not_leave_dangling_references_after_rollback(store):
    history = History(store)
    value = large_record("event")
    with pytest.raises(RuntimeError), store.db:
        history.version("event", value)
        raise RuntimeError("abort")
    assert history.codec.encoded
    with store.db:
        history.version("event", value)
    assert history.get("event-head:one") == value
    assert history.codec.cache_bytes <= CACHE_BYTES


def test_snapshot_encoding_cache_is_bounded_without_losing_evicted_bodies(store):
    codec = Codec(store.db)
    keys = []
    with store.db:
        for i in range(48):
            keys.append(codec.save({"value": str(i) + "x" * 100000}))
    assert codec.cache_bytes <= CACHE_BYTES and len(codec.encoded) < len(keys)
    assert codec.load(keys[0]) == {"value": "0" + "x" * 100000}


def test_validated_cache_rechecks_stored_bytes_and_returns_independent_objects(store):
    codec = Codec(store.db)
    value = {"candles": [{"close": 1.0, "notes": "цена"}]}
    with store.db:
        key = codec.save(value)
    first = codec.load(key)
    first["candles"][0]["close"] = 999
    assert encode(codec.load(key)) == encode(value)
    assert codec.validated
    with pytest.raises(StateError), store.db:
        store.db.execute(
            "UPDATE runtime SET payload_json=? WHERE key=?",
            (encode({"schema": "corrupt"}), BLOB_PREFIX + key),
        )
        codec.load(key)
    assert codec.load(key) == value  # Rolled-back corruption does not poison evidence.
    with store.db:
        store.db.execute("DELETE FROM runtime WHERE key=?", (BLOB_PREFIX + key,))
    with pytest.raises(StateError):
        codec.load(key)


@pytest.mark.parametrize("fault", ["missing", "schema", "size", "encoding", "wrong_hash", "invalid_json"])
def test_scanned_body_validation_rejects_conflicts_even_with_valid_database_cache(store, fault):
    codec = Codec(store.db)
    value = {"source": [{"close": 1.0, "notes": "цена"}]}
    with store.db:
        key = codec.save(value)
    row = store.db.execute("SELECT payload_json FROM runtime WHERE key=?", (BLOB_PREFIX + key,)).fetchone()
    assert codec.validate_raw(key, row[0]) == (encode(value).encode(), None)
    # The current DB/cache remain valid: a bad scanned payload must fail itself,
    # rather than accepting a second lookup of a different, valid row.
    body = json.loads(row[0])
    candidate = key
    if fault == "missing":
        payload = None
    elif fault == "invalid_json":
        payload = "{"
    else:
        if fault == "schema":
            body["schema"] = "future-schema"
        elif fault == "size":
            body["bytes"] += 1
        elif fault == "encoding":
            body["zlib"] = "invalid base64"
        else:
            candidate = "f" * 64
        payload = encode(body)
    with pytest.raises(StateError):
        codec.validate_raw(candidate, payload)
    assert codec.load(key) == value


def test_validated_cache_is_bounded_and_does_not_admit_rolled_back_rows(store):
    codec = Codec(store.db)
    with pytest.raises(RuntimeError), store.db:
        key = codec.save({"large": "x" * 100000})
        codec.load(key)
        raise RuntimeError("abort")
    with pytest.raises(StateError):
        codec.load(key)
    with store.db:
        for i in range(48):
            key = codec.save({"large": str(i) + "x" * 100000})
            assert codec.load(key)["large"].startswith(str(i))
    assert codec.validated_bytes <= CACHE_BYTES and len(codec.validated) < 48


def test_window_hash_concatenation_preserves_types_unicode_and_irregular_blocks(store):
    codec = Codec(store.db)
    value = {
        "source_inputs": [
            {"start": i * 60000, "step": 60000, "price": i / 3, "label": 'цена"\\'} for i in range(37)
        ]
    }
    with store.db:
        packed = codec.pack(value)
    assert encode(codec.unpack(packed)) == encode(value)
    assert (
        packed[MARKER]["fields"]["source_inputs"]["sha256"]
        == __import__("hashlib").sha256(encode(value["source_inputs"]).encode()).hexdigest()
    )


@pytest.mark.parametrize("kind", ["event", "outcome", "episode", "comparison"])
def test_json1_packing_and_raw_restore_preserve_full_canonical_values(store, kind):
    codec = Codec(store.db)
    value = large_record(kind)
    value.update(flag=True, no=False, empty=None, escaped='цена\n"\\', signed_zero=-0.0)
    value["samples"] = [{"source_inputs": value["source_inputs"], "future_inputs": value["source_inputs"]}]
    with store.db:
        packed = codec.pack_canonical(encode(value))
    assert codec.canonical_json(packed) == encode(value).encode()
    assert encode(codec.unpack(packed)) == encode(value)
    with store.db:
        again = codec.pack_canonical(encode(packed))
    assert codec.canonical_json(again) == encode(value).encode()


@pytest.mark.parametrize("placement", ["scalar", "window", "nested"])
def test_json1_large_integer_fallback_does_not_round_history(store, placement):
    codec = Codec(store.db)
    number = 2**100 + 123
    value = (
        {"number": number}
        if placement == "scalar"
        else {"source_inputs": [number]}
        if placement == "window"
        else {"source_inputs": [{"start": number, "step": 60000, "value": number}]}
    )
    with store.db:
        packed = codec.pack_canonical(encode(value))
    assert codec.canonical_json(packed) == encode(value).encode()


@pytest.mark.parametrize("number", [2.095370825591371, 1.0000000000000002, 5e-324, 1e308, -0.0])
def test_json1_real_lexemes_keep_exact_float_values_in_scalar_and_window(store, number):
    # SQLite 3.46.1 json_each.value rounds the first real production strength
    # to 2.0953708255913712. Decode the original JSON token instead.
    value = {"strength": number, "future_inputs": [number, {"close": number}]}
    codec = Codec(store.db)
    with store.db:
        packed = codec.pack_canonical(encode(value))
    assert codec.canonical_json(packed) == encode(value).encode()
    assert encode(codec.unpack(packed)) == encode(value)


@pytest.mark.parametrize(
    "fault", ["projection", "unknown_schema", "wrong_hash", "missing_body", "reordered_parts"]
)
def test_raw_restore_keeps_complete_validation_gates(store, fault):
    codec = Codec(store.db)
    with store.db:
        packed = codec.pack_canonical(encode(large_record("event")))
    assert codec.canonical_json(packed)
    fields = packed[MARKER]["fields"]
    if fault == "projection":
        packed["settings_snapshot"]["profile"] = "foreign"
    elif fault == "unknown_schema":
        packed[MARKER]["schema"] = "future"
    elif fault == "wrong_hash":
        fields["source_inputs"]["sha256"] = "0" * 64
    elif fault == "reordered_parts":
        fields["source_inputs"]["ids"].reverse()
    else:
        with store.db:
            store.db.execute(
                "DELETE FROM runtime WHERE key=?", (BLOB_PREFIX + fields["source_inputs"]["ids"][0],)
            )
    with pytest.raises(StateError):
        codec.canonical_json(packed)


def test_raw_restore_does_not_substitute_tokens_in_unrelated_data(store, monkeypatch):
    from cryptowatcher import history_codec

    monkeypatch.setattr(history_codec.os, "urandom", lambda size: b"\0" * size)
    value = large_record("event")
    value["ordinary"] = "\0CWJSON:" + "0" * 24 + ":0\0"
    codec = Codec(store.db)
    with store.db:
        packed = codec.pack_canonical(encode(value))
    with pytest.raises(StateError, match="Ambiguous"):
        codec.canonical_json(packed)
    assert encode(codec.unpack(packed)) == encode(value)
