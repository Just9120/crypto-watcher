import sqlite3
import uuid

import pytest

from cryptowatcher import catalogue_aliases as aliases
from cryptowatcher import catalogue_reader as reader
from cryptowatcher import record_catalog as catalog
from cryptowatcher.events import DECISION_HEAD, PREFIX, History
from cryptowatcher.record_blocks import Coordinate
from cryptowatcher.storage import RUNTIME_UPSERT, StateError, encode
from tests.test_catalogue_aliases import populated
from tests.test_record_blocks import entries
from tests.test_record_catalog import DAY, prepare


def cold(store):
    prepare(store)
    store.bounded = True
    store.clock_ms = lambda: 100 * DAY
    values = []
    for coordinate, body, _ in entries():
        if coordinate.kind == "episode":
            body = {
                **body,
                "state": "CLOSED",
                "started_ms": 100 * DAY,
                "last_observation_ms": 100 * DAY,
                "longest_tf_ms": 900000,
                "instrument": "spot/BTCUSDT",
                "direction": "UP",
                "strength": 1.0,
                "available_at_ms": 100 * DAY,
            }
        projected = (
            None
            if coordinate.kind == "decision"
            else History._head(coordinate.kind, body, coordinate.key, bounded=True)
        )
        values.append((coordinate, body, projected))
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        catalog.append(
            store.db,
            values,
            bucket=130,
            owners={c: None for c, _, _ in values},
            deadlines={c: 130 * DAY for c, _, _ in values},
            current=[c for c, _, _ in values],
        )
        store.db.execute("INSERT INTO market_meta VALUES('record_catalogue_runtime','1')")
    return values


def head_key(coordinate):
    return (
        PREFIX
        + ("decision:telegram:" if coordinate.kind == "decision" else coordinate.kind + "-head:")
        + coordinate.identity
    )


def test_cold_immutable_retries_stay_cold_conflicts_and_missing_or_stale_heads_preserve_all_state(store):
    values = cold(store)
    history = History(store)
    for coordinate, body, projection in values:
        before = list(store.db.iterdump())
        store.put_runtime(PREFIX + coordinate.key, body)
        store.put_runtime(
            head_key(coordinate),
            {DECISION_HEAD: coordinate.revision} if coordinate.kind == "decision" else projection,
        )
        assert list(store.db.iterdump()) == before
        assert (
            store.db.execute("SELECT 1 FROM runtime WHERE key=?", (PREFIX + coordinate.key,)).fetchone()
            is None
        )
        with pytest.raises(StateError, match="Conflicting"):
            store.put_runtime(PREFIX + coordinate.key, {**body, "unknown_field": -0.0})
        wrong = Coordinate(coordinate.kind, str(uuid.UUID(int=100000)), coordinate.revision)
        with pytest.raises(StateError, match="identity"):
            store.put_runtime(PREFIX + wrong.key, body)
        newer = Coordinate(coordinate.kind, coordinate.identity, coordinate.revision + 1)
        value = (
            {DECISION_HEAD: newer.revision}
            if coordinate.kind == "decision"
            else History._head(coordinate.kind, {**body, "revision": newer.revision}, newer.key, bounded=True)
        )
        with pytest.raises(StateError, match="Missing"):
            store.put_runtime(head_key(coordinate), value)
        assert list(store.db.iterdump()) == before
    assert history.count("") == 10


def test_new_hot_revisions_replace_only_cold_current_flag_and_keep_every_old_value(store):
    values = cold(store)
    history = History(store)
    for coordinate, body, projected in values:
        newer = Coordinate(coordinate.kind, coordinate.identity, coordinate.revision + 1)
        value = {**body, "revision": newer.revision, "unknown_next": -0.0}
        projection = (
            {DECISION_HEAD: newer.revision}
            if coordinate.kind == "decision"
            else History._head(coordinate.kind, value, newer.key, bounded=True)
        )
        store.put_runtime(PREFIX + newer.key, value)
        store.put_runtime(head_key(coordinate), projection)
        assert catalog.row(store.db, coordinate)[4] == 0
        assert catalog.head(store.db, coordinate.kind, coordinate.identity) is None
        assert encode(history.get(coordinate.key)) == encode(body)
        assert encode(history.get(newer.key)) == encode(value)
        assert encode(history.get(head_key(coordinate)[len(PREFIX) :])) == encode(value)
        assert store.count_runtime(PREFIX + coordinate.kind + ":") == (
            3 if coordinate.kind == "decision" else 2
        )
        before = list(store.db.iterdump())
        old_head = {DECISION_HEAD: coordinate.revision} if coordinate.kind == "decision" else projected
        with pytest.raises(StateError, match="Stale"):
            store.put_runtime(head_key(coordinate), old_head)
        assert list(store.db.iterdump()) == before
        with store.db:
            history.delete(head_key(coordinate)[len(PREFIX) :])
        assert history.get(head_key(coordinate)[len(PREFIX) :]) is None
        assert history.get(coordinate.key) == body
        assert history.get(newer.key) == value


