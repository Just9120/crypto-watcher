"""Optional asset-matched derivatives metrics; no detector or trading decisions."""

from __future__ import annotations

import math
from typing import Any

from .universe import Instrument

VERSION = "1"
DEFINITION = {
    "version": VERSION,
    "mapping": "eligible BYBIT instruments with exact base/USDT identity; one perpetual match; no aliases",
    "ticker_cadence_ms": 60_000,
    "oi_change_cadence_ms": 300_000,
    "basis": "100*(perpetual_last/spot_last-1); both available snapshots within ticker cadence",
    "oi": "linear BASE both sides; delta from two exact5min historical points; zero baseline has no pct",
    "funding": "raw fraction*100 percent; next funding UTC milliseconds",
    "freshness": "source observation age<=cadence FRESH, otherwise STALE; future/invalid INVALID",
}


def match(spot: Instrument, instruments: dict[str, Instrument]) -> tuple[Instrument | None, str]:
    if spot.market != "spot" or instruments.get(spot.key) != spot:
        return None, "NOT_ELIGIBLE_SPOT"
    candidates = [item for item in instruments.values() if item.market == "linear" and item.base == spot.base]
    if len(candidates) != 1:
        return None, "PERPETUAL_MISSING" if not candidates else "AMBIGUOUS_ASSET_IDENTITY"
    return candidates[0], "EXACT_METADATA_ASSET_IDENTITY"


def freshness(metric: dict[str, Any], now: int) -> str:
    if metric.get("value") is None:
        return metric.get("state", "MISSING")
    observed = metric.get("market_observation_ms")
    available = metric.get("available_at_ms")
    if (
        type(observed) is not int
        or type(available) is not int
        or observed < 0
        or observed > available
        or available > now
    ):
        return "INVALID"
    return "FRESH" if now - observed <= metric["cadence_ms"] else "STALE"


def metric(
    value: Any,
    *,
    unit: str,
    symbol: str,
    source: str,
    observed: Any,
    available: Any,
    cadence: int = 60_000,
    asset: str | None = None,
    positive: bool = False,
    nonnegative: bool = False,
    integer: bool = False,
) -> dict[str, Any]:
    result = {
        "value": None,
        "unit": unit,
        "venue": "BYBIT",
        "market_type": "linear",
        "symbol": symbol,
        "source": source,
        "market_observation_ms": observed,
        "available_at_ms": available,
        "cadence_ms": cadence,
        "state": "MISSING",
    }
    if asset:
        result["base_asset"] = asset
    try:
        number = float(value)
        if (
            isinstance(value, bool)
            or not math.isfinite(number)
            or (positive and number <= 0)
            or (nonnegative and number < 0)
            or (integer and not number.is_integer())
        ):
            raise ValueError
        result.update(value=int(number) if integer else number, state="AVAILABLE")
        if type(observed) is not int or type(available) is not int or observed < 0 or observed > available:
            result.update(value=None, state="INVALID", reason="INVALID_SOURCE_TIMESTAMP")
    except (ValueError, TypeError, OverflowError):
        result["reason"] = "MISSING_OR_INVALID_VALUE"
    return result


