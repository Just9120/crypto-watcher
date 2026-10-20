import copy
import sqlite3
import time
from contextlib import closing

import pytest

from cryptowatcher import quality_frames, quality_packets
from cryptowatcher.market_storage import enable
from cryptowatcher.quality_layout import put
from cryptowatcher.storage import StateError, Store, encode
from tests.test_outcomes import NOW
from tests.test_quality_packets import fact, save


def setup(store, count=3):
    enable(store.db)
    store.bounded = True
    store.clock_ms = lambda: NOW
    with store.db:
        for index in range(count):
            save(store, [fact(index * 100 + i) for i in range(8)], cohort=f"cohort{index}")
    return public(store)


def public(store):
    return [
        (day, cohort, chunk, encode(quality_packets.unpack(store.db, bytes(raw))))
        for day, cohort, chunk, raw in store.db.execute(
            "SELECT day,cohort,chunk,facts FROM market_quality ORDER BY day,cohort,chunk"
        )
    ]


def numeric(store):
    return [tuple(row) for row in store.db.execute("SELECT * FROM market_numeric ORDER BY id")]


def tick(store, state, batch=64):
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        return quality_frames.advance(store.db, state, batch, time.monotonic() + 5)


def test_framing_preserves_numeric_ids_leases_old_snapshot_and_reopen(store):
    expected = setup(store)
    numbers = numeric(store)
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        reader.db.execute("BEGIN")
        assert public(reader) == expected
        quality_frames.install(store.db)
        state = {}
        assert tick(store, state)
        assert state["packets_moved"] == 3 and state["frames_created"] == 1
        assert public(store) == expected and numeric(store) == numbers
        assert public(reader) == expected
        reader.db.rollback()
        assert public(reader) == expected
    actual_bytes = store.db.execute("SELECT SUM(length(payload)) FROM market_quality_frames").fetchone()[0]
    assert (
        store.db.execute(
            "SELECT logical_bytes FROM market_counts WHERE category='quality_frames'"
        ).fetchone()[0]
        == actual_bytes
    )
    store.db.execute("VACUUM")
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        assert public(reader) == expected and numeric(reader) == numbers
        assert reader.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not reader.db.execute("PRAGMA foreign_key_check").fetchall()


def test_partial_expiry_clears_only_unowned_slot_and_last_owner_releases_frame(store):
    expected = setup(store, 2)
    quality_frames.install(store.db)
    tick(store, {})
    with store.db:
        store.db.execute("DELETE FROM market_quality WHERE cohort='cohort0'")
    assert quality_frames.status(store.db, NOW)["pending_work"]
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        quality_frames.collect(store.db, time.monotonic() + 5)
    assert public(store) == expected[1:]
    from cryptowatcher.indexed_frame import all_values

    frame = store.db.execute("SELECT payload FROM market_quality_frames").fetchone()[0]
    assert all_values(bytes(frame))[0] is None
    with store.db:
        store.db.execute("DELETE FROM market_quality")
        quality_frames.collect(store.db, time.monotonic() + 5)
    assert not store.db.execute("SELECT 1 FROM market_quality_frames").fetchone()


def test_late_append_keeps_prefix_and_uses_original_queue_age_until_reframed(store):
    setup(store, 1)
    quality_frames.install(store.db)
    state = {}
    tick(store, state)
    numbers = set(row[0] for row in numeric(store))
    original = [fact(i) for i in range(8)]
    for index, stamp in ((8, NOW + 10), (9, NOW + 20)):
        store.clock_ms = lambda: stamp
        raw = bytes(store.db.execute("SELECT facts FROM market_quality").fetchone()[0])
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            appended = quality_packets.append(store.db, raw, fact(index), expected_count=index)
            put(
                store.db,
                (20000, "cohort0", 0, 20366, encode({"detector": "price"}), "{}", appended, index + 1),
            )
    pending = quality_frames.status(store.db, NOW + 100)
    assert pending["oldest_ms"] == NOW + 10 and pending["oldest_age_ms"] == 90
    assert tick(store, state)
    assert not quality_frames.status(store.db, NOW + 100)["pending_work"]
    raw = bytes(store.db.execute("SELECT facts FROM market_quality").fetchone()[0])
    assert encode(quality_packets.unpack(store.db, raw)) == encode([*original, fact(8), fact(9)])
    assert numbers <= set(row[0] for row in numeric(store))


def test_late_publication_failure_rolls_back_whole_frame_without_losing_outer_changes(store):
    setup(store, 2)
    quality_frames.install(store.db)
    records = [
        quality_frames.prepare(store.db, row)
        for row in store.db.execute(
            "SELECT day,cohort,chunk,facts FROM market_quality_blocks ORDER BY cohort"
        )
    ]
    second = records[1].key[1]
    store.db.execute(
        f"CREATE TRIGGER fail_frame BEFORE UPDATE OF facts ON market_quality_blocks WHEN NEW.cohort={second} AND substr(NEW.facts,1,4)=x'51463100' BEGIN SELECT RAISE(ABORT,'injected'); END"
    )
    store.db.commit()
    with store.db:
        store.db.execute("INSERT INTO runtime VALUES('unrelated','123')")
        before = list(store.db.iterdump())
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            quality_frames.publish(store.db, records)
        assert list(store.db.iterdump()) == before
    assert store.get_runtime("unrelated") == 123


