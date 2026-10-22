import hashlib
import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest

from cryptowatcher import context_tile_codec as codec
from cryptowatcher import context_tiles as tiles
from cryptowatcher.context_integrity import convert, restore
from cryptowatcher.market_storage import DAY, enable, read_context, restore_context, save_context
from cryptowatcher.storage import StateError, Store, encode
from tests.test_outcomes import NOW

BASE = NOW // DAY * DAY + 600000


def values(index):
    return {
        "available_at_ms": BASE + index * 60000,
        "market_observation_ms": BASE + index * 60000 - 17,
        "values": {"symbol": "BTCUSDT", "price": str(100 + index), "zero": -0.0},
        "extra": [None, False, True, 0, 0.0, "тест"],
    }


def setup(store, count=8, *, freeze=True):
    enable(store.db)
    store.bounded = True
    store.clock_ms = lambda: BASE
    keys, refs, old = [], [], []
    with store.db:
        for index in range(count):
            value = values(index)
            key = save_context(store.db, "ticker", "spot/BTCUSDT", value, BASE)
            keys.append(key)
            legacy = {
                "mode": "contexts",
                "points": {"ticker": list(key)},
                "sha256": hashlib.sha256(encode({"ticker": value}).encode()).hexdigest(),
            }
            old.append(legacy)
            refs.append(convert(store.db, legacy) if freeze else None)
    tiles.install(store.db, store.cached_bytes)
    return keys, refs, old


def tile(store, identity=1):
    return tiles._tile(
        store.db.execute(f"SELECT {tiles.FIELDS} FROM {tiles.TABLE} WHERE id=?", (identity,)).fetchone()
    )


def counts(store):
    return {row[0]: tuple(row[1:3]) for row in store.db.execute("SELECT * FROM market_counts")}


