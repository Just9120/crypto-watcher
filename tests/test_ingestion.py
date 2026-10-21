import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from cryptowatcher import ingestion
from cryptowatcher.bot import Controller
from cryptowatcher.config import TIMEFRAMES, OwnerSettings
from cryptowatcher.events import History
from cryptowatcher.ingestion import BybitRadar, shards, topic
from cryptowatcher.market import MarketError
from cryptowatcher.sources import Candle
from cryptowatcher.storage import Store
from cryptowatcher.universe import classify

from .conftest import update
from .test_universe import metadata

NOW = 1_800_000_000_000


def history(tf="1m", count=256):
    step = int(tf[:-1]) * 60_000
    return [
        [
            NOW - (i + 1) * step,
            "100",
            "104",
            "98",
            "103" if i == 0 else "100",
            "10",
            "4000" if i == 0 else "1000",
        ]
        for i in range(count)
    ]


def message(tf="1m", symbol="BTCUSDT", confirm=True, start=NOW, **changes):
    step = int(tf[:-1]) * 60_000
    return {
        "topic": f"kline.{tf[:-1]}.{symbol}",
        "ts": start + step,
        "data": [
            {
                "start": start,
                "end": start + step - 1,
                "interval": tf[:-1],
                "open": "103",
                "close": "106",
                "high": "107",
                "low": "102",
                "volume": "20",
                "turnover": "12000",
                "confirm": confirm,
                **changes,
            }
        ],
    }


@pytest.fixture
def radar(store):
    instance = BybitRadar(store)
    instance.connector = lambda *args, **kwargs: Socket()
    instance.observe_clock(NOW)
    for market in ("spot", "linear"):
        item, _ = classify(market, metadata(market=market))
        instance.instruments[item.key] = item
        instance.tickers[item.key] = {"turnover24h": "1000000", "lastPrice": "106"}
        instance.metadata_at[market] = instance.tickers_at[market] = 1
        for tf in TIMEFRAMES:
            stream = f"{item.key}/{tf}"
            instance.streams.add(stream)
            candles = [Candle.parse(row, tf, "rest", NOW, NOW) for row in history(tf)]
            instance.sources.put(stream, candles, recovery=True)
    yield instance


def test_persisted_block_diagnostics_are_exact_bounded_and_distinguish_inactive_streams(radar):
    active = sorted(radar.streams)[0]
    radar.sources.blocked = {active: "conflicting_closed_candle"}
    radar.sources.blocked.update({f"spot/SYNTHETIC{i:02d}USDT/1m": "candle_gap" for i in range(27)})
    radar.persist_diagnostics()
    value = radar.store.get_runtime("ingestion")
    assert value["blocked_streams"] == 28
    assert value["blocked_reasons"] == {"candle_gap": 27, "conflicting_closed_candle": 1}
    assert len(value["blocked_stream_samples"]) == 20
    assert value["blocked_stream_samples_truncated"]
    assert any(row["stream"] == active and row["active"] for row in value["blocked_stream_samples"])
    assert any(not row["active"] for row in value["blocked_stream_samples"])
    radar.sources.blocked.clear()
    radar.persist_diagnostics()
    value = radar.store.get_runtime("ingestion")
    assert value["blocked_streams"] == 0
    assert value["blocked_reasons"] == {} and value["blocked_stream_samples"] == []
    assert not value["blocked_stream_samples_truncated"]


async def test_retiring_session_cannot_freeze_discovery_and_partial_scope_is_retryable(radar, monkeypatch):
    entered = asyncio.Event()
    closing = asyncio.Event()
    release = asyncio.Event()

    async def old_socket():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            closing.set()
            await release.wait()

    old = asyncio.create_task(old_socket())
    retained = asyncio.create_task(asyncio.Event().wait())
    await entered.wait()
    radar._groups = {
        market + ":0": tuple(sorted(s for s in radar.streams if s.startswith(market + "/")))
        for market in ("spot", "linear")
    }
    radar._sessions = {"linear:0": old, "spot:0": retained}
    radar.connections = {
        name: {"state": "CONNECTED", "requested": len(group), "acknowledged": len(group)}
        for name, group in radar._groups.items()
    }
    before = radar.store.snapshot()
    current = radar.store.update(
        before.revision,
        before.settings.changed(alert_universe_mode="custom", watchlist=("spot/BTCUSDT",)),
    )
    monkeypatch.setattr(ingestion, "SESSION_SHUTDOWN_SECONDS", 0.01, raising=False)
    try:
        with pytest.raises(MarketError, match="subscription_shutdown_timeout"):
            await asyncio.wait_for(radar.reconcile(current.settings), 0.5)
        assert closing.is_set()
        assert radar.streams == {"spot/BTCUSDT/" + tf for tf in TIMEFRAMES}
        assert radar._groups["linear:0"] and not radar.scope_synced()
        await asyncio.sleep(0)
        await asyncio.wait_for(radar.reconcile(current.settings), 0.5)
        assert set(radar._groups) == set(radar._sessions) == {"spot:0"}
        assert radar._sessions["spot:0"] is retained and not retained.done()
        assert radar.scope_synced() and radar.components_healthy()
        assert radar.store.snapshot() == current
    finally:
        release.set()
        await radar.close()


