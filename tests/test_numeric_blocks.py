import copy
import random
import sqlite3
import time
from contextlib import closing

import pytest

from cryptowatcher import metric_codec, numeric_pool
from cryptowatcher import numeric_block_codec as codec
from cryptowatcher import numeric_blocks as blocks
from cryptowatcher.events import identity
from cryptowatcher.market_storage import enable
from cryptowatcher.storage import Store, encode
from tests.test_quality_packets import fact, metric, save


def setup(store, *, activate=True):
    enable(store.db)
    store.bounded = True
    blocks.install(store.db, store.cached_bytes)
    if activate:
        with store.db:
            store.db.execute("INSERT INTO market_meta VALUES('numeric_block_runtime','1')")


def finish(store, state=None, batch=1):
    if state is None:
        state = {}
    for _ in range(100):
        with store.db:
            store.db.execute("BEGIN")
            done = blocks.transfer(store.db, state, batch, time.monotonic() + 10)
            store.db.execute(
                "INSERT INTO runtime VALUES('numeric-test-checkpoint',?) ON CONFLICT(key) DO UPDATE SET payload_json=excluded.payload_json",
                (encode(state),),
            )
        if done:
            return state
    pytest.fail("finite numeric transfer did not finish")


def owner(store, number, reference):
    key = f"radar:v1:outcome:{identity('numeric-owner/' + str(number))}:0000000001"
    payload = {
        "_history_storage": {"schema": "cryptowatcher.shared-fields.v1", "fields": {"metrics": reference}}
    }
    store.db.execute("INSERT INTO runtime VALUES(?,?)", (key, encode(payload)))
    return key


def test_codec_preserves_all_float_bits_integer_flags_sparse_slots_and_bounds():
    frames = [b""] * 64
    rng = random.Random(101)
    for slot in range(64):
        if slot % 5:
            p = rng.uniform(-1e100, 1e100)
            value = dict(zip(metric_codec.DIRECTED, (p, p, 0 if slot % 2 else -0.0, -rng.random())))
            frames[slot] = metric_codec.pack(value)
    raw = codec.pack(73, frames)
    assert codec.unpack(raw, 73) == frames
    details = [0, 1, 127, 128, 2**63 - 1] + [0] * 59
    quality = [2**63 - 1] * 64
    counts = codec.counts(73, details, quality)
    assert codec.leases(counts, 73) == (details, quality)
    for damaged in (raw + b"x", raw[:100], raw[:90] + bytes((raw[90] ^ 1,)) + raw[91:]):
        with pytest.raises(ValueError):
            codec.unpack(damaged, 73)
    with pytest.raises(ValueError):
        codec.unpack(raw, 74)
    with pytest.raises(ValueError):
        codec.leases(counts, 74)
    with pytest.raises(ValueError):
        codec.counts(73, [-1] * 64, quality)
    for invalid in (0, -1, True, 1.0, 2**63):
        with pytest.raises(ValueError):
            codec.coordinate(invalid)


def test_legacy_transfer_keeps_ids_views_leases_restart_and_issued_high_water(store):
    enable(store.db)
    store.bounded = True
    values = [metric(i) for i in range(140)]
    with store.db:
        ids = [numeric_pool.intern(store.db, value) for value in values]
        numeric_pool.adjust(store.db, set(ids[:70]), "detail_refs", 1)
        numeric_pool.adjust(store.db, set(ids[::3]), "quality_refs", 1)
        # Deleted AUTOINCREMENT IDs must never reappear after transfer or GC.
        store.db.execute("UPDATE sqlite_sequence SET seq=1000000 WHERE name='market_numeric'")
    before = numeric_pool.raw_many(store.db, set(ids))
    blocks.install(store.db)
    with store.db:
        store.db.execute("INSERT INTO market_meta VALUES('numeric_block_runtime','1')")
        state = {}
        assert not blocks.transfer(store.db, state, 1, time.monotonic() + 10)
    assert state["numeric_cursor"] == 64
    assert len([v for v in numeric_pool.raw_many(store.db, set(ids)).values() if v[3]]) == 64
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        assert [encode(numeric_pool.load(reader.db, i)) for i in ids] == list(map(encode, values))
    store.db.execute("VACUUM")
    store.db.execute("CREATE TABLE numeric_test_ddl(x)")
    finish(store, state)
    assert state["numeric_upper"] == 140 and state["numeric_rows_moved"] == 140
    after = numeric_pool.raw_many(store.db, set(ids))
    assert {i: v[:3] for i, v in after.items()} == {i: v[:3] for i, v in before.items()}
    assert all(v[3] for v in after.values())
    assert store.db.execute("SELECT COUNT(*) FROM market_numeric").fetchone()[0] == 0
    with store.db:
        new_id = numeric_pool.intern(store.db, metric(9999))
    assert new_id == 1000001
    assert numeric_pool.existing_reference(store.db, metric(2)) is not None
    assert blocks.summary(store.db)["issued"] == 1000001


