"""Metadata eligibility, classification v1 and full market-specific discovery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import ConfigError, OwnerSettings, instrument_key
from .market import BybitREST, MarketError, finite

CLASSIFICATION_VERSION = "crypto-eligibility-v1"
# Asset-backed gold tokens; RWA governance/utility tokens are not excluded by sector.
TOKENIZED_GOLD = frozenset({"PAXG", "XAUT", "XAUT0", "XAUM"})
CRYPTO_TYPES = {"spot": {"", "adventure"}, "linear": {"", "innovation"}}


@dataclass(frozen=True)
class Instrument:
    key: str
    market: str
    symbol: str
    base: str
    tick_size: float
    metadata: dict[str, Any]


def classify(market: str, row: dict[str, Any]) -> tuple[Instrument | None, str]:
    if market not in CRYPTO_TYPES:
        return None, "unsupported_market"
    if row.get("status") != "Trading":
        return None, "not_trading"
    if row.get("quoteCoin") != "USDT":
        return None, "not_usdt_quote"
    if row.get("symbolType", "") not in CRYPTO_TYPES[market]:
        return None, "special_or_unknown_type"
    if row.get("marketRegion") or row.get("underlyingTicker"):
        return None, "tradfi_underlying"
    if market == "linear" and (
        row.get("contractType") != "LinearPerpetual"
        or row.get("settleCoin") != "USDT"
        or row.get("isPreListing") is not False
    ):
        return None, "not_live_usdt_perpetual"
    base = row.get("baseCoin")
    if not isinstance(base, str) or not base:
        return None, "missing_asset_identity"
    if base.upper() in TOKENIZED_GOLD:
        return None, "tokenized_gold"
    try:
        symbol = row["symbol"]
        key = instrument_key(f"{market}/{symbol}")
        if key != f"{market}/{symbol}":
            raise ConfigError("Invalid metadata symbol")
        tick = finite(row["priceFilter"]["tickSize"], positive=True)
    except (KeyError, TypeError, ConfigError, MarketError):
        return None, "invalid_metadata"
    return Instrument(key, market, symbol, base, tick, row), "eligible"


async def discover(adapter: BybitREST, market: str) -> tuple[dict[str, Instrument], dict[str, str]]:
    instruments: dict[str, Instrument] = {}
    quarantine: dict[str, str] = {}
    cursor = ""
    seen: set[str] = set()
    while True:
        params: dict[str, Any] = {"category": market, "status": "Trading"}
        if market == "linear":
            params["limit"] = 1000
            if cursor:
                params["cursor"] = cursor
        response = await adapter._get("/v5/market/instruments-info", params)
        result = response.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("list"), list):
            raise MarketError("invalid_metadata_response")
        for row in result["list"]:
            if not isinstance(row, dict) or not isinstance(row.get("symbol"), str):
                raise MarketError("invalid_metadata_record")
            key = f"{market}/{row['symbol']}"
            if key in instruments or key in quarantine:
                raise MarketError("duplicate_metadata_record")
            instrument, reason = classify(market, row)
            if instrument:
                instruments[key] = instrument
            else:
                quarantine[key] = reason
        if market == "spot":
            break  # Bybit explicitly has no Spot cursor/limit support.
        cursor = result.get("nextPageCursor", "")
        if not isinstance(cursor, str):
            raise MarketError("invalid_metadata_cursor")
        if not cursor:
            break
        if cursor in seen:
            raise MarketError("repeated_metadata_cursor")
        seen.add(cursor)
    return instruments, quarantine


def active_instruments(
    instruments: dict[str, Instrument], tickers: dict[str, dict[str, Any]], settings: OwnerSettings
) -> tuple[list[Instrument], dict[str, str]]:
    selected = set(settings.instruments())
    active: list[Instrument] = []
    excluded: dict[str, str] = {}
    for key, instrument in instruments.items():
        if settings.alert_universe_mode == "custom" and key not in selected:
            continue
        try:
            liquidity = finite(tickers[key]["turnover24h"])
            finite(tickers[key]["lastPrice"], positive=True)
        except (KeyError, MarketError):
            excluded[key] = "missing_or_invalid_ticker"
            continue
        if liquidity < settings.liquidity_floor_24h:
            excluded[key] = "below_liquidity_floor"
        else:
            active.append(instrument)
    if settings.alert_universe_mode == "custom":
        for key in selected - instruments.keys():
            excluded[key] = "instrument_ineligible_or_unknown"
    return active, excluded
