import copy
import sqlite3
import time
from contextlib import closing

import pytest

from cryptowatcher import numeric_frame_work as work
from cryptowatcher import numeric_frames, quality_packets
from cryptowatcher.storage import Store
from tests.test_numeric_frames import prepared
from tests.test_numeric_search import finish
from tests.test_quality_packets import fact, metric, save

NOW = 1800000000000


def setup(store):
    store.clock_ms = lambda: NOW
    packet, public, sources = prepared(store)
    work.install(store.db)
    return packet, public, sources


def advance(store, state, batch=64, now=NOW, deadline=None):
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        result = work.advance(
            store.db,
            state,
            batch,
            time.monotonic() + 3 if deadline is None else deadline,
            now_ms=now,
            cache=store.cached_bytes,
        )
        store.put_runtime("numeric-work-test-checkpoint", state)
    return result


def test_finite_initial_resume_preserves_values_and_does_not_reopen_its_boundaries(store):
    packet, public, _ = setup(store)
    state = {"operator_note": "keep"}
    assert not advance(store, state, batch=1)
    assert state["cursor"] == 0 and state["upper"] == 2 and "queue_upper" not in state
    first = copy.deepcopy(state)
    with closing(Store(store.path, store.owner_id)) as reopened:
        reopened.clock_ms = lambda: NOW
        state = reopened.get_runtime("numeric-work-test-checkpoint")
        assert state == first and advance(reopened, state)
        assert state["cursor"] == state["upper"] == 2 and state["queue_upper"] > 0
        assert state["blocks_packed"] == 2 and state["operator_note"] == "keep"
        assert quality_packets.unpack(reopened.db, packet) == public
        assert work.through(reopened.db, state["queue_upper"])
        assert work.status(reopened.db, NOW)["count"] == 0
        before = copy.deepcopy(state)
        assert advance(reopened, state)
        assert state == before


def test_deadline_does_not_admit_work_and_partial_publication_rolls_back_checkpoint_and_all_objects(
    store, monkeypatch
):
    _, _, _ = setup(store)
    state = {}
    before = list(store.db.iterdump())
    with store.db:
        store.db.execute("BEGIN")
        assert not work.advance(
            store.db, state, 64, time.monotonic() - 1, now_ms=NOW, cache=store.cached_bytes
        )
    assert state == {} and list(store.db.iterdump()) == before
    original = numeric_frames.publish

    def fail(*args):
        original(*args)
        raise RuntimeError("injected numeric worker publication")

    monkeypatch.setattr(numeric_frames, "publish", fail)
    with pytest.raises(RuntimeError, match="worker publication"), store.db:
        store.db.execute("BEGIN")
        work.advance(store.db, state, 64, time.monotonic() + 3, now_ms=NOW, cache=store.cached_bytes)
    assert state == {} and list(store.db.iterdump()) == before


def add_live(store):
    values = [fact(900, [metric(1000 + i) for i in range(129)])]
    with store.db:
        raw = save(store, values, cohort="live")
    finish(store)
    return raw, values


def test_live_small_group_wait_is_not_analytics_backlog_and_old_id_can_be_admitted_after_initial_watermark(
    store,
):
    _, _, _ = setup(store)
    state = {}
    assert advance(store, state)
    old_upper, queue_upper = state["upper"], state["queue_upper"]
    raw, public = add_live(store)
    rows = store.db.execute(
        "SELECT id,block,queued_at,eligible_at FROM market_numeric_frame_pending ORDER BY id"
    ).fetchall()
    assert len(rows) == 2 and rows[0][1] <= old_upper and all(r[0] > queue_upper for r in rows)
    pending = work.status(store.db, NOW)
    assert (
        pending["count"] == 2
        and not pending["pending_work"]
        and pending["next_eligible_ms"] == NOW + work.DELAY_MS
    )
    assert work.through(store.db, queue_upper) and advance(store, state, now=NOW + work.DELAY_MS - 1)
    assert work.status(store.db, NOW + work.DELAY_MS)["pending_work"]
    assert advance(store, state, now=NOW + work.DELAY_MS)
    assert state["upper"] == old_upper and state["queue_upper"] == queue_upper and state["blocks_packed"] == 4
    assert not work.status(store.db, NOW + work.DELAY_MS)["pending_work"]
    assert quality_packets.unpack(store.db, raw) == public


