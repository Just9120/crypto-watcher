"""Bounded expiry grouping preserves oldest-first progress and whole transactions."""

import copy
import json
from collections import Counter
from contextlib import closing
from unittest.mock import patch

import pytest

from cryptowatcher import (
    catalogue_repack,
    catalogue_retention,
    parent_affinity,
    quality_packets,
    quality_summary,
)
from cryptowatcher.events import PREFIX, History, identity
from cryptowatcher.outcomes import DEFINITION
from cryptowatcher.storage import Store, encode, json_payload
from cryptowatcher.storage_lifecycle import BoundedRetention
from cryptowatcher.storage_repack import work
from tests.test_bounded_storage import bounded
from tests.test_catalogue_repack import complete
from tests.test_outcomes import DAY, MINUTE, NOW, OutcomeWorker, observe


def prepare(store, count=12, *, collision=False, future=False, wide=False):
    bounded(store)
    worker = OutcomeWorker(store)
    observe(store, worker)
    history = History(store)
    template = min(history.events(100), key=lambda event: (event["market_observation_ms"], event["event_id"]))
    created = []
    with store.db:
        for index in range(count):
            event = copy.deepcopy(template)
            event["event_id"] = identity(f"archive-admission/{index}")
            observed = NOW + (30 * MINUTE if future and index == count - 1 else index * 1000 + 1)
            event.update(market_observation_ms=observed, available_at_ms=observed, reference_time_ms=observed)
            if collision and index % 3 == 1:
                event["direction"] = "DOWN" if template["direction"] != "DOWN" else "UP"
            history.version("event", event)
            history.put(f"history:{observed:016d}:{event['event_id']}", {"event_id": event["event_id"]})
            history.put(
                f"episode-event:{event['episode_id']}:{observed:016d}:{event['event_id']}",
                {"event_id": event["event_id"]},
            )
            worker.admit(event)
            if wide:
                history.version(
                    "outcome",
                    {
                        "schema": "cryptowatcher.outcome.v1",
                        "outcome_id": identity(f"outcome/{event['event_id']}/5/1"),
                        "event_id": event["event_id"],
                        "event_revision": 1,
                        "revision": 1,
                        "available_at_ms": observed + 5 * MINUTE,
                        "horizon_min": 5,
                        "methodology_version": "1",
                        "state": "COMPLETE",
                        "classification": "CONTINUATION",
                        "metrics": {
                            "price_return_pct": index + 0.25,
                            "legacy_extension": str(index) + "z" * 350000,
                        },
                    },
                )
            created.append(event)
    if count <= 12:
        complete(store)
    else:
        # This fixture needs many parents, not tiny startup slices. Keep the
        # existing500-call ceiling and use the normal64-row preparation batch.
        for _ in range(500):
            state = work(store, NOW, batch=64)
            if state["state"] == "COMPLETE" and not catalogue_repack.busy(state):
                break
        else:
            pytest.fail("Large admission fixture did not settle")
    return template, created


def expected_facts(store):
    history = History(store)
    return Counter(
        encode([group, fact])
        for _, event in history.items("event-head:")
        for _, group, _, fact in quality_summary.prepare_event_facts(store, event)
    )


def archived_facts(store):
    return Counter(
        encode([json.loads(dimensions), fact])
        for dimensions, raw in store.db.execute("SELECT dimensions,facts FROM market_quality")
        for fact in quality_packets.unpack(store.db, bytes(raw))
    )


@pytest.mark.parametrize("batch", [1, 2, 8, 200])
def test_grouping_preserves_oldest_seed_batch_limit_all_facts_and_reopen(store, batch):
    prepare(store)
    expected, settings = expected_facts(store), store.snapshot()
    now = NOW + 30 * DAY + 20 * MINUTE
    initial = History(store).count("event-head:")
    seed = catalogue_retention.due_events(store, now, now - 30 * DAY, batch).rows[0][1]["event_id"]
    assert History(store).metadata("event-head:" + seed)["market_observation_ms"] == NOW
    removed = []
    publish = quality_summary.archive_facts
    remove = catalogue_retention.remove_event
    groups = []

    def record_group(current, facts):
        groups.append(len(facts))
        return publish(current, facts)

    def record_remove(current, event_id, terminal):
        removed.append(event_id)
        return remove(current, event_id, terminal)

    with (
        patch("time.monotonic", return_value=0),
        patch.object(quality_summary, "archive_facts", side_effect=record_group),
        patch.object(catalogue_retention, "remove_event", side_effect=record_remove),
    ):
        BoundedRetention(store).work(now, batch=batch)
    assert removed[0] == seed
    assert len(removed) == min(initial, batch)
    assert max(groups) <= 8 * 4  # Four original frozen horizons per parent.
    if batch >= 8:
        assert 8 * 4 in groups
    value = store.get_runtime("market_retention")
    assert value["expired_events"] == len(removed) and value["remaining_due_batch"] >= 0
    assert store.snapshot() == settings
    with closing(Store(store.path, store.owner_id)) as reopened:
        for _ in range(30):
            if not History(reopened).count("event-head:"):
                break
            BoundedRetention(reopened).work(now, batch=8)
        else:
            pytest.fail("Bounded grouped expiry did not drain")
        assert archived_facts(reopened) == expected
        BoundedRetention(reopened).work(now, batch=8)
        assert archived_facts(reopened) == expected  # Replay does not add facts.
        assert reopened.snapshot() == settings
        assert reopened.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not reopened.db.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("failure", ["archive", "first_remove"])