def snapshot(
    spot: Instrument,
    perpetual: Instrument,
    spot_ticker: dict[str, Any] | None,
    perp_ticker: dict[str, Any] | None,
    oi: dict[str, Any] | None,
    now: int,
) -> dict[str, Any]:
    """Pure calculation; absent inputs remain unavailable and each metric fails independently."""
    current = perp_ticker or {}
    values = current.get("values", {})
    if not isinstance(values, dict):
        values = {}
    observed, available = current.get("market_observation_ms"), current.get("available_at_ms")
    common: dict[str, Any] = {
        "symbol": perpetual.symbol,
        "observed": observed,
        "available": available,
        "source": "BYBIT_REST_TICKERS",
    }
    metrics: dict[str, dict[str, Any]] = {
        "open_interest_base": metric(
            values.get("openInterest"), unit="BASE", asset=perpetual.base, nonnegative=True, **common
        ),
        "next_funding_ms": metric(
            values.get("nextFundingTime"), unit="UTC_MS", positive=True, integer=True, **common
        ),
    }
    funding = metric(values.get("fundingRate"), unit="%", **common)
    if funding["value"] is not None:
        funding["value"] *= 100
        if not math.isfinite(funding["value"]):
            funding.update(value=None, state="INVALID", reason="NONFINITE_FUNDING_PERCENT")
    metrics["funding_rate_pct"] = funding
    basis = metric(None, unit="%", **common)
    first = spot_ticker or {}
    first_values = first.get("values", {})
    if not isinstance(first_values, dict):
        first_values = {}
    spot_price = metric(
        first_values.get("lastPrice"),
        unit="USDT/BASE",
        positive=True,
        symbol=spot.symbol,
        observed=first.get("market_observation_ms"),
        available=first.get("available_at_ms"),
        source="BYBIT_REST_TICKERS",
    )
    perp_price = metric(values.get("lastPrice"), unit="USDT/BASE", positive=True, **common)
    if freshness(spot_price, now) == freshness(perp_price, now) == "FRESH":
        skew = abs(spot_price["market_observation_ms"] - perp_price["market_observation_ms"])
        if skew <= 60_000:
            value = 100 * (perp_price["value"] / spot_price["value"] - 1)
            basis = metric(
                value,
                unit="%",
                symbol=perpetual.symbol,
                source="BYBIT_PAIRED_REST_SNAPSHOTS",
                observed=min(spot_price["market_observation_ms"], perp_price["market_observation_ms"]),
                available=max(spot_price["available_at_ms"], perp_price["available_at_ms"]),
            )
            basis.update(
                spot_price=spot_price["value"],
                perpetual_price=perp_price["value"],
                spot_observation_ms=spot_price["market_observation_ms"],
                perpetual_observation_ms=perp_price["market_observation_ms"],
                skew_ms=skew,
            )
    if basis["value"] is None:
        basis["reason"] = "MISSING_STALE_OR_UNSYNCHRONIZED_PRICE_SNAPSHOTS"
    metrics["snapshot_basis_pct"] = basis
    historical = oi or {}
    oi_common: dict[str, Any] = {
        "symbol": perpetual.symbol,
        "source": "BYBIT_REST_OPEN_INTEREST_5MIN",
        "observed": None,
        "available": historical.get("available_at_ms"),
        "cadence": 300_000,
    }
    delta = metric(None, unit="BASE", asset=perpetual.base, **oi_common)
    percent = metric(None, unit="%", **oi_common)
    rows = historical.get("points", [])
    valid_points = (
        isinstance(rows, list)
        and len(rows) == 2
        and all(
            isinstance(row, dict)
            and type(row.get("timestamp")) is int
            and row["timestamp"] >= 0
            and "open_interest" in row
            for row in rows
        )
    )
    if valid_points:
        previous, latest = sorted(rows, key=lambda r: r["timestamp"])
        oi_common["observed"] = latest["timestamp"]
        if latest["timestamp"] - previous["timestamp"] == 300_000 and latest["timestamp"] <= now:
            old = metric(
                previous["open_interest"],
                unit="BASE",
                nonnegative=True,
                asset=perpetual.base,
                **{**oi_common, "observed": previous["timestamp"]},
            )
            new = metric(
                latest["open_interest"], unit="BASE", nonnegative=True, asset=perpetual.base, **oi_common
            )
            if old["value"] is not None and new["value"] is not None:
                delta = metric(new["value"] - old["value"], unit="BASE", asset=perpetual.base, **oi_common)
                if old["value"] > 0:
                    percent = metric(100 * (new["value"] / old["value"] - 1), unit="%", **oi_common)
                else:
                    percent.update(reason="ZERO_OI_BASELINE")
                delta["source_points"] = percent["source_points"] = rows
        else:
            delta["reason"] = percent["reason"] = "NONCONTIGUOUS_OR_FUTURE_OI_POINTS"
    else:
        delta["reason"] = percent["reason"] = "TWO5MIN_OI_POINTS_UNAVAILABLE"
    metrics.update(oi_change_base_5min=delta, oi_change_pct_5min=percent)
    for item in metrics.values():
        item["state"] = freshness(item, now)
        if item["unit"] == "UTC_MS" and item["value"] is not None and item["value"] <= now:
            item.update(state="STALE", reason="FUNDING_SCHEDULE_PASSED")
    return {
        "schema": "cryptowatcher.derivatives.v1",
        "methodology_version": VERSION,
        "spot_instrument": spot.key,
        "perpetual_instrument": perpetual.key,
        "base_asset": spot.base,
        "mapping": "EXACT_METADATA_ASSET_IDENTITY",
        "mapping_metadata": {"spot": spot.metadata, "perpetual": perpetual.metadata},
        "metrics": metrics,
        "snapshot_ms": now,
        "state": "COMPLETE" if all(v["state"] == "FRESH" for v in metrics.values()) else "PARTIAL",
        "optional": True,
        "detector_input": False,
    }
