import copy
import json
import sqlite3
import time
from contextlib import closing

import pytest

from cryptowatcher import candle_block_codec as codec
from cryptowatcher import candle_blocks as blocks
from cryptowatcher import source_column_work as work
from cryptowatcher.market_storage import read, stream_id
from cryptowatcher.reset_history import private_state, signature
from cryptowatcher.storage import Store, encode
from tests.test_bounded_storage import bounded
from tests.test_source_slot_columns import dense


def prepared(store, count=3):
    bounded(store)
    blocks.install(store.db, store.cached_text)
    tile, values = dense()
    with store.db:
        sid = stream_id(store.db, "spot/BTCUSDT/1m", create=True)
        store.db.execute("INSERT INTO market_meta VALUES('candle_block_runtime','1')")
        for i in range(count):
            slots = copy.deepcopy(values)
            for slot in slots:
                slot[0][3][7:] = [tile + (count + 10) * 32 * 60000] * 2
            raw = codec.pack(sid, tile + i * 32 * 60000, 60000, slots)
            blocks.put(store.db, raw, None)
    rows = [
        (identity, bytes(raw), codec.compact(bytes(raw)))
        for identity, raw in store.db.execute("SELECT id,payload FROM market_candle_blocks ORDER BY id")
    ]
    assert all(old != new for _, old, new in rows)
    return sid, tile, rows


def snapshot(store):
    return {
        name: sorted(
            repr(tuple(row)) for row in store.db.execute('SELECT * FROM "' + name.replace('"', '""') + '"')
        )
        for (name,) in store.db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    }


def test_explicit_adoption_same_ids_values_counters_old_snapshot_fresh_reopen_and_normal_updates(store):
    sid, tile, rows = prepared(store)
    private, settings = signature(private_state(store)), store.snapshot()
    expected = read(store.db, "spot/BTCUSDT/1m", limit=256, cache=store.cached_bytes)
    before = blocks.summary(store.db)
    counter = store.db.execute(
        "SELECT inserted_bytes,deleted_bytes FROM market_counts WHERE category='source_blocks'"
    ).fetchone()
    with closing(Store(store.path, store.owner_id, readonly=True)) as old:
        old.db.execute("BEGIN")
        assert old.db.execute("SELECT value FROM market_meta WHERE key='candle_blocks'").fetchone()[0] == "1"
        old_payloads = old.db.execute("SELECT id,payload FROM market_candle_blocks ORDER BY id").fetchall()
        with store.db:
            work.enable(store.db)
            saved = work.rewrite(store.db, rows)
        assert (
            old.db.execute("SELECT id,payload FROM market_candle_blocks ORDER BY id").fetchall()
            == old_payloads
        )
        assert read(old.db, "spot/BTCUSDT/1m", limit=256, cache=old.cached_bytes) == expected
        assert work.enabled(store.db) and not work.enabled(old.db)
    assert list(map(tuple, store.db.execute("SELECT id,payload FROM market_candle_blocks ORDER BY id"))) == [
        (i, new) for i, _, new in rows
    ]
    after = blocks.summary(store.db)
    assert after == {**before, "payload_bytes": before["payload_bytes"] - saved}
    assert tuple(
        store.db.execute(
            "SELECT inserted_bytes,deleted_bytes FROM market_counts WHERE category='source_blocks'"
        ).fetchone()
    ) == (counter[0], counter[1] + saved)
    assert signature(private_state(store)) == private and store.snapshot() == settings
    with closing(Store(store.path, store.owner_id, readonly=True)) as current:
        assert work.enabled(current.db)
        assert read(current.db, "spot/BTCUSDT/1m", limit=256, cache=current.cached_bytes) == expected
        assert current.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not current.db.execute("PRAGMA foreign_key_check").fetchall()
    value = codec.point(rows[0][2], sid, tile, 1)[0]
    with store.db:
        points = blocks.admit_many(store.db, sid, [{**value, "close": 102.0}], tile + 40 * 86400000)
    raw = blocks.tile(store.db, sid, tile, 60000)
    assert raw.startswith(codec.COLUMN_MAGIC)
    assert points == [(sid, tile, 2)]
    assert encode(codec.point(raw, sid, tile, 1)[0]) == encode(value)
    before = snapshot(store)
    with pytest.raises(sqlite3.OperationalError), store.db:
        store.db.execute("DELETE FROM market_candle_blocks")
    assert snapshot(store) == before and work._AUTHORITY.get() is None