def test_group_publication_and_all_parent_deletions_rollback_together(store, failure):
    prepare(store)
    before = "\n".join(store.db.iterdump())
    publish, remove = quality_summary.archive_facts, catalogue_retention.remove_event

    def fail_archive(current, facts):
        publish(current, facts)
        raise RuntimeError("Injected post-publication failure")

    def fail_remove(current, event_id, terminal):
        remove(current, event_id, terminal)
        raise RuntimeError("Injected first parent removal failure")

    with (
        patch("time.monotonic", return_value=0),
        patch.object(
            quality_summary, "archive_facts", side_effect=fail_archive if failure == "archive" else publish
        ),
        patch.object(
            catalogue_retention,
            "remove_event",
            side_effect=fail_remove if failure == "first_remove" else remove,
        ),
        pytest.raises(RuntimeError, match="Injected"),
    ):
        BoundedRetention(store).work(NOW + 31 * DAY)
    assert "\n".join(store.db.iterdump()) == before
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()


def test_hint_collision_and_future_parent_cannot_bypass_actual_native_or_expiry_checks(store):
    # Model a truncated-hash collision while admitting hints; full tuples must
    # still separate the parents in the same writer transaction.
    with patch.object(parent_affinity, "fingerprint", return_value=b"collisio"):
        template, created = prepare(store, collision=True, future=True)
        now = NOW + 30 * DAY + 20 * MINUTE
        seed = History(store).metadata("event-head:" + template["event_id"])
        changes = store.db.total_changes
        candidates = list(
            catalogue_retention.archive_candidates(store, seed, now, now - 30 * DAY, float("inf"))
        )
        assert store.db.total_changes == changes
        assert candidates
        assert all(parent_affinity.native(event) == parent_affinity.native(seed) for _, event in candidates)
        assert created[-1]["event_id"] not in {event["event_id"] for _, event in candidates}
        with patch("time.monotonic", return_value=0):
            BoundedRetention(store).work(now, batch=8)
        assert History(store).metadata("event-head:" + created[-1]["event_id"]) is not None


def test_extras_outside_initial_due_window_do_not_make_remaining_count_negative(store):
    template, _ = prepare(store)
    now = NOW + 31 * DAY
    key = PREFIX + f"history:{template['market_observation_ms']:016d}:" + template["event_id"]
    selected = catalogue_retention.EventBatch([(key, {"event_id": template["event_id"]})], "")
    with (
        patch.object(catalogue_retention, "due_events", return_value=selected),
        patch("time.monotonic", return_value=0),
    ):
        BoundedRetention(store).work(now, batch=2)
    value = store.get_runtime("market_retention")
    assert value["expired_events"] == 2 and value["remaining_due_batch"] == 0


def test_prepared_byte_limit_splits_real_wide_legacy_metrics_without_losing_public_facts(store):
    prepare(store, count=5, wide=True)
    expected = expected_facts(store)
    publish = quality_summary.archive_facts
    charges = []

    def record_group(current, facts):
        charges.append(len(encode(facts).encode()))
        return publish(current, facts)

    with (
        patch("time.monotonic", return_value=0),
        patch.object(quality_summary, "archive_facts", side_effect=record_group),
    ):
        BoundedRetention(store).work(NOW + 31 * DAY, batch=200)
    assert len(charges) >= 3 and max(charges) <= 1024 * 1024
    assert archived_facts(store) == expected
    assert not History(store).count("event-head:")
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()


def test_cohort_hint_window_is_finite_and_an_elapsed_deadline_does_not_decode_parents(store):
    template, _ = prepare(store, count=70)
    seed = History(store).metadata("event-head:" + template["event_id"])
    now = NOW + 31 * DAY
    candidates = list(catalogue_retention.archive_candidates(store, seed, now, now - 30 * DAY, float("inf")))
    assert 63 <= len(candidates) <= 64  # More than70 qualify; at most64 hints are read.
    with (
        patch("time.monotonic", return_value=0),
        patch.object(History, "metadata", side_effect=AssertionError("Expired budget decoded a parent")),
    ):
        assert list(catalogue_retention.archive_candidates(store, seed, now, now - 30 * DAY, -1)) == []


def test_same_native_batch_preserves_distinct_frozen_outcome_methods_and_definitions(store):
    _, created = prepare(store, count=4)
    history = History(store)
    definition = {**copy.deepcopy(DEFINITION), "version": "2"}
    with store.db:
        history.put("outcome-method:2", definition)
        for index, event in enumerate(created):
            history.version(
                "outcome",
                {
                    "schema": "cryptowatcher.outcome.v1",
                    "outcome_id": identity(f"outcome/{event['event_id']}/5/2"),
                    "event_id": event["event_id"],
                    "event_revision": 1,
                    "revision": 1,
                    "available_at_ms": event["available_at_ms"] + 5 * MINUTE,
                    "horizon_min": 5,
                    "methodology_version": "2",
                    "state": "COMPLETE",
                    "classification": "CONTINUATION",
                    "metrics": {"price_return_pct": index + 0.12345},
                },
            )
    expected = expected_facts(store)
    with patch("time.monotonic", return_value=0):
        BoundedRetention(store).work(NOW + 31 * DAY)
    assert archived_facts(store) == expected
    rows = store.db.execute("SELECT dimensions,provenance FROM market_quality").fetchall()
    assert {json.loads(group)["methodology_version"] for group, _ in rows} == {"1", "2"}
    assert definition in [
        history.codec.unpack(json_payload(provenance))["quality_provenance"]["outcome_definition"]
        for group, provenance in rows
        if json.loads(group)["methodology_version"] == "2"
    ]
