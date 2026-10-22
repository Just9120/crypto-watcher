import asyncio
import sqlite3
import threading
import time
from contextlib import closing

import pytest

from cryptowatcher import domain_packets, packet_bundles, packet_store
from cryptowatcher import shared_record_work as work
from cryptowatcher import shared_records as shared
from cryptowatcher.storage import Store
from tests.test_shared_records import equal, prepare, setup

NOW = 1_800_000_000_000


def ready(store):
    originals = setup(store)
    packet_bundles.install(store.db)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        for identity, _ in originals:
            packet_bundles.checked(store.db, identity)
    store.clock_ms = lambda: NOW
    store.bounded = True
    work.install(store.db, NOW, store.cached_bytes)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        work.scan(store.db, NOW)
    return originals


def test_staged_group_keeps_old_packets_and_resumes_one_atomic_transition_after_reopen(store):
    originals = ready(store)
    selected = prepare(store, originals)
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        reader.db.execute("BEGIN")
        assert packet_store.read(reader.db, originals[0][0]) == originals[0][1]
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            group = work.stage(store.db, selected)
            assert group is not None
            assert shared.collect(store.db) == 0
        for identity, old in originals:
            assert packet_store.read(store.db, identity) == old
        assert work.header(store.db)["pending"] == 2 and not work.header(store.db)["initial_complete"]
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            assert work.advance(store.db, store.cached_bytes) == originals[0][0]
        equal(originals[0][1], domain_packets.read(store.db, originals[0][0], store.cached_bytes))
        assert packet_store.read(store.db, originals[1][0]) == originals[1][1]
        assert packet_store.read(reader.db, originals[0][0]) == originals[0][1]
    with closing(Store(store.path, store.owner_id)) as reopened:
        shared.validate(reopened.db)
        work.install(reopened.db, NOW, reopened.cached_bytes)
        with reopened.db:
            reopened.db.execute("BEGIN IMMEDIATE")
            assert work.advance(reopened.db, reopened.cached_bytes) == originals[1][0]
        state = work.header(reopened.db)
        assert state["initial_complete"] and state["pending"] == state["queued"] == 0
        assert not reopened.db.execute("SELECT 1 FROM market_shared_staging").fetchone()
        for identity, old in originals:
            equal(old, domain_packets.read(reopened.db, identity))
        assert not reopened.db.execute("PRAGMA foreign_key_check").fetchall()
        assert reopened.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize(
    "target", ["AFTER INSERT ON market_shared_pending", "AFTER UPDATE OF arena ON market_packets"]
)
def test_staged_failure_rolls_back_only_this_step_and_preserves_resume_state(store, target):
    originals = ready(store)
    selected = prepare(store, originals)
    if "UPDATE" in target:
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            work.stage(store.db, selected)
    with store.db:
        store.db.execute(f"CREATE TRIGGER fail_step {target} BEGIN SELECT RAISE(ABORT,'step failed'); END")
    before = list(store.db.iterdump())
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.IntegrityError, match="step failed"):
            if "UPDATE" in target:
                work.advance(store.db)
            else:
                work.stage(store.db, selected)
        assert list(store.db.iterdump()) == before


def test_pending_gc_and_stale_capture_keep_live_sources_and_current_counters(store):
    originals = ready(store)
    selected = prepare(store, originals)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        packet_store.retain(store.db, originals[0][0])
        before = list(store.db.iterdump())
        assert work.stage(store.db, selected) is None
        assert list(store.db.iterdump()) == before
    selected = prepare(store, originals)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        work.stage(store.db, selected)
        packet_store.release(store.db, originals[1][0])
        packet_store.collect(store.db)
        assert work.header(store.db)["pending"] == 1
        assert shared.collect(store.db) == 0
        work.advance(store.db)
        assert work.header(store.db)["pending"] == 0
        equal(originals[0][1], domain_packets.read(store.db, originals[0][0]))


def test_initial_prefix_live_inbox_wait_and_unknown_upgrade_guard(store):
    originals = ready(store)
    with store.db:
        store.db.execute("BEGIN")
        assert work.select(store.db, NOW) == [r[0] for r in originals]
    selected = prepare(store, originals)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        work.stage(store.db, selected)
        while work.advance(store.db) is not None:
            pass
        identity = packet_store.append(store.db, "detail_packets", 130, originals[0][1])
        packet_store.retain(store.db, identity)
        packet_bundles.checked(store.db, identity)
        assert work.select(store.db, NOW + work.COALESCE_MS - 1) == []
        assert work.select(store.db, NOW + work.COALESCE_MS) == [identity]
    with store.db:
        store.db.execute("DROP TRIGGER market_shared_queue_keep")
    before = list(store.db.iterdump())
    with pytest.raises(ValueError, match="guard"):
        work.install(store.db, NOW)
    assert list(store.db.iterdump()) == before