def test_new_admissions_cannot_extend_finite_transfer_or_open_activate_writer(store):
    enable(store.db)
    store.bounded = True
    with store.db:
        for i in range(65):
            numeric_pool.intern(store.db, metric(i))
    blocks.install(store.db)
    assert not blocks.writing(store.db)
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        assert not blocks.writing(reader.db)
    with store.db:
        store.db.execute("BEGIN")
        state = {}
        assert not blocks.transfer(store.db, state, 1, time.monotonic() + 10)
    with store.db:
        # A legacy admission before explicit activation updates the ceiling.
        new = numeric_pool.intern(store.db, metric(999))
    assert new == 66 and blocks.summary(store.db)["issued"] == 66
    with store.db:
        store.db.execute("INSERT INTO market_meta VALUES('numeric_block_runtime','1')")
        newest = numeric_pool.intern(store.db, metric(1000))
    finish(store, state)
    assert state["numeric_upper"] == 65 and newest == 67
    assert store.db.execute("SELECT id FROM market_numeric").fetchone()[0] == 66
    assert numeric_pool.load(store.db, newest) == metric(1000)


def test_runtime_quality_and_partial_gc_keep_last_owner_and_never_reuse_ids(store):
    setup(store)
    with store.db:
        refs = [numeric_pool.encode_reference(store.db, metric(i)) for i in range(130)]
        owners = [owner(store, i, ref) for i, ref in enumerate(refs)]
        raw = save(store, [fact(i, [metric(i), metric(i + 1), metric(i)]) for i in range(4)])
    facts = [fact(i, [metric(i), metric(i + 1), metric(i)]) for i in range(4)]
    assert encode(
        __import__("cryptowatcher.quality_packets", fromlist=["unpack"]).unpack(store.db, raw)
    ) == encode(facts)
    ids = {numeric_pool.reference_id(ref) for ref in refs}
    with store.db:
        for key in owners:
            store.db.execute("DELETE FROM runtime WHERE key=?", (key,))
        assert numeric_pool.collect(store.db, 7) == 7
    assert blocks.summary(store.db)["live"] == 123
    assert all(numeric_pool.raw_many(store.db, {i})[i][2] == 1 for i in range(1, 6))
    with store.db:
        store.db.execute("DELETE FROM market_quality")
        for _ in range(20):
            numeric_pool.collect(store.db, 7)
        assert blocks.summary(store.db)["live"] == 0
        new = numeric_pool.intern(store.db, metric(0))
    assert new > max(ids)
    assert blocks.summary(store.db)["issued"] == new
    assert store.db.execute("SELECT COUNT(*) FROM market_numeric_transfer").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM market_numeric_gc").fetchone()[0] == 0
    assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_rollback_transfer_and_failed_multi_owner_statement_preserve_every_table(store):
    enable(store.db)
    store.bounded = True
    with store.db:
        first = numeric_pool.intern(store.db, metric(1))
        numeric_pool.adjust(store.db, {first}, "detail_refs", 1)
    blocks.install(store.db)
    before = list(store.db.iterdump())
    with pytest.raises(RuntimeError), store.db:
        store.db.execute("BEGIN")
        state = {}
        blocks.transfer(store.db, state, 1, time.monotonic() + 10)
        raise RuntimeError("checkpoint interrupted")
    assert list(store.db.iterdump()) == before
    finish(store)
    before = list(store.db.iterdump())
    with pytest.raises(sqlite3.OperationalError), store.db:
        owner(store, 1, {"mode": "numeric", "id": 9999999})
    assert list(store.db.iterdump()) == before
    with pytest.raises(ValueError), store.db:
        numeric_pool.adjust(store.db, {first, 2}, "quality_refs", 1)
    assert list(store.db.iterdump()) == before
    with pytest.raises(sqlite3.OperationalError), store.db:
        store.db.execute("DELETE FROM market_numeric_blocks")
    assert list(store.db.iterdump()) == before


