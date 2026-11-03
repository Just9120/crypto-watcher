"""Actual retention bindings remain authoritative across grouped reads."""

from contextlib import closing

import pytest

from cryptowatcher import expiry_index, key_registry
from cryptowatcher.domain_repository import install
from cryptowatcher.events import EventEngine
from cryptowatcher.storage_lifecycle import track
from tests.test_events import NOW, window
from tests.test_runtime_repository import opened


def prepared(path):
    store = opened(path, True)
    install(store)
    engine = EventEngine(store)
    engine.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True)
    event = engine.history.items("event:")[0][1]
    return store, engine.history, event


def test_pair_matches_individual_authoritative_reads_and_rejects_duplicate_binding(tmp_path):
    store, history, event = prepared(tmp_path / "pair.sqlite3")
    with closing(store):
        key = "radar:v1:agent:intent:fixture"
        parent = "radar:v1:event-head:" + event["event_id"]
        with store.db:
            history.put("agent:intent:fixture", {"event_id": event["event_id"]})
        hot, leases, canonical = expiry_index.paired_expiries(store, (key, parent))
        assert hot == {k: expiry_index.hot_binding(store.db, k) for k in (key, parent)}
        assert leases == {k: expiry_index.expires(store.db, k) for k in (key, parent)}
        assert canonical == {key, parent}
        with store.db:
            store.db.execute(
                "INSERT INTO market_expiry_legacy VALUES(?,?,?,?)",
                (key, 123, "conflicting", event["event_id"]),
            )
            with pytest.raises(ValueError, match="Conflicting native"):
                expiry_index.paired_expiries(store, (key, parent))


def test_identical_binding_keeps_counters_and_changed_native_byte_charge_is_repaired(tmp_path):
    store, history, event = prepared(tmp_path / "repeat.sqlite3")
    with closing(store):
        key = "radar:v1:agent:intent:fixture"
        value = {"event_id": event["event_id"], "created_ms": NOW}
        with store.db:
            history.put("agent:intent:fixture", value)
        before = list(store.db.iterdump())
        with store.db:
            track(store, key, value)
        assert list(store.db.iterdump()) == before
        with store.db:
            store.db.execute(
                "UPDATE market_expiry SET key_bytes=key_bytes+10 WHERE rid="
                "(SELECT id FROM market_runtime_keys WHERE external=?)",
                (key_registry.pack(key),),
            )
            track(store, key, value)
        after = list(store.db.iterdump())
        # Repair preserves the cumulative inserted/deleted-byte history.
        assert [row for row in after if not row.startswith('INSERT INTO "market_counts"')] == [
            row for row in before if not row.startswith('INSERT INTO "market_counts"')
        ]
        charge = store.db.execute(
            "SELECT logical_bytes,inserted_bytes,deleted_bytes FROM market_counts WHERE category='service_indexes'"
        ).fetchone()
        assert charge[1] - charge[2] == charge[0]
        assert charge[2] == 10


def test_pair_has_a_fixed_key_limit_and_cannot_keep_rows_after_rollback(tmp_path):
    store, history, event = prepared(tmp_path / "rollback.sqlite3")
    with closing(store):
        with pytest.raises(ValueError, match="one or two"):
            expiry_index.paired_expiries(store, ("a", "b", "c"))
        key = "radar:v1:agent:intent:rollback"
        assert expiry_index.paired_expiries(store, (key,))[0][key] is None
        with pytest.raises(RuntimeError), store.db:
            history.put("agent:intent:rollback", {"event_id": event["event_id"]})
            assert expiry_index.paired_expiries(store, (key,))[0][key] is not None
            raise RuntimeError("rollback")
        assert expiry_index.paired_expiries(store, (key,))[0][key] is None