def test_warmed_timestamp_index_cannot_keep_a_rolled_back_sql_lease(store):
    keys, refs, _ = setup(store)
    tiles.publish(store.db, keys)
    store.db.commit()
    original = tile(store)
    codec.times(original, store.cached_bytes)

    def snapshot():
        return {
            name: sorted(
                repr(tuple(row))
                for row in store.db.execute('SELECT * FROM "' + name.replace('"', '""') + '"')
            )
            for (name,) in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

    before = snapshot()
    due = codec.stats(original)["first_due"]
    with pytest.raises(RuntimeError, match="rollback"), store.db:
        save_context(store.db, "ticker", "spot/BTCUSDT", values(0), BASE + 7 * DAY)
        assert codec.stats(tile(store), store.cached_bytes)["first_due"] > due
        raise RuntimeError("rollback")
    assert snapshot() == before
    assert tile(store) == original
    assert codec.stats(tile(store), store.cached_bytes)["first_due"] == due
    assert restore(store.db, refs[0]) == encode({"ticker": values(0)}).encode()


def test_explicit_install_and_atomic_exact_frozen_publication_reopen_and_legacy(store):
    keys, refs, old = setup(store)
    before = [tiles.lookup(store.db, *key) for key in keys]
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        assert tiles.installed(reader.db)
        assert tiles.publish(store.db, keys) == 1
        after = [tiles.lookup(reader.db, *key) for key in keys]
        assert codec.signature(after) == codec.signature(before)
        for index, reference in enumerate(refs):
            expected = encode({"ticker": values(index)}).encode()
            assert restore(reader.db, reference) == restore_context(reader.db, old[index]) == expected
    assert store.db.execute("SELECT COUNT(*) FROM market_context").fetchone()[0] == 0
    assert store.db.execute("SELECT SUM(refs) FROM market_context_bodies").fetchone()[0] == 0
    assert counts(store)["context_tiles"][0] == len(keys)
    assert counts(store)["context"] == counts(store)["context_integrity"] == (0, 0)
    assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert list(store.db.execute("PRAGMA foreign_key_check")) == []
    with closing(Store(store.path, store.owner_id)) as writer:
        assert restore(writer.db, refs[0]) == encode({"ticker": values(0)}).encode()


def test_store_open_never_installs_and_old_connection_can_read_after_explicit_upgrade(store):
    enable(store.db)
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        assert not tiles.installed(reader.db)
        with store.db:
            key = save_context(store.db, "ticker", "spot/BTCUSDT", values(0))
        tiles.install(store.db)
        tiles.publish(store.db, [key])
        assert codec.public(tiles.lookup(reader.db, *key)) == values(0)


def test_mixed_indexed_asof_handles_late_point_inside_tile_and_separate_intervals(store):
    keys, _, _ = setup(store, 8)
    tiles.publish(store.db, keys[:4])
    tiles.publish(store.db, keys[4:])
    late = {**values(2), "available_at_ms": BASE + 150000}
    with store.db:
        save_context(store.db, "ticker", "spot/BTCUSDT", late)
    cases = [
        (BASE - 1, None),
        (BASE, values(0)),
        (BASE + 149999, values(2)),
        (BASE + 150000, late),
        (BASE + 179999, late),
        (BASE + 180000, values(3)),
        (BASE + 8 * 60000, values(7)),
    ]
    for cutoff, expected in cases:
        actual = read_context(store.db, "ticker", "spot/BTCUSDT", cutoff)
        assert encode(actual) == encode(expected)
    assert read_context(store.db, "instrument", "spot/BTCUSDT", BASE) is None
    assert read_context(store.db, "ticker", "linear/ABSENTUSDT", BASE) is None
    for sql in (
        "SELECT payload FROM market_context_tiles WHERE stream=1 AND kind='ticker' AND first_at<=1 ORDER BY first_at DESC LIMIT 1",
        "SELECT body FROM market_context WHERE stream=1 AND kind='ticker' AND at<=1 ORDER BY at DESC LIMIT 1",
    ):
        plan = " ".join(row[3] for row in store.db.execute("EXPLAIN QUERY PLAN " + sql))
        assert "SEARCH" in plan and "TEMP B-TREE" not in plan and "SCAN" not in plan


def test_cold_duplicate_save_renews_full_dependency_monotonically_and_rejects_changed_types(store):
    keys, refs, _ = setup(store)
    tiles.publish(store.db, keys)
    old = tile(store)
    late = BASE + 37 * DAY
    with store.db:
        assert save_context(store.db, "ticker", "spot/BTCUSDT", values(0), late) == keys[0]
        save_context(store.db, "ticker", "spot/BTCUSDT", values(0), BASE)
    updated = tile(store)
    assert updated.payload == old.payload and updated.seal == old.seal
    assert codec.get(updated, 0).anchor == late
    assert codec.get(updated, 0).purge == late + 30 * DAY
    assert restore(store.db, refs[0]) == encode({"ticker": values(0)}).encode()
    with pytest.raises(ValueError, match="Conflicting public"), store.db:
        save_context(store.db, "ticker", "spot/BTCUSDT", {**values(0), "extra": [0]}, late)
    assert store.db.execute("SELECT COUNT(*) FROM market_context").fetchone()[0] == 0


def test_late_freeze_and_stale_revision_cannot_lose_a_lease_or_frozen_bit(store):
    keys, _, old = setup(store, freeze=False)
    tiles.publish(store.db, keys)
    stale = tiles.cold(store.db, *keys[0])
    with store.db:
        ref = convert(store.db, old[0])
    assert restore(store.db, ref) == encode({"ticker": values(0)}).encode()
    with pytest.raises(StateError, match="Stale"), store.db:
        tiles.renew(store.db, stale, anchor=BASE + DAY, purge=BASE + 40 * DAY)
    assert codec.get(tile(store), 0).integrity is not None


def test_outer_rollback_and_publish_failure_preserve_hot_rows_counters_and_body_owners(store, monkeypatch):
    keys, refs, _ = setup(store)
    original = counts(store)
    store.db.execute("BEGIN")
    tiles.publish(store.db, keys)
    store.db.rollback()
    assert counts(store) == original
    assert store.db.execute("SELECT COUNT(*) FROM market_context_tiles").fetchone()[0] == 0
    original_values = tiles._values
    monkeypatch.setattr(tiles, "_values", lambda db, value: (*original_values(db, value)[:-1], 0))
    with pytest.raises(sqlite3.IntegrityError, match="metadata"):
        tiles.publish(store.db, keys)
    assert counts(store) == original
    assert store.db.execute("SELECT SUM(refs) FROM market_context_bodies").fetchone()[0] == 8
    assert restore(store.db, refs[0]) == encode({"ticker": values(0)}).encode()
    assert not store.db.in_transaction


def test_sql_guards_reject_early_release_lease_regression_rewrite_and_duplicate_hot_point(store):
    keys, _, _ = setup(store)
    tiles.publish(store.db, keys)
    old = tile(store)
    before = codec.get(old, 0)
    regress = codec.pack(1, [replace(point, anchor=0, purge=0) for point in codec.points(old)])
    rewrite = codec.pack(
        1, [replace(point, stable={"changed": True}, integrity=None) for point in codec.points(old)]
    )
    for candidate in (regress, rewrite):
        with pytest.raises(sqlite3.IntegrityError, match="Immutable"), store.db:
            tiles._update(store.db, old, candidate)
    with pytest.raises(sqlite3.IntegrityError, match="Retained"), store.db:
        store.db.execute("DELETE FROM market_context_tiles")
    with pytest.raises(sqlite3.IntegrityError, match="tiled"), store.db:
        body = store.db.execute("SELECT id FROM market_context_bodies LIMIT 1").fetchone()[0]
        store.db.execute(
            "INSERT INTO market_context(stream,kind,at,body,timing,purge_after) VALUES (?,?,?,?,?,?)",
            (*keys[0], body, b"J{}", before.purge),
        )
    assert codec.signature(codec.points(tile(store))) == codec.signature(codec.points(old))


def test_partial_expiry_retention_increase_and_final_release_preserve_remaining_frozen_points(store):
    keys, refs, _ = setup(store)
    tiles.publish(store.db, keys)
    with store.db:
        save_context(store.db, "ticker", "spot/BTCUSDT", values(7), BASE + 37 * DAY)
    store.clock_ms = lambda: BASE + 35 * DAY
    assert tiles.expire(store.db, store.clock_ms()) == 1
    with pytest.raises(ValueError, match="Missing retained"):
        restore(store.db, refs[0])
    assert restore(store.db, refs[7]) == encode({"ticker": values(7)}).encode()
    with store.db:
        store.db.execute(
            "UPDATE owner_settings SET settings_json=json_set(settings_json,'$.candle_retention_days',40)"
        )
    store.clock_ms = lambda: BASE + 36 * DAY
    assert tiles.expire(store.db, store.clock_ms()) == 0
    assert codec.get(tile(store), 0).purge == keys[1][2] + 40 * DAY
    store.clock_ms = lambda: BASE + 41 * DAY
    assert tiles.expire(store.db, store.clock_ms()) == 6
    assert counts(store)["context_tiles"][0] == 1
    store.clock_ms = lambda: BASE + 68 * DAY
    assert tiles.expire(store.db, store.clock_ms()) == 1
    assert counts(store)["context_tiles"] == (0, 0)
    assert store.db.execute("SELECT COUNT(*) FROM market_context_tiles").fetchone()[0] == 0
    assert tiles.expire(store.db, store.clock_ms()) == 0


@pytest.mark.parametrize("failure", ["missing", "unknown", "unmarked", "transaction"])
def test_unknown_or_incomplete_layout_and_nonouter_install_fail_without_ddl(store, failure):
    enable(store.db)
    if failure == "missing":
        store.db.execute("DELETE FROM market_meta WHERE key='context_integrity'")
    elif failure == "unknown":
        store.db.execute("INSERT INTO market_meta VALUES('context_tiles','999')")
    elif failure == "unmarked":
        store.db.execute("CREATE TABLE market_context_tiles(id)")
    else:
        store.db.execute("BEGIN")
    if failure != "transaction":
        store.db.commit()
    before = list(store.db.execute("SELECT type,name,sql FROM sqlite_master"))
    with pytest.raises(StateError):
        tiles.install(store.db)
    assert list(store.db.execute("SELECT type,name,sql FROM sqlite_master")) == before
    store.db.rollback()
