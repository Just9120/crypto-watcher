from contextlib import closing
from unittest.mock import patch

import pytest

from cryptowatcher import catalogue_queue, catalogue_repack, record_catalog
from cryptowatcher.capacity import stable_reason
from cryptowatcher.events import History
from cryptowatcher.sources import Sources
from cryptowatcher.storage import StateError, Store, encode
from cryptowatcher.storage_repack import VERSION, async_work, work
from tests.test_bounded_storage import bounded
from tests.test_outcomes import MINUTE, NOW, OutcomeWorker, future, observe


def complete(store, now=NOW):
    for _ in range(500):
        state = work(store, now, batch=7, seconds=0.04)
        if state["state"] == "COMPLETE" and not catalogue_repack.busy(state):
            return state
    pytest.fail("Finite initial catalogue work did not settle")


def test_repack13_initial_and_live_queue_preserve_graph_and_reopen_without_rescanning(store):
    bounded(store)
    observe(store, OutcomeWorker(store))
    Sources(store).put("spot/BTCUSDT/1m", future())
    OutcomeWorker(store).work(NOW + 6 * MINUTE)
    expected = [(key, encode(value)) for key, value in History(store).scan("")]
    initial = complete(store)
    assert initial["version"] == VERSION and initial["catalogue_transfer"]["bodies_moved"] > 0
    assert initial["catalogue_complete"] and catalogue_queue.verify(store.db)["count"] == 0
    assert [(key, encode(value)) for key, value in History(store).scan("")] == expected
    assert work(store, NOW + 1) == initial
    with closing(Store(store.path, store.owner_id)) as reopened:
        reopened.clock_ms = lambda: NOW + 10 * MINUTE
        observe(reopened, OutcomeWorker(reopened), NOW + 10 * MINUTE)
        expected = [(key, encode(value)) for key, value in History(reopened).scan("")]
        assert catalogue_queue.header(reopened.db)["count"] > 0
        settled = complete(reopened, NOW + 10 * MINUTE)
        assert settled["catalogue_transfer"] == initial["catalogue_transfer"]
        assert settled["catalogue_queue"]["bodies_moved"] > 0
        assert settled["catalogue_queue_upper"] == initial["catalogue_queue_upper"]
        assert [(key, encode(value)) for key, value in History(reopened).scan("")] == expected
        assert reopened.db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_upgrade13_revisits_only_quality_and_preserves_initial_catalogue_geometry(store):
    import copy

    from cryptowatcher import quality_packets
    from tests.test_quality_packets import fact, save_v4

    bounded(store)
    initial = complete(store)
    previous = copy.deepcopy(initial)
    previous.update(version=13, operator_note="preserve extension")
    with store.db:
        old = save_v4(store, [fact(7)])
        store.put_runtime("storage_repack", previous)
    original_numeric = quality_packets.ownership(store.db, old)["numeric"]
    first = work(store, NOW, batch=1, seconds=0.001)
    assert first["version"] == VERSION and first["previous_version"] == 13
    assert first["catalogue_transfer"] == initial["catalogue_transfer"]
    final = complete(store)
    assert final["operator_note"] == previous["operator_note"]
    assert final["catalogue_transfer"] == initial["catalogue_transfer"]
    assert final["catalogue_queue_upper"] == initial["catalogue_queue_upper"]
    assert final["quality_rows_scanned"] == 1
    upgraded = bytes(store.db.execute("SELECT facts FROM market_quality").fetchone()[0])
    assert upgraded.startswith(quality_packets.FRAME_MAGIC)
    assert encode(quality_packets.unpack(store.db, upgraded)) == encode([fact(7)])
    assert quality_packets.ownership(store.db, upgraded)["numeric"] == original_numeric
    assert work(store, NOW) == final


def test_upgrade15_resumes_bundle_install_without_repeating_finite_source_catalogue_or_quality(store):
    import copy

    bounded(store)
    observe(store, OutcomeWorker(store))
    initial = complete(store)
    previous = copy.deepcopy(initial)
    previous.update(version=15, next_phase=0, operator_note="preserve15")
    previous.pop("packet_bundles")
    previous.pop("numeric_refs")
    store.put_runtime("storage_repack", previous)
    final = complete(store)
    assert final["version"] == VERSION and final["previous_version"] == 15
    assert final["operator_note"] == "preserve15"
    assert final["packet_bundles"]["initial_complete"]
    for key in (
        "catalogue_transfer",
        "catalogue_queue_upper",
        "quality_rows_scanned",
        "source_transfer_upper",
        "numeric_transfer_upper",
    ):
        assert final.get(key) == previous.get(key)


