import random
import sqlite3
import struct
import time
from contextlib import closing

import pytest
import zstandard

from cryptowatcher import numeric_block_codec as codec
from cryptowatcher import numeric_blocks, numeric_pool, numeric_refs, numeric_search, quality_packets
from cryptowatcher.storage import Store, encode
from tests.test_numeric_blocks import owner
from tests.test_numeric_search import assert_accounted, finish, population
from tests.test_quality_packets import metric


@pytest.mark.parametrize("pattern", ["zero", "maximum", "sparse", "random"])
def test_nr2_exact_counts_with_smaller_only_chooser_and_frozen_nr1_reader(pattern):
    rng = random.Random(11791)
    values = (
        [0] * 128
        if pattern == "zero"
        else [2**63 - 1] * 128
        if pattern == "maximum"
        else [int(i % 7 == 0) if pattern == "sparse" else rng.randrange(2**63) for i in range(128)]
    )
    details, quality = values[:64], values[64:]
    legacy = codec.counts(71, details, quality)
    modern = codec.counts(71, details, quality, compressed=True)
    assert legacy.startswith(b"NR1\0") and len(modern) <= len(legacy)
    assert codec.leases(legacy, 71) == codec.leases(modern, 71) == (details, quality)
    if pattern != "random":
        assert modern.startswith(b"NR2\0") and len(modern) < len(legacy)
    for bad in (modern + b"x", modern[:-1], modern[:53] + bytes([modern[53] ^ 1]) + modern[54:]):
        with pytest.raises(ValueError):
            codec.leases(bad, 71)
    with pytest.raises(ValueError, match="identity"):
        codec.leases(modern, 72)


@pytest.mark.parametrize("size", [0, 127, 1153, 65535])
def test_nr2_rejects_forged_expanded_bound_before_decompression(size, monkeypatch):
    compressed = zstandard.ZstdCompressor(write_checksum=True).compress(bytes(128))
    raw = codec.seal(b"NR2\0", struct.pack(">QH", 9, size) + compressed)
    monkeypatch.setattr(zstandard, "ZstdDecompressor", lambda **kw: pytest.fail("allocation before guard"))
    with pytest.raises(ValueError, match="bounds"):
        codec.leases(raw, 9)


def recode_all(store, state=None):
    state = {} if state is None else state
    for _ in range(20):
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            numeric_refs.advance(store.db, state, 1, time.monotonic() + 5)
        if state.get("complete"):
            return state
    pytest.fail("Finite numeric lease pass did not finish")


def test_recode_preserves_values_ids_scalar_leases_seals_renewal_gc_and_exact_accounting(store):
    raw, values = population(store)
    search_state = finish(store)
    before = numeric_pool.raw_many(store.db, set(range(1, 130)))
    old_status = numeric_blocks.summary(store.db)
    lengths = sum(row[0] for row in store.db.execute("SELECT length(counts) FROM market_numeric_block_refs"))
    numeric_refs.install(store.db)
    state = recode_all(store)
    saved = lengths - sum(
        row[0] for row in store.db.execute("SELECT length(counts) FROM market_numeric_block_refs")
    )
    assert state["saved_bytes"] == saved > 0
    assert numeric_pool.raw_many(store.db, set(before)) == before
    status = numeric_blocks.summary(store.db)
    assert status["quality_bytes"] == old_status["quality_bytes"] - saved
    assert status["detail_bytes"] == old_status["detail_bytes"]
    assert_accounted(store)
    assert encode(quality_packets.unpack(store.db, raw)) == encode(values)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        changes = store.db.total_changes
        assert numeric_refs.recode(store.db, 0) == 0
        assert store.db.total_changes == changes
        key = owner(store, 33, {"mode": "numeric2", "id": 4})
    assert not numeric_search.sealed(store.db, 0)
    assert_accounted(store)
    with store.db:
        store.db.execute("DELETE FROM runtime WHERE key=?", (key,))
    finish(store, search_state)
    assert numeric_search.sealed(store.db, 0)
    assert numeric_pool.raw_many(store.db, set(before)) == before
    assert_accounted(store)
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        assert numeric_pool.raw_many(reader.db, set(before)) == before
        assert encode(quality_packets.unpack(reader.db, raw)) == encode(values)
    with store.db:
        store.db.execute("DELETE FROM market_quality")
    for _ in range(30):
        with store.db:
            removed = numeric_pool.collect(store.db, 7)
        assert_accounted(store)
        if not removed:
            break
    assert numeric_blocks.summary(store.db)["live"] == 0
    assert numeric_blocks.summary(store.db)["issued"] == 129


