import copy
import random
import string
import time
from contextlib import closing

import pytest

from cryptowatcher import catalogue_transfer, record_catalog
from cryptowatcher.analytics import cohorts
from cryptowatcher.events import DECISION_HEAD, PREFIX, History
from cryptowatcher.outcomes import OutcomeWorker
from cryptowatcher.sources import Sources
from cryptowatcher.storage import RUNTIME_UPSERT, StateError, Store, encode
from tests.test_bounded_storage import bounded
from tests.test_outcomes import MINUTE, NOW, future, observe


@pytest.mark.parametrize("fail_second", [False, True])
def test_dense_legal_group_splits_at_compressed_bound_and_rolls_back_all_children(
    store, monkeypatch, fail_second
):
    from cryptowatcher import record_blocks
    from cryptowatcher.events import identity
    from cryptowatcher.indexed_frame import FrameSizeError
    from tests.test_quality_packets import metric

    populated(store)
    history = History(store)
    event = history.events(1)[0]
    rng = random.Random(318)
    prepared = []
    expected = []
    with store.db:
        for index in range(2):
            uid = identity("compressed-frame-split/" + str(index))
            value = {
                "schema": "cryptowatcher.outcome.v1",
                "outcome_id": uid,
                "revision": 1,
                "event_id": event["event_id"],
                "event_revision": 1,
                "methodology_version": "1",
                "horizon_min": 5,
                "state": "COMPLETE",
                "classification": "CONTINUATION",
                "metrics": metric(index + 7),
                "noise_threshold_pct": 0.1,
                "expected_candles": 5,
                "observed_candles": 5,
                "opaque_future_field": "".join(rng.choices(string.ascii_letters + string.digits, k=400000)),
            }
            coordinate = record_blocks.Coordinate("outcome", uid, 1)
            history.put(coordinate.key, value)
            raw = store.db.execute(
                "SELECT payload_json FROM runtime WHERE key=?", (PREFIX + coordinate.key,)
            ).fetchone()[0]
            entry = catalogue_transfer.prepare(store, PREFIX + coordinate.key, raw, {})
            assert entry is not None
            assert (
                len(encode(entry.body).encode()) + len(encode(entry.projection).encode()) + 128
                < catalogue_transfer.MAX_RECORD
            )
            prepared.append(entry)
            expected.append(value)
    group = [(e.coordinate, e.body, e.projection) for e in prepared]
    with pytest.raises(FrameSizeError):
        record_blocks.pack(group)
    before = list(store.db.iterdump())
    original = record_catalog.append
    calls = []

    def fault(db, entries, **kwargs):
        calls.append(len(entries))
        if fail_second and calls == [2, 1, 1]:
            raise StateError("injected second split publication failure")
        return original(db, entries, **kwargs)

    monkeypatch.setattr(record_catalog, "append", fault)
    state = {}
    if fail_second:
        with pytest.raises(StateError, match="second split"), store.db:
            store.db.execute("BEGIN IMMEDIATE")
            catalogue_transfer._flush(store, prepared, state)
        assert list(store.db.iterdump()) == before
    else:
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            catalogue_transfer._flush(store, prepared, state)
        assert state["bodies_moved"] == 2
        for entry, value in zip(prepared, expected, strict=True):
            assert encode(history.get(entry.coordinate.key)) == encode(value)
            assert (
                store.db.execute(
                    "SELECT 1 FROM runtime WHERE key=?", (PREFIX + entry.coordinate.key,)
                ).fetchone()
                is None
            )
        assert len({record_catalog.row(store.db, e.coordinate)[0] for e in prepared}) == 2
    assert calls == [2, 1, 1]


def populated(store):
    bounded(store)
    worker = OutcomeWorker(store)
    observe(store, worker)
    Sources(store).put("spot/BTCUSDT/1m", future())
    worker.work(NOW + 6 * MINUTE)
    history = History(store)
    event = history.events(1)[0]
    with store.db:
        history.put(
            f"decision:telegram:{event['event_id']}:0000000001",
            {
                "schema": "cryptowatcher.delivery-decision.v1",
                "event_id": event["event_id"],
                "revision": 1,
                "state": "RECORDED",
                "at_ms": NOW,
                "unknown": -0.0,
            },
        )
        history.put("decision:telegram:" + event["event_id"], {DECISION_HEAD: 1})
    expected = [(key, encode(value)) for key, value in history.scan("")]
    expected_cohorts = cohorts(store)
    record_catalog.install(store.db, store.cached_text, store.cached_bytes)
    with store.db:
        store.db.execute("INSERT INTO market_meta VALUES('record_catalogue_runtime','1')")
    return expected, expected_cohorts


def tick(store, state, batch=7):
    with store.db:
        if not store.db.in_transaction:
            store.db.execute("BEGIN IMMEDIATE")
        done = catalogue_transfer.advance(store, state, batch, time.monotonic() + 0.03)
        store.db.execute(RUNTIME_UPSERT, ("catalogue_transfer_test", encode(state)))
        return done


