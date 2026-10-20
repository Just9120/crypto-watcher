import sqlite3
import time
import uuid
from contextlib import closing

import pytest

from cryptowatcher import (
    catalogue_compaction,
    packet_bundles,
    packet_store,
    record_blocks,
    record_catalog,
    record_relocation,
)
from cryptowatcher import parent_affinity as affinity
from cryptowatcher.market_storage import enable
from cryptowatcher.storage import StateError, Store, encode

DAY = 86_400_000


def prepare(store, *, install=True):
    enable(store.db)
    store.bounded = True
    record_catalog.install(store.db, store.cached_text, store.cached_bytes)
    packet_bundles.install(store.db)
    if install:
        affinity.install(store.db, store.cached_bytes)


def parent(
    store,
    index,
    *,
    revision=1,
    cohort=None,
    current=True,
    family="composite",
    direction="UP",
    timeframe="1m",
    settings_revision=2,
):
    if not store.db.in_transaction:
        store.db.execute("BEGIN IMMEDIATE")
    if cohort is None:
        cohort = index % 3
    identity = str(uuid.UUID(int=index + 1000))
    entries = []
    for position, kind in enumerate(
        ("event", "outcome", "outcome", "outcome", "outcome", "comparison", "decision")
    ):
        coordinate = record_blocks.Coordinate(
            kind,
            identity
            if kind in ("event", "decision")
            else str(uuid.UUID(int=(index + 1000) * 100 + position)),
            revision,
        )
        value = {
            coordinate.field: coordinate.identity,
            "event_id": identity,
            "revision": revision,
            "schema": "cryptowatcher." + kind + ".v1",
            "market_type": "spot",
            "symbol": f"COIN{cohort}USDT",
            "timeframe": timeframe,
            "detector_id": family,
            "direction": direction,
            "settings_revision": settings_revision,
            "metrics": {"zero": -0.0, "integer": 0, "other": float(index)},
            "unknown": [True, None, "точно"],
            "shared": "same native family and immutable public context " * 6,
        }
        head = (
            None
            if kind == "decision"
            else {
                coordinate.field: coordinate.identity,
                "revision": revision,
                "schema": record_blocks.HEAD_SCHEMA,
                "revision_key": coordinate.key,
                "symbol": value["symbol"],
            }
        )
        entries.append((coordinate, value, head))
    owner = record_catalog.parent(store.db, identity, 100 * DAY + index, 130 * DAY + index)
    record_catalog.append(
        store.db,
        entries,
        bucket=130,
        owners={entry[0]: owner for entry in entries},
        deadlines={entry[0]: None for entry in entries},
        current=[c for c, _, _ in entries] if current else [],
    )
    return owner, entries


def all_values(store):
    rows = []
    for kind, uid, revision in store.db.execute(
        "SELECT kind,public_uuid,revision FROM market_records ORDER BY kind,public_uuid,revision"
    ):
        coordinate = record_blocks.Coordinate(
            record_blocks.KINDS[kind], str(uuid.UUID(bytes=bytes(uid))), revision
        )
        captured = record_relocation.capture(store, coordinate)
        rows.append(
            (
                coordinate.key,
                encode(captured.value),
                encode(captured.projection),
                captured.expires,
                captured.binding[2:],
            )
        )
    return rows


def tick(store, state, batch=64):
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        affinity.advance(store.db, state, batch, time.monotonic() + 5, store.cached_bytes)


def test_indexed_cohort_prefers_whole_peers_beyond_old_four_neighbors_and_preserves_every_record(store):
    prepare(store)
    with store.db:
        roots = [parent(store, index) for index in range(27)]
    before = all_values(store)
    seed = record_catalog.row(store.db, roots[0][1][0][0])[0]
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        moved, fallbacks = catalogue_compaction.compact(store, seed)
        packet_store.collect(store.db)
    assert moved == 63 and fallbacks == 0 and all_values(store) == before
    packets = {record_catalog.row(store.db, roots[index][1][0][0])[0] for index in range(0, 27, 3)}
    assert len(packets) == 1 and seed not in packets
    for index in range(27):
        assert len({record_catalog.row(store.db, entry[0])[0] for entry in roots[index][1]}) == 1
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()
    assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        assert all_values(reader) == before


