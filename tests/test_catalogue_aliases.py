import uuid
from contextlib import closing

import pytest

from cryptowatcher import catalogue_aliases as aliases
from cryptowatcher import catalogue_reader as reader
from cryptowatcher import record_catalog as catalog
from cryptowatcher import record_masks
from cryptowatcher.catalogue_reader import MISSING
from cryptowatcher.record_blocks import Coordinate
from cryptowatcher.storage import StateError, Store, encode
from tests.test_record_catalog import DAY, prepare


def admitted(store, value, observed=100 * DAY):
    event = str(uuid.UUID(int=value))
    coordinate = Coordinate("event", event, 1)
    parent = catalog.parent(store.db, event, observed, 130 * DAY)
    body = {"event_id": event, "revision": 1, "market_observation_ms": observed, "unknown": -0.0}
    catalog.append(
        store.db,
        [(coordinate, body, None)],
        bucket=130,
        owners={coordinate: parent},
        deadlines={coordinate: None},
    )
    return event, observed


def populated(store):
    prepare(store)
    public = {}
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        for value in (1, 2, 15, 255, 256, 65535):
            event, observed = admitted(store, value, 100 * DAY + value % 2)
            for episode in (None, str(uuid.UUID(int=101)), str(uuid.UUID(int=65535))):
                binding = aliases.Alias(event, observed, episode)
                value = {"event_id": event}
                assert aliases.publish(store.db, binding.key, value, store.cached_bytes)
                public[binding.key] = value
        # A prepared parent is deliberately not a published history entry.
        catalog.parent(store.db, str(uuid.UUID(int=999999)), 100 * DAY, 130 * DAY)
        store.db.execute("INSERT INTO market_meta VALUES('record_catalogue_runtime','1')")
    return public


def test_exact_bindings_retry_no_implicit_history_or_unknown_field_loss(store):
    prepare(store)
    with store.db:
        event, observed = admitted(store, 1)
    history = aliases.Alias(event, observed)
    member = aliases.Alias(event, observed, str(uuid.UUID(int=101)))
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        before = list(store.db.iterdump())
        for key, value in (
            (history.key, {"event_id": event, "future_field": 0}),
            (history.key + "unknown", {"event_id": event}),
            (aliases.Alias(event, observed + 1).key, {"event_id": event}),
            (history.key, {"event_id": str(uuid.UUID(int=2))}),
        ):
            assert not aliases.publish(store.db, key, value)
        assert list(store.db.iterdump()) == before
        assert aliases.publish(store.db, history.key, {"event_id": event})
        assert aliases.publish(store.db, member.key, {"event_id": event})
    assert aliases.lookup(store.db, history.key) is MISSING
    with store.db:
        store.db.execute("INSERT INTO market_meta VALUES('record_catalogue_runtime','1')")
    before = list(store.db.iterdump())
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        assert aliases.publish(store.db, history.key, {"event_id": event})
        assert aliases.publish(store.db, member.key, {"event_id": event})
    assert list(store.db.iterdump()) == before
    for key in (history.key, member.key):
        value = {"event_id": event}
        assert aliases.lookup(store.db, key) == value
        assert aliases.lookup(store.db, key, value) is value
        with pytest.raises(StateError, match="Conflicting"):
            aliases.lookup(store.db, key, {"event_id": event, "future_field": -0.0})
    assert record_masks.tallies(store.db, "record_aliases")["history"] == 1


