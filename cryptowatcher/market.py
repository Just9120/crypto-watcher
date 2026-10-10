"""Bounded Bybit REST adapter and the preserved closed-candle composite."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import OwnerSettings


class MarketError(RuntimeError):
    pass


def finite(value: Any, *, positive: bool = False) -> float:
    try:
        number = float(value)
        if isinstance(value, bool) or not math.isfinite(number) or (positive and number <= 0):
            raise ValueError
        return number
    except (ValueError, TypeError, OverflowError) as exc:
        raise MarketError("invalid_market_number") from exc


def evaluate_composite(
    rows: list[list[Any]], ticker: dict[str, Any], settings: OwnerSettings, now_ms: int
) -> dict[str, Any] | None:
    """History excludes the current bar; thresholds retain inclusive equality."""
    step = int(settings.signal_timeframe[:-1]) * 60_000
    closed: dict[int, list[Any]] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 7:
            raise MarketError("invalid_candle")
        start = int(finite(row[0]))
        if start + step <= now_ms:
            if start in closed and row != closed[start]:
                raise MarketError("conflicting_candle")
            closed[start] = row
    candles = [closed[key] for key in sorted(closed, reverse=True)]
    window = candles[: settings.sma_periods + 2]
    if any(int(window[i][0]) - int(window[i + 1][0]) != step for i in range(len(window) - 1)):
        raise MarketError("candle_gap")
    if len(candles) < settings.sma_periods + 2:
        return None
    latest, previous = window[:2]
    latest_close = finite(latest[4], positive=True)
    previous_close = finite(previous[4], positive=True)
    current_turnover = finite(latest[6])
    historical_turnover = [finite(row[6]) for row in window[1 : settings.sma_periods + 1]]
    if current_turnover < 0 or any(value < 0 for value in historical_turnover):
        raise MarketError("negative_turnover")
    sma = sum(historical_turnover) / settings.sma_periods
    if sma <= 0:
        return None
    change = (latest_close / previous_close - 1) * 100
    spike = current_turnover / sma
    liquidity = finite(ticker.get("turnover24h"))
    start = int(latest[0])
    return {
        "price": finite(ticker.get("lastPrice", latest_close), positive=True),
        "reference_price": latest_close,
        "signal_timeframe": settings.signal_timeframe,
        "price_change_tf": change,
        "current_tf_turnover": current_turnover,
        "sma_tf_turnover": sma,
        "turnover_spike_ratio": spike,
        "turnover24h": liquidity,
        "candle_start_ms": start,
        "candle_end_ms": start + step,
        "meets_signal": (
            abs(change) >= settings.price_move_min
            and spike >= settings.turnover_spike_min
            and liquidity >= settings.liquidity_floor_24h
        ),
    }


@dataclass(frozen=True)
class RadarResult:
    quotes: dict[str, dict[str, Any]]
    errors: dict[str, str]
    requested: int


class BybitREST:
    """G1 preserves Top100/custom scan; broad metadata/WS comes in G2A."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client or httpx.AsyncClient(timeout=12, limits=httpx.Limits(max_connections=4))
        self._owned = client is None
        self._concurrency = asyncio.Semaphore(4)
        self._rate_lock = asyncio.Lock()
        self._next_request = 0.0

    async def close(self) -> None:
        if self._owned:
            await self.client.aclose()

    async def _get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        async with self._concurrency:
            async with self._rate_lock:
                delay = self._next_request - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                self._next_request = time.monotonic() + 0.2
            try:
                response = await self.client.get(f"https://api.bybit.com{endpoint}", params=params)
                if response.status_code in (403, 429):
                    self._next_request = max(
                        self._next_request, time.monotonic() + (600 if response.status_code == 403 else 60)
                    )
                    raise MarketError("rate_limited")
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise MarketError("market_request_failed") from exc
            if not isinstance(data, dict) or data.get("retCode") != 0:
                if isinstance(data, dict) and data.get("retCode") == 10006:
                    self._next_request = max(self._next_request, time.monotonic() + 60)
                    raise MarketError("rate_limited")
                raise MarketError("market_response_rejected")
            return data

    async def snapshot(self, settings: OwnerSettings) -> RadarResult:
        data = await self._get("/v5/market/tickers", {"category": settings.bybit_category})
        try:
            now_ms = int(finite(data["time"], positive=True))
            items = data["result"]["list"]
            tickers = {
                str(row["symbol"]): row for row in items if str(row.get("symbol", "")).endswith("USDT")
            }
            symbols = (
                sorted(tickers, key=lambda symbol: finite(tickers[symbol]["turnover24h"]), reverse=True)[:100]
                if settings.alert_universe_mode == "top"
                else list(settings.custom_pairs)
            )
        except (KeyError, TypeError) as exc:
            raise MarketError("invalid_ticker_snapshot") from exc
        quotes: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}

        async def fetch(symbol: str) -> None:
            if symbol not in tickers:
                errors[symbol] = "instrument_not_in_ticker_snapshot"
                return
            try:
                klines = await self._get(
                    "/v5/market/kline",
                    {
                        "category": settings.bybit_category,
                        "symbol": symbol,
                        "interval": settings.signal_timeframe[:-1],
                        "limit": settings.sma_periods + 4,
                    },
                )
                # Use the response's Bybit clock at candle closure, never the host clock.
                observed_now = int(finite(klines.get("time", now_ms), positive=True))
                payload = evaluate_composite(
                    klines["result"]["list"], tickers[symbol], settings, observed_now
                )
                if payload is None:
                    errors[symbol] = "insufficient_history_or_zero_baseline"
                else:
                    quotes[symbol] = payload
            except MarketError as exc:
                errors[symbol] = str(exc)
            except (KeyError, TypeError, ValueError):
                errors[symbol] = "invalid_candle_snapshot"

        await asyncio.gather(*(fetch(symbol) for symbol in symbols))
        return RadarResult(quotes, errors, len(symbols))
