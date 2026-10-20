import sqlite3
import time
from contextlib import closing

import pytest

from cryptowatcher import (
    metric_codec,
    numeric_block_codec,
    numeric_frame_codec,
    numeric_frames,
    numeric_pool,
    numeric_search,
    quality_packets,
)
from cryptowatcher import numeric_blocks as blocks
from cryptowatcher.storage import Store
from tests.test_numeric_search import assert_accounted, finish, population
from tests.test_quality_packets import metric


def prepared(store):
    packet, public = population(store)
    finish(store)
    numeric_frames.install(store.db, store.cached_bytes)
    sources = [
        (identity, blocks.read(store.db, identity)[3])
        for (identity,) in store.db.execute(
            "SELECT id FROM market_numeric_block_refs WHERE search_seal IS NOT NULL ORDER BY id"
        )
    ]
    assert len(sources) == 2
    return packet, public, sources


def publish(store, sources):
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        identity = numeric_frames.publish(store.db, sources, store.cached_bytes)
    assert identity is not None
    return identity


def test_real_pool_readers_leases_original_frames_counter_and_old_wal_snapshot(store):
    packet, public, sources = prepared(store)
    before = {identity: blocks.read(store.db, identity)[:3] for identity, _ in sources}
    with closing(Store(store.path, store.owner_id, readonly=True)) as old:
        old.db.execute("BEGIN")
        assert quality_packets.unpack(old.db, packet) == public
        identity = publish(store, sources)
        assert quality_packets.unpack(store.db, packet) == public
        assert quality_packets.unpack(old.db, packet) == public
        assert (
            bytes(
                old.db.execute(
                    "SELECT payload FROM market_numeric_blocks WHERE id=?", (sources[0][0],)
                ).fetchone()[0]
            )
            == sources[0][1]
        )
        old.db.rollback()
        assert quality_packets.unpack(old.db, packet) == public
    for block, _ in sources:
        assert blocks.read(store.db, block)[:3] == before[block]
        assert numeric_search.sealed(store.db, block)
    assert_accounted(store)
    count = store.db.execute(
        "SELECT records,logical_bytes FROM market_counts WHERE category=?", (numeric_frames.CATEGORY,)
    ).fetchone()
    assert tuple(count) == (
        1,
        len(store.db.execute("SELECT payload FROM market_numeric_frames").fetchone()[0]),
    )
    assert (
        bytes(
            store.db.execute("SELECT slots FROM market_numeric_frames WHERE id=?", (identity,)).fetchone()[0]
        )
        == b"\0" * 7 + b"\3"
    )
    with closing(Store(store.path, store.owner_id, readonly=True)) as fresh:
        numeric_frames.header(fresh.db)
        assert quality_packets.unpack(fresh.db, packet) == public
        assert fresh.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert fresh.db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_post_first_publication_failure_rolls_back_frames_bindings_all_masks_and_counters(store, monkeypatch):
    _, _, sources = prepared(store)
    before = list(store.db.iterdump())
    original = numeric_search.reaccount

    def fail(*args):
        original(*args)
        raise RuntimeError("injected after first numeric frame binding")

    monkeypatch.setattr(numeric_search, "reaccount", fail)
    with pytest.raises(RuntimeError, match="first numeric frame"), store.db:
        store.db.execute("BEGIN IMMEDIATE")
        numeric_frames.publish(store.db, sources, store.cached_bytes)
    assert list(store.db.iterdump()) == before
    assert_accounted(store)


def test_detail_renewal_detaches_only_its_block_and_gc_preserves_other_frozen_operands(store):
    packet, public, sources = prepared(store)
    identity = publish(store, sources)
    block = sources[0][0]
    before = blocks.read(store.db, block)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        details = list(before[1])
        details[0] += 1
        blocks.put(store.db, block, before[0], details, before[2])
    assert numeric_frames.coordinate(blocks.read(store.db, block)[3]) is None
    assert not numeric_search.sealed(store.db, block)
    assert numeric_frames.coordinate(blocks.read(store.db, sources[1][0])[3]) == (identity, 1)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        assert numeric_frames.collect(store.db, time.monotonic() + 3, cache=store.cached_bytes) == 0
    payload = bytes(
        store.db.execute("SELECT payload FROM market_numeric_frames WHERE id=?", (identity,)).fetchone()[0]
    )
    assert numeric_frame_codec.all_values(payload) == [None, sources[1]]
    assert quality_packets.unpack(store.db, packet) == public
    assert_accounted(store)


