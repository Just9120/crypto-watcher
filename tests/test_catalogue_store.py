import uuid
from contextlib import closing

import pytest

from cryptowatcher import record_catalog as catalog
from cryptowatcher.events import DECISION_HEAD, PREFIX, History
from cryptowatcher.market_storage import enable
from cryptowatcher.record_blocks import Coordinate
from cryptowatcher.storage import StateError, Store, encode
from tests.test_catalogue_aliases import populated
from tests.test_record_blocks import entries
from tests.test_record_catalog import DAY


def mixed(store):
    enable(store.db)
    store.bounded = True
    catalog.install(store.db, store.cached_text, store.cached_bytes)
    values = [
        (c, value, None if c.kind == "decision" else History._head(c.kind, value, c.key, bounded=True))
        for c, value, _ in entries()
    ]
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
    expected = populated(store, offset=10**6, initialized=True)
    for key in list(expected):
        if key.startswith(PREFIX + "history:"):
            event = expected[key]["event_id"]
            expected[PREFIX + Coordinate("event", event, 1).key] = {
                "event_id": event,
                "revision": 1,
                "market_observation_ms": int(key.split(":")[3]),
                "unknown": -0.0,
            }
    for coordinate, body, projected in values:
        expected[PREFIX + coordinate.key] = body
        key = (
            PREFIX
            + ("decision:telegram:" if coordinate.kind == "decision" else coordinate.kind + "-head:")
            + coordinate.identity
        )
        expected[key] = {DECISION_HEAD: coordinate.revision} if coordinate.kind == "decision" else projected
    # Frozen duplicate, old full-head representation, newer hot head and hot-only key.
    c, value, _ = values[1]
    store.put_runtime(PREFIX + c.key, value)
    store.put_runtime(PREFIX + "outcome-head:" + c.identity, value)
    expected[PREFIX + "outcome-head:" + c.identity] = value
    c, value, _ = values[0]
    second = Coordinate("event", c.identity, 2)
    newer = {**value, "revision": 2}
    head_key = PREFIX + "event-head:" + c.identity
    hot = {
        PREFIX + second.key: newer,
        head_key: History._head("event", newer, second.key, bounded=True),
        PREFIX + "diagnostic:русский": {"value": -0.0},
    }
    for key, value in hot.items():
        store.put_runtime(key, value)
    expected.update(hot)
    # One exact duplicate binding must appear once in the mixed stream.
    alias = next(key for key in expected if key.startswith(PREFIX + "history:"))
    store.put_runtime(alias, expected[alias])
    return expected, values


def test_store_and_history_resolve_cold_only_versions_heads_aliases_and_mixed_duplicates(store):
    expected, values = mixed(store)
    history = History(store)
    for key, value in expected.items():
        assert encode(store.get_runtime(key)) == encode(value)
    default = {"no_object": -0.0}
    missing = PREFIX + "event:" + str(uuid.UUID(int=99999)) + ":0000000001"
    assert store.get_runtime(missing, default) is default
    for coordinate, body, _ in values:
        if coordinate.kind == "event":
            assert history.get("event-head:" + coordinate.identity)["revision"] == 2
        elif coordinate.kind == "decision":
            assert encode(history.get("decision:telegram:" + coordinate.identity)) == encode(body)
        else:
            assert encode(history.get(coordinate.kind + "-head:" + coordinate.identity)) == encode(body)
    ordered = sorted(expected.items())
    assert store.count_runtime(PREFIX) == len(expected)
    assert history.count("") == len(expected)
    assert [(key, encode(value)) for key, value in store.scan_runtime(PREFIX)] == [
        (key, encode(value)) for key, value in ordered
    ]
    assert history.items("", 4, reverse=True) == [
        (key[len(PREFIX) :], history.resolve(key[len(PREFIX) :], value))
        for key, value in reversed(ordered[-4:])
    ]
    assert list(history.scan("history:")) == [
        (key[len(PREFIX) :], value) for key, value in ordered if key.startswith(PREFIX + "history:")
    ]


def test_mixed_store_keyset_limits_readonly_snapshot_and_conflicts(store):
    expected, values = mixed(store)
    ordered = sorted(expected.items())
    for prefix in (
        PREFIX,
        PREFIX + "event",
        PREFIX + "history:",
        PREFIX + "episode-event:",
        PREFIX + "diagnostic:",
        "absent",
    ):
        for after, before in ((None, None), (ordered[4][0], ordered[-3][0]), ("radar:v1:event:~", None)):
            selected = [
                (key, value)
                for key, value in ordered
                if key.startswith(prefix)
                and (after is None or key > after)
                and (before is None or key < before)
            ]
            for reverse in (False, True):
                wanted = list(reversed(selected)) if reverse else selected
                for limit in (None, 0, 1, 4):
                    assert [
                        (key, encode(value))
                        for key, value in store.scan_runtime(
                            prefix, limit, reverse=reverse, after=after, before=before
                        )
                    ] == [
                        (key, encode(value)) for key, value in (wanted if limit is None else wanted[:limit])
                    ]
    with closing(Store(store.path, store.owner_id, readonly=True)) as reopened:
        reopened.db.execute("BEGIN")
        assert list(reopened.scan_runtime(PREFIX, 1)) == ordered[:1]
        key = PREFIX + "diagnostic:a-new"
        store.put_runtime(key, {"new": True})
        assert reopened.get_runtime(key) is None
        assert list(reopened.scan_runtime(PREFIX, 1)) == ordered[:1]
        reopened.db.rollback()
        assert reopened.get_runtime(key) == {"new": True}
    coordinate, body, _ = values[1]
    store.put_runtime(PREFIX + coordinate.key, {**body, "tampered": 1})
    with pytest.raises(StateError, match="Conflicting"):
        store.get_runtime(PREFIX + coordinate.key)
    with pytest.raises(StateError, match="Conflicting"):
        list(store.scan_runtime(PREFIX + "outcome:"))


def test_unbounded_and_inactive_store_scans_need_no_catalogue_and_include_unicode(store):
    expected = {"radar:v1:example:я": {"value": -0.0}, "radar:v1:example:a": None}
    for key, value in expected.items():
        store.put_runtime(key, value)
    assert list(store.scan_runtime("radar:v1:example:")) == sorted(expected.items())
    assert list(store.scan_runtime("radar:v1:example:", 1)) == sorted(expected.items())[:1]
    for limit in (True, -1, 1.5, 2**63):
        with pytest.raises(ValueError):
            list(store.scan_runtime("radar:v1:example:", limit))
