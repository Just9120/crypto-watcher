import sqlite3
import time
from contextlib import closing
from types import SimpleNamespace

import pytest

from cryptowatcher import catalogue_queue as queue
from cryptowatcher import record_catalog
from cryptowatcher.events import PREFIX, History
from cryptowatcher.storage import RUNTIME_UPSERT, StateError, Store, encode
from tests.test_catalogue_transfer import populated
from tests.test_outcomes import MINUTE, NOW, OutcomeWorker, observe


def queued(store):
    populated(store)
    queue.install(store.db)
    with store.db:
        queue.activate(store.db)
    store.clock_ms = lambda: NOW + 10 * MINUTE
    observe(store, OutcomeWorker(store), NOW + 10 * MINUTE)


def test_pending_queue_tracks_live_work_once_preserves_initial_boundary_and_cascades_after_transfer(store):
    queued(store)
    history = History(store)
    expected = [(key, encode(value)) for key, value in history.scan("")]
    before = queue.header(store.db)
    assert before["count"] > 0 and before["oldest_ms"] == NOW + 10 * MINUTE
    high = queue.ceiling(store.db)
    # A duplicate rewrite retains the original queue sequence/age.
    key, value = history.items("event-head:", 1)[0]
    existing = list(store.db.execute("SELECT id,rid,at_ms FROM market_record_pending ORDER BY id"))
    with store.db:
        history.put(key, store.get_runtime("radar:v1:" + key))
    assert (
        list(store.db.execute("SELECT id,rid,at_ms FROM market_record_pending ORDER BY id"))[: len(existing)]
        == existing
    )
    state = {}
    for _ in range(200):
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            queue.advance(store, state, 7, time.monotonic() + 0.03)
        if queue.header(store.db)["count"] == 0:
            break
    assert queue.header(store.db)["count"] == 0
    assert queue.through(store.db, high)
    assert state["bodies_moved"] > 0
    assert [(key, encode(value)) for key, value in history.scan("")] == expected
    with closing(Store(store.path, store.owner_id)) as reopened:
        assert queue.header(reopened.db)["count"] == 0
        observe(reopened, OutcomeWorker(reopened), NOW + 20 * MINUTE)
        assert queue.header(reopened.db)["count"] > 0
        assert queue.through(reopened.db, high)
        assert not reopened.db.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("next_lane", ["body", "head"])
def test_queue_failure_rolls_back_pending_counters_parents_and_partial_publication(
    store, monkeypatch, next_lane
):
    queued(store)
    state = {"next_lane": next_lane}
    before = list(store.db.iterdump())
    actual = record_catalog.append

    def fail(*args, **kwargs):
        actual(*args, **kwargs)
        raise RuntimeError("injected queued publication failure")

    monkeypatch.setattr(record_catalog, "append", fail)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        with pytest.raises(RuntimeError, match="queued publication"):
            queue.advance(store, state, 256, time.monotonic() + 10)
        assert state == {"next_lane": next_lane}
        assert list(store.db.iterdump()) == before


def test_queue_missing_counter_is_never_empty_or_success_and_blocks_cascade(store):
    queued(store)
    with store.db:
        store.db.execute("DELETE FROM market_counts WHERE category='record_pending'")
    before = list(store.db.iterdump())
    with pytest.raises(StateError, match="counter"):
        queue.header(store.db)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        with pytest.raises(StateError, match="counter"):
            queue.advance(store, {}, 7, time.monotonic() + 1)
        rid = store.db.execute("SELECT rid FROM market_record_pending LIMIT 1").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="queue counter"):
            store.db.execute("DELETE FROM market_runtime_keys WHERE id=?", (rid,))
    assert list(store.db.iterdump()) == before