@pytest.mark.parametrize(
    "damage", ["payload", "mask", "mask_extra", "counter", "missing_frame", "missing_seal"]
)
def test_current_dependencies_are_checked_after_the_pure_cache_is_warm(store, damage):
    packet, public, sources = prepared(store)
    identity = publish(store, sources)
    assert quality_packets.unpack(store.db, packet) == public
    with store.db:
        if damage == "payload":
            with store.db.blobopen("market_numeric_frames", "payload", identity) as blob:
                blob.seek(len(blob) - 1)
                last = blob.read(1)
                blob.seek(len(blob) - 1)
                blob.write(bytes([last[0] ^ 1]))
        elif damage == "mask":
            store.db.execute(
                "UPDATE market_numeric_frames SET slots=zeroblob(8),checksum=cw_owner_digest(3,id,zeroblob(8))"
            )
        elif damage == "mask_extra":
            store.db.execute(
                "UPDATE market_numeric_frames SET slots=x'8000000000000000',checksum=cw_owner_digest(3,id,x'8000000000000000')"
            )
        elif damage == "counter":
            store.db.execute("DELETE FROM market_counts WHERE category=?", (numeric_frames.CATEGORY,))
        elif damage == "missing_seal":
            store.db.execute("UPDATE market_numeric_block_refs SET search_seal=NULL")
        else:
            store.db.execute("DROP TRIGGER market_nframe_keep")
            store.db.execute("DELETE FROM market_numeric_frames")
    with pytest.raises((ValueError, sqlite3.Error)):
        quality_packets.unpack(store.db, packet)


def test_old_populated_prototype_retains_its_exact_ddl_bytes_and_explicit_compatibility(store, monkeypatch):
    with monkeypatch.context() as legacy:
        legacy.setattr(numeric_frames, "TABLE_SQL", numeric_frames.LEGACY_SQL)
        packet, public = population(store)
    finish(store)
    before = [tuple(r) for r in store.db.execute("SELECT * FROM market_numeric_blocks ORDER BY id")]
    sql = store.db.execute("SELECT sql FROM sqlite_master WHERE name='market_numeric_blocks'").fetchone()[0]
    numeric_frames.install(store.db, store.cached_bytes)
    assert numeric_frames.header(store.db) == "NB_COMPAT" and not numeric_frames.writing(store.db)
    assert (
        store.db.execute("SELECT sql FROM sqlite_master WHERE name='market_numeric_blocks'").fetchone()[0]
        == sql
    )
    assert [tuple(r) for r in store.db.execute("SELECT * FROM market_numeric_blocks ORDER BY id")] == before
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        assert quality_packets.unpack(reader.db, packet) == public
        assert not reader.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='market_numeric_frames'"
        ).fetchone()


def test_ns2_rejects_changed_validly_checksummed_values_after_a_warm_read(store):
    packet, public, sources = prepared(store)
    identity = publish(store, sources)
    assert quality_packets.unpack(store.db, packet) == public
    row = store.db.execute("SELECT payload FROM market_numeric_frames WHERE id=?", (identity,)).fetchone()
    values = numeric_frame_codec.all_values(bytes(row[0]))
    block, raw = values[0]
    changed = numeric_block_codec.unpack(raw, block)
    changed[0] = metric_codec.pack(metric(987654))
    values[0] = (block, numeric_block_codec.pack(block, changed))
    replacement = numeric_frame_codec.pack(values)
    # Model storage mutation below the immutable-value guard. The BF1 checksum
    # and the category's actual-byte counter remain valid.
    with store.db:
        store.db.execute("DROP TRIGGER market_nframe_payload")
        store.db.execute("UPDATE market_numeric_frames SET payload=? WHERE id=?", (replacement, identity))
    assert numeric_frame_codec.all_values(replacement) == values
    before = list(store.db.iterdump())
    with pytest.raises(ValueError, match="search seal"):
        numeric_search.sealed(store.db, block)
    with pytest.raises(ValueError, match="search seal"):
        quality_packets.unpack(store.db, packet)
    with pytest.raises(ValueError, match="search seal"), store.db:
        store.db.execute("BEGIN IMMEDIATE")
        blocks.put(store.db, block, changed, [0] * 64, [1] * 64)
    assert list(store.db.iterdump()) == before