async def test_manager_retains_sanitized_failure_and_continues_refresh(radar, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def refresh(settings):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("fixture_private_error_body")
        entered.set()
        await release.wait()

    monkeypatch.setattr(radar, "refresh", refresh)
    monkeypatch.setattr(ingestion, "MANAGER_INTERVAL_SECONDS", 0.01, raising=False)
    task = asyncio.create_task(radar._manager())
    radar._tasks = [task]
    try:
        await asyncio.wait_for(entered.wait(), 0.5)
        assert not task.done() and calls == 2
        radar.persist_diagnostics()
        value = radar.store.get_runtime("ingestion")
        assert value["manager_failures"]["count"] == 1
        assert value["manager_failures"]["last_error_class"] == "OperationalError"
        assert "fixture_private_error_body" not in json.dumps(value)
    finally:
        release.set()
        await radar.close()


async def test_configuration_retries_partial_scope_after_retirement_timeout(radar, monkeypatch):
    retried = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    current = radar.store.snapshot()

    async def reconcile(settings):
        nonlocal calls
        calls += 1
        radar._scope_revision = current.revision
        if calls == 1:
            raise MarketError("subscription_shutdown_timeout")
        retried.set()
        await release.wait()

    monkeypatch.setattr(radar, "reconcile", reconcile)
    monkeypatch.setattr(ingestion, "MANAGER_INTERVAL_SECONDS", 0.01)
    task = asyncio.create_task(radar._configuration_loop())
    radar._tasks = [task]
    try:
        await asyncio.wait_for(retried.wait(), 0.5)
        assert calls == 2 and not task.done()
        assert radar.market_errors["configuration"] == "subscription_shutdown_timeout"
        assert radar.store.snapshot() == current
    finally:
        release.set()
        await radar.close()


async def test_independent_diagnostics_expose_finished_component_and_discovery_ages(radar):
    task = asyncio.create_task(asyncio.sleep(0))
    await task
    radar._tasks = [task]
    # Fixture refresh stamps are1. Age must not depend on runner uptime;
    # freeze only this synchronous snapshot, restoring before any await.
    with patch.object(ingestion.time, "monotonic", return_value=1000):
        radar.persist_diagnostics()
    value = radar.store.get_runtime("ingestion")
    assert value["components_healthy"] is False
    assert value["components"][0]["state"] == "FINISHED"
    for market in ("spot", "linear"):
        assert value["discovery"][market]["metadata_age_seconds"] > 600
        assert value["discovery"][market]["ticker_age_seconds"] > 300
        assert value["discovery"][market]["metadata_age_seconds"] == 999
        assert value["discovery"][market]["ticker_age_seconds"] == 999
    await radar.close()


async def test_owner_scope_applies_while_rest_manager_is_waiting(radar, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def waiting_refresh(settings):
        entered.set()
        await release.wait()

    monkeypatch.setattr(radar, "refresh", waiting_refresh)
    manager = asyncio.create_task(radar._manager())
    await asyncio.wait_for(entered.wait(), 1)
    before = radar.store.snapshot()
    changed = radar.store.update(before.revision, before.settings.changed(alert_universe_mode="custom"))
    configuration = asyncio.create_task(radar._configuration_loop())
    try:
        await wait_condition(lambda: not radar.streams)
        diagnostic = radar.store.get_runtime("ingestion")
        assert diagnostic["configuration_revision"] == changed.revision
        assert diagnostic["active_streams"] == 0
        assert set(diagnostic["discovery"]) == {"spot", "linear"}
        assert not manager.done() and not release.is_set()
        assert radar.store.snapshot() == changed
    finally:
        manager.cancel()
        configuration.cancel()
        await asyncio.gather(manager, configuration, return_exceptions=True)
        await radar.close()


def test_closed_observation_honors_current_scope_before_subscription_reconcile(radar):
    radar.on_observation = Mock()
    stream = "spot/BTCUSDT/1m"
    radar.publish_observation(stream, live=True)
    radar.on_observation.assert_called_once()
    radar.on_observation.reset_mock()
    before = radar.store.snapshot()
    radar.store.update(before.revision, before.settings.changed(alert_universe_mode="custom"))
    radar.publish_observation(stream, live=True)
    radar.on_observation.assert_not_called()
    assert stream in radar.streams and radar.sources.windows[stream]
    current = radar.store.snapshot()
    radar.store.update(
        current.revision, current.settings.changed(alert_universe_mode="top", liquidity_floor_24h=2e6)
    )
    radar.publish_observation(stream, live=True)
    radar.on_observation.assert_not_called()


async def test_rest_refresh_cannot_restore_scope_from_before_owner_change(radar, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def waiting_tickers(market):
        entered.set()
        await release.wait()

    now = __import__("time").monotonic()
    radar.metadata_at = {"spot": now, "linear": now}
    monkeypatch.setattr(radar, "refresh_tickers", waiting_tickers)
    before = radar.store.snapshot()
    refresh = asyncio.create_task(radar.refresh(before.settings))
    await asyncio.wait_for(entered.wait(), 1)
    changed = radar.store.update(before.revision, before.settings.changed(alert_universe_mode="custom"))
    release.set()
    await asyncio.wait_for(refresh, 2)
    assert not radar.streams and radar._scope_revision == changed.revision
    await radar.close()


def test_initial_bucket_windows_are_not_starved_by_an_ongoing_one_minute_gap_queue(radar):
    buckets = {(market, tf) for market in ("spot", "linear") for tf in TIMEFRAMES}
    streams = {f"{market}/BTCUSDT/{tf}" for market, tf in buckets}
    busy = {f"linear/GAP{i}USDT/1m" for i in range(40)}
    radar.streams = streams | busy | {"spot/HISTORYUSDT/1m"}
    for stream in sorted(busy):
        radar.request_recovery(stream, 0)
    for stream in sorted(streams):
        radar.request_recovery(stream, 10)
    radar.request_recovery("spot/HISTORYUSDT/1m", 20)
    seeded = []
    for _ in range(8):
        stream, (priority, _) = radar.next_recovery()
        radar._pending.pop(stream)
        bucket = radar.recovery_bucket(stream)
        seeded.append(bucket)
        assert priority != 20
        radar._recovered_buckets.add(bucket)
        radar._last_market = bucket[0]
    assert set(seeded) == buckets and len(set(seeded)) == 8
    assert len(radar._pending) == len(radar.streams) - 8
    assert radar.next_recovery()[1][0] == 0


def test_initial_recovery_workers_do_not_all_claim_the_same_missing_bucket(radar):
    radar.streams = {
        f"{market}/{symbol}/{tf}"
        for market in ("spot", "linear")
        for symbol in ("BTCUSDT", "ETHUSDT")
        for tf in TIMEFRAMES
    }
    for stream in sorted(radar.streams):
        radar.request_recovery(stream, 10)
    claimed = []
    for _ in range(4):
        stream, _ = radar.next_recovery()
        radar._pending.pop(stream)
        radar._working.add(stream)
        claimed.append(radar.recovery_bucket(stream))
    assert len(set(claimed)) == 4


def test_seed_requires_fresh_usable_baseline_not_just_a_successful_rest_response(radar):
    assert radar.recovery_window_usable("spot/BTCUSDT/1m")
    rows = history("1m")
    for row in rows:
        row[5] = row[6] = "0"
    candles = [Candle.parse(row, "1m", "rest", NOW, NOW) for row in rows]
    radar.sources.windows["spot/BTCUSDT/1m"] = {c.start: c for c in candles}
    assert not radar.recovery_window_usable("spot/BTCUSDT/1m")


@pytest.mark.asyncio
async def test_failed_initial_window_does_not_mark_bucket_recovered(radar):
    radar.streams = {"spot/BTCUSDT/1m"}
    radar.request_recovery("spot/BTCUSDT/1m", 10)
    radar.restore_current_window = Mock(return_value=False)
    radar.backfill = AsyncMock(side_effect=MarketError("fixture_failure"))
    task = asyncio.create_task(radar._worker())
    try:
        for _ in range(20):
            if radar.backfill.called:
                break
            await asyncio.sleep(0)
        assert radar.backfill.called and radar._recovered_buckets == set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_native_interleaved_streams_duplicates_and_no_bootstrap_alert(radar):
    before = await radar.snapshot(OwnerSettings())
    assert len(before.quotes) == 8 and not any(q["live"] for q in before.quotes.values())
    for market in ("spot", "linear"):
        for tf in TIMEFRAMES:
            open_message = message(tf, confirm=False)
            open_message["ts"] = NOW + 30_000
            radar.accept_message(market, open_message, radar.streams)
            assert not radar.sources.live
    radar.accept_message("spot", message("1m"), radar.streams)
    radar.accept_message("linear", message("3m"), radar.streams)
    changes = radar.store.db.total_changes
    radar.accept_message("spot", message("1m"), radar.streams)
    assert radar.store.db.total_changes == changes
    result = await radar.snapshot(OwnerSettings())
    assert len(result.quotes) == 8
    assert result.quotes["linear/BTCUSDT/3m"]["market"] == "linear"
    assert result.quotes["spot/BTCUSDT/1m"]["freshness"] == "STALE"  # later exchange clock, not receipt time
    assert result.quotes["linear/BTCUSDT/3m"]["freshness"] == "FRESH"


async def test_gap_priority_recovery_never_promotes_historical_to_live(radar):
    stream = "spot/BTCUSDT/1m"
    radar.request_recovery(stream, 10)
    radar.accept_message("spot", message(start=NOW + 60_000), radar.streams)
    assert radar.sources.blocked[stream] == "candle_gap"
    assert radar._pending[stream][0] == 0 and len(radar._pending) == 1
    assert stream not in (await radar.snapshot(OwnerSettings())).quotes
    rows = history()
    rows = [[int(row[0]) + 120_000, *row[1:]] for row in rows]
    radar._get = AsyncMock(return_value={"time": NOW + 120_000, "result": {"list": rows}})
    await radar.backfill(stream)
    assert stream not in radar.sources.blocked and stream not in radar.sources.live
    assert not (await radar.snapshot(OwnerSettings())).quotes[stream]["live"]
    assert radar._get.call_args.args[1]["limit"] == 258


async def test_invalid_end_conflict_and_old_frame_are_not_live(radar):
    stream = "linear/BTCUSDT/1m"
    radar.accept_message("linear", message(end=NOW), radar.streams)
    assert radar.sources.blocked[stream] == "candle_end_mismatch"
    assert not radar.sources.live
    radar.sources.blocked.clear()
    old = message(start=NOW - 180_000)
    old["ts"] = NOW
    radar.accept_message("linear", old, radar.streams)
    assert radar.sources.blocked[stream] == "conflicting_closed_candle"
    assert not radar.sources.live


async def test_recovery_open_candle_and_invalid_response(radar):
    stream = "linear/BTCUSDT/1m"
    rows = [[NOW, "1", "999", "1", "999", "1", "99999"], *history()]
    radar._get = AsyncMock(return_value={"time": NOW, "result": {"list": rows}})
    await radar.backfill(stream)
    assert max(radar.sources.windows[stream]) == NOW - 60_000
    radar._get = AsyncMock(return_value={"time": NOW, "result": {"list": [["bad"]]}})
    with pytest.raises(MarketError, match="invalid_recovery_history"):
        await radar.backfill(stream)


def test_wide_subscription_shards_preserve_every_tf_and_spot_batch_limits():
    streams = [f"spot/C{i}USDT/{tf}" for i in range(1000) for tf in TIMEFRAMES]
    groups = shards(streams)
    assert len(groups) > 1
    assert all(len(group) <= 100 for group in groups)
    assert sum(map(len, groups)) == len(streams)
    assert set(s for g in groups for s in g) == set(streams)
    assert all(len(json.dumps([topic(s) for s in group])) <= 20_000 for group in groups)


class Socket:
    def __init__(self, rejected=False):
        self.messages = asyncio.Queue()
        self.sent = []
        self.rejected = rejected
        self.closed = False

    async def send(self, raw):
        payload = json.loads(raw)
        self.sent.append(payload)
        if payload["op"] == "subscribe":
            await self.messages.put(
                json.dumps({"op": "subscribe", "req_id": payload["req_id"], "success": not self.rejected})
            )

    async def recv(self):
        return await self.messages.get()

    async def close(self):
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True


async def wait_condition(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_acknowledged_resubscribe_after_rejection_and_bounded_batches(radar):
    sockets = []

    def connector(url, **options):
        assert url == "wss://stream.bybit.com/v5/public/spot"
        assert options["max_queue"] == 16 and options["ping_interval"] is None
        socket = Socket(rejected=not sockets)
        sockets.append(socket)
        return socket

    radar.connector = connector
    streams = [f"spot/C{i}USDT/1m" for i in range(21)]
    radar.streams.update(streams)
    task = asyncio.create_task(radar._session("spot:0", "spot", streams))
    try:
        await wait_condition(lambda: radar.connections.get("spot:0", {}).get("state") == "CONNECTED")
        assert len(sockets) == 2 and sockets[0].closed
        assert radar.connections["spot:0"]["acknowledged"] == 21
        assert [len(p["args"]) for p in sockets[1].sent if p["op"] == "subscribe"] == [10, 10, 1]
        assert len(radar._pending) <= len(radar.streams)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await radar.close()
    assert sockets[1].closed


@pytest.mark.parametrize("delayed", [False, True])
async def test_all_subscription_ack_precede_candle_work_and_receipt_survives(radar, monkeypatch, delayed):
    streams = [f"spot/C{i}USDT/1m" for i in range(21)]
    radar.streams.update(streams)
    receipt = NOW + 60_007
    monkeypatch.setattr(ingestion.time, "time", lambda: receipt / 1000)

    class BeforeAck(Socket):
        async def send(self, raw):
            payload = json.loads(raw)
            if payload["op"] == "subscribe":
                for candle_topic in payload["args"]:
                    symbol = candle_topic.rsplit(".", 1)[1]
                    await self.messages.put(json.dumps(message(symbol=symbol)))
                if delayed and payload["req_id"] == "cw20":
                    radar.observe_clock(NOW + 4 * 60_000)
            await super().send(raw)

    socket = BeforeAck()
    radar.connector = lambda *args, **kwargs: socket
    original = radar.accept_message
    seen = []

    def observe(market, payload, allowed, **kwargs):
        assert radar.connections["spot:0"]["acknowledged"] == len(streams)
        assert radar.connections["spot:0"]["state"] == "CONNECTED"
        original(market, payload, allowed, **kwargs)
        seen.append(payload)

    monkeypatch.setattr(radar, "accept_message", observe)
    task = asyncio.create_task(radar._session("spot:0", "spot", streams))
    try:
        await wait_condition(lambda: len(seen) == len(streams))
        for stream in streams:
            assert radar.sources.windows[stream][NOW].received_ms == receipt
            assert (stream in radar.sources.live) is not delayed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await radar.close()


@pytest.mark.parametrize("bound", ["messages", "bytes"])
async def test_subscription_buffer_overflow_blocks_signal_and_requests_recovery(radar, monkeypatch, bound):
    if bound == "messages":
        monkeypatch.setattr(ingestion, "SUBSCRIPTION_BUFFER_MESSAGES", 1)
    else:
        monkeypatch.setattr(ingestion, "SUBSCRIPTION_BUFFER_BYTES", 1)
    stream = "spot/BTCUSDT/1m"

    class FloodBeforeAck(Socket):
        async def send(self, raw):
            if json.loads(raw)["op"] == "subscribe":
                for _ in range(3):
                    await self.messages.put(json.dumps(message()))
            await super().send(raw)

    socket = FloodBeforeAck()
    radar.connector = lambda *args, **kwargs: socket
    radar.on_observation = Mock()
    task = asyncio.create_task(radar._session("spot:0", "spot", [stream]))
    try:
        await wait_condition(lambda: radar.connections.get("spot:0", {}).get("state") == "CONNECTED")
        assert radar.connections["spot:0"]["acknowledged"] == 1
        assert radar.connections["spot:0"]["buffer_overflows"] >= 2
        assert radar.sources.blocked[stream] == "subscription_buffer_overflow"
        assert radar._pending[stream][0] == 0
        radar.on_observation.assert_not_called()
        radar._get = AsyncMock(return_value={"time": NOW, "result": {"list": history()}})
        await radar.backfill(stream)
        assert stream not in radar.sources.blocked and stream not in radar.sources.live
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await radar.close()


async def test_rejected_subscription_discards_buffer_before_any_candle_write(radar):
    class RejectWithData(Socket):
        async def send(self, raw):
            if json.loads(raw)["op"] == "subscribe":
                await self.messages.put(json.dumps(message()))
            await super().send(raw)

    socket = RejectWithData(rejected=True)
    radar.connector = lambda *args, **kwargs: socket
    stream = "spot/BTCUSDT/1m"
    before = {k: v.row() for k, v in radar.sources.windows[stream].items()}
    task = asyncio.create_task(radar._session("spot:0", "spot", [stream]))
    try:
        await wait_condition(lambda: radar.connections.get("spot:0", {}).get("state") == "RECONNECTING")
        assert radar.connections["spot:0"]["acknowledged"] == 0
        assert {k: v.row() for k, v in radar.sources.windows[stream].items()} == before
        assert radar.store.get_runtime(radar.sources.prefix(stream) + f"{NOW:016d}") is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await radar.close()


async def test_continuous_open_updates_cannot_extend_subscription_deadline(radar, monkeypatch):
    clock = iter(range(10000))
    monkeypatch.setattr(
        ingestion, "time", SimpleNamespace(monotonic=lambda: next(clock), time=lambda: NOW / 1000)
    )

    class NoAcknowledgement(Socket):
        async def send(self, raw):
            if json.loads(raw)["op"] == "subscribe":
                for _ in range(100):
                    await self.messages.put(json.dumps(message(confirm=False)))

    socket = NoAcknowledgement()
    radar.connector = lambda *args, **kwargs: socket
    task = asyncio.create_task(radar._session("spot:0", "spot", ["spot/BTCUSDT/1m"]))
    try:
        await wait_condition(lambda: radar.connections.get("spot:0", {}).get("state") == "RECONNECTING")
        assert radar.connections["spot:0"]["reason"] == "TimeoutError"
        assert radar.connections["spot:0"]["acknowledged"] == 0
        assert socket.messages.qsize() > 50  # timeout despite recv being continuously ready
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await radar.close()


async def test_wide_linear_shards_ack_independently_before_closed_candle_work(radar, monkeypatch):
    streams = [f"linear/C{i}USDT/{tf}" for i in range(90) for tf in TIMEFRAMES]
    radar.streams = set(streams)
    sockets = []

    class InitialBurst(Socket):
        async def send(self, raw):
            payload = json.loads(raw)
            if payload["op"] == "subscribe":
                for value in payload["args"]:
                    _, interval, symbol = value.split(".")
                    await self.messages.put(json.dumps(message(interval + "m", symbol)))
            await super().send(raw)

    def connector(*args, **kwargs):
        socket = InitialBurst()
        sockets.append(socket)
        return socket

    seen = []

    def accept(market, payload, allowed, **kwargs):
        name = names[next(s for s in allowed)]
        assert radar.connections[name]["acknowledged"] == len(allowed)
        seen.append(payload["topic"])

    groups = shards(streams)
    names = {s: f"linear:{i}" for i, group in enumerate(groups) for s in group}
    monkeypatch.setattr(radar, "accept_message", accept)
    radar.connector = connector
    tasks = [
        asyncio.create_task(radar._session(f"linear:{i}", "linear", group)) for i, group in enumerate(groups)
    ]
    try:
        await wait_condition(lambda: len(seen) == len(streams))
        assert len(sockets) == 4
        assert len(set(seen)) == len(streams)
        assert all(len(s.sent) == 1 for s in sockets)  # one bounded linear subscribe per shard
        assert all(c["state"] == "CONNECTED" for c in radar.connections.values())
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await radar.close()


async def test_delivery_requires_both_metadata_all_ack_and_each_tf(radar):
    result = await radar.snapshot(OwnerSettings())
    radar.connections["spot:0"] = {"state": "CONNECTED", "requested": 4, "acknowledged": 3}
    assert not radar.delivery_ready(result)
    radar.connections["spot:0"]["acknowledged"] = 4
    assert radar.delivery_ready(result)
    radar.market_errors["linear"] = "rate_limited"
    assert not radar.delivery_ready(result)
    assert (await radar.snapshot(OwnerSettings())).errors["linear"] == "rate_limited"


async def test_typed_owner_list_validation_restart_and_market_mutes(radar, infrastructure):
    controller = Controller(infrastructure, radar.store, radar)
    await controller.handle(update("/setlist spot/BTC,linear/BTC"), None)
    saved = radar.store.snapshot()
    assert saved.settings.instruments() == ("spot/BTCUSDT", "linear/BTCUSDT")
    reopened = Store(radar.store.path, radar.store.owner_id, readonly=True)
    try:
        assert reopened.snapshot() == saved
    finally:
        reopened.close()
    invalid = update("/setlist spot/PAXG")
    await controller.handle(invalid, None)
    assert radar.store.snapshot() == saved
    assert "не выполнено" in invalid.effective_message.reply_text.call_args.args[0]
    await controller.handle(update(callback="al:mute:BYBIT:linear:BTCUSDT:3600"), None)
    assert radar.store.muted("linear/BTCUSDT", 0)
    assert not radar.store.muted("spot/BTCUSDT", 0)
    before = radar.store.db.total_changes
    await controller.handle(update(callback="al:mute:BYBIT:BTCUSDT:3600"), None)
    assert radar.store.db.total_changes == before  # ambiguous legacy callback cannot guess a market
    await controller.handle(update("/clearlist"), None)
    assert radar.store.snapshot().settings.instruments() == ()


async def test_pending_queue_bounded_under_repeated_promotions_and_empty_universe(radar):
    for _ in range(100):
        for stream in radar.streams:
            radar.request_recovery(stream, 10)
            radar.request_recovery(stream, 0)
    assert len(radar._pending) == len(radar.streams)
    radar._sessions = {}
    await radar.reconcile(OwnerSettings(alert_universe_mode="custom"))
    assert not radar.streams and not radar._pending and not radar.sources.windows
    result = await radar.snapshot(OwnerSettings(alert_universe_mode="custom"))
    assert result.requested == 0 and radar.delivery_ready(result)


async def test_recovery_rejects_conflicting_duplicate_without_state_mutation(radar):
    before = radar.store.db.total_changes
    rows = history()
    rows.append([rows[0][0], "100", "110", "98", "109", "10", "4000"])
    radar._get = AsyncMock(return_value={"time": NOW, "result": {"list": rows}})
    with pytest.raises(MarketError, match="conflicting_recovery_candle"):
        await radar.backfill("spot/BTCUSDT/1m")
    assert radar.store.db.total_changes == before


async def test_long_outage_history_pages_resume_without_historical_live(radar):
    stream = "spot/BTCUSDT/1m"
    old_start = NOW - 1500 * 60_000
    radar.sources.windows[stream] = {
        old_start: Candle.parse(
            [old_start, 100, 101, 99, 100, 1, 100], "1m", "rest", old_start + 60_000, old_start + 60_000
        )
    }
    radar._get = AsyncMock(return_value={"time": NOW, "result": {"list": history()}})
    await radar.backfill(stream)
    assert len(radar.sources.windows[stream]) == 256
    assert stream in radar._history and stream not in radar.sources.live
    gap = radar._history[stream]
    end = gap["end"]
    page = [[end + 1 - (i + 1) * 60_000, 100, 101, 99, 100, 1, 100] for i in range(1000)]
    radar._get.return_value = {"time": NOW, "result": {"list": page}}
    await radar.history_page(stream)
    assert radar._history[stream]["end"] == min(row[0] for row in page) - 1
    cursor = radar.store.get_runtime(f"source:v1:gap:{stream}")
    assert cursor["state"] == "PENDING" and not radar.sources.live
    assert len(radar.sources.windows[stream]) == 256
    end, start = cursor["end"], cursor["start"]
    page = [[t, 100, 101, 99, 100, 1, 100] for t in range(start, end + 1, 60_000)]
    radar._get.return_value = {"time": NOW, "result": {"list": page}}
    await radar.history_page(stream)
    assert stream not in radar._history
    assert radar.store.get_runtime(f"source:v1:gap:{stream}")["state"] == "COMPLETE"
    assert not radar.sources.live


async def test_watchlist_effective_thresholds_do_not_rewrite_custom_owner_values(radar):
    settings = OwnerSettings(watchlist=("spot/BTCUSDT",), watchlist_multiplier=0.6)
    before = radar.store.snapshot()
    result = await radar.snapshot(settings)
    spot = result.quotes["spot/BTCUSDT/3m"]
    linear = result.quotes["linear/BTCUSDT/3m"]
    assert spot["effective_settings"]["price_move_min"] == 1.2
    assert spot["effective_settings"]["turnover_spike_min"] == pytest.approx(1.8)
    assert linear["effective_settings"]["price_move_min"] == 2
    assert radar.store.snapshot() == before


async def test_universe_delta_keeps_unchanged_market_session(radar):
    async def hold(*args):
        await asyncio.Event().wait()

    radar._session = AsyncMock(side_effect=hold)
    radar.streams.clear()
    await radar.reconcile(OwnerSettings())
    linear_task = radar._sessions["linear:0"]
    spot_task = radar._sessions["spot:0"]
    radar.tickers["spot/BTCUSDT"]["turnover24h"] = "0"
    try:
        await radar.reconcile(OwnerSettings())
        assert radar._sessions["linear:0"] is linear_task
        assert not linear_task.cancelled() and spot_task.cancelled()
        assert "spot:0" not in radar._sessions
        assert all(s.startswith("linear/") for s in radar.sources.windows)
    finally:
        await radar.close()


async def test_restart_subscribes_without_eager_windows_and_snapshot_does_not_load_all(radar, monkeypatch):
    # Persisted windows exist, but a restart must connect before reading every window.
    radar.streams.clear()
    radar.sources.windows.clear()

    async def hold(*args):
        await asyncio.Event().wait()

    radar._session = AsyncMock(side_effect=hold)
    original = radar.sources.load
    loader = Mock(side_effect=AssertionError("eager startup window read"))
    monkeypatch.setattr(radar.sources, "load", loader)
    try:
        await radar.reconcile(OwnerSettings())
        assert set(radar._sessions) == {"spot:0", "linear:0"}
        result = await radar.snapshot(OwnerSettings())
        assert result.requested == 8 and not result.quotes
        assert set(result.errors.values()) == {"bootstrap_pending"}
        assert not radar.delivery_ready(result)
        loader.assert_not_called()
        monkeypatch.setattr(radar.sources, "load", original)
        # Recovery reuses persisted candles, yet the historical window is never live.
        radar._get = AsyncMock(return_value={"time": NOW, "result": {"list": history()}})
        await radar.backfill("spot/BTCUSDT/1m")
        result = await radar.snapshot(OwnerSettings())
        assert len(result.quotes) == 1 and not result.quotes["spot/BTCUSDT/1m"]["live"]
        assert not radar.delivery_ready(result)  # other market/TF remain pending
        # A closed live candle still loads only its own persisted window.
        radar.accept_message("linear", message("3m"), radar.streams)
        assert "linear/BTCUSDT/3m" in radar.sources.windows
        assert len(radar.sources.windows) == 2
    finally:
        await radar.close()


@pytest.mark.parametrize("bounded", [False, True])
async def test_restart_worker_reuses_all_market_tf_windows_without_rest_or_live(radar, bounded):
    if bounded:
        from cryptowatcher.market_storage import enable

        windows = {s: list(w.values()) for s, w in radar.sources.windows.items()}
        radar.store.db.execute("DELETE FROM runtime")
        radar.store.db.commit()
        enable(radar.store.db)
        radar.store.bounded = True
        radar.sources.windows.clear()
        for stream, candles in windows.items():
            radar.sources.put(stream, candles, recovery=True)
    radar.sources.windows.clear()
    radar.sources.live.clear()
    radar._get = AsyncMock(side_effect=AssertionError("Unnecessary REST bootstrap"))
    radar.on_observation = Mock()
    for stream in sorted(radar.streams):
        radar.sources.blocked[stream] = "stream_disconnected"
        radar.request_recovery(stream, 10)
    turns = []

    async def witness():
        while radar._pending or radar._working:
            turns.append(len(radar.sources.windows))
            await asyncio.sleep(0)

    witness_task = asyncio.create_task(witness())
    worker = asyncio.create_task(radar._worker())
    try:
        await wait_condition(lambda: not radar._pending and not radar._working)
        await witness_task
        result = await radar.snapshot(radar.store.snapshot().settings)
        assert len(result.quotes) == 8 and radar.delivery_ready(result)
        assert not radar.sources.blocked and not radar.sources.live
        assert any(0 < n < 8 for n in turns)  # no synchronous whole-universe restore
        radar._get.assert_not_called()
        radar.on_observation.assert_not_called()
        # The next actual closed WS observation remains eligible for live delivery.
        radar.accept_message("spot", message(), radar.streams)
        assert radar.sources.live["spot/BTCUSDT/1m"] == NOW
        assert radar.on_observation.call_args.args[-1] is True
    finally:
        worker.cancel()
        witness_task.cancel()
        await asyncio.gather(worker, witness_task, return_exceptions=True)
        await radar.close()


@pytest.mark.parametrize("condition", ["stale", "gap", "short", "conflict", "larger_lookback"])
async def test_local_restart_restore_does_not_bypass_required_rest(radar, condition):
    stream = "spot/BTCUSDT/1m"
    window = radar.sources.windows[stream]
    if condition == "stale":
        del window[max(window)]
    elif condition == "gap":
        del window[sorted(window)[-3]]
    elif condition == "short":
        radar.sources.windows[stream] = dict(sorted(window.items())[-5:])
    elif condition == "conflict":
        radar.sources.blocked[stream] = "conflicting_closed_candle"
    else:
        radar.sources.windows[stream] = dict(sorted(window.items())[-25:])
        settings = radar.store.snapshot()
        radar.store.update(settings.revision, settings.settings.changed(sma_periods=40))
    radar._get = AsyncMock(return_value={"time": NOW, "result": {"list": history()}})
    radar.on_observation = Mock()
    radar.request_recovery(stream, 0)
    worker = asyncio.create_task(radar._worker())
    try:
        await wait_condition(lambda: not radar._pending and not radar._working)
        radar._get.assert_awaited_once()
        assert stream not in radar.sources.live
        assert all(not call.args[-1] for call in radar.on_observation.call_args_list)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await radar.close()


async def test_inflight_recovery_cannot_resurrect_removed_stream(radar):
    stream = "spot/BTCUSDT/1m"

    async def response(*args):
        radar.streams.remove(stream)
        radar.sources.remove_window(stream)
        return {"time": NOW, "result": {"list": history()}}

    radar._get = response
    changes = radar.store.db.total_changes
    await radar.backfill(stream)
    assert stream not in radar.sources.windows
    assert radar.store.db.total_changes == changes


@pytest.mark.parametrize("priority", [0, 20])
async def test_failed_inflight_recovery_does_not_resurrect_retired_block_or_history_error(radar, priority):
    stream = "spot/BTCUSDT/1m"
    failed = asyncio.Event()
    radar.sources.blocked[stream] = "candle_gap"
    if priority == 20:
        radar._history[stream] = {"start": NOW - 60000, "end": NOW - 1, "state": "PENDING"}

    async def response(*args):
        radar.streams.remove(stream)
        radar.sources.remove_window(stream)
        radar._history.pop(stream, None)
        failed.set()
        raise MarketError("fixture_removed_during_http")

    radar._get = response
    radar.request_recovery(stream, priority)
    worker = asyncio.create_task(radar._worker())
    try:
        await asyncio.wait_for(failed.wait(), 1)
        assert stream not in radar.sources.blocked
        assert stream not in radar._history_errors
        assert stream not in radar._working and stream not in radar._pending
        assert not worker.done()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await radar.close()


async def test_socket_failure_after_scope_change_blocks_only_remaining_active_streams(radar):
    retired = "spot/BTCUSDT/1m"
    retained = "spot/BTCUSDT/3m"

    class RemovedSocket(Socket):
        async def __aenter__(self):
            radar.streams.remove(retired)
            radar.sources.remove_window(retired)
            raise MarketError("fixture_removed_during_connect")

    radar.connector = lambda *args, **kwargs: RemovedSocket()
    task = asyncio.create_task(radar._session("spot:0", "spot", [retired, retained]))
    try:
        await wait_condition(lambda: radar.connections.get("spot:0", {}).get("state") == "RECONNECTING")
        assert retired not in radar.sources.blocked
        assert radar.sources.blocked[retained] == "stream_disconnected"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await radar.close()


async def test_no_gap_reconnect_preserves_current_window_without_rest_fanout(radar):
    stream = "spot/BTCUSDT/1m"
    socket = Socket()
    radar.connector = lambda *args, **kwargs: socket
    radar.sources.blocked[stream] = "stream_disconnected"
    task = asyncio.create_task(radar._session("spot:0", "spot", [stream]))
    try:
        await wait_condition(lambda: radar.connections.get("spot:0", {}).get("state") == "CONNECTED")
        assert stream not in radar._pending and stream not in radar.sources.blocked
        assert not radar.sources.live
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await radar.close()


async def test_reconnect_recovers_known_gap_even_when_latest_boundary_is_current(radar):
    stream = "spot/BTCUSDT/1m"
    socket = Socket()
    radar.connector = lambda *args, **kwargs: socket
    radar.sources.blocked[stream] = "candle_gap"
    task = asyncio.create_task(radar._session("spot:0", "spot", [stream]))
    try:
        await wait_condition(lambda: radar.connections.get("spot:0", {}).get("state") == "CONNECTED")
        assert radar._pending[stream][0] == 0
        assert radar.sources.blocked[stream] == "candle_gap"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await radar.close()


async def test_persisted_ws_producer_durable_delivery_no_legacy_double_send(
    radar, infrastructure, monkeypatch
):
    # Keep exchange ticks without wall-time drift across the 2s notification coalescing boundary.
    monkeypatch.setattr(radar, "server_now", lambda: radar._server_ms)
    controller = Controller(infrastructure, radar.store, radar)
    stream = "spot/BTCUSDT/1m"
    history_store = History(radar.store)
    radar.accept_message("spot", message("1m", confirm=False), radar.streams)
    assert not history_store.events()
    radar.accept_message("spot", message("1m"), radar.streams)
    events = history_store.events()
    assert {e["detector_id"] for e in events} >= {"price", "turnover", "breakout", "composite"}
    assert all(e["source_inputs"][-1]["start"] == NOW for e in events)
    source_row = radar.store.get_runtime(radar.sources.prefix(stream) + f"{NOW:016d}")
    assert source_row["source"] == "websocket"
    before = radar.store.db.total_changes
    radar.accept_message("spot", message("1m"), radar.streams)
    assert radar.store.db.total_changes == before
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=8)))
    await controller.poll(SimpleNamespace(bot=bot))
    bot.send_message.assert_not_called()
    radar.server_now = lambda: NOW + 62_001
    await controller.deliver(SimpleNamespace(bot=bot))
    bot.send_message.assert_awaited_once()
    text = bot.send_message.call_args.args[1]
    assert "Совпадение цены и оборота" in text and "свежее" in text and "1m" in text
    assert "Первое уведомление" in text and "Интенсивность" in text and "Оборот свечи" in text
    assert all(e["settings_revision"] == 1 for e in events)
    radar.sources.blocked[stream] = "candle_gap"
    count = len(history_store.events())
    radar.publish_observation(stream, live=True)
    assert len(history_store.events()) == count
    await radar.close()


async def test_rest_bootstrap_history_never_queues_live(radar, infrastructure):
    Controller(infrastructure, radar.store, radar)
    radar._get = AsyncMock(return_value={"time": NOW, "result": {"list": history()}})
    await radar.backfill("spot/BTCUSDT/1m")
    saved = History(radar.store)
    assert saved.events() and all(not e["live"] for e in saved.events())
    assert not saved.items("pending:") and not saved.items("active:")
    await radar.close()
