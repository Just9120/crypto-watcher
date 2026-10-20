import json
import sqlite3
import time
from contextlib import closing

import pytest

from cryptowatcher import quality_frames, quality_packets
from cryptowatcher.storage import Store
from tests.test_quality_frames import numeric, public, setup, tick
from tests.test_quality_packets import fact, save, save_v4


def prepare(store, framed):
    expected = setup(store)
    if framed:
        quality_frames.install(store.db)
        tick(store, {})
    quality_packets.install_functions(store.db, store.cached_bytes)
    raw = bytes(store.db.execute("SELECT facts FROM market_quality WHERE cohort='cohort0'").fetchone()[0])
    return raw, expected


def sql_refs(store, raw):
    return json.loads(store.db.execute("SELECT cw_quality_refs(?)", (raw,)).fetchone()[0])


@pytest.mark.parametrize("framed", [False, True])
def test_cached_sql_refs_keep_owners_rollback_live_counts_and_reopen(store, framed):
    raw, expected_public = prepare(store, framed)
    expected = quality_packets.ownership(store.db, raw)
    numbers = numeric(store)
    assert sql_refs(store, raw) == sql_refs(store, raw) == expected
    assert any(key[0] == "bytes/quality-inline-owners" for key in store.reference_cache)
    assert store.reference_cache_bytes <= 1024 * 1024
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        assert sql_refs(reader, raw) == expected
        assert public(reader) == expected_public
        assert reader.db.total_changes == 0
    with pytest.raises(RuntimeError), store.db:
        store.db.execute("DELETE FROM market_quality WHERE cohort='cohort0'")
        raise RuntimeError("rollback cached owner deletion")
    assert numeric(store) == numbers and public(store) == expected_public
    assert sql_refs(store, raw) == expected
    with store.db:
        store.db.execute("DELETE FROM market_quality WHERE cohort='cohort0'")
    after = {row[0]: row for row in numeric(store)}
    for identity, fingerprint, payload, details, quality in numbers:
        assert after[identity] == (
            identity,
            fingerprint,
            payload,
            details,
            quality - int(identity in expected["numeric"]),
        )
    assert public(store) == expected_public[1:]
    if framed:
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            quality_frames.collect(store.db, time.monotonic() + 5)
        # A cached pure summary cannot make a retired slot readable again.
        with pytest.raises(sqlite3.Error):
            sql_refs(store, raw)
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("legacy", [False, True])
def test_sql_cache_preserves_inline_unknown_metrics_and_legacy_fallback(store, legacy):
    from cryptowatcher.market_storage import enable

    enable(store.db)
    values = [fact(index, [{"future_metric": -0.0, "integer": 2**300}]) for index in range(8)]
    with store.db:
        raw = (save_v4 if legacy else save)(store, values)
    expected = quality_packets.ownership(store.db, raw)
    quality_packets.install_functions(store.db, store.cached_bytes)
    assert sql_refs(store, raw) == sql_refs(store, raw) == expected
    present = any(key[0] == "bytes/quality-inline-owners" for key in store.reference_cache)
    assert present is not legacy


@pytest.mark.parametrize("framed,kind", [(False, "template2"), (False, "columns"), (True, "template2")])
def test_cached_refs_authenticate_actual_dictionary_bytes_even_without_total_changes(store, framed, kind):
    raw, _ = prepare(store, framed)
    expected = sql_refs(store, raw)
    rows, columns = quality_packets.packet(raw, store.db)
    identity = rows[0][0] if kind == "template2" else columns
    assert identity is not None
    store.db.execute("BEGIN IMMEDIATE")
    before = store.db.total_changes
    with store.db.blobopen("market_quality_dictionary", "payload", identity) as blob:
        original = blob.read(1)
        blob.seek(0)
        blob.write(bytes([original[0] ^ 1]))
    assert store.db.total_changes == before
    with pytest.raises(sqlite3.Error):
        sql_refs(store, raw)
    store.db.rollback()
    assert sql_refs(store, raw) == expected


@pytest.mark.parametrize("framed", [False, True])
def test_cached_refs_cannot_hide_a_missing_dictionary_dependency(store, framed):
    raw, _ = prepare(store, framed)
    expected = sql_refs(store, raw)
    identity = quality_packets.packet(raw, store.db)[0][0][0]
    store.db.execute("BEGIN IMMEDIATE")
    store.db.execute("DROP TRIGGER market_quality_dictionary_keep")
    store.db.execute("DELETE FROM market_quality_dictionary WHERE id=?", (identity,))
    with pytest.raises(sqlite3.Error):
        sql_refs(store, raw)
    store.db.rollback()
    assert sql_refs(store, raw) == expected


@pytest.mark.parametrize("column", ["payload", "slots"])
def test_cached_refs_authenticate_current_frame_and_mask(store, column):
    raw, _ = prepare(store, True)
    expected = sql_refs(store, raw)
    identity, _ = quality_frames.coordinate(raw)
    store.db.execute("BEGIN IMMEDIATE")
    before = store.db.total_changes
    with store.db.blobopen("market_quality_frames", column, identity) as blob:
        original = blob.read(1)
        blob.seek(0)
        blob.write(bytes([original[0] ^ 1]))
    assert store.db.total_changes == before
    with pytest.raises(sqlite3.Error):
        sql_refs(store, raw)
    store.db.rollback()
    assert sql_refs(store, raw) == expected
