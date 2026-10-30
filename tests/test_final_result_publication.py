import json
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from cryptowatcher import catalogue_queue, record_catalog
from cryptowatcher.analytics import ComparisonWorker
from cryptowatcher.events import PREFIX, History
from cryptowatcher.expiry_index import _renew_hot, expires
from cryptowatcher.export import Selection, ndjson
from cryptowatcher.indexed_frame import FrameSizeError
from cryptowatcher.market_storage import read
from cryptowatcher.outcomes import OutcomeWorker
from cryptowatcher.record_blocks import Coordinate
from cryptowatcher.reset_history import private_state, signature
from cryptowatcher.sources import Sources
from cryptowatcher.storage import StateError, Store, encode
from cryptowatcher.storage_lifecycle import BoundedRetention
from tests.catalogue_fixture import move_events
from tests.test_bounded_storage import bounded
from tests.test_outcomes import MINUTE, NOW, future, observe


def prepared(store, *, cold=True):
    bounded(store)
    worker = OutcomeWorker(store)
    observe(store, worker)
    Sources(store).put("spot/BTCUSDT/1m", future(), recovery=True)
    if cold:
        move_events(store)
    else:
        record_catalog.install(store.db, store.cached_text, store.cached_bytes)
        with store.db:
            store.db.execute("INSERT INTO market_meta VALUES('record_catalogue_runtime','1')")
    catalogue_queue.install(store.db)
    with store.db:
        catalogue_queue.activate(store.db)
    worker.bootstrap(batch=100)
    return worker, History(store)


def completed(store, kind, *, cold=True):
    worker, history = prepared(store, cold=cold)
    worker.work(NOW + 6 * MINUTE, batch=100)
    if kind == "comparison":
        event = history.events(1)[0]
        comparison = ComparisonWorker(store)
        comparison.requeue(event["event_id"], NOW + 7 * MINUTE)
        comparison.work(NOW + 8 * MINUTE)
    values = history.items(kind + "-head:")
    value = next((v for _, v in values if v["state"] == "COMPLETE"), values[0][1])
    return history, value


@pytest.mark.parametrize("cold", [False, True])
def test_due_incomplete_results_publish_with_the_original_retry_and_source_state(store, cold):
    worker, history = prepared(store, cold=cold)
    settings = store.snapshot()
    private = signature(private_state(store))
    sources = read(store.db, "spot/BTCUSDT/1m", NOW, NOW + 5 * MINUTE)
    worker.work(NOW + 61 * MINUTE, batch=100)
    incomplete = [value for _, value in history.items("outcome-head:") if value["state"] == "INCOMPLETE"]
    assert incomplete
    for value in incomplete:
        assert value["observed_candles"] < value["expected_candles"]
        job = history.get("outcome-job:" + value["outcome_id"])
        assert job["state"] == "INCOMPLETE" and job["next_attempt_ms"] == NOW + 62 * MINUTE
        coordinate = Coordinate("outcome", value["outcome_id"], value["revision"])
        assert record_catalog.row(store.db, coordinate) is not None
        assert encode(history.get(coordinate.key)) == encode(value)
        assert value["missing_ranges"] and all(isinstance(gap, list) for gap in value["missing_ranges"])
    heads = history.items("outcome-head:")
    revisions = store.db.execute("SELECT count(*) FROM market_records").fetchone()[0]
    worker.work(NOW + 62 * MINUTE, batch=100)
    assert history.items("outcome-head:") == heads
    assert store.db.execute("SELECT count(*) FROM market_records").fetchone()[0] == revisions
    for value in incomplete:
        assert history.get("outcome-job:" + value["outcome_id"])["next_attempt_ms"] == NOW + 63 * MINUTE
    assert read(store.db, "spot/BTCUSDT/1m", NOW, NOW + 5 * MINUTE) == sources
    assert store.snapshot() == settings and signature(private_state(store)) == private
    assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("kind", ["outcome", "comparison"])
def test_final_results_keep_the_original_json_wire_for_nested_tuple_extensions(store, kind):
    history, old = completed(store, kind)
    original = {
        **deepcopy(old),
        "revision": old["revision"] + 1,
        "future_extension": {"ranges": ((-0.0, 1.0000000000000002), (True, None, "extra"))},
    }
    expected = json.loads(encode(original))
    with store.db:
        history.version(kind, original)
    coordinate = Coordinate(kind, original[kind + "_id"], original["revision"])
    assert record_catalog.row(store.db, coordinate) is not None
    assert encode(history.get(coordinate.key)) == encode(expected)
    assert encode(history.get(kind + "-head:" + coordinate.identity)) == encode(expected)
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        assert encode(History(reader).get(coordinate.key)) == encode(expected)


