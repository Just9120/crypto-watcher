"""Bounded duplicate ownership validation and exact known-trigger upgrade."""

import sqlite3
from contextlib import closing

import pytest

from cryptowatcher import context_tile_codec as codec
from cryptowatcher import context_tiles as tiles
from cryptowatcher.storage import StateError, Store
from tests.test_context_tiles import counts, setup


def guard(db):
    return db.execute(
        "SELECT sql FROM sqlite_master WHERE name='market_context_tile_validate_insert'"
    ).fetchone()[0]


def legacy(db):
    db.executescript(
        "BEGIN; DROP TRIGGER market_context_tile_validate_insert;"
        + tiles.publication_guard(ordered=False)
        + "; COMMIT;"
    )


def test_each_publication_decodes_one_probe_with_many_hot_rows_and_still_rejects_duplicates(store):
    keys, _, _ = setup(store, 512, freeze=False)
    probes = []

    def times(payload):
        from cryptowatcher.indexed_frame import all_values
        from cryptowatcher.storage import encode

        probes.append(len(payload))
        return encode([row[2] for row in all_values(bytes(payload))])

    store.db.create_function("cw_context_tile_times", 1, times, deterministic=True)
    tiles.publish(store.db, keys[:64])
    assert len(probes) == 1
    original = counts(store)
    rows = [tiles.lookup(store.db, *key) for key in keys[64:72]]
    duplicate = codec.pack(2, rows)
    with pytest.raises(sqlite3.IntegrityError, match="Duplicate hot/cold context ownership"):
        store.db.execute(
            f"INSERT INTO {tiles.TABLE}({tiles.FIELDS},{tiles.META}) VALUES ({','.join('?' for _ in range(12))})",
            tiles._values(store.db, duplicate),
        )
    assert len(probes) == 2
    assert counts(store) == original
    assert store.db.execute("SELECT COUNT(*) FROM market_context").fetchone()[0] == 448
    assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_only_explicit_install_upgrades_known_guard_and_preserves_current_rows(store):
    keys, _, _ = setup(store, 8)
    legacy(store.db)
    previous = guard(store.db)
    before = codec.signature([tiles.lookup(store.db, *key) for key in keys])
    original_counts = counts(store)
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        assert guard(reader.db) == previous
    tiles.install(store.db)
    assert "CROSS JOIN market_context" in guard(store.db)
    assert counts(store) == original_counts
    assert codec.signature([tiles.lookup(store.db, *key) for key in keys]) == before
    changes = store.db.total_changes
    tiles.install(store.db)
    assert store.db.total_changes == changes
    tiles.publish(store.db, keys)
    assert codec.signature([tiles.lookup(store.db, *key) for key in keys]) == before


def test_unknown_guard_is_preserved(store):
    setup(store)
    store.db.executescript(
        "BEGIN; DROP TRIGGER market_context_tile_validate_insert;"
        "CREATE TRIGGER market_context_tile_validate_insert BEFORE INSERT ON market_context_tiles "
        "BEGIN SELECT RAISE(ABORT,'unknown external guard'); END; COMMIT;"
    )
    previous = guard(store.db)
    with pytest.raises(StateError, match="Unknown context publication guard"):
        tiles.install(store.db)
    assert guard(store.db) == previous


def test_guard_upgrade_requires_outer_boundary_and_rolls_back_failed_creation(store):
    setup(store)
    legacy(store.db)
    previous = guard(store.db)
    with store.db:
        store.db.execute("BEGIN")
        with pytest.raises(StateError, match="requires an outer boundary"):
            tiles.install(store.db)
    assert guard(store.db) == previous
    store.db.set_authorizer(
        lambda action, _a, _b, _db, _source: (
            sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_TRIGGER else sqlite3.SQLITE_OK
        )
    )
    try:
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            tiles.install(store.db)
    finally:
        store.db.set_authorizer(None)
    assert guard(store.db) == previous
    assert not store.db.in_transaction
    tiles.install(store.db)
    assert "CROSS JOIN market_context" in guard(store.db)