@pytest.mark.parametrize("damage", ["counts", "lookup", "totals", "missing_counts", "payload"])
def test_corruption_is_detected_on_actual_read_and_prevents_owner_publication(store, damage):
    setup(store)
    with store.db:
        ref = numeric_pool.encode_reference(store.db, metric(8))
    identity_value = numeric_pool.reference_id(ref)
    with store.db:
        if damage == "counts":
            store.db.execute("UPDATE market_numeric_block_refs SET counts=zeroblob(length(counts))")
        elif damage == "lookup":
            store.db.execute("DELETE FROM market_numeric_block_lookup")
        elif damage == "totals":
            store.db.execute("DELETE FROM market_numeric_totals")
        elif damage == "missing_counts":
            store.db.execute("DELETE FROM market_numeric_block_refs")
        else:
            store.db.execute("DROP TRIGGER market_numeric_block_guard")
            store.db.execute("UPDATE market_numeric_blocks SET payload=zeroblob(length(payload))")
    before = list(store.db.iterdump())
    with pytest.raises(ValueError):
        numeric_pool.load(store.db, identity_value)
    with pytest.raises(sqlite3.OperationalError), store.db:
        owner(store, 99, ref)
    assert list(store.db.iterdump()) == before


def test_exact_views_after_cold_transfer_and_catalogue_release(store):
    from cryptowatcher import quality_packets
    from cryptowatcher import record_catalog as catalog
    from tests.test_record_ownership import publish

    history, codec_value, values = publish(store)
    blocks.install(store.db)
    finish(store)
    ids = set(numeric_pool.metric_ids(values[0][1]))
    assert ids and all(v[3] for v in numeric_pool.raw_many(store.db, ids).values())
    for coordinate, _, _, original in values:
        assert encode(codec_value.unpack(catalog.read(store.db, coordinate))) == encode(original)
    with store.db:
        for coordinate, _, _, _ in values:
            history.delete(coordinate.key)
        store.db.execute("DELETE FROM market_records")
        assert numeric_pool.collect(store.db) == len(ids)
    assert blocks.summary(store.db)["live"] == 0
    with store.db:
        store.db.execute("INSERT INTO market_meta VALUES('numeric_block_runtime','1')")
        base = metric(5)
        refs = [
            numeric_pool.encode_reference(store.db, v)
            for v in (base, metric_codec.reflected(base), metric_codec.restore_view(base, 2))
        ]
        raw = save(store, [fact(1, [base, metric_codec.reflected(base), base])])
    assert len({numeric_pool.reference_id(ref) for ref in refs}) == 1
    for ref, value in zip(
        refs, (base, metric_codec.reflected(base), metric_codec.restore_view(base, 2)), strict=True
    ):
        assert encode(numeric_pool.resolve(store.db, ref)) == encode(value)
    assert quality_packets.unpack(store.db, raw)[0]["comparator_metrics"] == [
        base,
        metric_codec.reflected(base),
        base,
    ]