def test_fixed_initial_keys_survive_vacuum_and_late_lower_key_is_handled_by_live_queue(store):
    setup(store)
    quality_frames.install(store.db)
    state = {}
    assert not tick(store, state, 1)
    upper = copy.deepcopy(state["upper"])
    with store.db:
        save(store, [fact(999)], day=19999, cohort="late")
    store.db.execute("VACUUM")
    for _ in range(4):
        if tick(store, state, 1):
            break
    assert state["initial_complete"] and state["upper"] == upper
    assert quality_frames.status(store.db, NOW)["pending_work"]
    tick(store, state)
    assert not quality_frames.status(store.db, NOW)["pending_work"]
    assert len(public(store)) == 4


def test_corrupt_frame_rejected_before_new_work_and_invalid_checkpoint_has_no_dml(store):
    setup(store)
    quality_frames.install(store.db)
    state = {}
    tick(store, state)
    raw = bytes(store.db.execute("SELECT facts FROM market_quality LIMIT 1").fetchone()[0])
    with store.db:
        payload = bytearray(store.db.execute("SELECT payload FROM market_quality_frames").fetchone()[0])
        payload[-1] ^= 1
        store.db.execute("UPDATE market_quality_frames SET payload=?", (bytes(payload),))
    with pytest.raises(ValueError, match="checksum"):
        quality_packets.unpack(store.db, raw)
    before = list(store.db.iterdump())
    with pytest.raises(StateError, match="Incomplete"), store.db:
        store.db.execute("BEGIN IMMEDIATE")
        quality_frames.advance(store.db, {"cursor": [1, 1, 1]}, 64, time.monotonic() + 5)
    assert list(store.db.iterdump()) == before


@pytest.mark.parametrize("change", ["value", "duplicate", "stale"])
def test_prepared_mutation_duplicate_and_stale_source_preserve_all_rows(store, change):
    setup(store, 1)
    quality_frames.install(store.db)
    row = store.db.execute("SELECT day,cohort,chunk,facts FROM market_quality_blocks").fetchone()
    prepared = quality_frames.prepare(store.db, row)
    if change == "value":
        prepared.value[3][0][0][-2] = 999.0
    elif change == "stale":
        with store.db:
            raw = quality_packets.append(store.db, prepared.raw, fact(500), expected_count=8)
            put(store.db, (20000, "cohort0", 0, 20366, encode({"detector": "price"}), "{}", raw, 9))
    before = list(store.db.iterdump())
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        with pytest.raises(StateError, match="Changed|Duplicate|Stale"):
            quality_frames.publish(store.db, [prepared, prepared] if change == "duplicate" else [prepared])
    assert list(store.db.iterdump()) == before


def test_active_expiry_frontier_tail_stays_readable_until_closed_day_and_full_chunk_is_eligible(store):
    from tests.test_outcomes import DAY

    setup(store, 0)
    quality_frames.install(store.db)
    day = NOW // DAY - store.snapshot().settings.detail_retention_days
    with store.db:
        save(store, [fact(i) for i in range(8)], day=day, cohort="tail")
        save(store, [fact(i) for i in range(128)], day=day, cohort="full")
    expected = public(store)
    state = {}
    assert tick(store, state)
    raw = {cohort: bytes(raw) for cohort, raw in store.db.execute("SELECT cohort,facts FROM market_quality")}
    assert raw["tail"].startswith(quality_packets.MAGIC)
    assert raw["full"].startswith(quality_frames.MAGIC)
    pending = quality_frames.status(store.db, NOW)
    assert not pending["pending_work"] and pending["next_eligible_ms"] == (NOW // DAY + 1) * DAY
    assert public(store) == expected
    store.clock_ms = lambda: (NOW // DAY + 1) * DAY
    assert quality_frames.status(store.db, store.clock_ms())["pending_work"]
    tick(store, state)
    assert not quality_frames.status(store.db, store.clock_ms())["pending_work"]
    assert public(store) == expected


@pytest.mark.parametrize(
    "checkpoint",
    [
        {"cursor": [1, -1, 0], "upper": [2, 1, 0]},
        {"cursor": [-1, -1, -1], "upper": [2, 1, 0], "initial_complete": True},
    ],
)
def test_invalid_completion_or_mixed_sentinel_cannot_skip_initial_scan(store, checkpoint):
    setup(store)
    quality_frames.install(store.db)
    before = list(store.db.iterdump())
    with pytest.raises(StateError), store.db:
        store.db.execute("BEGIN IMMEDIATE")
        quality_frames.advance(store.db, checkpoint, 64, time.monotonic() + 5)
    assert list(store.db.iterdump()) == before


def test_explicit_worker_clock_controls_frontier_and_due_selection(store):
    from tests.test_outcomes import DAY

    setup(store, 0)
    quality_frames.install(store.db)
    day = NOW // DAY - store.snapshot().settings.detail_retention_days
    with store.db:
        save(store, [fact(1)], day=day)
    expected = public(store)
    state = {}
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        quality_frames.advance(store.db, state, 64, time.monotonic() + 5, now_ms=NOW + 2 * DAY)
    raw = bytes(store.db.execute("SELECT facts FROM market_quality").fetchone()[0])
    assert raw.startswith(quality_frames.MAGIC) and public(store) == expected
