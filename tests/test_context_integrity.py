import hashlib
import sqlite3
from contextlib import closing

import pytest

from cryptowatcher.context_integrity import convert, restore
from cryptowatcher.market_storage import context_window, enable, encoded, restore_context, save_context
from cryptowatcher.storage import Store, encode
from tests.test_outcomes import NOW


def old_reference(store):
    value = {
        "ticker": {
            "available_at_ms": NOW,
            "market_observation_ms": NOW - 1,
            "values": {"turnover": 2.0, "zero": -0.0},
        },
        "instrument": None,
    }
    point = save_context(store.db, "ticker", "spot/BTCUSDT", value["ticker"], NOW)
    return value, {
        "mode": "contexts",
        "points": {"ticker": list(point), "instrument": None},
        "sha256": hashlib.sha256(encode(value).encode()).hexdigest(),
    }


def test_context_digest_is_shared_exact_immutable_and_readable_after_reopen(store):
    enable(store.db)
    with store.db:
        value, old = old_reference(store)
        ref = convert(store.db, old)
        for _ in range(20):
            assert context_window(store.db, "spot/BTCUSDT/1m", value, NOW) == ref
    assert ref["mode"] == "contexts2" and "sha256" not in ref
    assert encode(value).encode() == restore(store.db, ref) == restore_context(store.db, old)
    assert tuple(
        store.db.execute(
            "SELECT records,logical_bytes FROM market_counts WHERE category='context_integrity'"
        ).fetchone()
    ) == (1, 32)
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        assert restore(reader.db, ref) == encode(value).encode()
    for sql in ("UPDATE market_context SET integrity=NULL", "UPDATE market_context SET timing=x'4a7b7d'"):
        with pytest.raises(sqlite3.IntegrityError), store.db:
            store.db.execute(sql)


def test_bad_legacy_hash_cannot_freeze_rows_and_binding_rollback_is_atomic(store):
    enable(store.db)
    with store.db:
        _, old = old_reference(store)
    with pytest.raises(ValueError, match="hash mismatch"), store.db:
        convert(store.db, {**old, "sha256": "0" * 64})
    assert (
        store.db.execute("SELECT COUNT(*) FROM market_context WHERE integrity IS NOT NULL").fetchone()[0] == 0
    )
    with pytest.raises(RuntimeError), store.db:
        convert(store.db, old)
        raise RuntimeError("interrupted conversion")
    assert (
        store.db.execute("SELECT COUNT(*) FROM market_context WHERE integrity IS NOT NULL").fetchone()[0] == 0
    )
    with store.db:
        ref = convert(store.db, old)
    # Corrupt only this disposable row after explicitly removing its guard.
    store.db.execute("DROP TRIGGER market_context_frozen")
    with store.db:
        store.db.execute(
            "UPDATE market_context SET timing=?",
            (encoded({"available_at_ms": NOW, "market_observation_ms": NOW + 1}),),
        )
    with pytest.raises(ValueError, match="hash mismatch"):
        restore(store.db, ref)
    with pytest.raises(ValueError, match="hash mismatch"):
        restore_context(store.db, old)


def test_context_freezing_preserves_types_and_rejects_coordinate_and_scalar_type_changes(store):
    enable(store.db)
    with store.db:
        value, old = old_reference(store)
        ref = convert(store.db, old)
    changed = {**value["ticker"], "values": {"turnover": 2, "zero": -0.0}}
    with pytest.raises(ValueError, match="Conflicting public"), store.db:
        save_context(store.db, "ticker", "spot/BTCUSDT", changed, NOW)
    assert restore(store.db, ref) == encode(value).encode()
    for point in ([True, NOW], [0, NOW], [1, float(NOW)], [1, 2**63], [1, NOW, "ticker"]):
        with pytest.raises(ValueError, match="coordinate"):
            restore(store.db, {"mode": "contexts2", "points": {"ticker": point}})
    with store.db:
        store.db.execute("DELETE FROM market_context")
    assert tuple(
        store.db.execute(
            "SELECT records,logical_bytes FROM market_counts WHERE category='context_integrity'"
        ).fetchone()
    ) == (0, 0)
    with pytest.raises(ValueError, match="Missing retained"):
        restore(store.db, ref)
