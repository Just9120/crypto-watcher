import copy
import time
from contextlib import closing
from unittest.mock import patch

import pytest

from cryptowatcher import catalogue_repack
from cryptowatcher import context_tile_maintenance as maintenance
from cryptowatcher import context_tiles as tiles
from cryptowatcher.capacity import stable_reason
from cryptowatcher.context_integrity import restore
from cryptowatcher.market_storage import DAY, read_context, save_context
from cryptowatcher.storage import StateError, Store, encode
from cryptowatcher.storage_meter import report
from cryptowatcher.storage_repack import VERSION, async_work, work
from tests.test_catalogue_repack import complete
from tests.test_context_tiles import BASE, setup, values


def tick(store, state, *, batch=64, now=BASE):
    store.clock_ms = lambda: now
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        return maintenance.advance(store.db, state, batch, time.monotonic() + 5, now_ms=now)


def test_initial_ceiling_is_fixed_while_new_hot_points_enter_independent_live_queue(store):
    keys, refs, _ = setup(store, 16)
    state = {}
    assert not tick(store, state, batch=1)
    upper = list(state["upper"])
    with store.db:
        for index in range(16, 72):
            save_context(store.db, "ticker", "spot/BTCUSDT", values(index))
    while not state.get("initial_complete"):
        tick(store, state, batch=7)
    assert state["upper"] == upper and state["cursor"] == upper
    # Current full chunks can be moved without waiting for the UTC boundary.
    tick(store, state)
    assert state["points_moved"] == 64 and state["tiles_created"] == 1
    assert maintenance.status(store.db, BASE)["next_eligible_ms"] == (BASE // DAY + 1) * DAY
    assert not maintenance.status(store.db, BASE)["pending_work"]
    for index, reference in enumerate(refs):
        assert restore(store.db, reference) == encode({"ticker": values(index)}).encode()
    assert read_context(store.db, "ticker", "spot/BTCUSDT", keys[-1][2]) == values(15)


def test_closed_tail_live_backlog_age_reopen_late_lease_and_bounded_gc(store):
    _, refs, _ = setup(store, 8)
    state = {}
    assert tick(store, state)
    assert not state.get("points_moved")
    boundary = (BASE // DAY + 1) * DAY
    assert not maintenance.status(store.db, boundary - 1)["pending_work"]
    pending = maintenance.status(store.db, boundary + 60001)
    assert pending["pending_work"] and pending["oldest_age_ms"] == 60001
    with closing(Store(store.path, store.owner_id)) as reopened:
        tick(reopened, state, now=boundary + 60001)
        assert (
            state["points_moved"] == 8
            and not maintenance.status(reopened.db, boundary + 60001)["pending_work"]
        )
        old_payload = reopened.db.execute("SELECT payload FROM market_context_tiles").fetchone()[0]
        with reopened.db:
            save_context(reopened.db, "ticker", "spot/BTCUSDT", values(0), BASE + 38 * DAY)
        assert reopened.db.execute("SELECT payload FROM market_context_tiles").fetchone()[0] == old_payload
        assert restore(reopened.db, refs[0]) == encode({"ticker": values(0)}).encode()


def test_partial_progress_keeps_the_original_live_queue_age_until_the_last_chunk(store):
    setup(store, 0)
    state = {}
    assert tick(store, state)
    with store.db:
        for index in range(128):
            save_context(store.db, "ticker", "spot/BTCUSDT", values(index))
    now = BASE + 60001
    tick(store, state, batch=1, now=now)
    pending = maintenance.status(store.db, now)
    assert pending["oldest_ms"] == BASE and pending["oldest_age_ms"] == 60001
    assert state["points_moved"] == 64
    tick(store, state, batch=1, now=now + 1)
    pending = maintenance.status(store.db, now + 1)
    assert state["points_moved"] == 128 and pending["oldest_ms"] is None
    assert pending["body_gc_pending"] and pending["pending_work"]
    tick(store, state, now=now + 2)
    assert not maintenance.status(store.db, now + 2)["pending_work"]


def test_late_interior_points_remain_hot_without_infinite_work_or_crossing_a_tile(store):
    keys, _, _ = setup(store, 8)
    tiles.publish(store.db, keys)
    state = {}
    assert tick(store, state)
    with store.db:
        for index in range(8):
            value = {**values(index), "available_at_ms": BASE + index * 60000 + 1}
            save_context(store.db, "ticker", "spot/BTCUSDT", value)
    boundary = (BASE // DAY + 1) * DAY
    tick(store, state, now=boundary)
    assert not maintenance.status(store.db, boundary)["pending_work"]
    assert store.db.execute("SELECT COUNT(*) FROM market_context").fetchone()[0] == 8
    assert store.db.execute("SELECT COUNT(*) FROM market_context_tiles").fetchone()[0] == 1
    assert read_context(store.db, "ticker", "spot/BTCUSDT", BASE + 1)["available_at_ms"] == BASE + 1


def test_unsupported_old_scalar_stays_exactly_hot_and_is_visible_as_unqualified_fallback(store):
    _, _, _ = setup(store, 0)
    with store.db:
        for index in range(8):
            save_context(
                store.db,
                "ticker",
                "spot/BTCUSDT",
                {
                    **values(index),
                    "wide": 2**200,
                    "market_observation_ms": "legacy-untyped",
                },
            )
    state = {}
    assert tick(store, state, now=BASE + DAY)
    value = maintenance.status(store.db, BASE + DAY)
    assert value["unsupported_pending"] and not value["pending_work"]
    assert state["fallback_points"] == 8
    assert read_context(store.db, "ticker", "spot/BTCUSDT", BASE)["wide"] == 2**200
    assert store.db.execute("SELECT COUNT(*) FROM market_context").fetchone()[0] == 8


@pytest.mark.parametrize(
    "checkpoint",
    [
        {"version": 999},
        {"cursor": [1, "ticker", 0]},
        {"upper": [1, "ticker", 0], "cursor": [2, "ticker", 0]},
        {"upper": [1, "ticker", 0], "cursor": [0, "", -1], "initial_complete": True},
        {"upper": [True, "ticker", 0], "cursor": [0, "", -1]},
    ],
)
def test_invalid_checkpoint_is_rejected_before_any_data_or_ddl_change(store, checkpoint):
    setup(store)
    before = list(store.db.iterdump())
    with pytest.raises(StateError), store.db:
        store.db.execute("BEGIN IMMEDIATE")
        maintenance.advance(store.db, checkpoint, 64, time.monotonic() + 5, now_ms=BASE)
    assert list(store.db.iterdump()) == before


def test_deadline_and_publication_failure_do_not_advance_source_cursor(store, monkeypatch):
    setup(store)
    state = {}
    with store.db:
        store.db.execute("BEGIN")
        assert not maintenance.advance(store.db, state, 64, time.monotonic() - 1, now_ms=BASE)
    assert state == {}
    before = list(store.db.iterdump())
    monkeypatch.setattr(
        tiles, "publish", lambda *_: (_ for _ in ()).throw(RuntimeError("interrupted publication"))
    )
    with pytest.raises(RuntimeError, match="interrupted"):
        tick(store, state, now=BASE + DAY)
    assert state == {} and list(store.db.iterdump()) == before


def test_upgrade18_preserves_all_existing_boundaries_and_blocks_forged_context_completion(store):
    setup(store, 0)
    state = complete(store, BASE)
    previous = copy.deepcopy(state)
    for key in ("context_tiles", "context_tile_maintenance", "context_tile_initial_reclaim_complete"):
        previous.pop(key)
    previous.update(version=18, next_phase=16, operator_note="preserve18")
    with store.db:
        store.put_runtime("storage_repack", previous)
    first = work(store, BASE, seconds=0)
    assert (
        first["version"] == VERSION and first["previous_version"] == 18 and first["initial_reclaim_complete"]
    )
    final = complete(store, BASE)
    for key in (
        "source_transfer_upper",
        "numeric_transfer_upper",
        "catalogue_transfer",
        "catalogue_queue_upper",
        "packet_bundles",
        "numeric_refs",
        "quality_frames",
    ):
        assert final.get(key) == previous.get(key)
    assert final["operator_note"] == "preserve18" and final["context_tile_initial_reclaim_complete"]
    forged = {**final, "context_tiles": {"version": 1}}
    with store.db:
        store.put_runtime("storage_repack", forged)
    before = list(store.db.iterdump())
    with pytest.raises(ValueError, match="context tile"):
        work(store, BASE)
    assert list(store.db.iterdump()) == before
    with patch("cryptowatcher.capacity.time.time", return_value=BASE / 1000):
        assert "CONTEXT_TILE_INITIAL_INCOMPLETE" in stable_reason(store)


def test_observer_and_meter_include_current_live_context_backlog_and_preserve_samples(store):
    setup(store, 0)
    complete(store, BASE)
    with store.db:
        for index in range(8):
            save_context(store.db, "ticker", "spot/BTCUSDT", values(index))
    boundary = (BASE // DAY + 1) * DAY
    with patch("cryptowatcher.capacity.time.time", return_value=(boundary + 60001) / 1000):
        assert "CONTEXT_TILE_BACKLOG" in stable_reason(store)
    assert report(store, BASE)["context_tile_layout"]["installed"]
    store.clock_ms = lambda: boundary + 60001
    complete(store, store.clock_ms())
    with patch("cryptowatcher.capacity.time.time", return_value=store.clock_ms() / 1000):
        assert not any(reason.startswith("CONTEXT_TILE_") for reason in stable_reason(store))


@pytest.mark.asyncio
async def test_context_pending_reuses_existing_worker_without_a_new_job(store):
    pending = {"state": "COMPLETE", "context_tile_maintenance": {"pending_work": True}}
    idle = {"state": "COMPLETE", "context_tile_maintenance": {"pending_work": False}}
    with patch("cryptowatcher.storage_repack.work", side_effect=[pending, pending, idle]) as tick_work:
        assert await async_work(store, BASE, slices=8) == idle
    assert tick_work.call_count == 3 and not catalogue_repack.busy(idle)


def test_unknown_context_layout_is_rejected_before_installing_other_extensions(store):
    setup(store, 0)
    with store.db:
        store.db.execute("UPDATE market_meta SET value='999' WHERE key='context_tiles'")
    before = list(store.db.iterdump())
    with pytest.raises(StateError, match="context tile"):
        work(store, BASE)
    assert list(store.db.iterdump()) == before
    assert not store.db.execute("SELECT 1 FROM market_meta WHERE key='quality_frames'").fetchone()


@pytest.mark.parametrize("readonly", [True, False])
def test_unknown_context_marker_rejects_store_open_without_migration(store, readonly):
    import hashlib

    setup(store, 0)
    with store.db:
        store.db.execute("UPDATE market_meta SET value='999' WHERE key='context_tiles'")
    store.close()
    before = hashlib.sha256(store.path.read_bytes()).digest()
    with pytest.raises(StateError, match="context tile"):
        Store(store.path, store.owner_id, readonly=readonly)
    assert hashlib.sha256(store.path.read_bytes()).digest() == before


def test_observer_rejects_explicit_hot_fallback_and_unknown_queue_clock(store):
    setup(store, 0)
    complete(store, BASE)
    with store.db:
        for index in range(8):
            save_context(
                store.db, "ticker", "spot/BTCUSDT", {**values(index), "market_observation_ms": "legacy"}
            )
    boundary = (BASE // DAY + 1) * DAY
    store.clock_ms = lambda: boundary
    complete(store, boundary)
    with patch("cryptowatcher.capacity.time.time", return_value=boundary / 1000):
        assert "CONTEXT_TILE_UNQUALIFIED_FALLBACK" in stable_reason(store)
    with store.db:
        store.db.execute("UPDATE market_context_tile_pending SET queued_at=?", (boundary + 30001,))
    with patch("cryptowatcher.capacity.time.time", return_value=boundary / 1000):
        assert "CONTEXT_TILE_CLOCK_UNKNOWN" in stable_reason(store)
