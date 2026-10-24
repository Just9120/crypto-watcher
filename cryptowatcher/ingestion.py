"""Shared four-timeframe radar: public WS, bounded REST and priority recovery."""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections import Counter
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any, Iterator

import httpx
from websockets.asyncio.client import connect

from .config import (
    METADATA_REFRESH_SECONDS,
    SOURCE_REFRESH_GRACE_SECONDS,
    TICKER_REFRESH_SECONDS,
    TIMEFRAMES,
    ConfigError,
    OwnerSettings,
)
from .detector_config import effective_parameters
from .market import BybitREST, MarketError, RadarResult, evaluate_composite, finite
from .source_context import save
from .sources import WINDOW_SIZE, Candle, Sources
from .storage import RUNTIME_UPSERT, Store, encode
from .universe import CLASSIFICATION_VERSION, Instrument, active_instruments, discover

SUBSCRIPTION_BUFFER_MESSAGES = 128
SUBSCRIPTION_BUFFER_BYTES = 2 * 1024 * 1024
SUBSCRIPTION_SHARD_TOPICS = 100
SESSION_SHUTDOWN_SECONDS = 15
MANAGER_INTERVAL_SECONDS = 5
ObservationCallback = Callable[[str, list[Candle], float, float, int, bool], None]


def topic(stream: str) -> str:
    _, symbol, tf = stream.split("/")
    return f"kline.{tf[:-1]}.{symbol}"


def shards(streams: list[str]) -> list[list[str]]:
    groups: list[list[str]] = []
    group: list[str] = []
    for stream in sorted(streams):
        if (
            len(group) >= SUBSCRIPTION_SHARD_TOPICS
            or len(json.dumps([topic(s) for s in (*group, stream)])) > 20_000
        ):
            groups.append(group)
            group = []
        group.append(stream)
    if group:
        groups.append(group)
    return groups