@pytest.mark.parametrize("kind", ["outcome", "comparison"])
@pytest.mark.parametrize("cold", [False, True])
def test_result_sources_heads_scans_reopen_and_frozen_parent_expiry(store, kind, cold):
    before_private = signature(private_state(store))
    settings = store.snapshot()
    history, value = completed(store, kind, cold=cold)
    coordinate = Coordinate(kind, value[kind + "_id"], value["revision"])
    assert record_catalog.row(store.db, coordinate) is not None
    for key in (coordinate.key, kind + "-head:" + coordinate.identity):
        assert store.db.execute("SELECT 1 FROM runtime WHERE key=?", (PREFIX + key,)).fetchone() is None
        assert encode(history.get(key)) == encode(value)
        assert expires(store.db, PREFIX + key)[0] == NOW + 30 * 86400000
    assert catalogue_queue.header(store.db)["count"] == 0
    if not cold:
        assert (
            store.db.execute(
                "SELECT history_published FROM market_record_parents WHERE public_uuid=?",
                (record_catalog.binary(value["event_id"]),),
            ).fetchone()[0]
            == 0
        )
        assert len(history.events(100)) == history.count("history:")
    assert history.count(kind + "-head:") == len(history.items(kind + "-head:"))
    assert dict(history.scan(kind + "-head:"))[kind + "-head:" + coordinate.identity] == value
    assert store.snapshot() == settings and signature(private_state(store)) == before_private
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        assert encode(History(reader).get(coordinate.key)) == encode(value)
        assert encode(History(reader).get(kind + "-head:" + coordinate.identity)) == encode(value)
        assert reader.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not reader.db.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("kind", ["outcome", "comparison"])
def test_failure_after_new_packet_restores_old_head_and_preserves_outer_transaction(store, monkeypatch, kind):
    history, old = completed(store, kind)
    value = {**deepcopy(old), "revision": old["revision"] + 1, "unknown_next": [-0.0, 1.0000000000000002]}
    append = record_catalog.append

    def fail_after_append(*args, **kwargs):
        append(*args, **kwargs)
        raise RuntimeError("injected after final append")

    monkeypatch.setattr(record_catalog, "append", fail_after_append)
    with store.db:
        history.put("caller-state", {"preserve": True})
        before = list(store.db.iterdump())
        with pytest.raises(RuntimeError, match="after final append"):
            history.version(kind, value)
        assert store.db.in_transaction
        assert list(store.db.iterdump()) == before
        assert history.get(kind + "-head:" + old[kind + "_id"]) == old
        with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
            assert History(reader).get("caller-state") is None
            assert History(reader).get(kind + "-head:" + old[kind + "_id"]) == old
    assert history.get("caller-state") == {"preserve": True}


@pytest.mark.parametrize("kind", ["outcome", "comparison"])
def test_cold_result_retry_conflict_snapshot_and_old_revision_never_replace_new_head(store, kind):
    history, old = completed(store, kind)
    key = kind + "-head:" + old[kind + "_id"]
    before = list(store.db.iterdump())
    with store.db:
        history.version(kind, old)
    assert list(store.db.iterdump()) == before
    with pytest.raises(StateError, match="Conflicting immutable"):
        with store.db:
            history.version(kind, {**deepcopy(old), "unknown_next": -0.0})
    assert list(store.db.iterdump()) == before
    newer = {
        **deepcopy(old),
        "revision": old["revision"] + 1,
        "available_at_ms": old["available_at_ms"] + 60000,
        "unknown_next": [-0.0, 1.0000000000000002],
    }
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        reader.db.execute("BEGIN")
        assert History(reader).get(key) == old
        with store.db:
            history.version(kind, newer)
        assert encode(History(reader).get(key)) == encode(old)
        reader.db.rollback()
        assert encode(History(reader).get(key)) == encode(newer)
    before = list(store.db.iterdump())
    with store.db:
        history.version(kind, old)
    assert list(store.db.iterdump()) == before
    with pytest.raises(StateError, match="Stale"):
        with store.db:
            history.put(key, History._head(kind, old, f"{kind}:{old[kind + '_id']}:{old['revision']:010d}"))
    assert list(store.db.iterdump()) == before


