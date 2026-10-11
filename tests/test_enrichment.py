from dataclasses import replace

import pytest

from cryptowatcher.config import ConfigError, OwnerSettings
from cryptowatcher.enrichment import freshness, match, snapshot
from cryptowatcher.universe import classify

from .test_universe import metadata

NOW = 1_800_000_000_000


def instruments():
    spot, _ = classify("spot", metadata())
    perp, _ = classify("linear", metadata(market="linear"))
    return spot, perp


def ticker(price=100, **changes):
    return {
        "market_observation_ms": NOW - 1000,
        "available_at_ms": NOW,
        "values": {
            "lastPrice": str(price),
            "openInterest": "120",
            "fundingRate": "0.0001",
            "nextFundingTime": str(NOW + 8 * 3_600_000),
            **changes,
        },
    }


def test_malformed_spot_price_snapshot_does_not_hide_valid_perpetual_metrics():
    spot, perp = instruments()
    value = snapshot(spot, perp, {"values": []}, ticker(), None, NOW)
    assert value["metrics"]["snapshot_basis_pct"]["state"] == "MISSING"
    assert value["metrics"]["funding_rate_pct"]["state"] == "FRESH"


def oi(previous=100, latest=120, start=NOW - 300_000, end=NOW):
    return {
        "available_at_ms": NOW,
        "points": [
            {"timestamp": start, "open_interest": previous},
            {"timestamp": end, "open_interest": latest},
        ],
    }


def test_mapping_exact_identity_missing_duplicate_and_scaled_alias():
    spot, perp = instruments()
    assert match(spot, {spot.key: spot, perp.key: perp}) == (perp, "EXACT_METADATA_ASSET_IDENTITY")
    assert match(spot, {spot.key: spot})[1] == "PERPETUAL_MISSING"
    alias = replace(perp, key="linear/1000BTCUSDT", symbol="1000BTCUSDT", base="1000BTC")
    assert match(spot, {spot.key: spot, alias.key: alias})[1] == "PERPETUAL_MISSING"
    duplicate = replace(perp, key="linear/BTC2USDT", symbol="BTC2USDT")
    assert (
        match(spot, {spot.key: spot, perp.key: perp, duplicate.key: duplicate})[1]
        == "AMBIGUOUS_ASSET_IDENTITY"
    )
    assert match(perp, {spot.key: spot, perp.key: perp})[1] == "NOT_ELIGIBLE_SPOT"


def test_units_timestamps_funding_basis_and_exact5min_delta():
    spot, perp = instruments()
    result = snapshot(spot, perp, ticker(), ticker(105), oi(), NOW)
    assert result["state"] == "COMPLETE" and result["detector_input"] is False
    metrics = result["metrics"]
    assert metrics["open_interest_base"]["value"] == 120 and metrics["open_interest_base"]["unit"] == "BASE"
    assert metrics["open_interest_base"]["base_asset"] == "BTC"
    assert metrics["funding_rate_pct"]["value"] == pytest.approx(0.01)
    assert metrics["snapshot_basis_pct"]["value"] == pytest.approx(5)
    assert metrics["next_funding_ms"]["unit"] == "UTC_MS"
    assert metrics["oi_change_pct_5min"]["value"] == pytest.approx(20)
    assert metrics["oi_change_base_5min"]["value"] == 20
    assert metrics["oi_change_pct_5min"]["market_observation_ms"] == NOW
    assert metrics["oi_change_pct_5min"]["cadence_ms"] == 300_000
    for item in metrics.values():
        assert item["venue"] == "BYBIT" and item["market_type"] == "linear" and item["symbol"] == "BTCUSDT"
        assert item["source"] and item["available_at_ms"] == NOW and item["state"] == "FRESH"
    assert result["mapping_metadata"]["spot"] == spot.metadata


@pytest.mark.parametrize("invalid", ["nan", "inf", "missing", None, True])
def test_metric_error_does_not_hide_other_context(invalid):
    spot, perp = instruments()
    result = snapshot(spot, perp, ticker(), ticker(105, fundingRate=invalid), oi(), NOW)
    assert result["state"] == "PARTIAL"
    assert result["metrics"]["funding_rate_pct"]["value"] is None
    assert result["metrics"]["open_interest_base"]["state"] == "FRESH"
    assert result["metrics"]["snapshot_basis_pct"]["value"] == pytest.approx(5)


def test_missing_stale_future_zero_baseline_and_schedule_expiry():
    spot, perp = instruments()
    empty = snapshot(spot, perp, None, None, None, NOW)
    assert all(item["value"] is None for item in empty["metrics"].values())
    stale = {**ticker(), "market_observation_ms": NOW - 60_001}
    result = snapshot(spot, perp, stale, ticker(), oi(0, 5), NOW)
    assert result["metrics"]["snapshot_basis_pct"]["value"] is None
    assert result["metrics"]["oi_change_pct_5min"]["reason"] == "ZERO_OI_BASELINE"
    assert result["metrics"]["oi_change_base_5min"]["value"] == 5
    future = {**ticker(), "market_observation_ms": NOW + 1, "available_at_ms": NOW + 2}
    result = snapshot(spot, perp, ticker(), future, oi(end=NOW + 1), NOW)
    assert result["metrics"]["open_interest_base"]["state"] == "INVALID"
    assert result["metrics"]["snapshot_basis_pct"]["value"] is None
    assert result["metrics"]["oi_change_pct_5min"]["value"] is None
    result = snapshot(spot, perp, ticker(), ticker(nextFundingTime=str(NOW)), oi(end=NOW - 1), NOW)
    assert result["metrics"]["next_funding_ms"]["state"] == "STALE"
    assert result["metrics"]["oi_change_pct_5min"]["reason"] == "NONCONTIGUOUS_OR_FUTURE_OI_POINTS"
    exact = {**ticker(), "market_observation_ms": NOW - 60_000}
    assert (
        freshness(snapshot(spot, perp, exact, exact, oi(), NOW)["metrics"]["open_interest_base"], NOW)
        == "FRESH"
    )


@pytest.mark.parametrize("value", ["on", "off", 1, 0, None, []])
def test_enrichment_toggle_is_typed_owner_setting(value):
    with pytest.raises(ConfigError):
        OwnerSettings(enrichment_enabled=value)
    assert OwnerSettings().enrichment_enabled is False