@pytest.mark.parametrize("rebuild", [False, True])
def test_finite_transfer_preserves_every_public_service_revision_cohort_and_reopens_after_vacuum(
    store, rebuild
):
    expected, expected_cohorts = populated(store)
    state = {}
    rebuilt = False
    for index in range(500):
        if tick(store, state):
            break
        if rebuild and not rebuilt and state.get("bodies_moved", 0) > 0:
            store.db.execute("CREATE TABLE unrelated_transfer_fixture(value INTEGER)")
            store.db.execute("VACUUM")
            with closing(Store(store.path, store.owner_id)) as reopened:
                resumed = reopened.get_runtime("catalogue_transfer_test")
                assert resumed == state
                tick(reopened, resumed)
                state = resumed
            rebuilt = True
    assert state["phase"] == "complete"
    assert state["bodies_moved"] > 0 and state["aliases_moved"] > 0
    assert rebuilt is rebuild
    assert [(key, encode(value)) for key, value in History(store).scan("")] == expected
    assert cohorts(store) == expected_cohorts
    before = list(store.db.iterdump())
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        assert catalogue_transfer.advance(store, copy.deepcopy(state), 7, time.monotonic() + 1)
    assert list(store.db.iterdump()) == before
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        assert [(key, encode(value)) for key, value in History(reader).scan("")] == expected
        assert cohorts(reader) == expected_cohorts
        assert not reader.db.execute("PRAGMA foreign_key_check").fetchall()


def test_invalid_checkpoint_and_mid_transfer_failure_leave_state_records_and_ownership_unchanged(
    store, monkeypatch
):
    populated(store)
    before = list(store.db.iterdump())
    for state in (
        {"version": True},
        {"version": 2},
        {"bodies_next": 1},
        {"phase": "complete"},
        {"phase": "future"},
        {"fallback_visits": []},
    ):
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            with pytest.raises(StateError):
                catalogue_transfer.advance(store, state, 7, time.monotonic() + 1)
        assert list(store.db.iterdump()) == before
    original = record_catalog.append

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected transfer failure")

    monkeypatch.setattr(record_catalog, "append", fail)
    state = {}
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        with pytest.raises(RuntimeError, match="transfer failure"):
            catalogue_transfer.advance(store, state, 256, time.monotonic() + 10)
        assert state == {}
        assert list(store.db.iterdump()) == before


def test_large_expanded_body_remains_hot_without_data_cuts(store):
    populated(store)
    history = History(store)
    old = history.events(1)[0]
    coordinate = f"event:{old['event_id']}:0000000009"
    body = {**old, "revision": 9, "unknown_large": "preserve" * 100000}
    # Reconstruct a supported old fixture directly; new immutable writer behavior
    # is tested separately. The compact pass must not allocate this expansion.
    from cryptowatcher.record_codec import pack_binary
    from cryptowatcher.storage_lifecycle import track

    with store.db:
        store.db.execute(RUNTIME_UPSERT, (PREFIX + coordinate, pack_binary(encode(body))))
        track(store, PREFIX + coordinate, body)
    state = {}
    for _ in range(500):
        if tick(store, state, 32):
            break
    assert state["phase"] == "complete"
    assert state["fallback_visits"]["oversized_expansion"] >= 1
    assert history.get(coordinate) == body
    assert store.db.execute("SELECT 1 FROM runtime WHERE key=?", (PREFIX + coordinate,)).fetchone()
    assert not store.db.execute("SELECT 1 FROM market_records WHERE revision=9").fetchone()


def test_largest_sqlite_rowid_has_a_finite_exclusive_successor_without_overflow(store):
    expected, _ = populated(store)
    with store.db:
        key = next(key for key, _ in expected if key.startswith("event:"))
        store.db.execute("UPDATE runtime SET rowid=? WHERE key=?", (-(2**63), PREFIX + key))
        store.db.execute(
            "INSERT INTO runtime(rowid,key,payload_json) VALUES(?,?,?)",
            (2**63 - 1, "unrelated:max-rowid", "{}"),
        )
    state = {}
    for _ in range(500):
        if tick(store, state, 32):
            break
    assert state["phase"] == "complete"
    catalogue_transfer.validate(state)
    assert state["bodies_next"] == 2**63
    assert [(key, encode(value)) for key, value in History(store).scan("")] == expected


def test_new_admissions_do_not_extend_the_initial_ceiling_and_the_next_finite_cycle_compacts_them(store):
    populated(store)
    history = History(store)
    old_events = {value["event_id"] for _, value in history.items("event-head:")}
    state = {}
    assert not tick(store, state, 1)
    ceiling = state["bodies_upper"]
    worker = OutcomeWorker(store)
    observe(store, worker, NOW + 10 * MINUTE)
    new_events = {value["event_id"] for _, value in history.items("event-head:")} - old_events
    assert new_events
    expected = [(key, encode(value)) for key, value in history.scan("")]
    for _ in range(500):
        if tick(store, state, 7):
            break
    assert state["phase"] == "complete" and state["bodies_upper"] == ceiling
    assert [(key, encode(value)) for key, value in history.scan("")] == expected
    assert all(
        not store.db.execute(
            "SELECT 1 FROM market_records WHERE kind=0 AND public_uuid=?", (record_catalog.binary(event),)
        ).fetchone()
        for event in new_events
    )
    next_cycle = {}
    for _ in range(500):
        if tick(store, next_cycle, 7):
            break
    assert next_cycle["phase"] == "complete"
    assert all(
        store.db.execute(
            "SELECT 1 FROM market_records WHERE kind=0 AND public_uuid=?", (record_catalog.binary(event),)
        ).fetchone()
        for event in new_events
    )
    assert [(key, encode(value)) for key, value in history.scan("")] == expected