def test_full_64_block_group_is_eligible_without_waiting_five_minutes(store):
    setup(store)
    state = {}
    assert advance(store, state)
    for batch in range(2):
        values = [
            fact(10000 + batch * 128 + i, [metric(20000 + (batch * 128 + i) * 20 + j) for j in range(20)])
            for i in range(128)
        ]
        with store.db:
            save(store, values, cohort="full-" + str(batch))
    finish(store, batch=128)
    pending = work.status(store.db, NOW)
    assert pending["count"] >= 64 and pending["pending_work"] and pending["oldest_age_ms"] == 0
    packed = state["blocks_packed"]
    assert advance(store, state)
    assert state["blocks_packed"] == packed + 64


@pytest.mark.parametrize(
    "state",
    [
        {"version": True},
        {"version": 1},
        {"cursor": 0},
        {"queue_upper": 0},
        {"version": 1, "mode": "1", "cursor": 0, "upper": 0, "initial_complete": True},
        {"version": 1, "mode": "1", "cursor": 1, "upper": 0},
        {"version": 1, "mode": "1", "cursor": 0, "upper": 0, "queue_upper": True},
    ],
)
def test_partial_forged_or_wrongly_typed_checkpoint_is_rejected_without_mutation(store, state):
    setup(store)
    before = list(store.db.iterdump())
    original = copy.deepcopy(state)
    with pytest.raises(ValueError), store.db:
        store.db.execute("BEGIN")
        work.advance(store.db, state, 64, time.monotonic() + 3, now_ms=NOW, cache=store.cached_bytes)
    assert state == original and list(store.db.iterdump()) == before


@pytest.mark.parametrize("fault", ["counter", "guard", "clock"])
def test_missing_metadata_or_future_clock_does_not_turn_into_empty_work(store, fault):
    setup(store)
    state = {}
    assert advance(store, state)
    add_live(store)
    with store.db:
        if fault == "counter":
            store.db.execute("DELETE FROM market_counts WHERE category=?", (work.CATEGORY,))
        elif fault == "guard":
            store.db.execute("DROP TRIGGER market_nframe_queue_clock")
    before = list(store.db.iterdump())
    with pytest.raises((ValueError, sqlite3.Error)):
        if fault == "guard":
            work.header(store.db)
        else:
            with store.db:
                store.db.execute("BEGIN")
                work.advance(
                    store.db,
                    state,
                    64,
                    time.monotonic() + 3,
                    now_ms=NOW - 60000 if fault == "clock" else NOW,
                    cache=store.cached_bytes,
                )
    assert list(store.db.iterdump()) == before


def test_runtime_queries_are_bounded_index_walks_and_admission_clocks_are_immutable(store):
    setup(store)
    state = {}
    assert advance(store, state)
    add_live(store)
    for query in (
        "SELECT block FROM market_numeric_frame_pending ORDER BY eligible_at,queued_at,id LIMIT 64",
        "SELECT id,block FROM market_numeric_frame_pending WHERE id<=999 ORDER BY id LIMIT 64",
    ):
        plan = " ".join(r[3] for r in store.db.execute("EXPLAIN QUERY PLAN " + query))
        assert "TEMP B-TREE" not in plan
        assert "market_numeric_frame_age" in plan or "INTEGER PRIMARY KEY" in plan
    before = list(store.db.iterdump())
    with pytest.raises(sqlite3.IntegrityError, match="admission clock"), store.db:
        store.db.execute(
            "UPDATE market_numeric_frame_pending SET queued_at=queued_at+1,eligible_at=eligible_at+1"
        )
    assert list(store.db.iterdump()) == before


def test_admission_reads_wall_clock_once_even_when_it_changes_between_calls(store):
    setup(store)
    assert advance(store, {})
    stamp = NOW

    def changing_clock():
        nonlocal stamp
        stamp += 1
        return stamp

    store.clock_ms = changing_clock
    add_live(store)
    rows = store.db.execute("SELECT queued_at,eligible_at FROM market_numeric_frame_pending").fetchall()
    assert len(rows) == 2 and all(row[1] - row[0] == work.DELAY_MS for row in rows)
    with store.db:
        store.db.execute(
            "INSERT INTO market_numeric_frame_pending(block,queued_at,eligible_at) VALUES(?,?,?)",
            (
                store.db.execute("SELECT max(id) FROM market_numeric_blocks").fetchone()[0],
                NOW + 60000,
                NOW + 60000 + work.DELAY_MS,
            ),
        )
    assert work.status(store.db, NOW)["clock_unknown"]