def test_late_failure_rolls_back_count_bytes_seal_and_accounting_preserving_outer_changes(store, monkeypatch):
    population(store)
    finish(store)
    numeric_refs.install(store.db)
    original = numeric_blocks.put

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected NR2 publication")

    monkeypatch.setattr(numeric_blocks, "put", fail)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        store.db.execute("INSERT INTO runtime VALUES('unrelated-numeric-lease-owner','true')")
        before = list(store.db.iterdump())
        with pytest.raises(RuntimeError, match="NR2 publication"):
            numeric_refs.recode(store.db, 0)
        assert list(store.db.iterdump()) == before
    assert store.get_runtime("unrelated-numeric-lease-owner") is True


def test_finite_lease_ceiling_survives_new_admissions_vacuum_and_reopen(store):
    population(store)
    finish(store)
    numeric_refs.install(store.db)
    state = {}
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        numeric_refs.advance(store.db, state, 1, time.monotonic() + 5)
    assert state["cursor"] == 0 and state["upper"] == 2
    with store.db:
        new_ids = [numeric_pool.intern(store.db, metric(10000 + i)) for i in range(131)]
    expected = numeric_pool.raw_many(store.db, set(range(1, 130)) | set(new_ids))
    store.db.execute("VACUUM")
    with closing(Store(store.path, store.owner_id)) as reopened:
        complete = recode_all(reopened, state)
        assert complete["cursor"] == complete["upper"] == 2
        assert numeric_pool.raw_many(reopened.db, set(expected)) == expected
        assert_accounted(reopened)
        changes = reopened.db.total_changes
        assert recode_all(reopened, state) == complete
        assert reopened.db.total_changes == changes


def test_nr2_without_marker_unknown_marker_and_trailing_frame_cannot_be_read_as_nr1(store):
    population(store)
    finish(store)
    numeric_refs.install(store.db)
    recode_all(store)
    with store.db:
        store.db.execute("DELETE FROM market_meta WHERE key='numeric_refs_compression'")
    with pytest.raises(ValueError):
        numeric_pool.load(store.db, 1)
    with store.db:
        store.db.execute("INSERT INTO market_meta VALUES('numeric_refs_compression','future')")
    with pytest.raises(ValueError, match="Unknown numeric lease"):
        numeric_refs.installed(store.db)
    raw = codec.counts(7, [0] * 64, [1] * 64, compressed=True)
    tampered = codec.seal(b"NR2\0", raw[36:] + zstandard.ZstdCompressor().compress(b"extra"))
    with pytest.raises(ValueError, match="compression"):
        codec.leases(tampered, 7)


def test_frozen_pre_nr2_local_table_is_explicit_compat_without_ddl_or_byte_changes():
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE TABLE market_meta(key TEXT PRIMARY KEY,value TEXT)")
        db.execute("INSERT INTO market_meta VALUES('numeric_blocks','3')")
        db.execute(
            "CREATE TABLE market_numeric_block_refs(id INTEGER PRIMARY KEY,counts BLOB NOT NULL CHECK(typeof(counts)='blob' AND length(counts) BETWEEN 172 AND 1196))"
        )
        raw = codec.counts(0, [0] * 64, [1] * 64)
        db.execute("INSERT INTO market_numeric_block_refs VALUES(0,?)", (raw,))
        db.commit()
        schema = db.execute("PRAGMA schema_version").fetchone()[0]
        numeric_refs.install(db)
        assert numeric_refs.installed(db) and not numeric_refs.writing(db)
        assert (
            db.execute("SELECT value FROM market_meta WHERE key='numeric_refs_compression'").fetchone()[0]
            == "NR1_COMPAT"
        )
        assert db.execute("SELECT counts FROM market_numeric_block_refs").fetchone()[0] == raw
        assert db.execute("PRAGMA schema_version").fetchone()[0] == schema
        with pytest.raises(ValueError, match="activation"):
            numeric_refs.header(db, codec.counts(0, [0] * 64, [1] * 64, compressed=True))


def test_nr1_sql_bound_and_nr2_format_gate_reject_unknown_or_zeroed_envelopes(store):
    population(store)
    with pytest.raises(sqlite3.IntegrityError), store.db:
        store.db.execute("UPDATE market_numeric_block_refs SET counts=zeroblob(length(counts))")
    with pytest.raises(sqlite3.IntegrityError), store.db:
        store.db.execute("UPDATE market_numeric_block_refs SET counts=?", (codec.seal(b"NR3\0", bytes(180)),))
