import copy
import uuid
from contextlib import closing

import pytest

from cryptowatcher import catalogue_reader as reader
from cryptowatcher import packet_store, record_catalog
from cryptowatcher.events import DECISION_HEAD
from cryptowatcher.market_storage import enable
from cryptowatcher.record_blocks import Coordinate
from cryptowatcher.storage import StateError, Store, encode
from tests.test_record_blocks import entries


def prepare(store, *, decision_without_schema=False):
    enable(store.db)
    record_catalog.install(store.db, store.cached_text, store.cached_bytes)
    values = entries()
    if decision_without_schema:
        coordinate, _, _ = values[-1]
        values[-1] = (
            coordinate,
            {
                "event_id": coordinate.identity,
                "revision": coordinate.revision,
                "at_ms": 123,
                "reason": "historical",
            },
            None,
        )
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        record_catalog.append(
            store.db,
            values,
            bucket=130,
            owners={c: None for c, _, _ in values},
            deadlines={c: 130 * 86400000 for c, _, _ in values},
            current=[c for c, _, _ in values],
        )
    return values


def activate(store):
    with store.db:
        store.db.execute("INSERT INTO market_meta VALUES('record_catalogue_runtime','1')")


def head_key(coordinate):
    return "radar:v1:" + (
        f"decision:telegram:{coordinate.identity}"
        if coordinate.kind == "decision"
        else f"{coordinate.kind}-head:{coordinate.identity}"
    )


def test_point_reader_is_explicit_and_preserves_defaults_unknown_keys_and_every_private_value(store):
    values = prepare(store)
    for coordinate, _, _ in values:
        assert reader.lookup(store.db, "radar:v1:" + coordinate.key) is reader.MISSING
    assert reader.lookup(store.db, "radar:v1:diagnostic", None) is None
    activate(store)
    before = list(store.db.iterdump())
    for coordinate, body, head in values:
        assert encode(
            reader.lookup(store.db, "radar:v1:" + coordinate.key, cache=store.cached_bytes)
        ) == encode(body)
        expected = {DECISION_HEAD: coordinate.revision} if coordinate.kind == "decision" else head
        assert encode(reader.lookup(store.db, head_key(coordinate), cache=store.cached_bytes)) == encode(
            expected
        )
    for key in ("radar:v1:event:unknown:0000000001", "radar:v1:event:", "radar:v1:outcome-job:one", "other"):
        hot = {"unchanged": -0.0}
        assert reader.lookup(store.db, key, hot) is hot
    assert list(store.db.iterdump()) == before


def test_unknown_or_missing_marker_rejects_without_repair_or_writes(store):
    coordinate, _, _ = prepare(store)[0]
    activate(store)
    for key, value in (
        ("record_catalogue_runtime", "2"),
        ("record_catalog", None),
        ("record_ownership", "2"),
    ):
        with store.db:
            store.db.execute("BEGIN")
            if value is None:
                store.db.execute("DELETE FROM market_meta WHERE key=?", (key,))
            else:
                store.db.execute("UPDATE market_meta SET value=? WHERE key=?", (value, key))
            before = list(store.db.iterdump())
            with pytest.raises(ValueError):
                reader.lookup(store.db, "radar:v1:" + coordinate.key)
            assert list(store.db.iterdump()) == before
            store.db.rollback()


def test_mixed_immutable_duplicate_compares_complete_values_and_never_masks_corrupt_cold_data(store):
    coordinate, body, _ = prepare(store)[0]
    activate(store)
    hot = copy.deepcopy(body)
    assert reader.lookup(store.db, "radar:v1:" + coordinate.key, hot, store.cached_bytes) is hot
    hot["future"]["unexpected"][-1] += 1
    with pytest.raises(StateError, match="immutable"):
        reader.lookup(store.db, "radar:v1:" + coordinate.key, hot, store.cached_bytes)
    assert encode(reader.lookup(store.db, "radar:v1:" + coordinate.key, cache=store.cached_bytes)) == encode(
        body
    )
    packet = record_catalog.row(store.db, coordinate)[0]
    arena, offset, size = store.db.execute(
        "SELECT arena,offset,length FROM market_packets WHERE id=?", (packet,)
    ).fetchone()
    with store.db.blobopen("market_packet_arenas", "payload", arena, readonly=False) as blob:
        blob.seek(offset + size - 1)
        old = blob.read(1)
        blob.seek(offset + size - 1)
        blob.write(bytes((old[0] ^ 1,)))
    with pytest.raises(ValueError):
        reader.lookup(store.db, "radar:v1:" + coordinate.key, body, store.cached_bytes)
    assert packet_store.summary(store.db)["live_bytes"] > 0