def test_hash_collision_cannot_substitute_for_actual_native_cohort(store, monkeypatch):
    prepare(store)
    monkeypatch.setattr(affinity, "fingerprint", lambda _: b"x" * 8)
    with store.db:
        roots = [parent(store, index) for index in range(27)]
    expected = all_values(store)
    seed = record_catalog.row(store.db, roots[0][1][0][0])[0]
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        assert catalogue_compaction.compact(store, seed)[0] == 63
    selected = record_catalog.row(store.db, roots[0][1][0][0])[0]
    owners = [
        row[0]
        for row in store.db.execute("SELECT parent FROM market_record_owners WHERE packet=?", (selected,))
    ]
    assert set(owners) == {roots[index][0] for index in range(0, 27, 3)}
    assert all_values(store) == expected


def test_source_group_includes_families_but_never_other_tf_settings_or_instrument(store):
    from cryptowatcher.detector_config import FAMILIES

    prepare(store)
    with store.db:
        roots = [
            parent(
                store, index, cohort=0, family=FAMILIES[index % 5], direction="DOWN" if index % 2 else "UP"
            )
            for index in range(12)
        ]
        foreign = [
            parent(store, 20, cohort=0, timeframe="5m"),
            parent(store, 21, cohort=0, settings_revision=3),
            parent(store, 22, cohort=1),
        ]
    before = all_values(store)
    seed = record_catalog.row(store.db, roots[0][1][0][0])[0]
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        assert catalogue_compaction.compact(store, seed) == (63, 0)
    packet = record_catalog.row(store.db, roots[0][1][0][0])[0]
    selected = {
        row[0]
        for row in store.db.execute("SELECT parent FROM market_record_owners WHERE packet=?", (packet,))
    }
    assert len(selected) == 9 and selected <= {root[0] for root in roots}
    assert not selected.intersection(root[0] for root in foreign)
    assert len({root[1][0][1]["detector_id"] for root in roots if root[0] in selected}) > 1
    assert all_values(store) == before


def episode(store, identity, revision, day=130):
    if not store.db.in_transaction:
        store.db.execute("BEGIN IMMEDIATE")
    coordinate = record_blocks.Coordinate("episode", identity, revision)
    value = {
        "episode_id": identity,
        "revision": revision,
        "schema": "cryptowatcher.episode.v1",
        "instrument": "spot/COIN0USDT",
        "direction": "UP",
        "conditions": ["same immutable history" * 15],
        "at": revision,
        "metrics": {"zero": -0.0},
    }
    record_catalog.append(
        store.db,
        [(coordinate, value, None)],
        bucket=day,
        owners={coordinate: None},
        deadlines={coordinate: day * DAY},
        current=[],
    )
    return coordinate


@pytest.mark.parametrize("rollback", [False, True])
def test_episode_revisions_beyond_neighbors_regroup_with_exact_days_and_rollback(
    store, monkeypatch, rollback
):
    prepare(store)
    identity = str(uuid.uuid4())
    with store.db:
        coordinates = []
        for revision in range(1, 19):
            coordinates.append(episode(store, identity, revision))
            parent(store, revision)
        other_day = episode(store, identity, 19, 131)
    before = all_values(store)
    before_sql = list(store.db.iterdump())
    seed = record_catalog.row(store.db, coordinates[0])[0]
    original = record_relocation.relocate

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("after episode publication")

    if rollback:
        monkeypatch.setattr(record_relocation, "relocate", fail)
        with pytest.raises(RuntimeError, match="episode publication"), store.db:
            store.db.execute("BEGIN IMMEDIATE")
            catalogue_compaction.compact(store, seed)
        assert list(store.db.iterdump()) == before_sql
    else:
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            assert catalogue_compaction.compact(store, seed)[0] >= 18
        packets = {record_catalog.row(store.db, coordinate)[0] for coordinate in coordinates}
        assert len(packets) == 1 and record_catalog.row(store.db, other_day)[0] not in packets
    assert all_values(store) == before
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()