@pytest.mark.parametrize(
    "failure", ["stale", "duplicate", "value", "lease", "checksum", "abort_second_insert", "counter"]
)
def test_exact_rewrite_failures_preserve_every_table_and_clear_private_authority(store, failure):
    _, _, rows = prepared(store)
    with store.db:
        work.enable(store.db)
    bad = list(rows)
    if failure == "stale":
        bad[0] = (999999, *bad[0][1:])
    elif failure == "duplicate":
        bad[1] = bad[0]
    elif failure in ("value", "lease"):
        identity, old, new = bad[0]
        sid, tile, step, slots = codec.unpack(new)
        if failure == "value":
            slots[0][0][3][4] = 0.0  # Signed zero is an immutable value.
        else:
            slots[0][0][1] += 1
        changed = codec.compact(codec.pack(sid, tile, step, slots))
        bad[0] = identity, old, changed
    elif failure == "checksum":
        identity, old, new = bad[0]
        bad[0] = identity, old, new[:-1] + bytes([new[-1] ^ 1])
    elif failure == "abort_second_insert":
        store.db.execute(
            "CREATE TRIGGER source_abort_test BEFORE INSERT ON market_candle_blocks "
            f"WHEN NEW.id={rows[1][0]} BEGIN SELECT RAISE(ABORT,'test insertion failure'); END"
        )
    else:
        store.db.execute(
            "CREATE TRIGGER source_counter_test AFTER INSERT ON market_candle_blocks BEGIN "
            "UPDATE market_counts SET logical_bytes=logical_bytes+1 WHERE category='source_blocks'; END"
        )
    store.db.commit()
    before = snapshot(store)
    with pytest.raises((ValueError, sqlite3.IntegrityError)), store.db:
        work.rewrite(store.db, bad)
    assert snapshot(store) == before and work._AUTHORITY.get() is None
    with pytest.raises(sqlite3.OperationalError), store.db:
        store.db.execute("DELETE FROM market_candle_blocks")
    assert snapshot(store) == before


def test_unadopted_direct_sql_and_warm_cache_after_rollback_cannot_write_new_format(store):
    _, _, rows = prepared(store, count=1)
    identity, old, new = rows[0]
    before = snapshot(store)
    with pytest.raises(RuntimeError, match="rollback"), store.db:
        work.enable(store.db)
        work.rewrite(store.db, rows)
        assert blocks.tile(store.db, *codec.header(new)[:3]).startswith(codec.COLUMN_MAGIC)
        raise RuntimeError("rollback")
    assert snapshot(store) == before and not work.enabled(store.db)
    sid, tile, step, n, due = codec.stats(new)
    for statement, params in (
        ("UPDATE market_candle_blocks SET payload=? WHERE id=?", (new, identity)),
        (
            "INSERT INTO market_candle_blocks(stream,tile,step,row_count,first_due,payload) VALUES(?,?,?,?,?,?)",
            (sid, tile, step, n, due, new),
        ),
    ):
        with pytest.raises(sqlite3.OperationalError), store.db:
            store.db.execute(statement, params)
        assert snapshot(store) == before


@pytest.mark.parametrize("trigger", list(work.GUARDS))
def test_unknown_guard_refuses_adoption_without_changing_any_data(store, trigger):
    prepared(store)
    store.db.execute('DROP TRIGGER "' + trigger + '"')
    store.db.commit()
    before = snapshot(store)
    with pytest.raises(ValueError, match="Unknown source relocation guard"), store.db:
        work.enable(store.db)
    assert snapshot(store) == before and not work.enabled(store.db)


def test_bounded_scan_keeps_recent_tail_advances_finite_boundary_then_revisits_aged_rows(store):
    _, tile, rows = prepared(store, count=12)
    with store.db:
        work.enable(store.db)
    before = snapshot(store)
    assert not work.advance(store.db, tile, deadline=time.monotonic() - 1)["initial_complete"]
    assert snapshot(store) == before
    recent_now = tile + 100 * 60000
    for _ in range(2):
        with store.db:
            state = work.advance(store.db, recent_now, deadline=time.monotonic() + 2)
    assert state["initial_complete"] and state["rewritten"] == 0
    old_payloads = list(
        map(tuple, store.db.execute("SELECT id,payload FROM market_candle_blocks ORDER BY id"))
    )
    assert old_payloads == [(i, old) for i, old, _ in rows]
    for _ in range(2):
        with store.db:
            state = work.advance(store.db, tile + 1000 * 60000, deadline=time.monotonic() + 2)
    assert state["initial_complete"] and state["rewritten"] == 12
    assert list(map(tuple, store.db.execute("SELECT id,payload FROM market_candle_blocks ORDER BY id"))) == [
        (i, new) for i, _, new in rows
    ]
    assert work.checkpoint(store.db) == {k: v for k, v in state.items() if k != "enabled"}