@pytest.mark.parametrize("kind", ["outcome", "comparison"])
def test_final_frame_fallback_rolls_back_append_before_using_original_hot_path(store, monkeypatch, kind):
    history, old = completed(store, kind)
    value = {**deepcopy(old), "revision": old["revision"] + 1, "unknown_next": [-0.0, 1.0000000000000002]}
    append = record_catalog.append

    def oversized_after_append(*args, **kwargs):
        append(*args, **kwargs)
        raise FrameSizeError("injected final frame bound")

    monkeypatch.setattr(record_catalog, "append", oversized_after_append)
    with store.db:
        history.version(kind, value)
    key = f"{kind}:{old[kind + '_id']}:{value['revision']:010d}"
    assert store.db.execute("SELECT 1 FROM runtime WHERE key=?", (PREFIX + key,)).fetchone()
    assert record_catalog.row(store.db, Coordinate(kind, old[kind + "_id"], value["revision"])) is None
    assert encode(history.get(key)) == encode(value)
    assert encode(history.get(kind + "-head:" + old[kind + "_id"])) == encode(value)
    assert history.get(f"{kind}:{old[kind + '_id']}:{old['revision']:010d}") == old
    assert catalogue_queue.header(store.db)["count"] == 2
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("cold", [False, True])
def test_outcome_callback_failure_restores_all_jobs_sources_counters_and_heads(store, cold):
    worker, history = prepared(store, cold=cold)
    before = list(store.db.iterdump())
    seen = []

    def failing_callback(value):
        key = "outcome-head:" + value["outcome_id"]
        assert history.get(key) == value
        assert store.db.execute("SELECT 1 FROM runtime WHERE key=?", (PREFIX + key,)).fetchone() is None
        seen.append(value["outcome_id"])
        raise RuntimeError("injected outcome consumer")

    worker.on_outcome = failing_callback
    with pytest.raises(RuntimeError, match="outcome consumer"):
        worker.work(NOW + 6 * MINUTE, batch=100)
    assert seen
    assert list(store.db.iterdump()) == before
    assert history.count("outcome-head:") == 0
    worker.on_outcome = None
    worker.work(NOW + 6 * MINUTE, batch=100)
    assert history.count("outcome-head:") > 0
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()


def test_oversized_first_result_does_not_leave_a_prepared_parent_or_change_any_source(store, monkeypatch):
    worker, history = prepared(store, cold=False)
    before_sources = read(store.db, "spot/BTCUSDT/1m", NOW, NOW + 5 * MINUTE)
    append = record_catalog.append

    def oversized_after_append(*args, **kwargs):
        append(*args, **kwargs)
        raise FrameSizeError("injected first result bound")

    monkeypatch.setattr(record_catalog, "append", oversized_after_append)
    worker.work(NOW + 6 * MINUTE, batch=100)
    assert store.db.execute("SELECT count(*) FROM market_record_parents").fetchone()[0] == 0
    assert history.count("outcome-head:") > 0
    assert read(store.db, "spot/BTCUSDT/1m", NOW, NOW + 5 * MINUTE) == before_sources
    for key, value in history.items("outcome-head:"):
        assert store.db.execute("SELECT 1 FROM runtime WHERE key=?", (PREFIX + key,)).fetchone()
        assert history.get(key) == value
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()


def test_conflicting_hot_and_prepared_parent_deadlines_reject_without_changing_the_record(store):
    history, old = completed(store, "outcome", cold=False)
    with store.db:
        _renew_hot(store.db, PREFIX + "event-head:" + old["event_id"], NOW + 31 * 86400000, shorten=False)
    before = list(store.db.iterdump())
    with pytest.raises(StateError, match="Conflicting frozen parent expiry"):
        with store.db:
            history.version("outcome", {**old, "revision": old["revision"] + 1})
    assert list(store.db.iterdump()) == before
    assert history.get("outcome-head:" + old["outcome_id"]) == old


@pytest.mark.parametrize("cold", [False, True])
def test_unknown_result_schema_preserves_original_hot_path_and_all_extra_values(store, cold):
    worker, history = prepared(store, cold=cold)
    worker.work(NOW + 6 * MINUTE, batch=100)
    old = history.items("outcome-head:")[0][1]
    value = {
        **deepcopy(old),
        "schema": "cryptowatcher.outcome.future",
        "revision": old["revision"] + 1,
        "unknown_next": [-0.0, 1.0000000000000002],
    }
    with store.db:
        history.version("outcome", value)
    key = f"outcome:{value['outcome_id']}:{value['revision']:010d}"
    assert store.db.execute("SELECT 1 FROM runtime WHERE key=?", (PREFIX + key,)).fetchone()
    assert encode(history.get(key)) == encode(value)
    assert encode(history.get("outcome-head:" + value["outcome_id"])) == encode(value)


