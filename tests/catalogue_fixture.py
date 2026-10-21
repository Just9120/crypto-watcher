"""Small explicit physical fixtures; this is not the production finite migration."""

from cryptowatcher import catalogue_aliases, record_catalog
from cryptowatcher.events import PREFIX, History
from cryptowatcher.expiry_index import hot_binding
from cryptowatcher.record_blocks import Coordinate


def move_events(store, kinds=("event",)):
    record_catalog.install(store.db, store.cached_text, store.cached_bytes)
    history = History(store)
    assert "event" in kinds
    records = [(kind, key, public) for kind in kinds for key, public in history.scan(kind + ":")]
    bindings = list(store.scan_runtime(PREFIX + "history:"))
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        for kind, key, public in records:
            coordinate = Coordinate(kind, public[kind + "_id"], public["revision"])
            body = store.get_runtime(PREFIX + key)
            head_key = kind + "-head:" + coordinate.identity
            projection = store.get_runtime(PREFIX + head_key)
            current = projection["revision"] == coordinate.revision
            binding = hot_binding(store.db, PREFIX + key)
            deadline = binding[0]
            root = history.metadata("event-head:" + binding[2]) if binding[2] is not None else None
            owner = (
                record_catalog.parent(store.db, binding[2], root["market_observation_ms"], deadline)
                if root is not None
                else None
            )
            record_catalog.append(
                store.db,
                [(coordinate, body, projection if current else None)],
                bucket=deadline // 86400000,
                owners={coordinate: owner},
                deadlines={coordinate: None if owner is not None else deadline},
                current=[coordinate] if current else [],
            )
        for _, key, _ in records:
            history.delete(key)
        for kind, identity in {(kind, public[kind + "_id"]) for kind, _, public in records}:
            history.delete(kind + "-head:" + identity)
        for key, binding in bindings:
            assert catalogue_aliases.publish(store.db, key, binding, store.cached_bytes)
            history.delete(key[len(PREFIX) :])
        store.db.execute("INSERT INTO market_meta VALUES('record_catalogue_runtime','1')")
    return [value for _, _, value in records]