def test_normal_expiry_still_uses_current_policy_and_leases_after_rewrite(store):
    sid, tile, rows = prepared(store, count=1)
    with store.db:
        work.enable(store.db)
        work.rewrite(store.db, rows)
        value = codec.point(rows[0][2], sid, tile, 1)[0]
        blocks.admit_many(store.db, sid, [value], tile + 80 * 86400000)
    raw = blocks.tile(store.db, sid, tile, 60000)
    assert raw.startswith(codec.COLUMN_MAGIC)
    assert codec.point(raw, sid, tile, 1)[1:] == (tile + 80 * 86400000, tile + 110 * 86400000)
    with store.db:
        assert blocks.gc(store.db, tile + 100 * 86400000, batch=8) == 31
    saved = blocks.tile(store.db, sid, tile, 60000)
    assert encode(codec.point(saved, sid, tile, 1)[0]) == encode(value)
    with store.db:
        assert blocks.gc(store.db, tile + 111 * 86400000, batch=8) == 1
    assert blocks.tile(store.db, sid, tile, 60000) is None
    assert blocks.summary(store.db)["versions"] == 0 and work._AUTHORITY.get() is None


@pytest.mark.parametrize(
    "field,value",
    [("version", True), ("cursor", -1), ("upper", 0), ("initial_complete", 1), ("rewritten", 100)],
)
def test_invalid_saved_boundary_refuses_open_without_install_or_repair(store, field, value):
    prepared(store)
    with store.db:
        work.enable(store.db)
        state = work.checkpoint(store.db)
        state[field] = value
        store.db.execute("UPDATE market_meta SET value=? WHERE key=?", (json.dumps(state), work.KEY))
    before = snapshot(store)
    with pytest.raises(ValueError):
        with closing(Store(store.path, store.owner_id, readonly=True)):
            pass
    assert snapshot(store) == before


def test_source_write_guards_require_foreign_keys_and_open_never_activates_a_format(store):
    _, _, rows = prepared(store)
    original_source = list(map(tuple, store.db.execute("SELECT * FROM market_candle_blocks ORDER BY id")))
    with closing(Store(store.path, store.owner_id)) as reopened:
        assert not work.enabled(reopened.db)
        assert (
            list(map(tuple, reopened.db.execute("SELECT * FROM market_candle_blocks ORDER BY id")))
            == original_source
        )
        assert reopened.db.execute("SELECT 1 FROM market_meta WHERE key=?", (work.KEY,)).fetchone() is None
    # An ordinary writer open may advance existing derived cohort metadata;
    # neither that pre-existing behavior nor our test activates a source codec.
    before = snapshot(store)
    store.db.execute("PRAGMA foreign_keys=OFF")
    with pytest.raises(ValueError, match="foreign key"):
        work.enable(store.db)
    assert snapshot(store) == before
    store.db.execute("PRAGMA foreign_keys=ON")
    with store.db:
        work.enable(store.db)
    before = snapshot(store)
    store.db.execute("PRAGMA foreign_keys=OFF")
    with pytest.raises(ValueError, match="foreign key"):
        work.rewrite(store.db, rows)
    assert snapshot(store) == before


def test_orphan_adoption_boundary_refuses_open_without_repair(store):
    prepared(store)
    with store.db:
        store.db.execute("INSERT INTO market_meta VALUES(?,?)", (work.KEY, "{}"))
    before = snapshot(store)
    with pytest.raises(ValueError, match="lacks explicit adoption"):
        with closing(Store(store.path, store.owner_id, readonly=True)):
            pass
    assert snapshot(store) == before and not work.enabled(store.db)


def test_existing_worker_admits_source_work_and_observer_requires_initial_scan_and_reclaim(store):
    from cryptowatcher import capacity, storage_repack

    _, tile, rows = prepared(store, count=9)
    now = tile + 1000 * 60000
    for tick in range(100):
        state = storage_repack.work(store, now + tick, batch=8, seconds=0.1)
        if state["state"] == "COMPLETE":
            break
    assert state["state"] == "COMPLETE" and not work.enabled(store.db)
    with store.db:
        work.enable(store.db)
    assert "SOURCE_COLUMN_INITIAL_INCOMPLETE" in capacity.stable_reason(store)
    for tick in range(100):
        state = storage_repack.work(store, now + tick, batch=8, seconds=0.1)
        if state.get("source_column_initial_reclaim_complete"):
            break
    assert state["source_column_maintenance"]["initial_complete"]
    assert state["source_column_maintenance"]["rewritten"] == len(rows)
    assert state["source_column_initial_reclaim_complete"] is True
    assert not any(r.startswith("SOURCE_COLUMN_") for r in capacity.stable_reason(store))
    with store.db:
        store.put_runtime("storage_repack", {**state, "source_column_initial_reclaim_complete": False})
    assert "SOURCE_COLUMN_INITIAL_RECLAIM_PENDING" in capacity.stable_reason(store)
    with store.db:
        store.db.execute("UPDATE market_meta SET value='{}' WHERE key=?", (work.KEY,))
    before = snapshot(store)
    assert "SOURCE_COLUMN_CHECKPOINT_UNKNOWN" in capacity.stable_reason(store)
    assert snapshot(store) == before