def test_refcount_and_issued_overflow_leave_state_unchanged(store):
    setup(store)
    with store.db:
        ref = numeric_pool.encode_reference(store.db, metric(2))
        row = blocks.read(store.db, 0)
        ds, qs = copy.deepcopy(row[1:3])
        ds[0] = 2**63 - 1
        blocks.put(store.db, 0, row[0], ds, qs)
    before = list(store.db.iterdump())
    with pytest.raises(ValueError), store.db:
        numeric_pool.adjust(store.db, {numeric_pool.reference_id(ref)}, "detail_refs", 1)
    assert list(store.db.iterdump()) == before
    with store.db:
        current = blocks.state(store.db)
        changed = (2**63 - 1, *current[1:])
        store.db.execute(
            "UPDATE market_numeric_totals SET issued=?,checksum=?", (changed[0], blocks.digest(changed))
        )
        store.db.execute("UPDATE sqlite_sequence SET seq=? WHERE name='market_numeric'", (changed[0],))
    before = list(store.db.iterdump())
    with pytest.raises(ValueError), store.db:
        numeric_pool.intern(store.db, metric(999))
    assert list(store.db.iterdump()) == before


def test_numeric_pure_cache_shares_budget_and_cannot_hide_writes_or_actual_corruption(store):
    setup(store)
    with store.db:
        ids = [numeric_pool.intern(store.db, metric(i)) for i in range(128)]
    for _ in range(2):
        assert numeric_pool.load_many(store.db, set(ids)) == dict(zip(ids, map(metric, range(128))))
    assert 0 < store.reference_cache_bytes <= 1024 * 1024
    with store.db:
        numeric_pool.adjust(store.db, {ids[0]}, "quality_refs", 1)
    assert numeric_pool.raw_many(store.db, {ids[0]})[ids[0]][2] == 1
    with store.db:
        store.db.execute("UPDATE market_numeric_block_refs SET counts=zeroblob(length(counts)) WHERE id=0")
    with pytest.raises(ValueError):
        numeric_pool.load(store.db, ids[0])
    assert store.reference_cache_bytes <= 1024 * 1024


def test_owner_update_and_transfer_delete_fault_roll_back_values_counts_and_checkpoint(store):
    enable(store.db)
    store.bounded = True
    with store.db:
        ref = numeric_pool.encode_reference(store.db, metric(1))
        key = owner(store, 1, ref)
    blocks.install(store.db, store.cached_bytes)
    with store.db:
        store.db.execute(
            "CREATE TRIGGER numeric_transfer_fault BEFORE DELETE ON market_numeric BEGIN SELECT RAISE(ABORT,'transfer fault'); END"
        )
    before = list(store.db.iterdump())
    with pytest.raises(sqlite3.IntegrityError, match="transfer fault"), store.db:
        store.db.execute("BEGIN")
        state = {}
        blocks.transfer(store.db, state, 1, time.monotonic() + 10)
    assert list(store.db.iterdump()) == before
    with store.db:
        store.db.execute("DROP TRIGGER numeric_transfer_fault")
    finish(store)
    before = list(store.db.iterdump())
    bad = {
        "_history_storage": {
            "schema": "cryptowatcher.shared-fields.v1",
            "fields": {"metrics": {"mode": "numeric", "id": 999}},
        }
    }
    with pytest.raises(sqlite3.OperationalError), store.db:
        store.db.execute("UPDATE runtime SET payload_json=? WHERE key=?", (encode(bad), key))
    assert list(store.db.iterdump()) == before
    assert (
        numeric_pool.raw_many(store.db, {numeric_pool.reference_id(ref)})[numeric_pool.reference_id(ref)][1]
        == 1
    )


def test_missing_byte_counter_blocks_expiry_and_report_without_partial_deletion(store):
    setup(store)
    with store.db:
        numeric_pool.intern(store.db, metric(4))
        store.db.execute("DELETE FROM market_counts WHERE category='numeric_details'")
    before = list(store.db.iterdump())
    with pytest.raises(ValueError, match="counter"):
        blocks.summary(store.db)
    with pytest.raises(ValueError, match="counter"), store.db:
        numeric_pool.collect(store.db)
    assert list(store.db.iterdump()) == before