@pytest.mark.parametrize("decision_without_schema", [False, True])
def test_mixed_heads_choose_newest_and_validate_full_legacy_body_at_equal_revision(
    store, decision_without_schema
):
    values = prepare(store, decision_without_schema=decision_without_schema)
    activate(store)
    for coordinate, body, projection in values:
        key = head_key(coordinate)
        assert reader.lookup(store.db, key, body, store.cached_bytes) is body
        changed = {**body, "future": {"changed": True}}
        with pytest.raises(StateError, match="same revision"):
            reader.lookup(store.db, key, changed, store.cached_bytes)
        current = {DECISION_HEAD: coordinate.revision} if projection is None else projection
        newer = {**current, DECISION_HEAD if projection is None else "revision": coordinate.revision + 1}
        assert reader.lookup(store.db, key, newer, store.cached_bytes) is newer
        if coordinate.revision > 1:
            older = {**current, DECISION_HEAD if projection is None else "revision": coordinate.revision - 1}
            assert encode(reader.lookup(store.db, key, older, store.cached_bytes)) == encode(current)
    coordinate, _, projection = values[0]
    with pytest.raises(StateError, match="same revision"):
        reader.lookup(store.db, head_key(coordinate), {**projection, "future_head_field": 1})
    with pytest.raises(StateError, match="revision"):
        reader.lookup(store.db, head_key(coordinate), {**projection, "revision": True})


def test_reopened_readonly_snapshot_keeps_head_until_new_transaction(store):
    coordinate, body, head = prepare(store)[0]
    activate(store)
    with closing(Store(store.path, store.owner_id, readonly=True)) as frozen:
        frozen.db.execute("BEGIN")
        assert reader.lookup(frozen.db, head_key(coordinate)) == head
        second = Coordinate(coordinate.kind, coordinate.identity, 2)
        second_body = {**body, "revision": 2}
        second_head = {**head, "revision": 2, "revision_key": second.key}
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            record_catalog.append(
                store.db,
                [(second, second_body, second_head)],
                bucket=130,
                owners={second: None},
                deadlines={second: 130 * 86400000},
                current=[second],
            )
        assert reader.lookup(frozen.db, head_key(coordinate)) == head
        assert reader.lookup(frozen.db, "radar:v1:" + second.key) is reader.MISSING
        frozen.db.rollback()
        assert reader.lookup(frozen.db, head_key(coordinate)) == second_head
        assert reader.lookup(frozen.db, "radar:v1:" + second.key) == second_body
        assert reader.lookup(frozen.db, "radar:v1:" + coordinate.key) == body


@pytest.mark.parametrize("reverse", [False, True])
def test_domain_scan_matches_public_prefix_order_limits_and_counts_without_full_materialization(
    store, monkeypatch, reverse
):
    values = prepare(store)
    activate(store)
    expected = {}
    for coordinate, body, head in values:
        expected["radar:v1:" + coordinate.key] = body
        expected[head_key(coordinate)] = {DECISION_HEAD: coordinate.revision} if head is None else head
    monkeypatch.setattr(
        reader.record_blocks.indexed_frame, "all_values", lambda _: pytest.fail("unselected records")
    )
    prefixes = [
        "radar:v1:",
        "radar:v1:event",
        "radar:v1:event:",
        "radar:v1:event-head:",
        "radar:v1:decision:telegram:",
        "radar:v1:unknown:",
        *[
            "radar:v1:" + c.key[:length]
            for c, _, _ in values
            for length in (12, 22, len(c.key) - 1, len(c.key))
        ],
        *[head_key(c) for c, _, _ in values],
    ]
    for prefix in prefixes:
        selected = sorted(((k, v) for k, v in expected.items() if k.startswith(prefix)), reverse=reverse)
        assert encode(
            list(reader.scan(store.db, prefix, reverse=reverse, cache=store.cached_bytes))
        ) == encode(selected)
        assert reader.count(store.db, prefix) == len(selected)
        for limit in (0, 1, 3):
            assert encode(
                list(reader.scan(store.db, prefix, reverse=reverse, limit=limit, cache=store.cached_bytes))
            ) == encode(selected[:limit])
    assert list(reader.scan(store.db, "radar:v1:event:UPPERCASE")) == []
    with pytest.raises(ValueError):
        list(reader.scan(store.db, "radar:v1:", limit=True))


