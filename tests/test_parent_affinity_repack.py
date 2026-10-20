import copy
import sqlite3
from unittest.mock import patch

import pytest

from cryptowatcher.capacity import stable_reason
from cryptowatcher.storage import StateError
from cryptowatcher.storage_meter import SAMPLE_FORMAT, report, sample
from cryptowatcher.storage_repack import VERSION, work
from tests.test_catalogue_repack import complete
from tests.test_outcomes import NOW
from tests.test_record_relocation import fragmented, public, references


def test_upgrade19_adds_only_finite_hint_retag_pass_and_preserves_all_old_data_and_boundaries(store):
    fragmented(store)
    initial = complete(store)
    expected, refs, settings = public(store), references(store), store.snapshot()
    previous = copy.deepcopy(initial)
    for key in ("parent_affinity", "parent_affinity_initial_reclaim_complete"):
        previous.pop(key)
    previous.update(version=19, next_phase=17, operator_note="preserve19")
    # Remove only this extension from the disposable fixture, reproducing the
    # predecessor's physical schema without altering canonical record storage.
    with store.db:
        store.db.execute("DROP TABLE market_parent_affinity")
        store.db.execute("DELETE FROM market_meta WHERE key='parent_affinity'")
        store.db.execute("DELETE FROM market_counts WHERE category='parent_affinity'")
        store.put_runtime("storage_repack", previous)
    first = work(store, NOW, seconds=0)
    assert first["version"] == VERSION and first["previous_version"] == 19
    assert first["initial_reclaim_complete"] and first["state"] == "IN_PROGRESS"
    final = complete(store)
    assert final["parent_affinity"]["initial_complete"] and final["parent_affinity_initial_reclaim_complete"]
    assert final["operator_note"] == "preserve19"
    for key in (
        "source_transfer_upper",
        "numeric_transfer_upper",
        "catalogue_transfer",
        "catalogue_queue_upper",
        "quality_frames",
        "numeric_refs",
        "context_tiles",
    ):
        assert final.get(key) == previous.get(key)
    assert final["packet_bundles"]["upper"] == previous["packet_bundles"]["upper"]
    assert public(store) == expected and references(store) == refs and store.snapshot() == settings
    assert work(store, NOW + 1) == final


@pytest.mark.parametrize("fault", ["marker", "boundary", "forged", "phase"])
def test_current_hint_layout_or_checkpoint_fault_is_not_accepted_or_reinstalled(store, fault):
    fragmented(store)
    state = complete(store)
    with store.db:
        if fault == "marker":
            store.db.execute("UPDATE market_meta SET value='999' WHERE key='parent_affinity'")
        else:
            state = copy.deepcopy(state)
            if fault == "boundary":
                state["parent_affinity"].pop("packet_cursor")
            elif fault == "forged":
                state["parent_affinity"] = {"version": 1}
            else:
                state["next_phase"] = 19
            store.put_runtime("storage_repack", state)
    before = list(store.db.iterdump())
    with pytest.raises((StateError, ValueError)):
        work(store, NOW)
    assert list(store.db.iterdump()) == before
    if fault != "phase":
        with patch("cryptowatcher.capacity.time.time", return_value=NOW / 1000):
            assert any(reason.startswith("PARENT_AFFINITY_") for reason in stable_reason(store))


def test_observer_requires_new_reclaim_and_meter_marks_the_previous_physical_measurement_stale(store):
    fragmented(store)
    initial = complete(store)
    old = {"format_version": SAMPLE_FORMAT - 1, "at_ms": NOW - 1000, "event_total": 20, "working_bytes": 1}
    store.put_runtime("storage_samples", [old])
    store.put_runtime("storage_index_measurement", {"state": "PASS", "tables": {"market_quality_blocks": {}}})
    value = report(store, NOW)
    assert value["parent_affinity_layout"]["installed"] and value["indexes"]["state"] == "STALE"
    assert (
        value["parent_affinity_layout"]["records"]
        == store.db.execute("SELECT COUNT(*) FROM market_parent_affinity").fetchone()[0]
    )
    sample(store, NOW)
    assert old in store.get_runtime("storage_samples")
    store.put_runtime("storage_repack", {**initial, "parent_affinity_initial_reclaim_complete": False})
    with patch("cryptowatcher.capacity.time.time", return_value=NOW / 1000):
        assert "PARENT_AFFINITY_INITIAL_RECLAIM_PENDING" in stable_reason(store)
    assert complete(store)["parent_affinity_initial_reclaim_complete"]


def test_missing_hint_counter_blocks_fresh_record_publication_atomically(store):
    from tests.test_parent_affinity import parent, prepare

    prepare(store)
    with store.db:
        parent(store, 0)
        store.db.execute("DELETE FROM market_counts WHERE category='parent_affinity'")
    before = list(store.db.iterdump())
    with pytest.raises(sqlite3.IntegrityError, match="parent affinity"), store.db:
        parent(store, 1)
    assert list(store.db.iterdump()) == before