def retained_alias(store):
    """A valid old hot alias over its retained cold event, then new arrivals."""
    from cryptowatcher.expiry_index import put

    queued(store)
    history = History(store)
    key, value = history.items("history:", 1, reverse=True)[0]
    state = {}
    for _ in range(200):
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            queue.advance(store, state, 256, time.monotonic() + 1)
        if not queue.header(store.db)["count"]:
            break
    assert queue.header(store.db)["count"] == 0
    root = store.db.execute(
        "SELECT expires FROM market_record_parents WHERE public_uuid=?",
        (record_catalog.binary(value["event_id"]),),
    ).fetchone()
    with store.db:
        store.db.execute(RUNTIME_UPSERT, (PREFIX + key, encode(value)))
        put(store.db, PREFIX + key, root[0], "history", value["event_id"])
    return key, value


def test_continuing_body_arrivals_cannot_starve_old_alias_and_turn_survives_reopen(store, monkeypatch):
    key, value = retained_alias(store)
    clock = [0.0]
    prepare = queue.catalogue_transfer.prepare

    def body_uses_quantum(*args):
        result = prepare(*args)
        clock[0] += 2  # A legal started body can consume the entire one-second test quantum.
        return result

    monkeypatch.setattr(queue, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(queue.catalogue_transfer, "prepare", body_uses_quantum)
    state = {}
    for turn in range(2):
        observe(store, OutcomeWorker(store), NOW + (20 + turn) * MINUTE)
        clock[0] = 0
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            queue.advance(store, state, 256, 1)
            store.db.execute(RUNTIME_UPSERT, ("fairness-checkpoint", encode(state)))
        if turn == 0:
            assert state["next_lane"] == "head" and state["bodies_moved"] > 0
            assert store.db.execute("SELECT 1 FROM runtime WHERE key=?", (PREFIX + key,)).fetchone()
            with closing(Store(store.path, store.owner_id, readonly=True)) as reopened:
                state = reopened.get_runtime("fairness-checkpoint")
                assert state["next_lane"] == "head"
    assert state["next_lane"] == "body" and state["aliases_moved"] > 0
    assert not store.db.execute("SELECT 1 FROM runtime WHERE key=?", (PREFIX + key,)).fetchone()
    assert History(store).get(key) == value
    assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("turn", [True, 1, None, "future", []])
def test_unknown_queue_turn_rejects_before_any_change(store, turn):
    queued(store)
    before = list(store.db.iterdump())
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        with pytest.raises(StateError, match="queue turn"):
            queue.advance(store, {"next_lane": turn}, 256, time.monotonic() + 1)
    assert list(store.db.iterdump()) == before


def test_head_first_retains_selected_body_dependencies_until_all_aliases_are_cold(store):
    queued(store)
    history = History(store)
    aliases = [(key, value) for key, value in history.items("history:") if str(NOW + 10 * MINUTE) in key]
    assert aliases
    original = [(key, encode(value)) for key, value in history.scan("")]
    state = {"next_lane": "head"}
    for _ in range(200):
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            queue.advance(store, state, 256, time.monotonic() + 0.01)
        if not queue.header(store.db)["count"]:
            break
    assert queue.header(store.db)["count"] == 0
    for key, value in aliases:
        assert not store.db.execute("SELECT 1 FROM runtime WHERE key=?", (PREFIX + key,)).fetchone()
        assert history.get(key) == value
    assert [(key, encode(value)) for key, value in history.scan("")] == original


def test_empty_queue_keeps_exact_checkpoint_and_performs_no_writes(store):
    queued(store)
    state = {}
    for _ in range(200):
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            queue.advance(store, state, 256, time.monotonic() + 1)
        if not queue.header(store.db)["count"]:
            break
    assert queue.header(store.db)["count"] == 0
    with store.db:
        store.db.execute(RUNTIME_UPSERT, ("empty-queue-checkpoint", encode(state)))
    original = encode(state)
    for _ in range(2):
        changes = store.db.total_changes
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            queue.advance(store, state, 256, time.monotonic() + 1)
        assert encode(state) == original
        assert store.db.total_changes == changes
    with closing(Store(store.path, store.owner_id)) as reopened:
        saved = reopened.get_runtime("empty-queue-checkpoint")
        with reopened.db:
            reopened.db.execute("BEGIN IMMEDIATE")
            queue.advance(reopened, saved, 256, time.monotonic() + 1)
        assert encode(saved) == original


def test_queue_install_does_not_enqueue_existing_data_or_activate_itself_and_unknown_marker_rejects(store):
    populated(store)
    queue.install(store.db)
    assert queue.header(store.db)["count"] == 0
    observe(store, OutcomeWorker(store), NOW + 10 * MINUTE)
    assert queue.header(store.db)["count"] == 0
    with store.db:
        store.db.execute("UPDATE market_meta SET value='future' WHERE key='catalogue_queue'")
    before = list(store.db.iterdump())
    with pytest.raises(StateError, match="unknown"):
        queue.install(store.db)
    assert list(store.db.iterdump()) == before


def test_queue_checkpoint_and_deep_boundary_reject_corruption_before_mutation(store):
    queued(store)
    before = list(store.db.iterdump())
    for state in ({"version": True}, {"version": 2}, {"bodies_moved": -1}, {"fallback_visits": []}):
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            with pytest.raises(StateError, match="queue"):
                queue.advance(store, state, 7, time.monotonic() + 1)
        assert list(store.db.iterdump()) == before
    assert queue.verify(store.db) == queue.header(store.db)
    with store.db:
        store.db.execute(
            "UPDATE market_counts SET records=records+1,logical_bytes=logical_bytes+32 "
            "WHERE category='record_pending'"
        )
    # The cheap occupancy header cannot prove a complete physical boundary.
    assert queue.header(store.db)["count"] > 0
    with pytest.raises(StateError, match="count"):
        queue.verify(store.db)


def test_queue_accounting_includes_age_and_physical_index_and_survives_recode_vacuum(store):
    import apsw

    from cryptowatcher.storage_meter import measure_indexes, report

    queued(store)
    expected = queue.verify(store.db)
    measured = measure_indexes(store)
    if measured["state"] == "UNAVAILABLE":
        # Windows stdlib SQLite lacks DBSTAT; inspect actual pages with pinned APSW.
        with closing(apsw.Connection(str(store.path), flags=apsw.SQLITE_OPEN_READONLY)) as physical:
            sizes = dict(physical.execute("SELECT name,pgsize FROM dbstat WHERE aggregate=TRUE"))
            schema = dict(physical.execute("SELECT name,type FROM sqlite_master"))
            measured = {
                "state": "PASS",
                "tables": {key: size for key, size in sizes.items() if schema.get(key) != "index"},
                "indexes": {key: size for key, size in sizes.items() if schema.get(key) == "index"},
            }
    store.put_runtime("storage_index_measurement", measured)
    value = report(store, NOW + 12 * MINUTE)
    assert value["catalogue_layout"]["pending"] == {**expected, "oldest_age_ms": 2 * MINUTE}
    assert value["categories"]["record_pending"]["logical_bytes"] == expected["count"] * 32
    assert "market_record_pending" in value["indexes"]["tables"]
    assert "sqlite_autoindex_market_record_pending_1" in value["indexes"]["indexes"]
    with store.db:
        rid = store.db.execute("SELECT rid FROM market_record_pending LIMIT 1").fetchone()[0]
        original = store.db.execute("SELECT external FROM market_runtime_keys WHERE id=?", (rid,)).fetchone()[
            0
        ]
        # A registry representation update retains its stable RID/queue sequence.
        store.db.execute("UPDATE market_runtime_keys SET external=? WHERE id=?", (original, rid))
    store.db.execute("VACUUM")
    assert queue.verify(store.db) == expected
    store.put_runtime("storage_index_measurement", {"state": "PASS", "tables": {}})
    assert report(store, NOW)["indexes"]["state"] == "STALE"
