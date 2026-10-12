import json

import pytest

from cryptowatcher.analytics import as_of
from cryptowatcher.events import HEAD_SCHEMA, PREFIX, History
from cryptowatcher.history_codec import BLOB_PREFIX, CACHE_BYTES, MARKER, Codec
from cryptowatcher.storage import StateError, encode

from .test_history_heads import record


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
