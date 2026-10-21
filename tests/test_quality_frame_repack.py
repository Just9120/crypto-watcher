import copy
from contextlib import closing
from unittest.mock import patch

import pytest

from cryptowatcher import catalogue_repack, quality_frames
from cryptowatcher.capacity import stable_reason
from cryptowatcher.storage import StateError, Store
from cryptowatcher.storage_meter import SAMPLE_FORMAT, report, sample
from cryptowatcher.storage_repack import VERSION, async_work, work
from tests.test_catalogue_repack import complete
from tests.test_outcomes import DAY, NOW
from tests.test_quality_frames import numeric, public, setup
from tests.test_quality_packets import fact, save


def test_upgrade17_only_adds_frame_boundary_preserving_body_reclaim_and_private_state(store):
    setup(store, 0)
    previous = complete(store)
    previous.pop("quality_frames")
    previous.pop("quality_frame_maintenance")
    previous.update(version=17, next_phase=15, operator_note="keep17")
    # A retained CQ5 source models an old17 write, not a reopened finite scan.
    with store.db:
        save(store, [fact(i) for i in range(8)])
        store.put_runtime("storage_repack", previous)
    expected, numbers, settings = public(store), numeric(store), store.snapshot()
    first = work(store, NOW, seconds=0)
    assert first["version"] == VERSION and first["previous_version"] == 17
    assert first["initial_reclaim_complete"] and first["state"] == "IN_PROGRESS"
    final = complete(store)
    assert final["operator_note"] == "keep17" and final["quality_frames"]["initial_complete"]
    assert public(store) == expected and numeric(store) == numbers and store.snapshot() == settings
    assert bytes(store.db.execute("SELECT facts FROM market_quality").fetchone()[0]).startswith(
        quality_frames.MAGIC
    )
    for key in (
        "catalogue_transfer",
        "catalogue_queue_upper",
        "packet_bundles",
        "numeric_refs",
        "quality_rows_scanned",
        "source_transfer_upper",
        "numeric_transfer_upper",
        "numeric_search_upper",
    ):
        assert final.get(key) == previous.get(key)
    assert work(store, NOW + 1) == final
    with closing(Store(store.path, store.owner_id, readonly=True)) as reopened:
        assert public(reopened) == expected


@pytest.mark.parametrize("fault", ["marker", "columns", "boundary", "forged"])
def test_current_frame_checkpoint_failure_preserves_all_database_state(store, fault):
    setup(store)
    state = complete(store)
    if fault == "marker":
        with store.db:
            store.db.execute("DELETE FROM market_meta WHERE key='quality_frames'")
    elif fault == "columns":
        with store.db:
            store.db.execute("DELETE FROM market_meta WHERE key='quality_frame_columns'")
    else:
        frame = copy.deepcopy(state["quality_frames"])
        if fault == "boundary":
            frame.pop("initial_complete")
        else:
            frame["cursor"] = [-1, -1, -1]
        store.put_runtime("storage_repack", {**state, "quality_frames": frame})
    before = list(store.db.iterdump())
    with pytest.raises((StateError, ValueError), match="quality (frame|column)"):
        work(store, NOW)
    assert list(store.db.iterdump()) == before
    with patch("cryptowatcher.capacity.time.time", return_value=NOW / 1000):
        assert any(reason.startswith("QUALITY_FRAME_") for reason in stable_reason(store))


