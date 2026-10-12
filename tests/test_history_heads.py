import json

import pytest

from cryptowatcher.events import HEAD_SCHEMA, PREFIX, History
from cryptowatcher.storage import StateError, encode


def record(kind, record_id="one", revision=1):
    return {
        "schema": f"cryptowatcher.{kind}.v1",
        f"{kind}_id": record_id,
        "revision": revision,
        "event_id": record_id,
        "event_revision": 1,
        "episode_id": "episode",
        "reference_time_ms": 1800000000000,
        "reference_price": 100,
        "market_observation_ms": 1800000000000,
        "available_at_ms": 1800000001000 + revision,
        "market_type": "spot",
        "detector_id": "price",
        "detector_version": "1",
        "symbol": "BTCUSDT",
        "timeframe": "1m",
        "direction": "UP",
        "strength": revision,
        "state": "COMPLETE",
        "horizon_min": 60,
        "methodology_version": "1",
        "classification": "CONTINUATION",
        "noise_threshold_pct": 0.1,
        "metrics": {"price_return_pct": 2},
        "source_inputs": [{"start": i, "close": 100, "provenance": "x" * 200} for i in range(60)],
        "settings_snapshot": {"lookback": 20, "version": 1},
    }


@pytest.mark.parametrize("kind", ["event", "outcome"])
def test_compact_head_preserves_full_record_revisions_and_mixed_legacy_reads(store, kind):
    history = History(store)
    legacy = record(kind, "legacy")
    first = record(kind)
    second = record(kind, revision=2)
    with store.db:
        history.put(f"{kind}:legacy:0000000001", legacy)
        history.put(f"{kind}-head:legacy", legacy)
        history.version(kind, first)
        history.version(kind, second)
        history.version(kind, first)
    assert history.get(f"{kind}-head:one") == second
    assert dict(history.items(f"{kind}-head:")) == {f"{kind}-head:legacy": legacy, f"{kind}-head:one": second}
    assert dict(history.scan(f"{kind}-head:")) == dict(history.items(f"{kind}-head:"))
    assert history.get(f"{kind}:one:0000000001") == first
    assert history.get(f"{kind}:one:0000000002") == second
    saved = store.get_runtime(PREFIX + f"{kind}-head:one")
    assert saved["schema"] == HEAD_SCHEMA and saved["revision_key"] == f"{kind}:one:0000000002"
    assert "source_inputs" not in saved and "settings_snapshot" not in saved
    assert len(encode(saved)) < len(encode(second)) / 10
    # The SQL projection retains observable grouping/filter fields, not the large inputs.
    assert (
        saved["market_observation_ms"] == second["market_observation_ms"]
        if kind == "event"
        else saved["metrics"] == second["metrics"]
    )
    with pytest.raises(StateError, match="Conflicting immutable"):
        with store.db:
            history.version(kind, {**second, "source_inputs": []})
    if kind == "event":
        with pytest.raises(StateError, match="reference is immutable"):
            with store.db:
                history.version(kind, {**second, "revision": 3, "reference_price": 101})


@pytest.mark.parametrize("kind", ["event", "outcome"])
@pytest.mark.parametrize("failure", ["missing", "other_record", "other_kind", "projection"])
def test_head_references_fail_closed_instead_of_returning_partial_records(store, kind, failure):
    history = History(store)
    value = record(kind)
    with store.db:
        history.version(kind, value)
        head = store.get_runtime(PREFIX + f"{kind}-head:one")
        if failure == "missing":
            history.delete(f"{kind}:one:0000000001")
        elif failure == "other_record":
            head["revision_key"] = f"{kind}:other:0000000001"
        elif failure == "other_kind":
            head["revision_key"] = "episode:one:0000000001"
        else:
            head["event_id"] = "corrupt"
        history.put(f"{kind}-head:one", head)
    with pytest.raises(StateError, match="history head"):
        history.get(f"{kind}-head:one")


def test_existing_full_heads_and_revision_bytes_are_not_bulk_rewritten(store):
    history = History(store)
    legacy = record("event")
    with store.db:
        history.put("event:one:0000000001", legacy)
        history.put("event-head:one", legacy)
    before = store.db.execute(
        "SELECT payload_json FROM runtime WHERE key=?", (PREFIX + "event:one:0000000001",)
    ).fetchone()[0]
    with store.db:
        history.version("event", legacy)
    assert store.get_runtime(PREFIX + "event-head:one") == legacy
    assert (
        store.db.execute(
            "SELECT payload_json FROM runtime WHERE key=?", (PREFIX + "event:one:0000000001",)
        ).fetchone()[0]
        == before
    )
    assert json.loads(before) == legacy