def test_head_write_failure_and_whole_history_version_failure_restore_ownership_and_every_counter(store):
    values = cold(store)
    history = History(store)
    coordinate, body, _ = next(item for item in values if item[0].kind == "episode")
    newer = Coordinate("episode", coordinate.identity, coordinate.revision + 1)
    value = {**body, "revision": newer.revision}
    head = History._head("episode", value, newer.key, bounded=True)
    store.put_runtime(PREFIX + newer.key, value)
    with store.db:
        store.db.execute(
            "CREATE TRIGGER injected_head_failure BEFORE INSERT ON runtime WHEN NEW.key="
            + repr(head_key(coordinate))
            + " BEGIN SELECT RAISE(ABORT,'injected head failure'); END"
        )
    before = list(store.db.iterdump())
    with store.db:
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            history.put(head_key(coordinate)[len(PREFIX) :], head)
        assert list(store.db.iterdump()) == before
    # A failed version must also roll back its newly admitted immutable body and leases.
    with store.db:
        store.db.execute("DELETE FROM runtime WHERE key=?", (PREFIX + newer.key,))
    before = list(store.db.iterdump())
    with store.db:
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            history.version("episode", value)
        assert list(store.db.iterdump()) == before
    with store.db:
        store.db.execute("DROP TRIGGER injected_head_failure")
        history.version("episode", value)
    assert catalog.row(store.db, coordinate)[4] == 0
    assert history.get("episode-head:" + coordinate.identity) == value
    assert history.get(coordinate.key) == body


def test_compact_head_cannot_bypass_reserved_decision_reference_validation(store):
    values = cold(store)
    coordinate, body, _ = next(item for item in values if item[0].kind == "event")
    newer = Coordinate("event", coordinate.identity, coordinate.revision + 1)
    malformed = {**body, "revision": newer.revision, DECISION_HEAD: 7}
    store.put_runtime(PREFIX + newer.key, malformed)
    head = History._head("event", malformed, newer.key, bounded=True)
    before = list(store.db.iterdump())

    with pytest.raises(StateError, match="Invalid decision head reference"):
        store.put_runtime(head_key(coordinate), head)

    assert list(store.db.iterdump()) == before
    assert encode(History(store).get(coordinate.key)) == encode(body)
    assert catalog.row(store.db, coordinate)[4] == 1


def test_cold_alias_deletion_does_not_delete_events_or_resurrect_unpublished_history(store):
    public = populated(store)
    store.bounded = True
    history = History(store)
    key = next(key for key in public if key.startswith(aliases.HISTORY))
    selected = aliases.point(key)
    original = history.get(Coordinate("event", selected.event, 1).key)
    with store.db:
        store.db.execute(RUNTIME_UPSERT, (key, encode(public[key])))
        store.db.execute(
            "CREATE TRIGGER injected_alias_delete BEFORE DELETE ON runtime WHEN OLD.key="
            + repr(key)
            + " BEGIN SELECT RAISE(ABORT,'injected alias delete'); END"
        )
    before = list(store.db.iterdump())
    with store.db:
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            history.delete(key[len(PREFIX) :])
        assert list(store.db.iterdump()) == before
    with store.db:
        store.db.execute("DROP TRIGGER injected_alias_delete")
    with store.db:
        history.delete(key[len(PREFIX) :])
        history.delete(key[len(PREFIX) :])
    assert store.get_runtime(key) is None
    assert history.get(Coordinate("event", selected.event, 1).key) == original
    assert (
        store.db.execute(
            "SELECT history_published FROM market_record_parents WHERE public_uuid=?",
            (catalog.binary(selected.event),),
        ).fetchone()[0]
        == 2
    )
    with store.db:
        history.put(key[len(PREFIX) :], public[key])
        assert not aliases.publish(store.db, key, public[key])
    assert store.get_runtime(key) == public[key]
    # Membership remains independent of whether the chronological entry is published.
    member = aliases.Alias(selected.event, selected.observed, str(uuid.UUID(int=901)))
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        assert aliases.publish(store.db, member.key, public[key])
        history.delete(member.key[len(PREFIX) :])
        history.delete(key[len(PREFIX) :])
    assert store.get_runtime(member.key) is None and store.get_runtime(key) is None
    assert history.get(Coordinate("event", selected.event, 1).key) == original
    assert history.count("history:") == 5


def test_current_version_requires_explicit_head_removal_and_never_reappears_after_new_head_delete(store):
    values = cold(store)
    history = History(store)
    coordinate, body, projection = values[2]
    before = list(store.db.iterdump())
    with store.db:
        with pytest.raises(StateError, match="current head"):
            history.delete(coordinate.key)
        assert list(store.db.iterdump()) == before
        history.delete(head_key(coordinate)[len(PREFIX) :])
    assert history.get(head_key(coordinate)[len(PREFIX) :]) is None
    assert history.get(coordinate.key) == body
    with store.db:
        history.delete(coordinate.key)
    assert history.get(coordinate.key) is None
    assert reader.count(store.db, PREFIX + "episode:") == 0


def test_episode_unknown_metrics_field_stays_exact_after_unused_pool_collection(store):
    from cryptowatcher import numeric_pool

    values = cold(store)
    coordinate, old, _ = values[2]
    value = {
        **old,
        "revision": coordinate.revision + 1,
        "metrics": {
            "price_return_pct": 1.0,
            "directed_return_pct": 1.0,
            "mfe_pct": 2.0,
            "mae_pct": -0.0,
        },
    }
    history = History(store)
    with store.db:
        history.version("episode", value)
        numeric_pool.collect(store.db)
    assert encode(history.get("episode-head:" + coordinate.identity)) == encode(value)
    assert encode(history.get(coordinate.key)) == encode(old)