def test_future_tail_is_readable_current_then_blocks_baseline_when_due_and_resumes_after_reopen(store):
    setup(store, 0)
    initial = complete(store)
    with store.db:
        save(store, [fact(8)], day=NOW // DAY - store.snapshot().settings.detail_retention_days)
    expected = public(store)
    day_end = (NOW // DAY + 1) * DAY
    with patch("cryptowatcher.capacity.time.time", return_value=NOW / 1000):
        baseline = stable_reason(store)
        assert not any(reason.startswith("QUALITY_FRAME_") for reason in baseline)
    with patch("cryptowatcher.capacity.time.time", return_value=(day_end + 60001) / 1000):
        reasons = stable_reason(store)
        assert "QUALITY_FRAME_BACKLOG" in reasons
    with closing(Store(store.path, store.owner_id)) as reopened:
        reopened.clock_ms = lambda: day_end + 60001
        final = complete(reopened, day_end + 60001)
        assert final["quality_frames"] == initial["quality_frames"] | {
            "rows_examined": initial["quality_frames"].get("rows_examined", 0) + 1,
            "packets_moved": 1,
            "frames_created": 1,
        }
        assert public(reopened) == expected and not catalogue_repack.busy(final)
        assert bytes(reopened.db.execute("SELECT facts FROM market_quality").fetchone()[0]).startswith(
            quality_frames.MAGIC
        )
        with patch("cryptowatcher.capacity.time.time", return_value=(day_end + 60001) / 1000):
            assert not any(reason.startswith("QUALITY_FRAME_") for reason in stable_reason(reopened))


def test_retention_pressure_keeps_packing_pending_and_reopen_resumes_all_optional_phases(store):
    from cryptowatcher import catalogue_compaction, context_tile_maintenance, parent_affinity
    from cryptowatcher.market_storage import save_context
    from tests.test_context_tiles import values

    setup(store, 0)
    complete(store)
    with store.db:
        save(store, [fact(8)], day=NOW // DAY - store.snapshot().settings.detail_retention_days)
        for index in range(8):
            save_context(store.db, "ticker", "spot/BTCUSDT", values(index))
    expected, numbers, settings = public(store), numeric(store), store.snapshot()
    now = (NOW // DAY + 1) * DAY + 60001
    targets = (catalogue_compaction, quality_frames, context_tile_maintenance, parent_affinity)
    for reopened in (store, Store(store.path, store.owner_id)):
        try:
            with (
                patch.object(targets[0], "advance", wraps=targets[0].advance) as catalogue,
                patch.object(targets[1], "advance", wraps=targets[1].advance) as frames,
                patch.object(targets[2], "advance", wraps=targets[2].advance) as contexts,
                patch.object(targets[3], "advance", wraps=targets[3].advance) as affinity,
            ):
                state = work(reopened, now, seconds=1, defer_compaction=True)
            assert not any(call.call_count for call in (catalogue, frames, contexts, affinity))
            assert state["quality_frame_maintenance"]["pending_work"]
            assert state["context_tile_maintenance"]["pending_work"]
            assert catalogue_repack.busy(state)
            assert public(reopened) == expected and numeric(reopened) == numbers
        finally:
            if reopened is not store:
                reopened.close()
    with closing(Store(store.path, store.owner_id)) as reopened:
        with (
            patch.object(targets[0], "advance", wraps=targets[0].advance) as catalogue,
            patch.object(targets[1], "advance", wraps=targets[1].advance) as frames,
            patch.object(targets[2], "advance", wraps=targets[2].advance) as contexts,
            patch.object(targets[3], "advance", wraps=targets[3].advance) as affinity,
        ):
            final = complete(reopened, now)
        assert all(call.call_count for call in (catalogue, frames, contexts, affinity))
        assert not catalogue_repack.busy(final)
        assert public(reopened) == expected and numeric(reopened) == numbers
        assert reopened.snapshot() == settings
        assert not reopened.db.execute("PRAGMA foreign_key_check").fetchall()


def test_retention_deferral_does_not_skip_initial_packing_or_claim_its_boundary(store):
    setup(store)
    state = {}
    for _ in range(500):
        state = work(store, NOW, batch=7, defer_compaction=True)
        if state.get("initial_reclaim_complete"):
            break
    else:
        pytest.fail("Initial reclaim did not finish under retention pressure")
    # The optional phases may still be pending; ordinary work must finish them.
    final = complete(store)
    assert final["quality_frames"]["initial_complete"]
    assert final["context_tiles"]["initial_complete"]
    assert final["parent_affinity"]["initial_complete"]
    assert not catalogue_repack.busy(final)


def test_frame_transfer_forecast_counts_staging_once_and_marks_old_index_measurement_stale(store):
    setup(store)
    sample(store, NOW - DAY)
    old_samples = store.get_runtime("storage_samples")
    assert old_samples[0]["format_version"] == SAMPLE_FORMAT
    previous_tables = report(store, NOW)["indexes"]
    complete(store)
    with store.db:
        save(store, [fact(999)], day=NOW // DAY - store.snapshot().settings.detail_retention_days)
    # Fake an older measured list on Windows where dbstat may be unavailable.
    store.put_runtime(
        "storage_index_measurement",
        {**previous_tables, "state": "PASS", "tables": {"market_quality_blocks": {}}},
    )
    sample(store, NOW)
    value = report(store, NOW + DAY)
    rows = {row["category"]: row for row in value["forecast"]}
    assert rows["quality"]["model_applied"]
    assert rows["quality"]["flow_basis"] == "CURRENT_OCCUPANCY_PLUS_POSITIVE_NET_GROWTH"
    assert rows["quality_frames"]["allocation_excluded"]
    assert rows["quality_frame_pending"]["basis"] == "CURRENT_OCCUPANCY_PLUS_POSITIVE_NET_GROWTH"
    assert value["quality_frame_layout"]["installed"]
    assert value["quality_frame_layout"]["writer"] == "IF1_OR_QC1_MIN"
    assert value["indexes"]["state"] == "STALE"
    assert old_samples[0] in store.get_runtime("storage_samples")


@pytest.mark.asyncio
async def test_frame_pending_keeps_existing_worker_active_without_new_job(store):
    pending = {"state": "COMPLETE", "quality_frame_maintenance": {"pending_work": True}}
    idle = {"state": "COMPLETE", "quality_frame_maintenance": {"pending_work": False}}
    with patch("cryptowatcher.storage_repack.work", side_effect=[pending, pending, idle]) as tick:
        assert await async_work(store, NOW, slices=8) == idle
    assert tick.call_count == 3


@pytest.mark.parametrize("readonly", [True, False])
def test_store_open_rejects_unknown_frame_marker_without_migration(store, readonly):
    import hashlib

    setup(store)
    complete(store)
    with store.db:
        store.db.execute("UPDATE market_meta SET value='2' WHERE key='quality_frames'")
    store.close()
    original = hashlib.sha256(store.path.read_bytes()).digest()
    with pytest.raises(StateError, match="Unknown quality frame layout"):
        Store(store.path, store.owner_id, readonly=readonly)
    assert hashlib.sha256(store.path.read_bytes()).digest() == original