def test_episode_with_more_than64_revisions_uses_bounded_visible_fallback(store):
    prepare(store)
    identity = str(uuid.uuid4())
    with store.db:
        coordinates = [episode(store, identity, revision) for revision in range(1, 70)]
    before = all_values(store)
    seed = record_catalog.row(store.db, coordinates[0])[0]
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        moved, fallbacks = catalogue_compaction.compact(store, seed)
    assert moved == 0 and fallbacks == 1
    assert all_values(store) == before


def test_episode_that_cannot_fit_remaining_packet_is_examined_only_once(store, monkeypatch):
    prepare(store)
    identity = str(uuid.uuid4())
    with store.db:
        _, entries = parent(store, 0)
        # A complete valid episode is in the next packet, but cannot fit after
        # the oldest seed. Repeatedly capturing it per slot would multiply the
        # bounded64-record operation into thousands of reads in one transaction.
        revisions = []
        for revision in range(1, 61):
            coordinate = record_blocks.Coordinate("episode", identity, revision)
            revisions.append((coordinate, {"episode_id": identity, "revision": revision}, None))
        record_catalog.append(
            store.db,
            revisions,
            bucket=130,
            owners={row[0]: None for row in revisions},
            deadlines={row[0]: 130 * DAY for row in revisions},
            current=[],
        )
    before = all_values(store)
    calls = []
    original = catalogue_compaction._episode

    def examine(*args):
        calls.append(args[1].coordinate.identity)
        return original(*args)

    monkeypatch.setattr(catalogue_compaction, "_episode", examine)
    seed = record_catalog.row(store.db, entries[0][0])[0]
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        catalogue_compaction.compact(store, seed)
    assert calls == [identity]
    assert all_values(store) == before


def test_episode_extension_with_event_owner_keeps_its_distinct_retention_authority(store):
    prepare(store)
    identity = str(uuid.uuid4())
    with store.db:
        first = episode(store, identity, 1)
        owner, entries = parent(store, 0)
        coordinate = record_blocks.Coordinate("episode", identity, 2)
        value = {
            "episode_id": identity,
            "revision": 2,
            "event_id": entries[0][0].identity,
            "unknown_extension": "retained",
        }
        record_catalog.append(
            store.db,
            [(coordinate, value, None)],
            bucket=130,
            owners={coordinate: owner},
            deadlines={coordinate: None},
            current=[],
        )
    before = all_values(store)
    seed = record_catalog.row(store.db, first)[0]
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        catalogue_compaction.compact(store, seed)
    assert all_values(store) == before
    assert record_catalog.row(store.db, first)[2] is None
    assert record_catalog.row(store.db, coordinate)[2] == owner


def test_fixed_packet_upper_prevents_new_peers_from_extending_original_work(store):
    prepare(store)
    with store.db:
        roots = [parent(store, index, cohort=0) for index in range(10)]
    first = record_catalog.row(store.db, roots[0][1][0][0])[0]
    assert affinity.candidates(store.db, roots[0][0], first) == []
    before = all_values(store)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        assert catalogue_compaction.compact(store, first, upper=first) == (0, 0)
    assert all_values(store) == before


def test_hint_publication_rollback_older_revision_and_invalid_sql_changes(store):
    prepare(store)
    with store.db:
        owner, newer = parent(store, 0, revision=3, cohort=2)
    assert (
        store.db.execute("SELECT revision FROM market_parent_affinity WHERE parent=?", (owner,)).fetchone()[0]
        == 3
    )
    with pytest.raises(RuntimeError), store.db:
        parent(store, 0, revision=1, cohort=1, current=False)
        raise RuntimeError("abort older revision")
    assert (
        store.db.execute("SELECT revision FROM market_parent_affinity WHERE parent=?", (owner,)).fetchone()[0]
        == 3
    )
    with store.db:
        _, older = parent(store, 0, revision=1, cohort=1, current=False)
    row = store.db.execute("SELECT * FROM market_parent_affinity WHERE parent=?", (owner,)).fetchone()
    assert tuple(row) == (owner, 1, 130, affinity.fingerprint(affinity.native(older[0][1])))
    for sql in (
        "UPDATE market_parent_affinity SET fingerprint=zeroblob(8)",
        "UPDATE market_parent_affinity SET day=day+1",
        "UPDATE market_parent_affinity SET revision=3",
    ):
        with pytest.raises(sqlite3.IntegrityError), store.db:
            store.db.execute(sql)
    assert record_catalog.head(store.db, "event", newer[0][0].identity)[0].revision == 3