def test_ns2_does_not_depend_on_recompressing_with_a_future_nb_encoder(store, monkeypatch):
    import zstandard

    packet, public, sources = prepared(store)
    publish(store, sources)
    original = numeric_block_codec.pack

    def other_encoding(block, frames, *, recipes=False):
        raw = original(block, frames, recipes=recipes)
        body = numeric_block_codec.header(raw)
        expanded = b"".join(numeric_block_codec.unpack(raw, block))
        compressed = zstandard.ZstdCompressor(level=19, write_checksum=True).compress(expanded)
        return numeric_block_codec.seal(raw[:4], body[:72] + compressed)

    monkeypatch.setattr(numeric_block_codec, "pack", other_encoding)
    with closing(Store(store.path, store.owner_id, readonly=True)) as fresh:
        assert quality_packets.unpack(fresh.db, packet) == public
        for block, _ in sources:
            assert numeric_search.sealed(fresh.db, block)


def test_last_numeric_lease_controls_frame_gc_and_guards_reject_wrong_bindings(store):
    _, _, sources = prepared(store)
    identity = publish(store, sources)
    before = list(store.db.iterdump())
    with pytest.raises((ValueError, sqlite3.Error)), store.db:
        store.db.execute(
            "UPDATE market_numeric_blocks SET payload=? WHERE id=?",
            (numeric_frames.pointer(identity, 1), sources[0][0]),
        )
    assert list(store.db.iterdump()) == before
    with pytest.raises((ValueError, sqlite3.Error)), store.db:
        store.db.execute("DELETE FROM market_numeric_frames WHERE id=?", (identity,))
    assert list(store.db.iterdump()) == before
    with store.db:
        store.db.execute("DELETE FROM market_quality")
        numeric_pool.collect(store.db)
        numeric_frames.collect(store.db, time.monotonic() + 3, cache=store.cached_bytes)
    assert not store.db.execute("SELECT 1 FROM market_numeric_frames").fetchone()
    assert tuple(
        store.db.execute(
            "SELECT records,logical_bytes FROM market_counts WHERE category=?", (numeric_frames.CATEGORY,)
        ).fetchone()
    ) == (0, 0)
    assert_accounted(store)
    assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_meter_counts_actual_frame_bytes_once_and_reports_explicit_format_without_writes(store):
    from cryptowatcher.storage_meter import QUALITY_CATEGORIES, report

    _, _, sources = prepared(store)
    before_dump = list(store.db.iterdump())
    before = report(store)
    assert list(store.db.iterdump()) == before_dump
    identity = publish(store, sources)
    payload = bytes(
        store.db.execute("SELECT payload FROM market_numeric_frames WHERE id=?", (identity,)).fetchone()[0]
    )
    after_dump = list(store.db.iterdump())
    after = report(store)
    assert list(store.db.iterdump()) == after_dump
    assert after["numeric_layout"]["frame_mode"] == "1"
    assert after["categories"][numeric_frames.CATEGORY]["records"] == 1
    assert after["categories"][numeric_frames.CATEGORY]["logical_bytes"] == len(payload)
    expected_delta = len(payload) + sum(13 - len(raw) for _, raw in sources)

    def quality_bytes(snapshot):
        return sum(
            snapshot["categories"].get(category, {}).get("logical_bytes", 0)
            for category in QUALITY_CATEGORIES
        )

    assert quality_bytes(after) - quality_bytes(before) == expected_delta
