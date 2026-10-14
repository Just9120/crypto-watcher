import pytest

from cryptowatcher.market import MarketError
from cryptowatcher.sources import WINDOW_SIZE, Candle, Sources
from cryptowatcher.storage import Store

STEP = 60_000


def candle(index, close=100, tf="1m"):
    step = int(tf[:-1]) * STEP
    return Candle.parse(
        [index * step, 100, max(101, close), 99, close, 10, 1000], tf, "ws", 1, (index + 1) * step
    )


def test_recovery_correction_updates_existing_source_without_reinserting_row(store):
    sources = Sources(store)
    stream = "spot/BTCUSDT/1m"
    sources.put(stream, [candle(1)], recovery=True)
    key = Sources.prefix(stream) + f"{STEP:016d}"
    rowid = store.db.execute("SELECT rowid FROM runtime WHERE key=?", (key,)).fetchone()[0]
    sources.put(stream, [candle(1, close=101)], recovery=True)
    assert store.db.execute("SELECT rowid FROM runtime WHERE key=?", (key,)).fetchone()[0] == rowid
    assert store.get_runtime(key)["close"] == 101
    assert sources.rows(stream, 1)[0][4] == 101
    assert not sources.live


def test_market_tf_identity_restart_backup_and_no_historical_live(store, tmp_path):
    sources = Sources(store)
    for stream in ("spot/BTCUSDT/1m", "linear/BTCUSDT/1m", "spot/BTCUSDT/3m"):
        sources.put(stream, [candle(1, tf=stream.split("/")[-1])], live=True)
    assert len(sources.windows) == 3
    revision = store.snapshot().revision
    backup = tmp_path / "backup.sqlite3"
    store.backup(backup)
    copy = Store(backup, store.owner_id)
    try:
        restored = Sources(copy)
        assert restored.rows("spot/BTCUSDT/1m", 20) == sources.rows("spot/BTCUSDT/1m", 20)
        assert restored.live == {}
        assert copy.snapshot().revision == revision
    finally:
        copy.close()


def test_duplicate_out_of_order_gap_conflict_and_recovery(store):
    sources = Sources(store)
    stream = "spot/BTCUSDT/1m"
    sources.put(stream, [candle(1), candle(2)], recovery=True)
    sources.put(stream, [candle(3)], live=True)
    changes = store.db.total_changes
    assert not sources.put(stream, [candle(3)], live=True)
    assert store.db.total_changes == changes
    sources.live.clear()
    assert not sources.put(stream, [candle(0)], live=True)
    assert not sources.live
    sources.put(stream, [candle(5)], live=True)
    assert sources.blocked[stream] == "candle_gap" and not sources.live
    sources.put(stream, [candle(4), candle(5)], recovery=True)
    assert not sources.blocked and not sources.live
    with pytest.raises(MarketError, match="conflicting_closed_candle"):
        sources.put(stream, [candle(5, close=101)], live=True)
    assert sources.blocked
    sources.put(stream, [candle(5, close=101)], recovery=True)
    assert not sources.blocked and not sources.live


def test_bounded_memory_keeps_persisted_history(store):
    sources = Sources(store)
    stream = "linear/BTCUSDT/1m"
    sources.put(stream, [candle(i) for i in range(WINDOW_SIZE + 30)], recovery=True)
    assert len(sources.windows[stream]) == WINDOW_SIZE
    assert (
        store.db.execute("SELECT COUNT(*) FROM runtime WHERE key LIKE 'source:v1:candle:%'").fetchone()[0]
        == WINDOW_SIZE + 30
    )
    with pytest.raises(MarketError, match="lookback_exceeds"):
        sources.rows(stream, WINDOW_SIZE + 1)


@pytest.mark.parametrize(
    "row,now,reason",
    [
        ([1, 100, 101, 99, 100, 1, 100], STEP, "boundary"),
        ([0, 100, 101, 99, 100, 1, 100], STEP - 1, "unclosed"),
        ([0, 100, 99, 99, 100, 1, 100], STEP, "values"),
        ([0, 100, 101, 99, 100, -1, 100], STEP, "values"),
        ([0, 100, 101, 99, "NaN", 1, 100], STEP, "number"),
    ],
)
def test_invalid_sources_rejected(row, now, reason):
    with pytest.raises(MarketError, match=reason):
        Candle.parse(row, "1m", "rest", 0, now)