def test_last_parent_expiry_cascades_hint_and_counter_without_losing_outer_rollback(store):
    prepare(store)
    with store.db:
        owner, entries = parent(store, 0)
    assert tuple(
        store.db.execute(
            "SELECT records,logical_bytes FROM market_counts WHERE category='parent_affinity'"
        ).fetchone()
    ) == (1, 32)
    with pytest.raises(RuntimeError), store.db:
        store.db.execute("DELETE FROM market_records")
        store.db.execute("DELETE FROM market_record_parents WHERE id=?", (owner,))
        raise RuntimeError("abort collection")
    assert record_catalog.read(store.db, entries[0][0]) is not None
    with store.db:
        store.db.execute("DELETE FROM market_records")
        store.db.execute("DELETE FROM market_record_parents WHERE id=?", (owner,))
    assert tuple(
        store.db.execute(
            "SELECT records,logical_bytes,inserted_bytes,deleted_bytes FROM market_counts WHERE category='parent_affinity'"
        ).fetchone()
    ) == (0, 0, 32, 32)


def test_finite_existing_parent_and_retag_boundaries_survive_new_arrivals(store):
    prepare(store, install=False)
    with store.db:
        roots = [parent(store, index) for index in range(8)]
        for (identity,) in store.db.execute("SELECT id FROM market_packets").fetchall():
            packet_bundles.checked(store.db, identity)
    affinity.install(store.db)
    state = {}
    tick(store, state, batch=1)
    upper, packet_upper = state["upper"], state["packet_upper"]
    with store.db:
        late, _ = parent(store, 9)
    assert (
        late > upper
        and store.db.execute("SELECT 1 FROM market_parent_affinity WHERE parent=?", (late,)).fetchone()
    )
    while not state.get("initial_complete"):
        tick(store, state, batch=1)
    assert state["upper"] == upper and state["packet_upper"] == packet_upper
    assert state["examined"] == len(roots) and state["retagged"] == 8
    assert packet_bundles.header(store.db)["count"] == 9
    plan = " ".join(
        row[3]
        for row in store.db.execute(
            "EXPLAIN QUERY PLAN SELECT parent FROM market_parent_affinity WHERE day=130 AND fingerprint=x'0000000000000000' AND parent>1 ORDER BY parent LIMIT 32"
        )
    )
    assert "SEARCH" in plan and "TEMP B-TREE" not in plan


@pytest.mark.parametrize("damage", ["marker", "index", "trigger", "counter"])
def test_damaged_layout_cannot_be_reinstalled_or_opened_as_a_valid_hint_index(store, damage):
    prepare(store)
    with store.db:
        if damage == "marker":
            store.db.execute("UPDATE market_meta SET value='999' WHERE key='parent_affinity'")
        elif damage == "index":
            store.db.execute("DROP INDEX market_parent_affinity_cohort")
        elif damage == "trigger":
            store.db.execute("DROP TRIGGER market_parent_affinity_update")
        else:
            store.db.execute("DELETE FROM market_counts WHERE category='parent_affinity'")
    before = list(store.db.iterdump())
    with pytest.raises(StateError, match="parent affinity"):
        affinity.install(store.db)
    assert list(store.db.iterdump()) == before
    with pytest.raises(StateError, match="parent affinity"):
        Store(store.path, store.owner_id, readonly=True)


@pytest.mark.parametrize(
    "state",
    [
        {"cursor": 0},
        {"version": 2},
        {"upper": 1, "cursor": 2, "packet_upper": 1, "packet_cursor": 0},
        {"upper": 1, "cursor": 0, "packet_upper": 1, "packet_cursor": 1, "initial_complete": True},
    ],
)
def test_unknown_or_incomplete_checkpoint_never_changes_source(store, state):
    prepare(store)
    before = list(store.db.iterdump())
    with pytest.raises(StateError):
        tick(store, state)
    assert list(store.db.iterdump()) == before
