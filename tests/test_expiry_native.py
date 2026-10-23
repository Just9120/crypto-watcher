import sqlite3

import pytest

from cryptowatcher import expiry_index, key_registry
from cryptowatcher.market_storage import enable


def test_retracking_a_changed_record_keeps_its_index_charge_without_delete_insert_churn(store):
    from cryptowatcher.events import History

    enable(store.db)
    store.bounded = True
    store.clock_ms = lambda: 1_800_000_000_000
    history = History(store)
    key = "condition:spot/BTCUSDT/1m:price:UP"
    with store.db:
        history.put(key, {"last_ms": 1})
    before = tuple(
        store.db.execute(
            "SELECT records,logical_bytes,inserted_bytes,deleted_bytes FROM market_counts "
            "WHERE category='service_indexes'"
        ).fetchone()
    )
    binding = expiry_index.hot_binding(store.db, "radar:v1:" + key)
    with store.db:
        history.put(key, {"last_ms": 2})
    after = tuple(
        store.db.execute(
            "SELECT records,logical_bytes,inserted_bytes,deleted_bytes FROM market_counts "
            "WHERE category='service_indexes'"
        ).fetchone()
    )
    assert history.get(key) == {"last_ms": 2}
    assert expiry_index.hot_binding(store.db, "radar:v1:" + key) == binding
    assert after == before


def test_native_owner_batches_and_bindings_mix_legacy_text_and_compact_keys_with_a_small_budget(store):
    enable(store.db)
    keys = [f"radar:v1:outcome-job:00000000-0000-0000-0000-{i:012x}" for i in range(3000)]
    with store.db:
        for i, key in enumerate(keys):
            store.db.execute("INSERT INTO runtime VALUES(?,?)", (key, "{}"))
            if i == 2998:
                store.db.execute(
                    "INSERT INTO market_expiry_legacy VALUES(?,?,?,?)", (key, 123, "job", "selected")
                )
            else:
                # Exercise both old physical text and compact registry tokens.
                store.db.execute(
                    "INSERT INTO market_runtime_keys(external) VALUES(?)",
                    (key if i % 2 else key_registry.pack(key),),
                )
                store.db.execute(
                    "INSERT INTO market_lifetimes VALUES(?,?,?,?)",
                    (key, 123, "job", "selected" if i == 2999 else "other"),
                )
    steps = 0

    def budget():
        nonlocal steps
        steps += 1
        return int(steps > 25)

    store.db.set_progress_handler(budget, 100)
    try:
        for key, expected in (
            (keys[-1], (123, "job", "selected")),
            (keys[-2], (123, "job", "selected")),
            ("absent", None),
        ):
            steps = 0
            assert expiry_index.hot_binding(store.db, key) == expected
        steps = 0
        assert set(expiry_index.owned_hot(store.db, "selected")) == set(keys[-2:])
        steps = 0
        assert expiry_index.owned_hot(store.db, "absent") == []
    finally:
        store.db.set_progress_handler(None, 0)
    with store.db:
        removed = []
        while batch := expiry_index.owned_hot(store.db, "selected", limit=1):
            removed.extend(batch)
            expiry_index.remove(store.db, batch[0])
        assert set(removed) == set(keys[-2:])
        store.db.execute(
            "INSERT INTO market_expiry_legacy VALUES(?,?,?,?)", (keys[0], 999, "job", "conflict")
        )
        with pytest.raises(ValueError, match="Conflicting native"):
            expiry_index.hot_binding(store.db, keys[0])
    for limit in (0, -1, True, 1025):
        with pytest.raises(ValueError, match="batch"):
            expiry_index.owned_hot(store.db, "other", limit)


def test_unmigrated_expiry_table_keeps_exact_owner_and_lease():
    db = sqlite3.connect(":memory:")
    try:
        db.execute(
            "CREATE TABLE market_lifetimes(key TEXT PRIMARY KEY,expires INTEGER,category TEXT,event_id TEXT)"
        )
        db.execute("INSERT INTO market_lifetimes VALUES('event',123,'event','owner')")
        assert expiry_index.hot_binding(db, "event") == (123, "event", "owner")
        assert expiry_index.owned_hot(db, "owner", 1) == ["event"]
        assert expiry_index.hot_binding(db, "absent") is None
        assert expiry_index.owned_hot(db, "absent") == []
        expiry_index.put(db, "event", 456, "changed", "new-owner")
        assert expiry_index.hot_binding(db, "event") == (456, "changed", "new-owner")
        assert expiry_index.owned_hot(db, "owner") == []
        assert expiry_index.owned_hot(db, "new-owner") == ["event"]
    finally:
        db.close()


@pytest.mark.parametrize("legacy_key", [False, True])
def test_native_assignment_preserves_key_identity_and_atomic_owner_refcounts(store, legacy_key):
    enable(store.db)
    key = "radar:v1:outcome-job:00000000-0000-0000-0000-000000000001"
    with store.db:
        store.db.execute("INSERT INTO runtime VALUES(?,?)", (key, "{}"))
        store.db.execute(
            "INSERT INTO market_runtime_keys(external) VALUES(?)",
            (key if legacy_key else key_registry.pack(key),),
        )
        expiry_index.put(store.db, key, 123, "outcome-job", "old-owner")
    identity = store.db.execute("SELECT rid FROM market_expiry").fetchone()[0]
    with store.db:
        expiry_index.put(store.db, key, 456, "outcome-job", "new-owner")
    assert expiry_index.hot_binding(store.db, key) == (456, "outcome-job", "new-owner")
    assert store.db.execute("SELECT rid FROM market_expiry").fetchone()[0] == identity
    assert dict(store.db.execute("SELECT external,refs FROM market_event_ids")) == {
        "old-owner": 0,
        "new-owner": 1,
    }
    with pytest.raises(RuntimeError, match="rollback"):
        with store.db:
            expiry_index.put(store.db, key, 789, "changed", "rolled-back")
            raise RuntimeError("rollback")
    assert expiry_index.hot_binding(store.db, key) == (456, "outcome-job", "new-owner")
    assert dict(store.db.execute("SELECT external,refs FROM market_event_ids")) == {
        "old-owner": 0,
        "new-owner": 1,
    }
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()