def test_upgrade16_adds_finite_numeric_lease_pass_without_reopening_old_geometry(store):
    import copy

    bounded(store)
    observe(store, OutcomeWorker(store))
    initial = complete(store)
    previous = copy.deepcopy(initial)
    previous.update(version=16, next_phase=0, operator_note="preserve16")
    previous.pop("numeric_refs")
    store.put_runtime("storage_repack", previous)
    final = complete(store)
    assert final["version"] == VERSION and final["previous_version"] == 16
    assert final["operator_note"] == "preserve16"
    assert final["numeric_refs"]["complete"] and final["numeric_refs"]["format"] == "NR2"
    for key in (
        "catalogue_transfer",
        "catalogue_queue_upper",
        "packet_bundles",
        "quality_rows_scanned",
        "numeric_search_upper",
    ):
        assert final.get(key) == previous.get(key)


def test_upgrade20_adds_finite_numeric_frame_pass_without_reopening_qualified_boundaries(store):
    import copy

    bounded(store)
    observe(store, OutcomeWorker(store))
    initial = complete(store)
    expected = [(key, encode(value)) for key, value in History(store).scan("")]
    previous = copy.deepcopy(initial)
    previous.update(version=20, next_phase=0, operator_note="preserve20")
    for key in ("numeric_frames", "numeric_frame_initial_reclaim_complete", "numeric_frame_maintenance"):
        previous.pop(key, None)
    store.put_runtime("storage_repack", previous)
    final = complete(store)
    assert final["version"] == VERSION and final["previous_version"] == 20
    assert final["numeric_frames"]["initial_complete"] and final["numeric_frame_initial_reclaim_complete"]
    assert final["operator_note"] == "preserve20"
    for key in (
        "catalogue_transfer",
        "catalogue_queue_upper",
        "source_transfer_upper",
        "numeric_transfer_upper",
        "quality_rows_scanned",
        "numeric_search_upper",
        "numeric_refs",
        "quality_frames",
        "context_tiles",
        "parent_affinity",
        "initial_reclaim_complete",
    ):
        assert final.get(key) == previous.get(key)
    assert [(key, encode(value)) for key, value in History(store).scan("")] == expected
    assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("marker", ["numeric_frames", "numeric_frame_queue"])
def test_current_repack_cannot_reinstall_missing_numeric_frame_marker(store, marker):
    bounded(store)
    complete(store)
    with store.db:
        store.db.execute("DELETE FROM market_meta WHERE key=?", (marker,))
    before = list(store.db.iterdump())
    with pytest.raises(ValueError):
        work(store, NOW)
    assert list(store.db.iterdump()) == before
    with patch("cryptowatcher.capacity.time.time", return_value=NOW / 1000):
        assert "NUMERIC_FRAME_CHECKPOINT_UNKNOWN" in stable_reason(store)


@pytest.mark.parametrize("fault", ["writer", "boundary"])
def test_previous20_preserves_quality_frame_guards_before_installing_new_extensions(store, fault):
    bounded(store)
    previous = complete(store)
    previous = {**previous, "version": 20, "next_phase": 0}
    if fault == "writer":
        with store.db:
            store.db.execute("DELETE FROM market_meta WHERE key='quality_frames'")
    else:
        previous = {**previous, "quality_frames": {}}
    store.put_runtime("storage_repack", previous)
    before = list(store.db.iterdump())
    with pytest.raises(ValueError, match="quality frame"):
        work(store, NOW)
    assert list(store.db.iterdump()) == before


def test_current_repack_cannot_reinstall_missing_lease_marker_or_unknown_boundary(store):
    bounded(store)
    initial = complete(store)
    with store.db:
        store.db.execute("DELETE FROM market_meta WHERE key='numeric_refs_compression'")
    before = list(store.db.iterdump())
    with pytest.raises(ValueError, match="Missing numeric lease writer checkpoint"):
        work(store, NOW)
    assert list(store.db.iterdump()) == before
    with patch("cryptowatcher.capacity.time.time", return_value=NOW / 1000):
        assert "NUMERIC_LEASE_INITIAL_INCOMPLETE" in stable_reason(store)
    store.put_runtime("storage_repack", {**initial, "numeric_refs": {"cursor": True, "upper": 9}})
    before = list(store.db.iterdump())
    with pytest.raises(ValueError, match="numeric lease boundary"):
        work(store, NOW)
    assert list(store.db.iterdump()) == before


def test_completed_repack_live_failure_restores_pending_and_durable_checkpoint(store, monkeypatch):
    bounded(store)
    observe(store, OutcomeWorker(store))
    complete(store)
    store.clock_ms = lambda: NOW + 10 * MINUTE
    observe(store, OutcomeWorker(store), NOW + 10 * MINUTE)
    before = list(store.db.iterdump())
    append = record_catalog.append

    def fail(*args, **kwargs):
        append(*args, **kwargs)
        raise RuntimeError("injected maintenance failure")

    monkeypatch.setattr(record_catalog, "append", fail)
    with pytest.raises(RuntimeError, match="maintenance failure"):
        work(store, NOW + 10 * MINUTE, seconds=1)
    assert list(store.db.iterdump()) == before
    monkeypatch.setattr(record_catalog, "append", append)
    complete(store, NOW + 10 * MINUTE)
    assert catalogue_queue.verify(store.db)["count"] == 0


