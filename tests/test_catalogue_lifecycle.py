import uuid

import pytest

from cryptowatcher import catalogue_aliases as aliases
from cryptowatcher import record_catalog as catalog
from cryptowatcher.events import PREFIX, History
from cryptowatcher.expiry_index import expires, member_deadline
from cryptowatcher.market_storage import enable
from cryptowatcher.record_blocks import Coordinate
from cryptowatcher.storage import RUNTIME_UPSERT, encode
from cryptowatcher.storage_lifecycle import BoundedRetention
from tests.test_record_catalog import DAY, prepare


def test_small_expiry_batch_does_not_delete_episode_while_a_later_member_is_retained(store, monkeypatch):
    enable(store.db)
    store.bounded = True
    now = 100 * DAY
    store.clock_ms = lambda: now
    history = History(store)
    episode = str(uuid.UUID(int=500))
    body = {
        "episode_id": episode,
        "revision": 1,
        "schema": "cryptowatcher.episode.v1",
        "started_ms": now - 30 * DAY - 20 * 60000,
        "last_observation_ms": now - 30 * DAY + 10 * 60000,
        "state": "CLOSED",
        "instrument": "spot/BTCUSDT",
        "direction": "UP",
        "strength": 1.0,
        "longest_tf_ms": 60000,
    }
    with store.db:
        history.put(f"episode:{episode}:0000000001", body)
        history.put(
            "episode-head:" + episode,
            History._head("episode", body, f"episode:{episode}:0000000001", bounded=True),
        )
        # All members belong to a30minute episode, within the runtime's one-hour cap.
        for number, observed in (
            (1, now - 30 * DAY - 20 * 60000),
            (2, now - 30 * DAY - 10 * 60000),
            (3, now - 30 * DAY + 10 * 60000),
        ):
            event = str(uuid.UUID(int=number))
            value = {
                "event_id": event,
                "episode_id": episode,
                "revision": 1,
                "market_observation_ms": observed,
            }
            history.put(f"event:{event}:0000000001", value)
            history.put("event-head:" + event, value)
            history.put(f"history:{observed:016d}:{event}", {"event_id": event})
            history.put(f"episode-event:{episode}:{observed:016d}:{event}", {"event_id": event})
    archived = []
    # Archive correctness has separate tests; this regression isolates indexed expiry ordering.
    monkeypatch.setattr(
        "cryptowatcher.quality_summary.archive_event", lambda store, value: archived.append(value["event_id"])
    )
    BoundedRetention(store).work(now, batch=1)
    assert archived == [str(uuid.UUID(int=1))]
    assert history.get("event-head:" + str(uuid.UUID(int=3))) is not None
    assert history.get("episode-head:" + episode) == body
    assert history.get(f"episode:{episode}:0000000001") == body
    assert store.count_runtime(PREFIX + "episode-event:" + episode + ":") >= 1


def retained(store, number, observed, expiry):
    event = str(uuid.UUID(int=number))
    coordinate = Coordinate("event", event, 1)
    parent = catalog.parent(store.db, event, observed, expiry)
    body = {"event_id": event, "revision": 1, "market_observation_ms": observed}
    catalog.append(
        store.db,
        [(coordinate, body, None)],
        bucket=expiry // DAY,
        owners={coordinate: parent},
        deadlines={coordinate: None},
    )
    return coordinate, body


def test_late_service_and_new_head_keep_frozen_parent_deadline_without_a_cold_current_head(store):
    prepare(store)
    store.bounded = True
    store.clock_ms = lambda: 110 * DAY
    with store.db:
        coordinate, body = retained(store, 1, 100 * DAY, 130 * DAY)
        store.db.execute("INSERT INTO market_meta VALUES('record_catalogue_runtime','1')")
    assert History(store).get("event-head:" + coordinate.identity) is None
    key = PREFIX + "outbox:telegram:" + str(uuid.UUID(int=900))
    history = History(store)
    with store.db:
        history.put(
            key[len(PREFIX) :], {"event_id": coordinate.identity, "created_ms": 110 * DAY, "state": "RETRY"}
        )
        newer = Coordinate("event", coordinate.identity, 2)
        value = {**body, "revision": 2}
        history.put(newer.key, value)
        history.put(
            "event-head:" + coordinate.identity, History._head("event", value, newer.key, bounded=True)
        )
    assert expires(store.db, key)[0] == 130 * DAY
    for candidate in (coordinate.key, newer.key, "event-head:" + coordinate.identity):
        assert expires(store.db, PREFIX + candidate)[0] == 130 * DAY
    with store.db:
        store.db.execute(RUNTIME_UPSERT, (PREFIX + coordinate.key, encode(body)))
        store.db.execute(
            "INSERT INTO market_lifetimes VALUES(?,?,?,?)",
            (PREFIX + coordinate.key, 131 * DAY, "event", coordinate.identity),
        )
    before = list(store.db.iterdump())
    with pytest.raises(ValueError, match="Conflicting"):
        expires(store.db, PREFIX + coordinate.key)
    assert list(store.db.iterdump()) == before


def test_latest_needed_episode_member_merges_hot_and_cold_deadlines_without_extending_events(store):
    prepare(store)
    store.bounded = True
    store.clock_ms = lambda: 102 * DAY
    episode = str(uuid.UUID(int=500))
    cold = []
    with store.db:
        for number in (1, 2):
            coordinate, body = retained(store, number, (99 + number) * DAY, (129 + number) * DAY)
            binding = aliases.Alias(coordinate.identity, body["market_observation_ms"], episode)
            assert aliases.publish(store.db, binding.key, {"event_id": coordinate.identity})
            cold.append((coordinate, body, binding))
        store.db.execute("INSERT INTO market_meta VALUES('record_catalogue_runtime','1')")
    prefix = PREFIX + "episode-event:" + episode + ":"
    assert member_deadline(store.db, prefix) == 131 * DAY
    history = History(store)
    event = str(uuid.UUID(int=3))
    value = {"event_id": event, "revision": 1, "market_observation_ms": 102 * DAY}
    binding = aliases.Alias(event, 102 * DAY, episode)
    with store.db:
        history.put(f"event:{event}:0000000001", value)
        history.put(
            "event-head:" + event, History._head("event", value, f"event:{event}:0000000001", bounded=True)
        )
        history.put(binding.key[len(PREFIX) :], {"event_id": event})
    assert member_deadline(store.db, prefix) == 132 * DAY
    with store.db:
        history.delete(binding.key[len(PREFIX) :])
    assert member_deadline(store.db, prefix) == 131 * DAY
    with store.db:
        history.delete(cold[1][2].key[len(PREFIX) :])
    assert member_deadline(store.db, prefix) == 130 * DAY
    with store.db:
        history.delete(cold[0][2].key[len(PREFIX) :])
    assert member_deadline(store.db, prefix) is None
    for coordinate, body, _ in cold:
        assert history.get(coordinate.key) == body
        assert expires(store.db, PREFIX + coordinate.key)[0] == body["market_observation_ms"] + 30 * DAY