@pytest.mark.parametrize("cold", [False, True])
def test_cold_result_expiry_does_not_extend_detail_retention_or_shorten_candle_retention(store, cold):
    history, _ = completed(store, "outcome", cold=cold)
    before = history.items("outcome-head:")
    deadline = NOW + 30 * 86400000
    retention = BoundedRetention(store)
    store.clock_ms = lambda: deadline - 1
    retention.work(deadline - 1, batch=100)
    assert history.items("outcome-head:") == before
    assert read(store.db, "spot/BTCUSDT/1m", NOW, NOW + 5 * MINUTE) == [asdict(c) for c in future()]
    shift = 29 * 86400000
    recent = [
        replace(c, start=c.start + shift, received_ms=c.received_ms + shift, server_ms=c.server_ms + shift)
        for c in future()
    ]
    Sources(store).put("spot/BTCUSDT/1m", recent, recovery=True)
    store.clock_ms = lambda: deadline + 1
    for _ in range(40):
        retention.work(deadline + 1, batch=100)
        if history.count("event-head:") == 0:
            break
    assert history.count("event-head:") == history.count("outcome-head:") == 0
    assert read(store.db, "spot/BTCUSDT/1m", NOW, NOW + 5 * MINUTE) == []
    assert read(store.db, "spot/BTCUSDT/1m", recent[0].start, recent[-1].end) == [asdict(c) for c in recent]
    assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("kind", ["outcome", "comparison"])
@pytest.mark.parametrize("damage", ["packet", "counter", "layout"])
def test_corruption_rejects_new_revision_without_hot_fallback_or_partial_write(store, kind, damage):
    history, old = completed(store, kind)
    coordinate = Coordinate(kind, old[kind + "_id"], old["revision"])
    # completed() has already read the old value: a warm cache cannot hide damage.
    with store.db:
        if damage == "packet":
            packet = record_catalog.row(store.db, coordinate)[0]
            arena, offset = store.db.execute(
                "SELECT arena,offset FROM market_packets WHERE id=?", (packet,)
            ).fetchone()
            with store.db.blobopen("market_packet_arenas", "payload", arena) as blob:
                blob.seek(offset)
                blob.write(b"bad!")
        elif damage == "counter":
            store.db.execute("DELETE FROM market_catalogue_index_counts WHERE name='owners'")
        else:
            store.db.execute("UPDATE market_meta SET value='unknown' WHERE key='record_catalogue_runtime'")
    before = list(store.db.iterdump())
    with pytest.raises(ValueError, match="checksum|counter|Unknown catalogue"), store.db:
        history.version(kind, {**deepcopy(old), "revision": old["revision"] + 1})
    assert list(store.db.iterdump()) == before


@pytest.mark.parametrize("cold", [False, True])
def test_export_restores_direct_results_and_old_revisions_from_a_pinned_snapshot(store, cold):
    history, old = completed(store, "comparison", cold=cold)
    original = {
        (kind, key): value for kind in ("outcome", "comparison") for key, value in history.items(kind + ":")
    }
    assert original
    before = list(store.db.iterdump())
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        stream = ndjson(reader, Selection())
        manifest = json.loads(next(stream))
        assert manifest["selected_events"] == history.count("event-head:")
        newer = {**deepcopy(old), "revision": old["revision"] + 1, "unknown_next": [-0.0]}
        with store.db:
            history.version("comparison", newer)
        rows = [json.loads(row) for row in stream]
        exported = {
            (
                row["type"],
                f"{row['type']}:{row['record'][row['type'] + '_id']}:{row['record']['revision']:010d}",
            ): row["record"]
            for row in rows
            if row["type"] in ("outcome", "comparison")
        }
        assert exported.keys() == original.keys()
        assert all(encode(exported[key]) == encode(value) for key, value in original.items())
        assert reader.db.in_transaction is False
        assert history.get("comparison-head:" + old["comparison_id"]) == newer
        fresh = [json.loads(row) for row in ndjson(reader, Selection())]
        assert any(row.get("record") == newer for row in fresh)
        assert reader.db.total_changes == 0
    assert list(store.db.iterdump()) != before  # Only the deliberate writer revision changed state.
