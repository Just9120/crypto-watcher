from dataclasses import asdict
from unittest.mock import AsyncMock

from cryptowatcher.analytics_recovery import AnalyticsRecovery
from cryptowatcher.events import History
from cryptowatcher.ingestion import BybitRadar
from cryptowatcher.outcomes import DAY, MINUTE
from cryptowatcher.sources import Candle, Sources
from cryptowatcher.storage import encode

NOW = 1_800_000_000_000


async def test_inactive_referenced_window_over90days_priority_and_restart(store):
    radar = BybitRadar(store)
    radar.observe_clock(NOW)
    start = NOW - 95 * DAY
    stream = "linear/BTCUSDT/1m"
    recovery = AnalyticsRecovery(store, radar)
    recovery.request(stream, start, start + 5 * MINUTE)
    radar._pending["spot/ETHUSDT/1m"] = (0, 1)
    radar._get = AsyncMock(
        return_value={
            "time": NOW,
            "result": {
                "list": [
                    [start + i * MINUTE, "100", "101", "99", "100", "1", "10"] for i in reversed(range(5))
                ]
            },
        }
    )
    await recovery.work(NOW)
    radar._get.assert_not_awaited()
    radar._pending.clear()
    # Referenced recovery is independent of subscriptions and survives reconstruction.
    assert stream not in radar.streams
    recovery = AnalyticsRecovery(store, radar)
    await recovery.work(NOW)
    assert radar._get.call_args.args[1]["start"] == start
    assert recovery.history.count("analytics-gap-pending:") == 0
    rows = store.db.execute(
        "SELECT key FROM runtime WHERE key>=? AND key<?",
        (Sources.prefix(stream), Sources.prefix(stream) + "~"),
    ).fetchall()
    assert len(rows) == 5 and radar.sources.windows == {}
    await radar.close()


async def test_conflicting_source_never_overwrites_used_candle_and_bad_page_retries(store):
    radar = BybitRadar(store)
    radar.observe_clock(NOW)
    start = NOW - 5 * MINUTE
    stream = "spot/BTCUSDT/1m"
    original = Candle.parse([start, 100, 101, 99, 100, 1, 10], "1m", "websocket", NOW, NOW)
    key = Sources.prefix(stream) + f"{start:016d}"
    with store.db:
        store.db.execute("INSERT INTO runtime VALUES (?,?)", (key, encode(asdict(original))))
    recovery = AnalyticsRecovery(store, radar)
    recovery.request(stream, start, NOW)
    radar._get = AsyncMock(
        return_value={
            "time": NOW,
            "result": {
                "list": [
                    [start + i * MINUTE, "100", "102", "99", "100", "1", "10"] for i in reversed(range(5))
                ]
            },
        }
    )
    await recovery.work(NOW)
    assert store.get_runtime(key) == asdict(original)
    job = History(store).items("analytics-gap:")[0][1]
    assert job["state"] == "RETRY" and job["reason"] == "referenced_source_conflict"
    await recovery.work(NOW + 1000)
    assert radar._get.await_count == 1
    radar._get.return_value = {"time": NOW, "result": {"list": []}}
    await recovery.work(job["next_attempt_ms"])
    assert History(store).items("analytics-gap:")[0][1]["reason"] == "incomplete_referenced_page"
    assert store.get_runtime(key) == asdict(original)
    await radar.close()