def test_forged_completion_with_real_old_admissions_is_rejected(store):
    setup(store)
    state = {}
    assert not advance(store, state, batch=1)
    maximum = store.db.execute("SELECT max(id) FROM market_numeric_frame_pending").fetchone()[0]
    assert maximum is not None
    forged = {**state, "cursor": state["upper"], "queue_upper": maximum, "initial_complete": True}
    before = list(store.db.iterdump())
    with pytest.raises(ValueError, match="Unfinished initial"), store.db:
        store.db.execute("BEGIN")
        work.advance(store.db, forged, 64, time.monotonic() + 3, now_ms=NOW)
    assert list(store.db.iterdump()) == before


def test_explicit_structural_boundary_reconciles_actual_owners_seals_bytes_and_pending_read_only(store):
    from cryptowatcher.capacity import boundary_blockers

    _, _, _ = setup(store)
    assert advance(store, {})
    add_live(store)
    before = list(store.db.iterdump())
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        reader.db.execute("BEGIN")
        measured = work.measure(reader.db, time.monotonic() + 3, reader.cached_bytes)
        assert measured["state"] == "MEASURED" and measured["frames"] == 1
        assert measured["owned_blocks"] == 2 and measured["pending_rows"] == 2
        assert (
            measured["payload_bytes"]
            == reader.db.execute("SELECT sum(length(payload)) FROM market_numeric_frames").fetchone()[0]
        )
        assert work.measure(reader.db, time.monotonic() - 1)["state"] == "PARTIAL_BUDGET"
    assert list(store.db.iterdump()) == before
    value = {"storage": {"numeric_layout": {"frame_mode": "1"}}}
    assert "BOUNDARY_NUMERIC_FRAME_LEDGER_INCOMPLETE" in boundary_blockers(value)
    value["runtime_ledger"] = {"numeric_frames": measured}
    assert "BOUNDARY_NUMERIC_FRAME_LEDGER_INCOMPLETE" not in boundary_blockers(value)
    value["runtime_ledger"]["numeric_frames"] = {"state": "N/A_COMPAT"}
    assert "BOUNDARY_NUMERIC_FRAME_LEDGER_INCOMPLETE" in boundary_blockers(value)


@pytest.mark.parametrize("fault", ["counter", "seal", "mask", "value"])
def test_structural_boundary_cannot_report_measured_with_corrupt_metadata(store, fault):
    setup(store)
    assert advance(store, {})
    # Corrupt existing bytes through incremental I/O, bypassing guarded SQL
    # only in this isolated fixture to represent actual disk corruption.
    if fault == "counter":
        with store.db:
            store.db.execute(
                "UPDATE market_counts SET logical_bytes=logical_bytes+1 WHERE category=?",
                (numeric_frames.CATEGORY,),
            )
    elif fault == "value":
        from cryptowatcher import metric_codec, numeric_block_codec, numeric_frame_codec

        identity, raw = store.db.execute("SELECT id,payload FROM market_numeric_frames LIMIT 1").fetchone()
        entries = numeric_frame_codec.all_values(bytes(raw))
        block, payload = entries[0]
        values = numeric_block_codec.unpack(payload, block)
        slot = next(i for i, value in enumerate(values) if value)
        values[slot] = metric_codec.pack(metric(987654))
        entries[0] = (block, numeric_block_codec.pack(block, values, recipes=True))
        replacement = numeric_frame_codec.pack(entries)
        guard = store.db.execute(
            "SELECT sql FROM sqlite_master WHERE name='market_nframe_payload'"
        ).fetchone()[0]
        # Represent external corruption using a fully validly checksummed body.
        # Restore the exact guard before measuring; only stale NS2 can catch it.
        with store.db:
            store.db.execute("BEGIN")
            store.db.execute("DROP TRIGGER market_nframe_payload")
            store.db.execute("UPDATE market_numeric_frames SET payload=? WHERE id=?", (replacement, identity))
            store.db.execute(guard)
    else:
        table, column = (
            ("market_numeric_block_refs", "search_seal")
            if fault == "seal"
            else ("market_numeric_frames", "slots")
        )
        identity = store.db.execute(
            "SELECT id FROM " + table + " WHERE " + column + " IS NOT NULL LIMIT 1"
        ).fetchone()[0]
        with store.db, store.db.blobopen(table, column, identity) as blob:
            value = blob.read()
            blob.seek(0)
            blob.write(bytes([value[0] ^ 1]) + value[1:])
    before = list(store.db.iterdump())
    with pytest.raises(ValueError):
        work.measure(store.db, time.monotonic() + 3, store.cached_bytes)
    assert list(store.db.iterdump()) == before