def test_indexed_uuid_revision_range_handles_boundaries_and_as_of_without_using_current_head(store):
    first, body, head = prepare(store)[0]
    activate(store)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        for revision in (2, 9, 10, 99, 100, 9999999999):
            c = Coordinate(first.kind, first.identity, revision)
            value = {**body, "revision": revision, "available_at_ms": revision * 10}
            projection = {**head, "revision": revision, "revision_key": c.key}
            record_catalog.append(
                store.db,
                [(c, value, projection)],
                bucket=130,
                owners={c: None},
                deadlines={c: 130 * 86400000},
                current=[c],
            )
    prefix = "radar:v1:event:" + first.identity + ":"
    assert [v["revision"] for _, v in reader.scan(store.db, prefix + "000000009")] == [99]
    assert [v["revision"] for _, v in reader.scan(store.db, prefix + "00000000")] == [1, 2, 9, 10, 99]
    assert reader.count(store.db, prefix + "9999999999") == 1
    assert reader.count(store.db, prefix + "0000000000") == 0
    assert reader.as_of(store.db, "event", first.identity, 999)["revision"] == 99
    assert reader.as_of(store.db, "event", first.identity, 20)["revision"] == 2
    # Revision1 deliberately has no availability field: never manufacture one.
    with pytest.raises(StateError, match="availability"):
        reader.as_of(store.db, "event", first.identity, 19)
    missing = str(uuid.UUID(int=100))
    assert reader.as_of(store.db, "event", missing, 100) is reader.MISSING
    with pytest.raises(ValueError):
        reader.as_of(store.db, "event", first.identity, True)
    statements = []
    store.db.set_trace_callback(statements.append)
    list(reader.scan(store.db, prefix + "000000009", limit=1))
    store.db.set_trace_callback(None)
    queries = [sql for sql in statements if sql.startswith("SELECT public_uuid,revision,packet,slot")]
    assert queries
    for sql in queries:
        plan = [row[3] for row in store.db.execute("EXPLAIN QUERY PLAN " + sql)]
        assert any("SEARCH market_records USING PRIMARY KEY" in detail for detail in plan)
        assert not any("TEMP B-TREE" in detail for detail in plan)


def test_inactive_scans_and_missing_namespace_counter_do_not_fabricate_results(store):
    prepare(store)
    assert list(reader.scan(store.db, "radar:v1:")) == []
    assert reader.count(store.db, "radar:v1:") == 0
    activate(store)
    with store.db:
        store.db.execute("DELETE FROM market_catalogue_counts WHERE kind=0")
    before = list(store.db.iterdump())
    with pytest.raises(ValueError, match="namespace count"):
        reader.count(store.db, "radar:v1:event:")
    assert list(store.db.iterdump()) == before


def test_uuid_prefix_nibbles_hyphens_and_maximum_value_keep_indexed_lexical_order(store):
    prepare(store)
    activate(store)
    originals = {}
    ids = [str(uuid.uuid5(uuid.NAMESPACE_URL, f"range/{index}")) for index in range(15)]
    ids += [str(uuid.UUID(int=2**128 - 1))]
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        rows = []
        for identity in ids:
            c = Coordinate("event", identity, 7)
            body = {"event_id": identity, "revision": 7, "value": -0.0}
            rows.append((c, body, None))
            originals["radar:v1:" + c.key] = body
        record_catalog.append(
            store.db,
            rows,
            bucket=130,
            owners={c: None for c, _, _ in rows},
            deadlines={c: 130 * 86400000 for c, _, _ in rows},
        )
    # Include the separately prepared event, which shares the same namespace.
    base = list(reader.scan(store.db, "radar:v1:event:"))
    for identity in ids:
        for length in (1, 2, 7, 8, 9, 13, 14, 23, 24, 36):
            prefix = "radar:v1:event:" + identity[:length]
            expected = [(k, v) for k, v in base if k.startswith(prefix)]
            assert encode(list(reader.scan(store.db, prefix))) == encode(expected)
            assert reader.count(store.db, prefix) == len(expected)
    assert all(encode(dict(base)[key]) == encode(value) for key, value in originals.items())
    for prefix in ("abc-", "a" * 37, "ffffffff:", "FFFFFFFF"):
        assert not list(reader.scan(store.db, "radar:v1:event:" + prefix))
    assert len(list(reader.scan(store.db, "radar:v1:", limit=1))) == 1
    # Internally reached limits close every namespace cursor.
    assert store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