def test_upgrade_starts_existing_sparse_group_reclaim_clock_and_counter(store):
    originals = setup(store)
    packet_bundles.install(store.db)
    selected = prepare(store, originals)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        group = shared.publish(store.db, selected, store.cached_bytes)
        packet_store.release(store.db, originals[0][0])
        packet_store.collect(store.db)
    store.clock_ms = lambda: NOW
    work.install(store.db, NOW, store.cached_bytes)
    assert tuple(store.db.execute("SELECT group_id,kind,since FROM market_shared_reclaim").fetchone()) == (
        group,
        "sparse",
        NOW,
    )
    assert tuple(
        store.db.execute(
            "SELECT records,logical_bytes FROM market_counts WHERE category=?", (work.RECLAIM,)
        ).fetchone()
    ) == (1, 24)
    state = work.status(store.db, NOW + work.SERVICE_MS + 1)
    assert state["sparse_pending"] and state["pending_work"]
    assert not state["checkpoint_unknown"]
    equal(originals[1][1], domain_packets.read(store.db, originals[1][0], store.cached_bytes))


@pytest.mark.asyncio
async def test_worker_prepares_readonly_while_writer_stays_available_and_stages_only_on_next_step(
    store, monkeypatch
):
    from cryptowatcher import shared_record_worker as worker

    originals = ready(store)
    entered, release = threading.Event(), threading.Event()
    original = worker.read_batch
    calls = []

    def blocked(*args):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return original(*args)

    monkeypatch.setattr(worker, "read_batch", blocked)
    instance = worker.Worker(store)
    try:
        assert (await instance.step(lambda: NOW))["state"] == "PREPARING"
        assert await asyncio.to_thread(entered.wait, 2)
        task = instance.preparing
        assert task is not None and not task.done()
        store.put_runtime("heartbeat", {"at": 123})
        assert (await instance.step(lambda: NOW + 1))["state"] == "PREPARING"
        assert instance.preparing is task and len(calls) == 1
        for identity, old in originals:
            assert packet_store.read(store.db, identity) == old
        release.set()
        await asyncio.wait_for(asyncio.shield(task), 3)
        assert not store.db.execute("SELECT 1 FROM market_shared_staging").fetchone()
        assert (await instance.step(lambda: NOW + 2))["state"] == "STAGED"
        assert work.header(store.db)["pending"] == 2
        assert (await instance.step(lambda: NOW + 3))["state"] == "ADVANCED"
        assert work.header(store.db)["pending"] == 1
        assert (await instance.step(lambda: NOW + 4))["state"] == "ADVANCED"
        assert work.header(store.db)["pending"] == 0
        for identity, old in originals:
            equal(old, domain_packets.read(store.db, identity))
        assert store.get_runtime("heartbeat") == {"at": 123}
    finally:
        release.set()
        await instance.close()


