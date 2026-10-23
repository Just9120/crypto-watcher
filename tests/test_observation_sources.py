import copy
import json
from dataclasses import asdict

import pytest

from cryptowatcher import market_storage as market
from cryptowatcher.market import MarketError
from cryptowatcher.sources import WINDOW_SIZE, Sources
from cryptowatcher.storage import Store, encode
from tests.test_bounded_storage import bounded
from tests.test_candle_access import writer
from tests.test_sources import STEP, candle


@pytest.fixture(params=[False, True], ids=["legacy-points", "tiled-points"])
def source_state(store, request):
    bounded(store)
    if request.param:
        writer(store)
    return store, Sources(store), "spot/BTCUSDT/1m"


def restored(store, reference):
    return json.loads(market.restore_window(store.db, reference, cache=store.cached_bytes))


def test_snapshot_keeps_actual_revisions_after_independent_backfill_and_correction(source_state):
    store, sources, stream = source_state
    original = [candle(i) for i in range(1, 5)]
    sources.put(stream, original, recovery=True)
    before = store.db.total_changes
    reference = sources.snapshot_reference(stream)
    assert store.db.total_changes == before
    assert restored(store, reference) == [asdict(v) for v in original]
    changed = candle(2, close=101)
    with store.db:
        market.admit_many(store.db, stream, [asdict(changed)], cache=store.cached_bytes)
    # An independent stored correction has not replaced the producer's memory.
    assert sources.snapshot_reference(stream) == reference
    assert restored(store, reference) == [asdict(v) for v in original]
    reloaded = Sources(store)
    reloaded.load(stream)
    newer = reloaded.snapshot_reference(stream)
    assert newer != reference
    assert restored(store, newer)[1] == asdict(changed)
    sources.put(stream, [changed], recovery=True)
    assert sources.snapshot_reference(stream) == newer
    assert restored(store, reference) == [asdict(v) for v in original]
    assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()


def test_snapshot_cutoff_duplicate_and_empty_preserve_exact_values_without_writes(source_state):
    store, sources, stream = source_state
    values = [candle(i) for i in (4, 1, 3, 2)]
    sources.put(stream, values, recovery=True)
    reference = sources.snapshot_reference(stream, through=2 * STEP)
    assert restored(store, reference) == [asdict(candle(1)), asdict(candle(2))]
    before = store.db.total_changes
    sources.put(stream, [candle(4)], live=True)
    assert sources.snapshot_reference(stream, through=2 * STEP) == reference
    assert sources.snapshot_reference(stream, through=0) is None
    assert sources.snapshot_reference("linear/ETHUSDT/1m") is None
    assert store.db.total_changes == before
    for invalid in (-1, True, "120000"):
        with pytest.raises(ValueError, match="cutoff"):
            sources.snapshot_reference(stream, through=invalid)


def test_snapshot_trim_and_restart_retain_only_the_original_bounded_window(source_state):
    store, sources, stream = source_state
    sources.put(stream, [candle(i) for i in range(WINDOW_SIZE + 11)], recovery=True)
    reference = sources.snapshot_reference(stream)
    values = restored(store, reference)
    assert len(values) == WINDOW_SIZE
    assert [v["start"] for v in values] == [i * STEP for i in range(11, WINDOW_SIZE + 11)]
    assert len(market.reference_points(reference)) == WINDOW_SIZE
    other = Store(store.path, store.owner_id, readonly=True)
    try:
        assert Sources(other).snapshot_reference(stream) == reference
        assert other.db.total_changes == 0
    finally:
        other.close()
    sources.remove_window(stream)
    assert stream not in sources.revisions
    assert sources.snapshot_reference(stream) == reference


def test_failed_source_admission_does_not_publish_memory_or_coordinate_changes(source_state, monkeypatch):
    store, sources, stream = source_state
    sources.put(stream, [candle(1)], recovery=True)
    reference = sources.snapshot_reference(stream)
    before_memory = copy.deepcopy(sources.windows)
    before_coordinates = copy.deepcopy(sources.revisions)
    before_rows = encode(market.read_points(store.db, stream))
    original = market.admit_many

    def fail_after_write(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected source publication failure")

    monkeypatch.setattr(market, "admit_many", fail_after_write)
    with pytest.raises(RuntimeError, match="injected"):
        sources.put(stream, [candle(1, close=101), candle(2)], recovery=True)
    assert sources.windows == before_memory
    assert sources.revisions == before_coordinates
    assert encode(market.read_points(store.db, stream)) == before_rows
    assert sources.snapshot_reference(stream) == reference


def test_snapshot_refuses_a_memory_only_candle_without_an_admitted_revision(source_state):
    _, sources, stream = source_state
    sources.put(stream, [candle(1)], recovery=True)
    sources.windows[stream][2 * STEP] = candle(2)
    with pytest.raises(MarketError, match="missing_source_revision"):
        sources.snapshot_reference(stream)


def test_chunks_authenticate_every_original_coordinate_independently(source_state):
    store, sources, stream = source_state
    originals = [candle(i) for i in range(1, 54)]
    sources.put(stream, originals, recovery=True)
    before = store.db.total_changes
    chunks = sources.snapshot_chunks(stream)
    assert [len(market.reference_points(ref)) for ref in chunks] == [16, 16, 16, 5]
    assert [value for ref in chunks for value in restored(store, ref)] == [asdict(c) for c in originals]
    cutoff = sources.snapshot_chunks(stream, through=17 * STEP)
    assert [value for ref in cutoff for value in restored(store, ref)] == [asdict(c) for c in originals[:17]]
    assert sources.snapshot_chunks(stream, through=0) == []
    assert store.db.total_changes == before
    with store.db:
        market.admit_many(store.db, stream, [asdict(candle(53, close=101))], cache=store.cached_bytes)
    assert sources.snapshot_chunks(stream) == chunks
    assert restored(store, chunks[-1]) == [asdict(c) for c in originals[-5:]]
    invalid = copy.deepcopy(chunks[-1])
    invalid["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="hash mismatch"):
        restored(store, invalid)