@pytest.mark.parametrize(
    "checkpoint",
    [
        {"catalogue_transfer": {"version": 2}},
        {"catalogue_queue": {"version": True}},
        {"catalogue_queue_upper": -1},
        {"catalogue_complete": True},
    ],
)
def test_unknown_nested_repack_checkpoint_rejects_before_any_ddl(store, checkpoint):
    bounded(store)
    store.put_runtime("storage_repack", checkpoint)
    before = list(store.db.iterdump())
    with pytest.raises(StateError):
        work(store, NOW)
    assert list(store.db.iterdump()) == before


def test_baseline_accepts_current_pending_compaction_but_rejects_overdue_unknown_and_missing(store):
    bounded(store)
    complete(store)
    with patch("cryptowatcher.capacity.time.time", return_value=NOW / 1000):
        baseline = stable_reason(store)
        assert not any(value.startswith("CATALOGUE") for value in baseline)
        store.clock_ms = lambda: NOW - MINUTE - 1
        observe(store, OutcomeWorker(store))
        reasons = stable_reason(store)
        assert "CATALOGUE_BACKLOG" in reasons
        assert [value for value in reasons if value != "CATALOGUE_BACKLOG"] == baseline
        # Other reasons retain their original ordering; only the backlog differs.
        with store.db:
            store.db.execute("DELETE FROM market_counts WHERE category='record_pending'")
        reasons = stable_reason(store)
        assert "CATALOGUE_CHECKPOINT_UNKNOWN" in reasons and "CATALOGUE_BACKLOG" not in reasons


@pytest.mark.parametrize(
    "age,reason", [(1, None), (60001, "CATALOGUE_BUNDLE_BACKLOG"), (-30001, "CATALOGUE_BUNDLE_CLOCK_UNKNOWN")]
)
def test_baseline_distinguishes_young_bundle_work_from_overdue_and_future_clock(store, age, reason):
    bounded(store)
    observe(store, OutcomeWorker(store))
    complete(store)
    with patch("cryptowatcher.capacity.time.time", return_value=NOW / 1000):
        baseline = stable_reason(store)
        packet = store.db.execute("SELECT id FROM market_packets WHERE refs>0 LIMIT 1").fetchone()[0]
        with store.db:
            store.db.execute(
                "UPDATE market_packets SET bundle_checked=0,bundle_since=? WHERE id=?", (NOW - age, packet)
            )
        actual = stable_reason(store)
        assert [value for value in actual if value != reason] == baseline
        assert reason is None or reason in actual


@pytest.mark.asyncio
async def test_async_repack_continues_live_work_after_finite_initial_complete(store):
    pending = {"state": "COMPLETE", "catalogue_maintenance": {"pending_work": True}}
    idle = {"state": "COMPLETE", "catalogue_maintenance": {"pending_work": False}}
    with patch("cryptowatcher.storage_repack.work", side_effect=[pending, pending, idle]) as tick:
        result = await async_work(store, NOW, slices=8)
    assert tick.call_count == 3 and result == idle


def test_deep_capacity_counts_all_cold_records_aliases_pending_and_comparator_samples(store):
    from collections import Counter

    from cryptowatcher.analytics import ComparisonWorker
    from cryptowatcher.capacity import boundary_blockers, comparator_sample, namespace, runtime_ledger

    bounded(store)
    observe(store, OutcomeWorker(store))
    Sources(store).put("spot/BTCUSDT/1m", future())
    OutcomeWorker(store).work(NOW + 61 * MINUTE)
    ComparisonWorker(store).work(NOW + 61 * MINUTE)
    before = comparator_sample(store)
    assert before["n"] > 0
    complete(store)
    actual = Counter(namespace(key) for key, _ in store.scan_runtime(""))
    ledger = runtime_ledger(store, seconds=5)
    assert ledger["state"] == ledger["catalogue"]["state"] == "MEASURED"
    assert {
        key: value for key, value in ledger["catalogue"]["public_namespace_records"].items() if value
    } == actual
    assert ledger["catalogue"]["public_records_accounted"] == sum(actual.values())
    assert ledger["revision_multiplicity"]["event"]["revisions"] == actual["event"] > 0
    assert comparator_sample(store) == before
    assert "BOUNDARY_CATALOGUE_LEDGER_INCOMPLETE" in boundary_blockers(
        {
            "storage": {"catalogue_layout": {"state": "MIXED_DOMAIN_CATALOGUE"}},
            "runtime_ledger": {"state": "MEASURED"},
        }
    )
    with store.db:
        store.db.execute("UPDATE market_catalogue_counts SET n=n+1 WHERE kind=0")
    with pytest.raises(StateError, match="actual catalogue domain count"):
        runtime_ledger(store, seconds=5)