def test_complete_repack_includes_numeric_phase_and_marks_old_physical_snapshot_stale(store):
    from cryptowatcher.storage_meter import report
    from cryptowatcher.storage_repack import work
    from tests.test_outcomes import NOW

    enable(store.db)
    store.bounded = True
    with store.db:
        ref = numeric_pool.encode_reference(store.db, metric(1))
        owner(store, 1, ref)
    store.put_runtime(
        "storage_index_measurement",
        {
            "state": "PASS",
            "tables": {
                name: {}
                for name in (
                    "market_expiry",
                    "market_quality_blocks",
                    "market_source_windows",
                    "market_numeric",
                    "market_namespaces",
                )
            },
        },
    )
    for tick in range(100):
        state = work(store, NOW + tick, seconds=1)
        if state["state"] == "COMPLETE":
            break
    assert state["state"] == "COMPLETE" and state["version"] == 13
    assert state["numeric_transfer_complete"] and state["numeric_rows_moved"] == 1
    value = report(store, NOW + 100)
    assert value["numeric_layout"]["live"] == 1
    assert value["indexes"]["state"] == "STALE"
    assert numeric_pool.resolve(store.db, ref) == metric(1)


def test_reader_opened_before_explicit_upgrade_can_read_new_transactions_without_install(store):
    enable(store.db)
    store.bounded = True
    with store.db:
        identity_value = numeric_pool.intern(store.db, metric(17))
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        assert not blocks.installed(reader.db)
        blocks.install(store.db, store.cached_bytes)
        finish(store)
        assert numeric_pool.load(reader.db, identity_value) == metric(17)
        assert not blocks.writing(reader.db)
        assert store.db.execute("SELECT COUNT(*) FROM market_numeric").fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["transfer", "promote", "collect", "release_quality"])
def test_missing_legacy_byte_counter_prevents_fabricated_cold_totals_or_deletion(store, operation):
    enable(store.db)
    store.bounded = True
    with store.db:
        identity_value = numeric_pool.intern(store.db, metric(4))
        if operation == "release_quality":
            numeric_pool.adjust(store.db, {identity_value}, "quality_refs", 1)
    blocks.install(store.db, store.cached_bytes)
    with store.db:
        category = "quality_numeric" if operation == "release_quality" else "numeric_details"
        store.db.execute("DELETE FROM market_counts WHERE category=?", (category,))
    before = list(store.db.iterdump())
    with pytest.raises(ValueError, match="counter"), store.db:
        if operation == "transfer":
            store.db.execute("BEGIN")
            blocks.transfer(store.db, {}, 1, time.monotonic() + 10)
        elif operation == "collect":
            numeric_pool.collect(store.db)
        else:
            numeric_pool.adjust(
                store.db, {identity_value}, "quality_refs", -1 if operation == "release_quality" else 1
            )
    assert list(store.db.iterdump()) == before


def test_cold_bytes_cannot_cover_missing_legacy_counts_during_mixed_transfer(store):
    enable(store.db)
    store.bounded = True
    with store.db:
        for i in range(130):
            numeric_pool.intern(store.db, metric(i))
    blocks.install(store.db, store.cached_bytes)
    state = {}
    with store.db:
        store.db.execute("BEGIN")
        blocks.transfer(store.db, state, 1, time.monotonic() + 10)
    cold = blocks.summary(store.db)
    assert cold["live"] == 64
    with store.db:
        store.db.execute(
            "UPDATE market_counts SET records=?,logical_bytes=? WHERE category='numeric_details'",
            (cold["details"], cold["detail_bytes"]),
        )
    before = list(store.db.iterdump())
    with pytest.raises(ValueError, match="counter"), store.db:
        store.db.execute("BEGIN")
        blocks.transfer(store.db, state, 1, time.monotonic() + 10)
    assert list(store.db.iterdump()) == before


@pytest.mark.parametrize(
    "column,expression", [("records", "records+1"), ("records", "'unknown'"), ("logical_bytes", "'unknown'")]
)
def test_phantom_counter_after_complete_transfer_blocks_new_admission_atomically(store, column, expression):
    setup(store)
    with store.db:
        numeric_pool.intern(store.db, metric(4))
        store.db.execute(f"UPDATE market_counts SET {column}={expression} WHERE category='numeric_details'")
    before = list(store.db.iterdump())
    with pytest.raises(ValueError, match="counter"), store.db:
        numeric_pool.intern(store.db, metric(5))
    assert list(store.db.iterdump()) == before
