"""Shared four-timeframe radar: public WS, bounded REST and priority recovery."""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Callable
from typing import Any

import httpx
from websockets.asyncio.client import connect

from .config import TIMEFRAMES, ConfigError, OwnerSettings
from .detector_config import effective_parameters
from .market import BybitREST, MarketError, RadarResult, evaluate_composite, finite
from .source_context import save
from .sources import WINDOW_SIZE, Candle, Sources
from .storage import Store, encode
from .universe import CLASSIFICATION_VERSION, Instrument, active_instruments, discover


def topic(stream: str) -> str:
    _, symbol, tf = stream.split("/")
    return f"kline.{tf[:-1]}.{symbol}"


def shards(streams: list[str]) -> list[list[str]]:
    groups: list[list[str]] = []
    group: list[str] = []
    for stream in sorted(streams):
        if len(json.dumps([topic(s) for s in (*group, stream)])) > 20_000:
            groups.append(group)
            group = []
        group.append(stream)
    if group:
        groups.append(group)
    return groups


class BybitRadar(BybitREST):
    def __init__(
        self,
        store: Store,
        client: httpx.AsyncClient | None = None,
        *,
        connector: Callable[..., Any] = connect,
    ) -> None:
        super().__init__(client)
        self.store = store
        self.sources = Sources(store)
        self.connector = connector
        self.instruments: dict[str, Instrument] = {}
        self.quarantine: dict[str, str] = {}
        self.tickers: dict[str, dict[str, Any]] = {}
        self.metadata_at: dict[str, float] = {}
        self.tickers_at: dict[str, float] = {}
        self.ticker_locks = {market: asyncio.Lock() for market in ("spot", "linear")}
        self.ticker_server_ms: dict[str, int] = {}
        self.market_errors: dict[str, str] = {}
        self.streams: set[str] = set()
        self.connections: dict[str, dict[str, Any]] = {}
        self._server_ms = 0
        self._server_at = 0.0
        self._tasks: list[asyncio.Task[Any]] = []
        self._sessions: dict[str, asyncio.Task[Any]] = {}
        self._groups: dict[str, tuple[str, ...]] = {}
        self._work_ready = asyncio.Event()
        self._pending: dict[str, tuple[int, int]] = {}
        self._working: set[str] = set()
        self._history: dict[str, dict[str, Any]] = {}
        self._history_errors: dict[str, str] = {}
        self._sequence = 0
        self._last_market = "linear"
        self._started = False
        self.on_observation: Callable[[str, list[Candle], float, float, int, bool], None] | None = None

    def publish_observation(self, stream: str, *, live: bool, through: int | None = None) -> None:
        if self.on_observation is None or stream in self.sources.blocked:
            return
        instrument = stream.rsplit("/", 1)[0]
        candles = sorted(self.sources.windows[stream].values(), key=lambda c: c.start)
        if through is not None:
            candles = [c for c in candles if c.start <= through]
        self.on_observation(
            stream,
            candles,
            finite(self.tickers[instrument]["turnover24h"]),
            self.instruments[instrument].tick_size,
            self.server_now(),
            live,
        )

    def server_now(self) -> int:
        return self._server_ms + int((time.monotonic() - self._server_at) * 1000) if self._server_ms else 0

    def observe_clock(self, ms: Any) -> None:
        value = int(finite(ms, positive=True))
        if value >= self.server_now():
            self._server_ms, self._server_at = value, time.monotonic()

    async def _get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        result = await super()._get(endpoint, params)
        self.observe_clock(result["time"])
        return result

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._tasks = [asyncio.create_task(self._manager()), asyncio.create_task(self._diagnostic_loop())]
        self._tasks += [asyncio.create_task(self._worker()) for _ in range(4)]

    async def close(self) -> None:
        tasks = [*self._tasks, *self._sessions.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._sessions.clear()
        self._groups.clear()
        self._started = False
        await super().close()

    async def refresh(self, settings: OwnerSettings) -> None:
        for market in ("spot", "linear"):
            try:
                if time.monotonic() - self.metadata_at.get(market, float("-inf")) >= 600:
                    instruments, quarantine = await discover(self, market)
                    records = [
                        (
                            f"source:v1:metadata:{key}",
                            encode(
                                {
                                    "classification": CLASSIFICATION_VERSION,
                                    "metadata": value.metadata,
                                    "server_ms": self.server_now(),
                                    "reason": "eligible",
                                }
                            ),
                        )
                        for key, value in instruments.items()
                    ]
                    records += [
                        (
                            f"source:v1:quarantine:{key}",
                            encode(
                                {
                                    "classification": CLASSIFICATION_VERSION,
                                    "reason": reason,
                                    "server_ms": self.server_now(),
                                }
                            ),
                        )
                        for key, reason in quarantine.items()
                    ]
                    with self.store.db:
                        self.store.db.executemany("INSERT OR REPLACE INTO runtime VALUES (?,?)", records)
                        for key, value in instruments.items():
                            save(
                                self.store,
                                "instrument",
                                key,
                                {
                                    "schema": "cryptowatcher.public-instrument.v1",
                                    "classification": CLASSIFICATION_VERSION,
                                    "metadata": value.metadata,
                                    "tick_size": value.tick_size,
                                    "market_observation_ms": self.server_now(),
                                    "available_at_ms": self.server_now(),
                                    "source": "BYBIT_REST_INSTRUMENTS",
                                },
                            )
                    self.instruments = {
                        **{k: v for k, v in self.instruments.items() if v.market != market},
                        **instruments,
                    }
                    self.quarantine = {
                        **{k: v for k, v in self.quarantine.items() if not k.startswith(market + "/")},
                        **quarantine,
                    }
                    self.metadata_at[market] = time.monotonic()
                await self.refresh_tickers(market)
                self.market_errors.pop(market, None)
            except (MarketError, KeyError, TypeError, ValueError) as exc:
                self.market_errors[market] = (
                    str(exc) if isinstance(exc, MarketError) else "invalid_discovery_response"
                )
        await self.reconcile(settings)
        self.persist_diagnostics()

    async def refresh_tickers(self, market: str, max_age: int = 300) -> None:
        async with self.ticker_locks[market]:
            if time.monotonic() - self.tickers_at.get(market, float("-inf")) < max_age:
                return
            result = await self._get("/v5/market/tickers", {"category": market})
            rows = result["result"]["list"]
            tickers = {}
            for row in rows:
                key = f"{market}/{row['symbol']}"
                if key in tickers:
                    raise MarketError("duplicate_ticker")
                tickers[key] = row
            with self.store.db:
                for key, row in tickers.items():
                    if key not in self.instruments:
                        continue
                    save(
                        self.store,
                        "ticker",
                        key,
                        {
                            "schema": "cryptowatcher.public-ticker.v1",
                            "values": row,
                            "units": {"turnover24h": "USDT"},
                            "market_observation_ms": int(result["time"]),
                            "available_at_ms": self.server_now(),
                            "source": "BYBIT_REST_TICKERS",
                        },
                    )
            self.tickers = {
                **{k: v for k, v in self.tickers.items() if not k.startswith(market + "/")},
                **tickers,
            }
            self.tickers_at[market] = time.monotonic()
            self.ticker_server_ms[market] = int(result["time"])

    async def reconcile(self, settings: OwnerSettings) -> None:
        active, _ = active_instruments(self.instruments, self.tickers, settings)
        new_streams = {f"{instrument.key}/{tf}" for instrument in active for tf in TIMEFRAMES}
        if new_streams == self.streams:
            return
        removed = self.streams - new_streams
        added = new_streams - self.streams
        self.streams = new_streams
        for stream in removed:
            self.sources.remove_window(stream)
            self._pending.pop(stream, None)
            self._history.pop(stream, None)
            self._history_errors.pop(stream, None)
        for stream in added:
            self.sources.load(stream)
            saved = self.store.get_runtime(f"source:v1:gap:{stream}", {})
            if saved.get("state") == "PENDING":
                self._history[stream] = saved
        # Each market gets early bootstrap slots; no market waits for the other's entire universe.
        by_market = {
            m: sorted(
                [s for s in added if s.startswith(m + "/")],
                key=lambda s: -finite(self.tickers[s.rsplit("/", 1)[0]]["turnover24h"]),
            )
            for m in ("spot", "linear")
        }
        for index in range(max(map(len, by_market.values()), default=0)):
            for market in by_market:
                if index < len(by_market[market]):
                    self.request_recovery(by_market[market][index], 10)
        new_groups: dict[str, tuple[str, ...]] = {}
        for market in ("spot", "linear"):
            for index, group in enumerate(shards([s for s in new_streams if s.startswith(market + "/")])):
                new_groups[f"{market}:{index}"] = tuple(group)
        changed = {
            name
            for name in set(self._groups) | set(new_groups)
            if self._groups.get(name) != new_groups.get(name)
        }
        old_sessions = [self._sessions[name] for name in changed if name in self._sessions]
        for task in old_sessions:
            task.cancel()
        await asyncio.gather(*old_sessions, return_exceptions=True)
        for name in changed:
            self._sessions.pop(name, None)
            self.connections.pop(name, None)
            if name in new_groups:
                self._sessions[name] = asyncio.create_task(
                    self._session(name, name.split(":")[0], list(new_groups[name]))
                )
        self._groups = new_groups

    async def _manager(self) -> None:
        while True:
            try:
                await self.refresh(self.store.snapshot().settings)
                self.market_errors.pop("manager", None)
            except MarketError as exc:
                self.market_errors["manager"] = str(exc)
            if not self.components_healthy():
                self.market_errors["manager"] = "ingestion_worker_failed"
            self.persist_diagnostics()
            await asyncio.sleep(5)

    async def _diagnostic_loop(self) -> None:
        # REST discovery/backoff must not freeze the persisted WS/recovery snapshot.
        while True:
            self.persist_diagnostics()
            await asyncio.sleep(5)

    def request_recovery(self, stream: str, priority: int = 0) -> None:
        if stream not in self.streams or stream in self._working:
            return
        previous = self._pending.get(stream)
        if previous and previous[0] <= priority:
            return
        self._sequence += 1
        self._pending[stream] = (priority, self._sequence)
        self._work_ready.set()

    async def backfill(self, stream: str) -> None:
        market, symbol, tf = stream.split("/")
        result = await self._get(
            "/v5/market/kline",
            {
                "category": market,
                "symbol": symbol,
                "interval": tf[:-1],
                "limit": WINDOW_SIZE + 2,
            },
        )
        if stream not in self.streams:
            return  # Universe changed during network I/O; do not resurrect its memory window.
        clock = int(result["time"])
        step = int(tf[:-1]) * 60_000
        raw_rows = result["result"]["list"]
        if not isinstance(raw_rows, list) or any(
            not isinstance(row, list) or len(row) != 7 for row in raw_rows
        ):
            raise MarketError("invalid_recovery_history")
        candles = [
            Candle.parse(row, tf, "rest", int(time.time() * 1000), clock)
            for row in raw_rows
            if int(finite(row[0])) + step <= clock
        ]
        candles = sorted(candles, key=lambda c: c.start)[-WINDOW_SIZE:]
        if not candles:
            raise MarketError("empty_closed_history")
        self.sources.load(stream)
        previous_end = max((c.end for c in self.sources.windows[stream].values()), default=0)
        oldest = candles[0].start
        if previous_end and previous_end < oldest:
            self.record_gap(stream, previous_end, oldest - 1)
        self.sources.put(stream, candles, recovery=True)
        self.publish_observation(stream, live=False)
        if stream in self._history:
            gap = self._history[stream]
            gap["end"] = min(gap["end"], oldest - 1)
            if gap["end"] < gap["start"]:
                self._history.pop(stream)
                gap["state"] = "COMPLETE"
            self.store.put_runtime(f"source:v1:gap:{stream}", gap)

    def record_gap(self, stream: str, start: int, end: int) -> None:
        tf = stream.rsplit("/", 1)[1]
        step = int(tf[:-1]) * 60_000
        retention_start = ((self.server_now() - 90 * 86_400_000 + step - 1) // step) * step
        start = max(start, retention_start)
        if start > end:
            return
        previous = self._history.get(stream, {})
        gap = {
            "state": "PENDING",
            "start": min(start, previous.get("start", start)),
            "end": max(end, previous.get("end", end)),
        }
        self._history[stream] = gap
        self.store.put_runtime(f"source:v1:gap:{stream}", gap)

    async def history_page(self, stream: str) -> None:
        gap = self._history[stream]
        market, symbol, tf = stream.split("/")
        step = int(tf[:-1]) * 60_000
        result = await self._get(
            "/v5/market/kline",
            {
                "category": market,
                "symbol": symbol,
                "interval": tf[:-1],
                "limit": 1000,
                "start": gap["start"],
                "end": gap["end"],
            },
        )
        if stream not in self.streams:
            return
        rows = result["result"]["list"]
        if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) != 7 for row in rows):
            raise MarketError("invalid_historical_page")
        candles = sorted(
            [
                Candle.parse(row, tf, "rest-history", int(time.time() * 1000), int(result["time"]))
                for row in rows
            ],
            key=lambda c: c.start,
        )
        if not candles or candles[-1].start != ((gap["end"] + 1) // step - 1) * step:
            raise MarketError("incomplete_historical_page")
        if any(b.start - a.start != step for a, b in zip(candles, candles[1:])):
            raise MarketError("incomplete_historical_page")
        if candles[0].start < gap["start"] or candles[-1].end > gap["end"] + 1:
            raise MarketError("historical_page_out_of_bounds")
        self.sources.put(stream, candles)
        gap["end"] = candles[0].start - 1
        if gap["end"] < gap["start"]:
            self._history.pop(stream)
            gap["state"] = "COMPLETE"
        self._history_errors.pop(stream, None)
        self.store.put_runtime(f"source:v1:gap:{stream}", gap)

    async def _worker(self) -> None:
        while True:
            if not self._pending:
                self._work_ready.clear()
                await self._work_ready.wait()
                continue
            stream, (priority, _) = min(
                self._pending.items(),
                key=lambda item: (
                    item[1][0],
                    item[0].split("/", 1)[0] == self._last_market,
                    item[1][1],
                ),
            )
            self._last_market = stream.split("/", 1)[0]
            self._pending.pop(stream)
            if stream not in self.streams:
                continue
            self._working.add(stream)
            try:
                try:
                    if priority == 20 and stream in self._history:
                        await self.history_page(stream)
                    else:
                        await self.backfill(stream)
                except (MarketError, KeyError, TypeError, ValueError):
                    if priority == 20:
                        self._history_errors[stream] = "historical_recovery_failed"
                    else:
                        self.sources.blocked[stream] = "rest_recovery_failed"
                    # Bounded retry; rate-limit backoff is shared in the REST adapter.
                    await asyncio.sleep(5)
                    self._working.discard(stream)
                    self.request_recovery(stream, priority)
            finally:
                self._working.discard(stream)
                if stream in self._history:
                    self.request_recovery(stream, 20)

    def accept_message(self, market: str, message: dict[str, Any], allowed: set[str]) -> None:
        parts = str(message.get("topic", "")).split(".")
        if len(parts) != 3 or parts[0] != "kline":
            return
        stream = f"{market}/{parts[2]}/{parts[1]}m"
        if stream not in allowed or stream not in self.streams:
            return
        self.observe_clock(message["ts"])
        for row in message.get("data", []):
            if row.get("confirm") is not True:
                continue
            tf = parts[1] + "m"
            try:
                if str(row.get("interval")) != parts[1]:
                    raise MarketError("candle_interval_mismatch")
                candle = Candle.parse(
                    [
                        row["start"],
                        row["open"],
                        row["high"],
                        row["low"],
                        row["close"],
                        row["volume"],
                        row["turnover"],
                    ],
                    tf,
                    "websocket",
                    int(time.time() * 1000),
                    int(message["ts"]),
                )
                if int(finite(row["end"])) != candle.end - 1:
                    raise MarketError("candle_end_mismatch")
                fresh = 0 <= self.server_now() - candle.end <= candle.step
                self.sources.load(stream)
                previous_end = max((c.end for c in self.sources.windows[stream].values()), default=0)
                if previous_end and candle.start > previous_end:
                    self.record_gap(stream, previous_end, candle.start - 1)
                previous = self.sources.windows[stream].get(candle.start)
                advanced = self.sources.put(stream, [candle], live=fresh)
                if previous is None:
                    self.publish_observation(stream, live=advanced, through=candle.start)
                if (
                    stream in self.sources.blocked
                    or len(self.sources.windows[stream]) < self.store.snapshot().settings.sma_periods + 2
                ):
                    self.request_recovery(stream, 0)
            except (MarketError, KeyError, TypeError, ValueError) as exc:
                self.sources.blocked[stream] = (
                    str(exc) if isinstance(exc, MarketError) else "invalid_ws_candle"
                )
                self.sources.live.pop(stream, None)
                self.request_recovery(stream, 0)

    async def _ping(self, websocket: Any) -> None:
        try:
            while True:
                await asyncio.sleep(20)
                await websocket.send(json.dumps({"op": "ping"}))
        finally:
            await websocket.close()

    async def _session(self, name: str, market: str, streams: list[str]) -> None:
        allowed = set(streams)
        backoff = 1.0
        while True:
            self.connections[name] = {
                "state": "CONNECTING",
                "market": market,
                "requested": len(streams),
                "acknowledged": 0,
            }
            try:
                async with self.connector(
                    f"wss://stream.bybit.com/v5/public/{market}",
                    ping_interval=None,
                    open_timeout=15,
                    close_timeout=5,
                    max_queue=16,
                    max_size=1_048_576,
                ) as websocket:
                    ping = asyncio.create_task(self._ping(websocket))
                    try:
                        for stream in streams:
                            self.sources.live.pop(stream, None)
                            window = self.sources.windows.get(stream, {})
                            latest_end = max((c.end for c in window.values()), default=0)
                            step = int(stream.rsplit("/", 1)[1][:-1]) * 60_000
                            current = latest_end and latest_end == (self.server_now() // step) * step
                            if current and self.sources.blocked.get(stream) == "stream_disconnected":
                                self.sources.blocked.pop(stream)
                            if (
                                not current or stream in self.sources.blocked
                            ) and stream not in self._pending:
                                self.request_recovery(stream, 0)
                        batch_size = 10 if market == "spot" else 100
                        for offset in range(0, len(streams), batch_size):
                            batch = streams[offset : offset + batch_size]
                            request_id = f"cw{offset}"
                            await websocket.send(
                                json.dumps(
                                    {
                                        "op": "subscribe",
                                        "req_id": request_id,
                                        "args": [topic(s) for s in batch],
                                    }
                                )
                            )
                            deadline = time.monotonic() + 15
                            while True:
                                raw = await asyncio.wait_for(
                                    websocket.recv(), max(0.01, deadline - time.monotonic())
                                )
                                message = json.loads(raw)
                                self.accept_message(market, message, allowed)
                                if message.get("op") == "subscribe" and message.get("req_id") == request_id:
                                    if message.get("success") is not True:
                                        raise MarketError("subscription_rejected")
                                    self.connections[name]["acknowledged"] += len(batch)
                                    break
                        self.connections[name]["state"] = "CONNECTED"
                        backoff = 1.0
                        while True:
                            raw = await asyncio.wait_for(websocket.recv(), 65)
                            self.accept_message(market, json.loads(raw), allowed)
                            self.connections[name]["last_received_ms"] = int(time.time() * 1000)
                    finally:
                        ping.cancel()
                        await asyncio.gather(ping, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connections[name]["state"] = "RECONNECTING"
                self.connections[name]["reason"] = (
                    str(exc) if isinstance(exc, MarketError) else type(exc).__name__
                )
                for stream in streams:
                    self.sources.blocked.setdefault(stream, "stream_disconnected")
                    self.sources.live.pop(stream, None)
                self.persist_diagnostics()
                await asyncio.sleep(random.uniform(backoff / 2, backoff))
                backoff = min(60, backoff * 2)

    async def validate_instruments(self, keys: tuple[str, ...]) -> None:
        for key in keys:
            market = key.split("/", 1)[0]
            if market not in self.metadata_at or market in self.market_errors:
                raise MarketError("metadata_unavailable_try_after_refresh")
            if key not in self.instruments:
                raise ConfigError("Instrument ineligible or unknown; list unchanged")
            if (
                key not in self.tickers
                or finite(self.tickers[key]["turnover24h"])
                < self.store.snapshot().settings.liquidity_floor_24h
            ):
                raise ConfigError("Instrument below liquidity floor; list unchanged")

    async def snapshot(self, settings: OwnerSettings) -> RadarResult:
        active, excluded = active_instruments(self.instruments, self.tickers, settings)
        quotes: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}
        now_ms = self.server_now()
        for instrument in active:
            market, symbol = instrument.market, instrument.symbol
            for tf in TIMEFRAMES:
                stream = f"{instrument.key}/{tf}"
                try:
                    if market in self.market_errors:
                        raise MarketError(self.market_errors[market])
                    if stream not in self.streams:
                        raise MarketError("subscription_pending")
                    if stream in self.sources.blocked:
                        raise MarketError(self.sources.blocked[stream])
                    parameters = effective_parameters(settings, instrument.key)
                    effective = settings.changed(
                        bybit_category=market,
                        signal_timeframe=tf,
                        price_move_min=parameters[f"price_{tf}"],
                        turnover_spike_min=parameters["turnover_ratio"],
                        sma_periods=parameters["turnover_lookback"],
                    )
                    payload = evaluate_composite(
                        self.sources.rows(stream, effective.sma_periods + 2),
                        self.tickers[instrument.key],
                        effective,
                        now_ms,
                    )
                    if payload is None:
                        raise MarketError("insufficient_history_or_zero_baseline")
                    age = now_ms - payload["candle_end_ms"]
                    payload["price"] = payload["reference_price"]
                    source = self.sources.windows[stream][payload["candle_start_ms"]]
                    payload.update(
                        symbol=symbol,
                        market=market,
                        instrument_key=instrument.key,
                        source="bybit",
                        observation_time_ms=payload["candle_end_ms"],
                        detected_time_ms=now_ms,
                        receipt_time_ms=source.received_ms,
                        source_window_end_ms=source.end,
                        ticker_time_ms=self.ticker_server_ms.get(market, 0),
                        freshness="FRESH" if 0 <= age <= int(tf[:-1]) * 60_000 else "STALE",
                        live=self.sources.live.get(stream) == payload["candle_start_ms"],
                        effective_settings=effective.payload(),
                    )
                    quotes[stream] = payload
                except (MarketError, ConfigError) as exc:
                    errors[stream] = (
                        str(exc) if isinstance(exc, MarketError) else "invalid_effective_watchlist_settings"
                    )
        errors.update({key: reason for key, reason in excluded.items() if reason != "below_liquidity_floor"})
        for market in ("spot", "linear"):
            if market not in self.metadata_at or market not in self.tickers_at:
                errors[market] = "discovery_pending"
            elif market in self.market_errors:
                errors[market] = self.market_errors[market]
        if "manager" in self.market_errors:
            errors["manager"] = self.market_errors["manager"]
        return RadarResult(quotes, errors, len(active) * len(TIMEFRAMES))

    def delivery_ready(self, result: RadarResult) -> bool:
        if not self.components_healthy() or "manager" in self.market_errors:
            return False
        if any(
            m not in self.metadata_at or m not in self.tickers_at or m in self.market_errors
            for m in ("spot", "linear")
        ):
            return False
        if any(
            c["state"] != "CONNECTED" or c["acknowledged"] != c["requested"]
            for c in self.connections.values()
        ):
            return False
        expected = {(s.split("/")[0], s.split("/")[2]) for s in self.streams}
        available = {
            (q["market"], q["signal_timeframe"]) for q in result.quotes.values() if q["freshness"] == "FRESH"
        }
        return expected <= available

    def components_healthy(self) -> bool:
        return all(not task.done() for task in self._tasks)

    def persist_diagnostics(self) -> None:
        self.store.put_runtime(
            "ingestion",
            {
                "classification": CLASSIFICATION_VERSION,
                "timeframes": list(TIMEFRAMES),
                "eligible_metadata": {
                    m: sum(i.market == m for i in self.instruments.values()) for m in ("spot", "linear")
                },
                "quarantine_reasons": {
                    r: list(self.quarantine.values()).count(r) for r in set(self.quarantine.values())
                },
                "subscriptions": self.connections,
                "active_streams": len(self.streams),
                "recovery_pending": len(self._pending),
                "recovery_working": len(self._working),
                "blocked_streams": len(self.sources.blocked),
                "historical_gaps_pending": len(self._history),
                "historical_gap_errors": self._history_errors,
                "buffered_candles": sum(map(len, self.sources.windows.values())),
                "market_errors": self.market_errors,
                "server_ms": self.server_now(),
                "at": time.time(),
            },
        )