def test_indexed_alias_points_prefixes_order_limits_and_counts_match_public_keys(store):
    public = populated(store)
    combined = list(reader.scan(store.db, reader.PREFIX))
    assert len(combined) == reader.count(store.db, reader.PREFIX) == len(public) + 6
    assert dict((key, value) for key, value in combined if aliases.point(key)) == public
    assert list(reader.scan(store.db, reader.PREFIX, reverse=True, limit=5)) == list(reversed(combined))[:5]
    prefixes = {
        "",
        "radar:",
        "radar:v1:",
        aliases.HISTORY,
        aliases.MEMBERS,
        "unknown",
        aliases.HISTORY + "-1",
        aliases.MEMBERS + "invalid:",
    }
    for key in public:
        prefixes.update(
            key[:width] for width in (20, 29, 35, 44, 47, 52, 60, 70, len(key) - 1, len(key), len(key) + 1)
        )
        assert aliases.point(key).key == key
        assert aliases.lookup(store.db, key) == public[key]
        assert reader.lookup(store.db, key) == public[key]
    before = list(store.db.iterdump())
    for prefix in prefixes:
        expected = sorted((key, value) for key, value in public.items() if key.startswith(prefix))
        assert aliases.count(store.db, prefix) == len(expected)
        for reverse in (False, True):
            ordered = list(reversed(expected)) if reverse else expected
            for limit in (None, 0, 1, 3, 100):
                assert list(aliases.scan(store.db, prefix, reverse=reverse, limit=limit)) == (
                    ordered if limit is None else ordered[:limit]
                )
        if prefix.startswith((aliases.HISTORY, aliases.MEMBERS)):
            assert list(reader.scan(store.db, prefix)) == expected
            assert reader.count(store.db, prefix) == len(expected)
    assert list(store.db.iterdump()) == before
    for member, prefix in (
        (False, aliases.HISTORY + f"{100 * DAY:016d}"),
        (True, aliases.MEMBERS + str(uuid.UUID(int=101))),
    ):
        selected = aliases.selection(prefix, member=member)
        fields = "episode,observed,event_uuid" if member else "observed,public_uuid"
        plan = store.db.execute(
            "EXPLAIN QUERY PLAN SELECT "
            + fields
            + " FROM "
            + selected.source
            + " WHERE "
            + selected.where
            + " ORDER BY "
            + fields,
            selected.parameters,
        ).fetchall()
        assert any("SEARCH" in row[3] for row in plan)
        assert all("TEMP B-TREE" not in row[3] for row in plan)


def test_alias_snapshot_rollback_counters_and_readonly_reopen(store):
    public = populated(store)
    before = list(store.db.iterdump())
    with pytest.raises(RuntimeError), store.db:
        event, observed = admitted(store, 1010)
        key = aliases.Alias(event, observed).key
        assert aliases.publish(store.db, key, {"event_id": event})
        raise RuntimeError("rollback publication")
    assert list(store.db.iterdump()) == before
    with closing(Store(store.path, store.owner_id, readonly=True)) as reopened:
        reopened.db.execute("BEGIN")
        assert list(aliases.scan(reopened.db, "radar:v1:")) == sorted(public.items())
        with store.db:
            event, observed = admitted(store, 1011)
            key = aliases.Alias(event, observed).key
            assert aliases.publish(store.db, key, {"event_id": event})
        assert aliases.lookup(reopened.db, key) is MISSING
        assert aliases.count(reopened.db, "radar:v1:") == len(public)
        reopened.db.rollback()
        assert aliases.lookup(reopened.db, key) == {"event_id": event}
    with store.db:
        store.db.execute("DELETE FROM market_counts WHERE category='record_aliases'")
    before = list(store.db.iterdump())
    with pytest.raises(ValueError, match="counter"):
        list(aliases.scan(store.db, aliases.HISTORY))
    assert list(store.db.iterdump()) == before


def test_invalid_alias_bounds_and_noncanonical_keys_preserve_hot_values(store):
    populated(store)
    for value in (True, -1, 1.0, 2**63):
        with pytest.raises(ValueError):
            list(aliases.scan(store.db, aliases.HISTORY, limit=value))
    for key in (
        "radar:v1:history:",
        "radar:v1:history:-1:uuid",
        "radar:v1:history:0000000000000000:" + "f" * 36,
    ):
        assert aliases.point(key) is None
        hot = {"untouched": -0.0}
        assert aliases.lookup(store.db, key, hot) is hot
        assert encode(hot) == '{"untouched":-0.0}'