@pytest.mark.asyncio
async def test_worker_shutdown_joins_actual_read_after_cancel_and_never_publishes_late(store, monkeypatch):
    from cryptowatcher import shared_record_worker as worker

    originals = ready(store)
    entered, release = threading.Event(), threading.Event()
    original = worker.read_batch

    def blocked(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    monkeypatch.setattr(worker, "read_batch", blocked)
    instance = worker.Worker(store)
    closing_task = None
    try:
        await instance.step(lambda: NOW)
        assert await asyncio.to_thread(entered.wait, 2)
        actual = instance.preparing
        closing_task = asyncio.create_task(instance.close())
        await asyncio.sleep(0)
        closing_task.cancel()
        await asyncio.sleep(0)
        assert not closing_task.done() and instance.preparing is actual and not actual.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing_task
        assert instance.preparing is None and actual.done()
        assert not store.db.execute("SELECT 1 FROM market_shared_staging").fetchone()
        for identity, old in originals:
            assert packet_store.read(store.db, identity) == old
    finally:
        release.set()
        await instance.close()
        if closing_task is not None and not closing_task.done():
            await asyncio.gather(closing_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_worker_failure_and_disabled_writer_leave_sources_and_inbox_visible(store, monkeypatch):
    from cryptowatcher import shared_record_worker as worker

    originals = ready(store)
    instance = worker.Worker(store)

    def failed(*args):
        raise ValueError("controlled preparation failure")

    monkeypatch.setattr(worker, "read_batch", failed)
    try:
        await instance.step(lambda: NOW)
        task = instance.preparing
        with pytest.raises(ValueError):
            await task
        with pytest.raises(ValueError):
            await instance.step(lambda: NOW + 1)
        assert store.get_runtime("shared_record_worker")["state"] == "FAILED"
        assert work.header(store.db)["queued"] == 2
        with store.db:
            store.db.execute("UPDATE market_meta SET value='0' WHERE key='shared_record_writer'")
        assert (await instance.step(lambda: NOW + 2))["state"] == "WRITER_DISABLED"
        assert work.header(store.db)["queued"] == 2
        for identity, old in originals:
            assert packet_store.read(store.db, identity) == old
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_sparse_group_reclaim_preserves_complete_public_history_and_dependency_leases(store):
    from cryptowatcher import record_relocation
    from cryptowatcher.shared_record_worker import Worker
    from tests.test_record_relocation import fragmented, public, references

    records = fragmented(store)
    expected, leases = public(store), references(store)
    packet_bundles.install(store.db)
    identities = sorted({r.binding[0] for r in records})
    shared.install(store.db, store.cached_bytes)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        for identity in identities:
            packet_bundles.checked(store.db, identity)
    work.install(store.db, NOW, store.cached_bytes)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        work.scan(store.db, NOW)
    selected = prepare(store, [(identity, b"") for identity in identities])
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        work.stage(store.db, selected)
        while work.advance(store.db, store.cached_bytes) is not None:
            pass
    assert public(store) == expected and references(store) == leases
    removed = set(identities[: (len(identities) + 1) // 2])
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        fresh = [record_relocation.capture(store, r.coordinate) for r in records if r.binding[0] in removed]
        record_relocation.relocate(store, fresh)
        packet_store.collect(store.db, 256)
    assert work.header(store.db)["sparse_pending"]
    plan = store.db.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM market_shared_records INDEXED BY market_shared_sparse WHERE refs>0 AND initial>=refs*2 ORDER BY id LIMIT 1"
    ).fetchall()
    assert any("USING INDEX market_shared_sparse" in r[3] for r in plan)
    instance = Worker(store)
    try:
        for _ in range(len(identities) + 1):
            assert (await instance.step(lambda: NOW))["state"] == "RECLAIMED"
            if not work.header(store.db)["sparse_pending"]:
                break
        else:
            pytest.fail("Sparse group did not reclaim its remaining members")
        assert public(store) == expected and references(store) == leases
        assert not store.db.execute("SELECT 1 FROM market_shared_members").fetchone()
        assert not store.db.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        await instance.close()


def test_sparse_descriptor_evacuation_preserves_exact_ids_bytes_and_dense_tail_pool(store):
    from cryptowatcher.catalogue_compaction import evacuate

    originals = ready(store)
    selected = prepare(store, originals)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        work.stage(store.db, selected)
        while work.advance(store.db) is not None:
            pass
        tail = store.db.execute("SELECT arena FROM market_shared_tails WHERE bucket=130").fetchone()[0]
        used = store.db.execute("SELECT used FROM market_packet_arenas WHERE id=?", (tail,)).fetchone()[0]
        remaining = 786433 - used
        while remaining:
            size = min(remaining, packet_store.MAXIMUM)
            packet_store.write_segment(store.db, "detail_packets", 130, b"x" * size, target=tail)
            remaining -= size
    before = {identity: packet_store.read(store.db, identity) for identity, _ in originals}
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        assert evacuate(store) == 1
        assert evacuate(store) == 1
        packet_store.collect(store.db)
    current = store.db.execute("SELECT arena FROM market_shared_tails WHERE bucket=130").fetchone()[0]
    assert current != tail
    assert not store.db.execute("SELECT 1 FROM market_packet_arenas WHERE id=?", (tail,)).fetchone()
    for identity, old in originals:
        assert packet_store.read(store.db, identity) == before[identity]
        assert (
            store.db.execute("SELECT arena FROM market_packets WHERE id=?", (identity,)).fetchone()[0]
            == current
        )
        equal(old, domain_packets.read(store.db, identity))


def test_full_shared_boundary_checks_actual_descriptors_and_refuses_partial_budget(store):
    originals = ready(store)
    selected = prepare(store, originals)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        work.stage(store.db, selected)
        assert work.verify(store.db, time.monotonic() + 5)["state"] == "MEASURED"
        while work.advance(store.db) is not None:
            pass
        assert work.verify(store.db, time.monotonic() + 5)["packets_checked"] == 2
        assert work.verify(store.db, 0)["state"] == "PARTIAL_BUDGET"
        arena, offset = store.db.execute(
            "SELECT arena,offset FROM market_packets WHERE id=?", (originals[0][0],)
        ).fetchone()
        with store.db.blobopen("market_packet_arenas", "payload", arena, readonly=False) as blob:
            blob.seek(offset)
            blob.write(b"x")
        with pytest.raises(ValueError, match="descriptor"):
            work.verify(store.db, time.monotonic() + 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("one_page", [False, True])
async def test_live_coalescing_deadline_and_forecast_boundary_do_not_count_old_admission_twice(
    store, monkeypatch, one_page
):
    from cryptowatcher import capacity
    from cryptowatcher.shared_record_worker import Worker
    from cryptowatcher.storage_meter import report, sample

    originals = ready(store)
    sample(store, NOW)
    selected = prepare(store, originals)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        work.stage(store.db, selected)
        while work.advance(store.db) is not None:
            pass
    instance = Worker(store)
    try:
        if one_page:
            original = store.db

            class OnePageVacuum:
                def __getattr__(self, name):
                    return getattr(original, name)

                def __enter__(self):
                    original.__enter__()
                    return self

                def __exit__(self, *args):
                    return original.__exit__(*args)

                def execute(self, sql, *args, **kwargs):
                    if sql == "PRAGMA incremental_vacuum(32)":
                        sql = "PRAGMA incremental_vacuum(1)"
                    return original.execute(sql, *args, **kwargs)

            monkeypatch.setattr(store, "db", OnePageVacuum())
        # N is an upper bound, not a promise to free N pages in one call.
        # Bound the rehearsal by actual file geometry even at one page/step;
        # the real COMPLETE/free-page criterion and production slice stay exact.
        maximum_steps = store.db.execute("PRAGMA page_count").fetchone()[0] + 64
        for _ in range(maximum_steps):
            await instance.step(lambda: NOW)
            if work.header(store.db)["initial_reclaim_complete"]:
                break
        else:
            pytest.fail(
                f"Initial physical reclaim did not complete: {work.header(store.db)}, "
                f"free_pages={store.db.execute('PRAGMA freelist_count').fetchone()[0]}"
            )
        # A pre-conversion sample cannot supply steady shared-layout growth.
        assert report(store, NOW + 60000)["sample_seconds"] == 0
        sample(store, NOW + 60000)
        assert len(store.get_runtime("storage_samples")) == 2
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            identity = packet_store.append(store.db, "detail_packets", 130, originals[0][1])
            packet_store.retain(store.db, identity)
            packet_bundles.checked(store.db, identity)
            store.db.execute(
                "UPDATE market_shared_scan SET at_ms=? WHERE id=1",
                (NOW + work.COALESCE_MS + work.SERVICE_MS,),
            )
        now = NOW + work.COALESCE_MS + work.SERVICE_MS
        assert not work.status(store.db, now)["pending_work"]
        assert work.status(store.db, now + 1)["pending_work"]
        value = report(store, now)
        for category in ("detail_packets", shared.GROUPS, work.QUEUE):
            row = next(r for r in value["forecast"] if r["category"] == category)
            assert row["basis"] == "CURRENT_OCCUPANCY_PLUS_POSITIVE_NET_GROWTH"
        monkeypatch.setattr(capacity.time, "time", lambda: (now + 1) / 1000)
        assert "SHARED_RECORD_BACKLOG" in capacity.stable_reason(store)
        with store.db:
            store.db.execute("UPDATE market_meta SET value='0' WHERE key='shared_record_writer'")
        assert "SHARED_RECORD_WRITER_DISABLED" in capacity.stable_reason(store)
    finally:
        await instance.close()


def test_shared_deep_boundary_requires_measured_ledger_even_if_other_labels_claim_complete():
    from cryptowatcher.capacity import boundary_blockers

    value = {
        "storage": {"shared_record_layout": {"installed": True}},
        "runtime_ledger": {"state": "MEASURED", "shared_records": {"state": "PARTIAL_BUDGET"}},
    }
    assert "BOUNDARY_SHARED_RECORD_LEDGER_INCOMPLETE" in boundary_blockers(value)
    value["runtime_ledger"]["shared_records"]["state"] = "MEASURED"
    assert "BOUNDARY_SHARED_RECORD_LEDGER_INCOMPLETE" not in boundary_blockers(value)