def test_forecast_does_not_annualize_hot_staging_after_the_same_records_are_transferred(store):
    from cryptowatcher.storage_meter import report, sample
    from tests.test_outcomes import DAY

    bounded(store)
    complete(store)
    store.put_runtime("radar:v1:event-counter", 1000)
    sample(store, NOW)
    observe(store, OutcomeWorker(store))
    complete(store)
    value = report(store, NOW + DAY)
    detail = next(row for row in value["forecast"] if row["category"] == "details")
    assert detail["gross_bytes_per_day"] > 0
    # Only the existing four-byte event counter remains hot; packet admissions
    # must not also become a thirty-day hot-payload flow estimate.
    assert detail["projected_logical_bytes"] == value["categories"]["details"]["logical_bytes"] == 4
    assert value["categories"]["record_catalog"]["records"] > 0


def test_queue_boundary_cannot_be_declared_complete_while_its_original_work_remains(store):
    bounded(store)
    observe(store, OutcomeWorker(store))
    state = complete(store)
    observe(store, OutcomeWorker(store), NOW + 10 * MINUTE)
    state["catalogue_queue_upper"] = catalogue_queue.ceiling(store.db)
    store.put_runtime("storage_repack", state)
    before = list(store.db.iterdump())
    with pytest.raises(StateError, match="Unfinished initial"):
        work(store, NOW + 10 * MINUTE)
    assert list(store.db.iterdump()) == before


def test_completed_repack_still_reclaims_free_pages_without_reopening_initial_scan(store):
    bounded(store)
    initial = complete(store)
    assert initial["initial_reclaim_complete"]
    store.db.execute("CREATE TABLE disposable_reclaim_fixture(value BLOB)")
    with store.db:
        store.db.executemany("INSERT INTO disposable_reclaim_fixture VALUES(?)", [(b"x" * 4000,)] * 700)
        store.db.execute("DELETE FROM disposable_reclaim_fixture")
    before = store.db.execute("PRAGMA freelist_count").fetchone()[0]
    assert before > 256
    next_state = work(store, NOW + 1)
    assert next_state["state"] == "COMPLETE" and catalogue_repack.busy(next_state)
    assert next_state["catalogue_maintenance"]["free_pages"] < before
    settled = complete(store, NOW + 2)
    assert settled["catalogue_transfer"] == initial["catalogue_transfer"]
    assert settled["catalogue_maintenance"]["free_pages"] <= 256


def test_idle_return_refreshes_a_pre_checkpoint_reclaim_gauge_without_writing_another_checkpoint(store):
    from cryptowatcher.catalogue_repack import busy, status

    bounded(store)
    settled = complete(store)
    assert not status(store, NOW)["pending_work"]
    # A real 38-day causal fixture reached free_pages278 before saving its
    # checkpoint; the save consumed22 pages. The persisted gauge must not keep
    # the ordinary worker busy forever once actual reclaim is complete.
    stale = {
        **settled["catalogue_maintenance"],
        "free_pages": 278,
        "vacuum_pending": True,
        "pending_work": True,
    }
    store.put_runtime("storage_repack", {**settled, "catalogue_maintenance": stale})
    actual = status(store, NOW)
    assert not actual["pending_work"]
    before = store.db.total_changes
    result = work(store, NOW)
    assert not busy(result)
    assert result["catalogue_maintenance"]["free_pages"] == actual["free_pages"]
    assert result["catalogue_transfer"] == settled["catalogue_transfer"]
    assert store.db.total_changes == before


def test_completed_initial_migration_survives_partial_meter_rebuild_after_vacuum_and_new_admissions(store):
    bounded(store)
    observe(store, OutcomeWorker(store))
    initial = complete(store)
    with store.db:
        for number in range(80):
            History(store).put(f"rebuild-fixture:{number}", {"number": number})
    store.db.execute("VACUUM")
    store.clock_ms = lambda: NOW + 10 * MINUTE
    observe(store, OutcomeWorker(store), NOW + 10 * MINUTE)
    expected = [(key, encode(value)) for key, value in History(store).scan("")]
    partial = work(store, NOW + 10 * MINUTE, batch=7, seconds=1)
    assert partial["catalogue_complete"] and partial["state"] == "IN_PROGRESS"
    assert partial["namespace_meter_complete"] is False
    settled = complete(store, NOW + 10 * MINUTE)
    assert settled["catalogue_transfer"] == initial["catalogue_transfer"]
    assert [(key, encode(value)) for key, value in History(store).scan("")] == expected
