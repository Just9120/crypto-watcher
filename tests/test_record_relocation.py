import uuid
from contextlib import closing
from dataclasses import replace

import pytest

from cryptowatcher import record_catalog
from cryptowatcher import record_relocation as relocation
from cryptowatcher.events import History
from cryptowatcher.outcomes import OutcomeWorker
from cryptowatcher.record_blocks import KINDS, Coordinate
from cryptowatcher.sources import Sources
from cryptowatcher.storage import StateError, Store, encode
from tests.catalogue_fixture import move_events
from tests.test_bounded_storage import bounded
from tests.test_outcomes import MINUTE, NOW, future, observe


def fragmented(store):
    bounded(store)
    worker = OutcomeWorker(store)
    observe(store, worker)
    Sources(store).put("spot/BTCUSDT/1m", future())
    worker.work(NOW + 61 * MINUTE, batch=200)
    move_events(store, ("event", "outcome"))
    return [
        relocation.capture(store, Coordinate(KINDS[kind], str(uuid.UUID(bytes=uid)), revision))
        for kind, uid, revision in store.db.execute(
            "SELECT kind,public_uuid,revision FROM market_records ORDER BY kind,public_uuid,revision"
        )
    ]


def public(store):
    return [(key, encode(value)) for key, value in History(store).scan("")]


def references(store):
    tables = ("market_numeric", "market_source_windows", "market_record_parents", "market_record_members")
    return {name: [tuple(row) for row in store.db.execute("SELECT * FROM " + name)] for name in tables}


def test_atomic_relocation_keeps_snapshot_public_heads_leases_references_and_noop_fresh_retry(store):
    records = fragmented(store)
    assert len({r.binding[0] for r in records}) > 1
    before_public = public(store)
    before_refs = references(store)
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        reader.db.execute("BEGIN")
        assert public(reader) == before_public
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            packet = relocation.relocate(store, records)
        assert public(reader) == before_public
        assert len({relocation.capture(reader, r.coordinate).binding[0] for r in records}) > 1
        reader.db.rollback()
        assert {relocation.capture(reader, r.coordinate).binding[0] for r in records} == {packet}
        assert public(reader) == before_public
    assert references(store) == before_refs
    fresh = [relocation.capture(store, r.coordinate) for r in records]
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        changes = store.db.total_changes
        assert relocation.relocate(store, fresh) == packet
        assert store.db.total_changes == changes
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        assert public(reader) == before_public


def test_late_failure_rolls_back_publication_but_preserves_other_outer_transaction_changes(
    store, monkeypatch
):
    records = fragmented(store)
    original = record_catalog.append

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected after publication")

    monkeypatch.setattr(record_catalog, "append", fail)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        store.db.execute("INSERT INTO runtime VALUES('unrelated-relocation-owner','{\"kept\":true}')")
        before = list(store.db.iterdump())
        with pytest.raises(RuntimeError, match="after publication"):
            relocation.relocate(store, records)
        assert list(store.db.iterdump()) == before
    assert store.get_runtime("unrelated-relocation-owner") == {"kept": True}


@pytest.mark.parametrize("damage", ["value", "projection", "lease", "current"])
def test_changed_snapshot_is_rejected_before_mutation(store, damage):
    records = fragmented(store)
    first = records[0]
    if damage == "value":
        changed = replace(first, value={**first.value, "unknown_future_field": "preserve"})
    elif damage == "projection":
        changed = replace(first, projection={**first.projection, "strength": 987})
    elif damage == "lease":
        changed = replace(first, expires=first.expires + 1)
    else:
        changed = replace(first, binding=(*first.binding[:4], 1 - first.binding[4]))
    before = list(store.db.iterdump())
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        with pytest.raises(StateError, match="snapshot"):
            relocation.relocate(store, [changed, *records[1:]])
    assert list(store.db.iterdump()) == before


def test_stale_retry_missing_object_and_no_outer_transaction_never_republish(store):
    records = fragmented(store)
    with pytest.raises(StateError, match="outer writer"):
        relocation.relocate(store, records)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        relocation.relocate(store, records)
    before = list(store.db.iterdump())
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        with pytest.raises(StateError, match="snapshot"):
            relocation.relocate(store, records)
    assert list(store.db.iterdump()) == before
    with pytest.raises(StateError, match="Missing"):
        relocation.capture(store, replace(records[0].coordinate, revision=987))
