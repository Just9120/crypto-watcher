from contextlib import closing

import pytest

from cryptowatcher import numeric_blocks, numeric_pool
from cryptowatcher.storage import Store
from tests.test_numeric_blocks import setup
from tests.test_quality_packets import metric


def test_readonly_initialization_validates_one_snapshot_and_returns_fresh_to_caller(store, monkeypatch):
    setup(store)
    with store.db:
        first = numeric_pool.intern(store.db, metric(1))
        numeric_pool.adjust(store.db, {first}, "detail_refs", 1)
    original = numeric_blocks.state
    published = False

    def publish_between_real_summary_queries(db):
        nonlocal published
        result = original(db)
        if db is not store.db and not published:
            published = True
            # Each committed state has matching totals and encoded-byte counts.
            # The initializer must never mix the old totals with new counters.
            with store.db:
                second = numeric_pool.intern(store.db, metric(2))
                numeric_pool.adjust(store.db, {second}, "detail_refs", 1)
        return result

    monkeypatch.setattr(numeric_blocks, "state", publish_between_real_summary_queries)
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        assert published
        assert not reader.db.in_transaction
        assert reader.db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert numeric_blocks.summary(reader.db)["details"] == 2
        reader.db.execute("BEGIN")
        assert numeric_blocks.summary(reader.db)["details"] == 2
        reader.db.rollback()


def test_readonly_initialization_keeps_corruption_fatal_and_releases_its_snapshot(store):
    setup(store)
    with store.db:
        first = numeric_pool.intern(store.db, metric(1))
        numeric_pool.adjust(store.db, {first}, "detail_refs", 1)
        store.db.execute(
            "UPDATE market_counts SET logical_bytes=logical_bytes+1 WHERE category='numeric_details'"
        )
    with pytest.raises(ValueError, match="counter"):
        Store(store.path, store.owner_id, readonly=True)
    with store.db:
        store.db.execute(
            "UPDATE market_counts SET logical_bytes=logical_bytes-1 WHERE category='numeric_details'"
        )
    assert store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        assert numeric_blocks.summary(reader.db)["details"] == 1
