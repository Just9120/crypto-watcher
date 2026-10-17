import asyncio
from unittest.mock import AsyncMock

import pytest

from cryptowatcher import ingestion
from cryptowatcher.ingestion import BybitRadar
from cryptowatcher.market_storage import enable
from cryptowatcher.sources import WINDOW_SIZE, Candle, Sources

from .test_ingestion import NOW, history


def prepare(store, tf, *, gap=True):
    enable(store.db)
    store.bounded = True
    store.clock_ms = lambda: NOW
    radar = BybitRadar(store)
    radar.observe_clock(NOW)
    stream = f"linear/METISUSDT/{tf}"
    radar.streams.add(stream)
    radar.tickers[stream.rsplit("/", 1)[0]] = {"turnover24h": "1000000", "lastPrice": "106"}
    candles = [Candle.parse(row, tf, "rest", NOW, NOW) for row in history(tf, 77)]
    radar.sources.put(stream, [c for i, c in enumerate(candles) if not gap or i != 40], recovery=True)
    return radar, stream, candles


@pytest.mark.parametrize("tf", ["1m", "15m"])
async def test_bounded_recovery_repairs_hole_outside_short_tail_without_live_replay(store, tf):
    radar, stream, candles = prepare(store, tf)
    # A separate analytics recovery filled disk; the live in-memory view still
    # has a hole. Repeating only the22 calculation candles cannot clear it.
    Sources(store).put(stream, candles, recovery=True)

    async def response(path, params):
        return {"time": NOW, "result": {"list": history(tf, params["limit"])}}

    radar._get = AsyncMock(side_effect=response)
    try:
        await radar.backfill(stream)
        assert radar._get.call_args.args[1]["limit"] == WINDOW_SIZE + 2
        assert stream not in radar.sources.blocked and not radar.sources.live
        assert len(radar.sources.windows[stream]) == WINDOW_SIZE
        assert set(c.start for c in candles) <= radar.sources.windows[stream].keys()
        fresh = Sources(store)
        fresh.load(stream)
        assert fresh.rows(stream, WINDOW_SIZE) == radar.sources.rows(stream, WINDOW_SIZE)
        changes = store.db.total_changes
        await radar.backfill(stream)
        assert radar._get.call_args.args[1]["limit"] == 24
        assert store.db.total_changes == changes
        assert not radar.sources.live
    finally:
        await radar.close()


async def test_incomplete_current_recovery_retries_with_existing_backoff_before_declaring_ready(
    store, monkeypatch
):
    radar, stream, _ = prepare(store, "15m")
    responses = iter([history("15m", 24), history("15m", WINDOW_SIZE)])
    radar._get = AsyncMock(side_effect=lambda *_: {"time": NOW, "result": {"list": next(responses)}})
    original_sleep = asyncio.sleep
    waits = []

    async def sleep(seconds):
        waits.append(seconds)
        if seconds == 5:
            assert stream in radar.sources.blocked
            assert not radar._recovered_buckets
        await original_sleep(0)

    monkeypatch.setattr(ingestion.asyncio, "sleep", sleep)
    radar.request_recovery(stream, 0)
    task = asyncio.create_task(radar._worker())
    try:
        for _ in range(100):
            if radar._get.await_count == 2 and not radar._working and not radar._pending:
                break
            await original_sleep(0)
        assert radar._get.await_count == 2 and 5 in waits
        assert not radar._working and not radar._pending
        assert not radar.sources.blocked and not radar.sources.live
        assert all(call.args[1]["limit"] == WINDOW_SIZE + 2 for call in radar._get.call_args_list)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await radar.close()


async def test_contiguous_bounded_bootstrap_keeps_short_calculation_request(store):
    radar, stream, _ = prepare(store, "15m", gap=False)
    radar._get = AsyncMock(return_value={"time": NOW, "result": {"list": history("15m", 24)}})
    try:
        await radar.backfill(stream)
        assert radar._get.call_args.args[1]["limit"] == 24
        assert not radar.sources.blocked and not radar.sources.live
    finally:
        await radar.close()