def _subscription_groups(
    market: str, streams: set[str], previous: dict[str, tuple[str, ...]]
) -> dict[str, tuple[str, ...]]:
    """Keep unaffected sockets while retaining the original count/byte bounds."""
    fresh = {f"{market}:{i}": tuple(group) for i, group in enumerate(shards(list(streams)))}
    prior = {name: group for name, group in previous.items() if name.startswith(market + ":")}
    if not prior:
        return fresh
    groups = {name: tuple(s for s in group if s in streams) for name, group in prior.items()}
    groups = {name: group for name, group in groups.items() if group}
    assigned = {stream for group in groups.values() for stream in group}

    def place(stream: str) -> bool:
        # Prefer already changed groups, then use existing spare capacity.
        for name in sorted(groups, key=lambda name: (groups[name] == prior.get(name), name)):
            group = groups[name]
            if (
                len(group) < SUBSCRIPTION_SHARD_TOPICS
                and len(json.dumps([topic(s) for s in (*group, stream)])) <= 20_000
            ):
                groups[name] = (*group, stream)
                return True
        return False

    for stream in sorted(streams - assigned):
        if not place(stream):
            index = 0
            while f"{market}:{index}" in groups:
                index += 1
            groups[f"{market}:{index}"] = (stream,)
    # Removals can leave sparse groups. Consolidate the smallest one first;
    # never allow more sockets than the original bounded packing would use.
    while len(groups) > len(fresh):
        smallest = min(groups, key=lambda name: (len(groups[name]), name))
        moved = groups.pop(smallest)
        if not all(place(stream) for stream in moved):
            return fresh  # Unusual topic byte sizes may require a full repack.
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
        self._recovered_buckets: set[tuple[str, str]] = set()
        self._started = False
        self._scope_revision: int | None = None
        self._scope_lock = asyncio.Lock()
        self._manager_failures: dict[str, Any] = {"count": 0}
        self._work_timing = {
            name: {"calls": 0, "failures": 0, "active_seconds": 0.0, "cpu_seconds": 0.0}
            for name in (
                "closed_ws_source",
                "observation",
                "recovery_source",
                "referenced_recovery_response",
                "storage_repack",
                "retention",
                "ticker_persistence",
                "discovery_persistence",
                "status_snapshot",
                "diagnostics",
            )
        }
        self._timing_stack: list[list[float]] = []
        self.on_observation: ObservationCallback | None = None
        self._transaction_observer: tuple[ObservationCallback, ObservationCallback] | None = None

    def bind_observation(self, callback: ObservationCallback, transactional: ObservationCallback) -> None:
        """Bind the synchronous joined form only while this observer is installed."""
        self.on_observation = callback
        self._transaction_observer = callback, transactional

    def _measure(self, name: str, call: Callable[[], Any]) -> Any:
        with self._timed(name):
            return call()

    @contextmanager
    def _timed(self, name: str) -> Iterator[None]:
        """Measure exclusive synchronous work; callers must not await here."""
        value = self._work_timing[name]
        began, cpu = time.monotonic(), time.thread_time()
        children = [0.0, 0.0]
        self._timing_stack.append(children)
        try:
            yield
        except BaseException:
            value["failures"] += 1
            raise
        finally:
            elapsed = max(0.0, time.monotonic() - began)
            used_cpu = max(0.0, time.thread_time() - cpu)
            self._timing_stack.pop()
            if self._timing_stack:
                self._timing_stack[-1][0] += elapsed
                self._timing_stack[-1][1] += used_cpu
            value["calls"] += 1
            value["active_seconds"] += max(0.0, elapsed - children[0])
            value["cpu_seconds"] += max(0.0, used_cpu - children[1])

    def publish_observation(
        self,
        stream: str,
        *,
        live: bool,
        through: int | None = None,
        _source: Sources | None = None,
        _callback: ObservationCallback | None = None,
    ) -> None:
        source = self.sources if _source is None else _source
        callback = self.on_observation if _callback is None else _callback
        if callback is None or stream in source.blocked:
            return
        instrument = stream.rsplit("/", 1)[0]
        settings = self.store.snapshot().settings
        if instrument not in self.instruments:
            return
        if settings.alert_universe_mode == "custom" and instrument not in settings.instruments():
            return
        liquidity = finite(self.tickers[instrument]["turnover24h"])
        if liquidity < settings.liquidity_floor_24h:
            return
        candles = sorted(source.windows[stream].values(), key=lambda c: c.start)
        if through is not None:
            candles = [c for c in candles if c.start <= through]
        if not candles:
            return
        now_ms = self.server_now()
        self._measure(
            "observation",
            lambda: callback(
                stream,
                candles,
                liquidity,
                self.instruments[instrument].tick_size,
                now_ms,
                live,
            ),
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
        self._tasks = [
            asyncio.create_task(self._manager(), name="ingestion:discovery"),
            asyncio.create_task(self._diagnostic_loop(), name="ingestion:diagnostics"),
            asyncio.create_task(self._configuration_loop(), name="ingestion:configuration"),
        ]
        self._tasks += [asyncio.create_task(self._worker(), name=f"ingestion:recovery:{i}") for i in range(4)]

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
        revision = self.store.snapshot().revision
        for market in ("spot", "linear"):
            try:
                if time.monotonic() - self.metadata_at.get(market, float("-inf")) >= METADATA_REFRESH_SECONDS:
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
                    with self._timed("discovery_persistence"), self.store.db:
                        self.store.db.executemany(RUNTIME_UPSERT, records)
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
        current = self.store.snapshot()
        await self.reconcile(current.settings if current.revision != revision else settings)
        self.persist_diagnostics()

    async def refresh_tickers(self, market: str, max_age: int = TICKER_REFRESH_SECONDS) -> None:
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
            with self._timed("ticker_persistence"), self.store.db:
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
        async with self._scope_lock:
            await self._reconcile(settings)

    async def _reconcile(self, settings: OwnerSettings) -> None:
        active, _ = active_instruments(self.instruments, self.tickers, settings)
        new_streams = {f"{instrument.key}/{tf}" for instrument in active for tf in TIMEFRAMES}
        current = self.store.snapshot()
        self._scope_revision = current.revision if settings == current.settings else None
        new_groups: dict[str, tuple[str, ...]] = {}
        for market in ("spot", "linear"):
            new_groups.update(
                _subscription_groups(
                    market, {s for s in new_streams if s.startswith(market + "/")}, self._groups
                )
            )
        if new_streams == self.streams and new_groups == self._groups and self.scope_synced():
            return
        removed = self.streams - new_streams
        added = new_streams - self.streams
        self.streams = new_streams
        self._recovered_buckets.intersection_update(self.recovery_bucket(stream) for stream in new_streams)
        for stream in removed:
            self.sources.remove_window(stream)
            self._pending.pop(stream, None)
            self._history.pop(stream, None)
            self._history_errors.pop(stream, None)
        for index, stream in enumerate(added):
            saved = self.store.get_runtime(f"source:v1:gap:{stream}", {})
            if saved.get("state") == "PENDING":
                self._history[stream] = saved
            if index % 32 == 31:
                await asyncio.sleep(0)
        # Recovery/closed WS observations load each bounded window on demand. Loading
        # every stored candle here blocks subscriptions and the event loop at restart.
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
        changed = {
            name
            for name in set(self._groups) | set(new_groups)
            if self._groups.get(name) != new_groups.get(name)
            or name not in self._sessions
            or self._sessions[name].done()
            or self._sessions[name].cancelling()
        }
        old_sessions = [self._sessions[name] for name in changed if name in self._sessions]
        for task in old_sessions:
            task.cancel()
        if old_sessions:
            done, pending = await asyncio.wait(old_sessions, timeout=SESSION_SHUTDOWN_SECONDS)
            for task in done:
                if not task.cancelled():
                    task.exception()  # Retire the old task without exporting its exception body.
            if pending:
                for task in pending:
                    task.cancel()
                # Keep ownership and the old groups until retirement completes.
                # A retry must finish this partial scope before replacing sockets.
                raise MarketError("subscription_shutdown_timeout")
        for name in changed:
            self._sessions.pop(name, None)
            self.connections.pop(name, None)
            if name in new_groups:
                self._sessions[name] = asyncio.create_task(
                    self._session(name, name.split(":")[0], list(new_groups[name])),
                    name="ingestion:ws:" + name,
                )
        self._groups = new_groups

    async def _manager(self) -> None:
        while True:
            try:
                await self.refresh(self.store.snapshot().settings)
                self.market_errors.pop("manager", None)
                if not self.components_healthy():
                    self.market_errors["manager"] = "ingestion_worker_failed"
                self.persist_diagnostics()
            except Exception as exc:
                self.market_errors["manager"] = (
                    str(exc) if isinstance(exc, MarketError) else "manager_failed_" + type(exc).__name__
                )
                self._manager_failures = {
                    "count": self._manager_failures["count"] + 1,
                    "last_error_class": type(exc).__name__,
                    "last_failure_ms": int(time.time() * 1000),
                    "last_failure_code": "SUBSCRIPTION_SHUTDOWN_TIMEOUT"
                    if isinstance(exc, MarketError) and str(exc) == "subscription_shutdown_timeout"
                    else "REFRESH_FAILED",
                }
            await asyncio.sleep(MANAGER_INTERVAL_SECONDS)

    async def _diagnostic_loop(self) -> None:
        # REST discovery/backoff must not freeze the persisted WS/recovery snapshot.
        while True:
            self.persist_diagnostics()
            await asyncio.sleep(5)

    async def _configuration_loop(self) -> None:
        # A REST request/backoff must not delay an explicit owner scope change.
        # Metadata/ticker refresh remains owned by the manager, with the same policy.
        while True:
            snapshot = self.store.snapshot()
            if snapshot.revision != self._scope_revision or not self.scope_synced():
                try:
                    await self.reconcile(snapshot.settings)
                    self.market_errors.pop("configuration", None)
                except MarketError as exc:
                    self.market_errors["configuration"] = str(exc)
                self.persist_diagnostics()
            await asyncio.sleep(MANAGER_INTERVAL_SECONDS)

    def request_recovery(self, stream: str, priority: int = 0) -> None:
        if stream not in self.streams or stream in self._working:
            return
        previous = self._pending.get(stream)
        if previous and previous[0] <= priority:
            return
        self._sequence += 1
        self._pending[stream] = (priority, self._sequence)
        self._work_ready.set()

    def restore_current_window(self, stream: str) -> bool:
        """Reuse a complete closed local window, without producing an observation."""
        self.sources.load(stream)
        blocked = self.sources.blocked.get(stream)
        if blocked not in (None, "stream_disconnected"):
            return False
        settings = self.store.snapshot().settings
        parameters = effective_parameters(settings, stream.rsplit("/", 1)[0])
        needed = min(
            WINDOW_SIZE,
            max(
                16,
                settings.sma_periods + 1,
                int(max(v for k, v in parameters.items() if k.endswith("lookback"))) + 2,
            ),
        )
        window = self.sources.windows[stream]
        ordered = sorted(window)
        step = int(stream.rsplit("/", 1)[1][:-1]) * 60_000
        if (
            len(ordered) < needed
            or window[ordered[-1]].end != (self.server_now() // step) * step
            or any(b - a != step for a, b in zip(ordered, ordered[1:]))
        ):
            return False
        if blocked == "stream_disconnected":
            self.sources.blocked.pop(stream)
        return True

    async def backfill(self, stream: str) -> None:
        market, symbol, tf = stream.split("/")
        self.sources.load(stream)
        window = self.sources.windows[stream]
        ordered = sorted(window)
        has_gap = any(b - a != window[b].step for a, b in zip(ordered, ordered[1:]))
        needed = WINDOW_SIZE
        # A short calculation tail cannot repair an older hole retained in
        # memory. Recover the existing bounded window for that stream only.
        if self.store.bounded and not has_gap:
            from .detector_config import effective_parameters

            settings = self.store.snapshot().settings
            parameters = effective_parameters(settings)
            needed = min(
                WINDOW_SIZE,
                max(
                    16,
                    settings.sma_periods + 1,
                    int(max(v for k, v in parameters.items() if k.endswith("lookback"))) + 2,
                ),
            )
        result = await self._get(
            "/v5/market/kline",
            {
                "category": market,
                "symbol": symbol,
                "interval": tf[:-1],
                "limit": needed + 2,
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
        candles = sorted(candles, key=lambda c: c.start)[-needed:]
        if not candles:
            raise MarketError("empty_closed_history")
        self.sources.load(stream)
        previous_end = max((c.end for c in self.sources.windows[stream].values()), default=0)
        oldest = candles[0].start
        if previous_end and previous_end < oldest:
            self.record_gap(stream, previous_end, oldest - 1)
        self._measure("recovery_source", lambda: self.sources.put(stream, candles, recovery=True))
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
        retention_start = (
            (self.server_now() - self.store.snapshot().settings.candle_retention_days * 86_400_000 + step - 1)
            // step
        ) * step
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
            stream, (priority, _) = self.next_recovery()
            self._last_market = stream.split("/", 1)[0]
            self._pending.pop(stream)
            if stream not in self.streams:
                continue
            self._working.add(stream)
            try:
                try:
                    if priority == 20 and stream in self._history:
                        await self.history_page(stream)
                    elif not self.restore_current_window(stream):
                        await self.backfill(stream)
                        if stream in self.sources.blocked:
                            raise MarketError("incomplete_recovery_window")
                    if (
                        priority < 20
                        and stream in self.streams
                        and self.recovery_bucket(stream) not in self._recovered_buckets
                        and self.recovery_window_usable(stream)
                    ):
                        self._recovered_buckets.add(self.recovery_bucket(stream))
                except (MarketError, KeyError, TypeError, ValueError):
                    if stream not in self.streams:
                        # An HTTP failure can arrive after scope reconciliation
                        # removed this stream. Preserve persisted history, but do
                        # not resurrect a retired in-memory blocker or retry.
                        self.sources.remove_window(stream)
                        self._history_errors.pop(stream, None)
                        continue
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
                if stream in self.streams and stream in self._history:
                    self.request_recovery(stream, 20)
                # Local restores have no network await. Give subscription ACKs,
                # heartbeat and Telegram a turn after each bounded window.
                await asyncio.sleep(0)

    @staticmethod
    def recovery_bucket(stream: str) -> tuple[str, str]:
        market, _, tf = stream.split("/")
        return market, tf

    def recovery_window_usable(self, stream: str) -> bool:
        market, symbol, tf = stream.split("/")
        settings = self.store.snapshot().settings
        parameters = effective_parameters(settings, f"{market}/{symbol}")
        effective = settings.changed(
            bybit_category=market,
            signal_timeframe=tf,
            price_move_min=parameters[f"price_{tf}"],
            turnover_spike_min=parameters["turnover_ratio"],
            sma_periods=parameters["turnover_lookback"],
        )
        value = evaluate_composite(
            self.sources.rows(stream, effective.sma_periods + 2),
            self.tickers[f"{market}/{symbol}"],
            effective,
            self.server_now(),
        )
        return value is not None and 0 <= self.server_now() - value["candle_end_ms"] <= int(tf[:-1]) * 60_000

    def next_recovery(self) -> tuple[str, tuple[int, int]]:
        # A busy1m repair queue must not starve the first usable3m/5m/15m
        # windows after restart. Seed at most one worker per missing bucket;
        # then retain the existing live-gap/bootstrap/history priorities.
        working = {self.recovery_bucket(stream) for stream in self._working}
        bootstrap = [
            item
            for item in self._pending.items()
            if item[1][0] < 20 and self.recovery_bucket(item[0]) not in self._recovered_buckets | working
        ]
        return min(
            bootstrap or self._pending.items(),
            key=lambda item: (
                item[1][0],
                item[0].split("/", 1)[0] == self._last_market,
                item[1][1],
            ),
        )

    def accept_message(
        self,
        market: str,
        message: dict[str, Any],
        allowed: set[str],
        *,
        received_ms: int | None = None,
    ) -> None:
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
                    int(time.time() * 1000) if received_ms is None else received_ms,
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
                observer = self._transaction_observer
                handled = False

                def after_write(candidate: Sources, advanced: bool) -> None:
                    nonlocal handled
                    if observer is not None and self.on_observation is observer[0]:
                        self.publish_observation(
                            stream,
                            live=advanced,
                            through=candle.start,
                            _source=candidate,
                            _callback=observer[1],
                        )
                        handled = True

                joined = previous is None and observer is not None and self.on_observation is observer[0]

                def write_source() -> bool:
                    if joined:
                        return self.sources.put(stream, [candle], live=fresh, after_write=after_write)
                    return self.sources.put(stream, [candle], live=fresh)

                advanced = self._measure("closed_ws_source", write_source)
                if previous is None and not handled:
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

    async def _receive_messages(self, websocket: Any, name: str, market: str, allowed: set[str]) -> None:
        started, processed = time.monotonic(), 0
        while True:
            # Python3.11 wait_for creates a separate task even for a buffered
            # frame. Under writer contention its extra turns delay confirmed
            # candles behind already received partial updates. Keep the same
            # cancellation/deadline, and bound draining to preserve fairness.
            async with asyncio.timeout(65):
                raw = await websocket.recv()
            received_ms = int(time.time() * 1000)
            self.accept_message(market, json.loads(raw), allowed, received_ms=received_ms)
            self.connections[name]["last_received_ms"] = int(time.time() * 1000)
            processed += 1
            if processed >= 16 or time.monotonic() - started >= 0.005:
                await asyncio.sleep(0)
                started, processed = time.monotonic(), 0

    async def _session(self, name: str, market: str, streams: list[str]) -> None:
        allowed = set(streams)
        backoff = 1.0
        while True:
            self.connections[name] = {
                "state": "CONNECTING",
                "phase": "CONNECT",
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
                        self.connections[name]["phase"] = "RECOVERY"
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
                        deferred: list[tuple[dict[str, Any], int]] = []
                        deferred_bytes = 0
                        for offset in range(0, len(streams), batch_size):
                            batch = streams[offset : offset + batch_size]
                            request_id = f"cw{offset}"
                            self.connections[name]["phase"] = "SUBSCRIBE_SEND"
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
                            started, processed = time.monotonic(), 0
                            self.connections[name]["phase"] = "SUBSCRIBE_ACK"
                            while True:
                                remaining = deadline - time.monotonic()
                                if remaining <= 0:
                                    raise TimeoutError("Subscription acknowledgement deadline")
                                async with asyncio.timeout(remaining):
                                    raw = await websocket.recv()
                                received_ms = int(time.time() * 1000)
                                message = json.loads(raw)
                                # Subscription responses must not wait for cold source
                                # reads, detector persistence or historical processing.
                                parts = str(message.get("topic", "")).split(".")
                                if len(parts) == 3 and parts[0] == "kline":
                                    stream = f"{market}/{parts[2]}/{parts[1]}m"
                                    if stream in allowed and stream in self.streams:
                                        self.observe_clock(message["ts"])
                                        if any(row.get("confirm") is True for row in message.get("data", [])):
                                            size = (
                                                len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
                                            )
                                            if (
                                                len(deferred) >= SUBSCRIPTION_BUFFER_MESSAGES
                                                or deferred_bytes + size > SUBSCRIPTION_BUFFER_BYTES
                                            ):
                                                self.sources.blocked[stream] = "subscription_buffer_overflow"
                                                self.sources.live.pop(stream, None)
                                                self.request_recovery(stream, 0)
                                                self.connections[name]["buffer_overflows"] = (
                                                    self.connections[name].get("buffer_overflows", 0) + 1
                                                )
                                            else:
                                                deferred.append((message, received_ms))
                                                deferred_bytes += size
                                if message.get("op") == "subscribe" and message.get("req_id") == request_id:
                                    if message.get("success") is not True:
                                        raise MarketError("subscription_rejected")
                                    self.connections[name]["acknowledged"] += len(batch)
                                    break
                                processed += 1
                                if processed >= 16 or time.monotonic() - started >= 0.005:
                                    await asyncio.sleep(0)
                                    started, processed = time.monotonic(), 0
                        self.connections[name]["state"] = "CONNECTED"
                        self.connections[name]["phase"] = "DEFERRED"
                        backoff = 1.0
                        for message, received_ms in deferred:
                            self.accept_message(market, message, allowed, received_ms=received_ms)
                            await asyncio.sleep(0)
                        deferred.clear()
                        self.connections[name]["phase"] = "RECEIVE"
                        await self._receive_messages(websocket, name, market, allowed)
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
                    if stream not in self.streams:
                        continue
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
        return self._measure("status_snapshot", lambda: self._snapshot(settings))

    def _snapshot(self, settings: OwnerSettings) -> RadarResult:
        active, excluded = active_instruments(self.instruments, self.tickers, settings)
        quotes: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}
        now_ms = self.server_now()
        listed = frozenset(settings.instruments())
        derived: dict[tuple[str, str, bool], OwnerSettings] = {}
        for instrument in active:
            market, symbol = instrument.market, instrument.symbol
            for tf in TIMEFRAMES:
                stream = f"{instrument.key}/{tf}"
                try:
                    if market in self.market_errors:
                        raise MarketError(self.market_errors[market])
                    if stream not in self.streams:
                        raise MarketError("subscription_pending")
                    if stream not in self.sources.windows:
                        raise MarketError("bootstrap_pending")
                    if stream in self.sources.blocked:
                        raise MarketError(self.sources.blocked[stream])
                    cohort = market, tf, instrument.key in listed
                    effective = derived.get(cohort)
                    if effective is None:
                        parameters = effective_parameters(settings, instrument.key)
                        effective = settings.changed(
                            bybit_category=market,
                            signal_timeframe=tf,
                            price_move_min=parameters[f"price_{tf}"],
                            turnover_spike_min=parameters["turnover_ratio"],
                            sma_periods=parameters["turnover_lookback"],
                        )
                        # Validated immutable settings live only for this synchronous
                        # snapshot; every quote below still receives a fresh payload.
                        derived[cohort] = effective
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
            elif self._started and not self.discovery_fresh(market):
                errors[market] = "discovery_stale"
        if "manager" in self.market_errors:
            errors["manager"] = self.market_errors["manager"]
        if "configuration" in self.market_errors:
            errors["configuration"] = self.market_errors["configuration"]
        return RadarResult(quotes, errors, len(active) * len(TIMEFRAMES))

    def delivery_ready(self, result: RadarResult) -> bool:
        if not self.components_healthy() or {"manager", "configuration"} & self.market_errors.keys():
            return False
        if self._started and (
            not self.scope_synced() or not all(self.discovery_fresh(m) for m in ("spot", "linear"))
        ):
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
        return all(
            not task.done() and not task.cancelling() for task in (*self._tasks, *self._sessions.values())
        )

    def scope_synced(self) -> bool:
        return (
            self.streams == {stream for group in self._groups.values() for stream in group}
            and set(self._groups) == set(self._sessions)
            and set(self._groups) == set(self.connections)
            and all(not task.done() and not task.cancelling() for task in self._sessions.values())
        )

    def discovery_fresh(self, market: str) -> bool:
        now = time.monotonic()
        return all(
            market in times and 0 <= now - times[market] <= cadence + SOURCE_REFRESH_GRACE_SECONDS
            for times, cadence in (
                (self.metadata_at, METADATA_REFRESH_SECONDS),
                (self.tickers_at, TICKER_REFRESH_SECONDS),
            )
        )

    @staticmethod
    def task_diagnostic(task: asyncio.Task[Any]) -> dict[str, Any]:
        result = {"name": task.get_name()}
        if task.cancelled():
            return {**result, "state": "CANCELLED"}
        if task.done():
            error = task.exception()
            return {
                **result,
                "state": "FAILED" if error else "FINISHED",
                "error_class": type(error).__name__ if error else None,
            }
        return {**result, "state": "STOPPING" if task.cancelling() else "RUNNING"}

    def confirmed_source_diagnostic(self, now_ms: int) -> dict[str, Any]:
        from .config import CLOSED_CANDLE_GRACE_MS

        unknown = late = 0
        earliest = None
        samples: list[dict[str, Any]] = []
        for stream in sorted(self.streams):
            window = self.sources.windows.get(stream)
            if not window:
                unknown += 1
                continue
            # Sources keys are validated candle starts for this one timeframe.
            # Read the actual latest candle; no cached deadline can go stale
            # after recovery, rollback, eviction or out-of-order insertion.
            latest = window[max(window)].end
            step = int(stream.rsplit("/", 1)[1][:-1]) * 60_000
            deadline = latest + step + CLOSED_CANDLE_GRACE_MS
            earliest = deadline if earliest is None else min(earliest, deadline)
            if now_ms > deadline:
                late += 1
                if len(samples) < 20:
                    samples.append(
                        {"stream": stream, "latest_closed_ms": latest, "lag_ms": now_ms - latest - step}
                    )
        return {
            "state": "MEASURED" if now_ms > 0 else "CLOCK_UNKNOWN",
            "streams": len(self.streams),
            "unknown_streams": unknown,
            "late_streams": late,
            "earliest_deadline_ms": earliest,
            "grace_ms": CLOSED_CANDLE_GRACE_MS,
            "samples": samples,
            "samples_truncated": late > len(samples),
        }

    def persist_diagnostics(self) -> None:
        self._measure("diagnostics", self._persist_diagnostics)

    def _persist_diagnostics(self) -> None:
        self.store.put_runtime(
            "ingestion",
            {
                "configuration_revision": self._scope_revision,
                "discovery": {
                    market: {
                        "metadata_ready": market in self.metadata_at,
                        "tickers_ready": market in self.tickers_at,
                        "metadata_age_seconds": time.monotonic() - self.metadata_at[market]
                        if market in self.metadata_at
                        else None,
                        "ticker_age_seconds": time.monotonic() - self.tickers_at[market]
                        if market in self.tickers_at
                        else None,
                    }
                    for market in ("spot", "linear")
                },
                "classification": CLASSIFICATION_VERSION,
                "timeframes": list(TIMEFRAMES),
                "eligible_metadata": {
                    m: sum(i.market == m for i in self.instruments.values()) for m in ("spot", "linear")
                },
                "quarantine_reasons": {
                    r: list(self.quarantine.values()).count(r) for r in set(self.quarantine.values())
                },
                "components_healthy": self.components_healthy(),
                "components": [self.task_diagnostic(task) for task in self._tasks],
                "manager_failures": dict(self._manager_failures),
                "work_timing": {
                    "schema": "cryptowatcher.ingestion-work.v1",
                    "basis": "process-lifetime exclusive thread CPU and synchronous active wall; nested stages are subtracted from parents; includes source/observation/recovery writes, referenced response, repack/retention, ticker/metadata persistence including commit, status snapshot and diagnostics; excludes wire parsing, discovery requests/processing outside persistence, REST/async waits, recovery selection/retirement and shared readonly preparation; current diagnostic call completes after its counter snapshot",
                    "stages": {name: dict(value) for name, value in self._work_timing.items()},
                },
                "scope_synced": self.scope_synced(),
                "subscriptions": {
                    name: {
                        **value,
                        "task_state": self.task_diagnostic(self._sessions[name])["state"]
                        if name in self._sessions
                        else "UNOWNED",
                    }
                    for name, value in self.connections.items()
                },
                "active_streams": len(self.streams),
                "confirmed_sources": self.confirmed_source_diagnostic(self.server_now()),
                "startup_recovered_buckets": sorted("/".join(bucket) for bucket in self._recovered_buckets),
                "bootstrap_pending_streams": len(self.streams - self.sources.windows.keys()),
                "recovery_pending": len(self._pending),
                "recovery_working": len(self._working),
                "blocked_streams": len(self.sources.blocked),
                "blocked_reasons": dict(Counter(self.sources.blocked.values())),
                "blocked_stream_samples": [
                    {
                        "stream": stream,
                        "reason": self.sources.blocked[stream],
                        "active": stream in self.streams,
                    }
                    for stream in sorted(self.sources.blocked)[:20]
                ],
                "blocked_stream_samples_truncated": len(self.sources.blocked) > 20,
                "historical_gaps_pending": len(self._history),
                "historical_gap_errors": self._history_errors,
                "buffered_candles": sum(map(len, self.sources.windows.values())),
                "market_errors": self.market_errors,
                "server_ms": self.server_now(),
                "at": time.time(),
            },
        )
