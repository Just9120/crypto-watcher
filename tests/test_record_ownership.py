import sqlite3
import uuid
from dataclasses import asdict

import pytest

from cryptowatcher import numeric_pool, packet_store, source_windows
from cryptowatcher import record_catalog as catalog
from cryptowatcher.events import History
from cryptowatcher.history_codec import Codec
from cryptowatcher.record_blocks import Coordinate
from cryptowatcher.storage import encode
from tests.test_bounded_storage import bounded
from tests.test_outcomes import NOW, future

DAY = 86400000


def publish(store):
    bounded(store)
    catalog.install(store.db, store.cached_text)
    history = History(store)
    codec = Codec(store.db)
    codec.source_stream = "spot/BTCUSDT/1m"
    codec.source_expiry = NOW
    event = str(uuid.UUID(int=99))
    values = []
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        owner = catalog.parent(store.db, event, NOW, NOW + 30 * DAY)
        for index in range(2):
            coordinate = Coordinate("outcome", str(uuid.UUID(int=index + 1)), 1)
            value = {
                "schema": "cryptowatcher.outcome.v1",
                "outcome_id": coordinate.identity,
                "event_id": event,
                "revision": 1,
                "event_revision": 1,
                "metrics": {
                    "price_return_pct": 1.0,
                    "directed_return_pct": 1.0,
                    "mfe_pct": 2.0,
                    "mae_pct": -0.0,
                },
                "settings_snapshot": {"profile": "custom", "alert_universe_mode": "top", "test": "retained"},
                "source_inputs": [asdict(c) for c in future()],
            }
            packed = codec.pack(value)
            history.put(coordinate.key, packed)
            values.append((coordinate, packed, None, value))
        catalog.append(
            store.db,
            [(c, p, h) for c, p, h, v in values],
            bucket=(NOW + 30 * DAY) // DAY,
            owners={c: owner for c, p, h, v in values},
            deadlines={c: None for c, p, h, v in values},
        )
    return history, codec, values


def test_catalogue_owns_dependencies_after_legacy_rows_are_removed_and_until_last_block(store):
    history, codec, values = publish(store)
    assert all(r[0] == 4 for r in store.db.execute("SELECT detail_refs FROM market_numeric"))
    assert all(r[0] == 4 for r in store.db.execute("SELECT refs FROM market_source_windows"))
    with store.db:
        for coordinate, _, _, _ in values:
            history.delete(coordinate.key)
        numeric_pool.collect(store.db)
        source_windows.collect(store.db, NOW + 40 * DAY, 128)
        store.db.execute(
            "DELETE FROM runtime WHERE key IN(SELECT 'historyblob:v1:'||body FROM market_body_counts WHERE refs=0)"
        )
    assert all(r[0] == 2 for r in store.db.execute("SELECT detail_refs FROM market_numeric"))
    for coordinate, _, _, original in values:
        assert encode(codec.unpack(catalog.read(store.db, coordinate))) == encode(original)
    with store.db:
        store.db.execute(
            "DELETE FROM market_records WHERE public_uuid=?", (catalog.binary(values[0][0].identity),)
        )
        assert packet_store.collect(store.db) == 0
    assert encode(codec.unpack(catalog.read(store.db, values[1][0]))) == encode(values[1][3])
    with store.db:
        store.db.execute("DELETE FROM market_records")
        assert packet_store.collect(store.db) == 2
        numeric_pool.collect(store.db)
        source_windows.collect(store.db, NOW + 40 * DAY, 128)
    assert store.db.execute("SELECT COUNT(*) FROM market_numeric").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM market_source_windows").fetchone()[0] == 0
    assert not any(r[0] for r in store.db.execute("SELECT refs FROM market_body_counts"))


def test_catalogue_dependency_counter_corruption_blocks_record_delete_and_partial_drain_rolls_back(store):
    history, codec, values = publish(store)
    before = list(store.db.iterdump())
    with pytest.raises(RuntimeError), store.db:
        history.delete(values[0][0].key)
        store.db.execute(
            "DELETE FROM market_records WHERE public_uuid=?", (catalog.binary(values[0][0].identity),)
        )
        raise RuntimeError("interrupted drain")
    assert list(store.db.iterdump()) == before
    with store.db:
        store.db.execute("UPDATE market_numeric SET detail_refs=0")
    with pytest.raises(sqlite3.IntegrityError, match="dependency counter"), store.db:
        store.db.execute("DELETE FROM market_records")
    assert codec.unpack(catalog.read(store.db, values[0][0])) == values[0][3]


def test_pure_ownership_cache_shares_store_budget_and_never_hides_actual_packet_corruption(
    store, monkeypatch
):
    from cryptowatcher import record_ownership

    original = record_ownership.block_references
    calls = []

    def counted(raw):
        calls.append(len(raw))
        return original(raw)

    monkeypatch.setattr(record_ownership, "block_references", counted)
    _, _, values = publish(store)
    assert len(calls) == 1
    assert 0 < store.reference_cache_bytes <= 1024 * 1024
    coordinate = values[0][0]
    packet = catalog.row(store.db, coordinate)[0]
    args = (1, catalog.binary(coordinate.identity), coordinate.revision, packet)
    assert store.db.execute("SELECT cw_catalogue_record(?,?,?,?,0)", args).fetchone()[0] == encode(
        values[0][1]
    )
    with pytest.raises(sqlite3.OperationalError):
        store.db.execute("SELECT cw_catalogue_record(?,?,?,?,1)", args).fetchone()
    arena, offset = store.db.execute(
        "SELECT arena,offset FROM market_packets WHERE id=?", (packet,)
    ).fetchone()
    with store.db:
        with store.db.blobopen("market_packet_arenas", "payload", arena) as blob:
            blob.seek(offset)
            blob.write(b"bad!")
    with pytest.raises(sqlite3.OperationalError), store.db:
        store.db.execute("DELETE FROM market_records")
    assert store.db.execute("SELECT COUNT(*) FROM market_records").fetchone()[0] == 2
    assert len(calls) == 1
