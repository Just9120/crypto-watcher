"""Public v2 projection: immutable source references, never execution authority."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from .events import History
from .storage import encode

MARKETS = {"spot": "SPOT", "linear": "USDT_PERPETUAL"}
UNITS = {
    "%": "PERCENT",
    "ratio": "RATIO",
    "USDT": "USDT",
    "BASE": "BASE",
    "UTC_MS": "UTC_MS",
    "USDT/BASE": "PRICE_USDT",
    "count": "COUNT",
}


def timestamp(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds")


def digest(value: Any) -> str:
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def confirmation(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": event["event_id"],
        "source_event_revision": event["revision"],
        "detector_id": event["detector_id"],
        "detector_version": f"{event['detector_id']}.v{event['detector_version']}",
        "settings_revision": event["settings_revision"],
        "settings_sha256": digest(event["settings_snapshot"]),
        "timeframe": event["timeframe"],
        "strength": float(event["strength"]),
        "market_observed_at": timestamp(event["market_observation_ms"]),
        "fresh_until": timestamp(event["fresh_until_ms"]),
        "expires_at": timestamp(event["expires_at_ms"]),
        "metrics": event["metrics"],
        "metric_units": {k: UNITS[v] for k, v in event["units"].items()},
    }


def episode_snapshot(history: History, episode: dict[str, Any]) -> dict[str, Any]:
    market, symbol = episode["instrument"].split("/")
    confirmations = [
        confirmation(history.get(f"event:{c['event_id']}:{c['revision']:010d}"))
        for c in episode["confirmations"].values()
    ]
    return {
        "episode_id": episode["episode_id"],
        "revision": episode["revision"],
        "market_type": MARKETS[market],
        "symbol": symbol,
        "direction": episode["direction"],
        "state": episode["state"],
        "started_at": timestamp(episode["started_ms"]),
        "last_observation_at": timestamp(episode["last_observation_ms"]),
        "available_at": timestamp(episode["available_at_ms"]),
        "longest_timeframe": f"{episode['longest_tf_ms'] // 60000}m",
        "strength": float(episode["strength"]),
        "confirmations": confirmations,
        "closed_at": timestamp(episode["closed_ms"]) if "closed_ms" in episode else None,
        "close_reason": episode.get("close_reason"),
    }


def enrichment_snapshot(raw: dict[str, Any]) -> dict[str, Any]:
    metrics = {}
    for name, item in raw.get("metrics", {}).items():
        metrics[name] = {
            "value": float(item["value"]) if item.get("value") is not None else None,
            "unit": UNITS[item["unit"]],
            "market_type": "USDT_PERPETUAL",
            "symbol": item["symbol"],
            "source": item["source"],
            "market_observed_at": timestamp(item["market_observation_ms"])
            if item.get("market_observation_ms") is not None
            else None,
            "available_at": timestamp(item["available_at_ms"])
            if item.get("available_at_ms") is not None
            else None,
            "cadence_seconds": item["cadence_ms"] // 1000,
            "state": item["state"],
            "reason": item.get("reason"),
        }
    state = raw.get("state", "UNAVAILABLE")
    if state not in ("COMPLETE", "PARTIAL", "MISSING", "AMBIGUOUS", "DISABLED", "UNAVAILABLE", "N/A"):
        state = "UNAVAILABLE"
    return {
        "schema_version": "cryptowatcher.derivatives_context.v1",
        "methodology_version": "1",
        "state": state,
        "mapping_source_sha256": digest(raw["mapping_metadata"]) if "mapping_metadata" in raw else None,
        "metrics": metrics,
        "detector_input": False,
    }


def outcome_snapshot(raw: dict[str, Any]) -> dict[str, Any]:
    complete = raw["state"] == "COMPLETE"
    metrics = raw["metrics"] if complete else {}
    return {
        "outcome_id": raw["outcome_id"],
        "revision": raw["revision"],
        "methodology_version": raw["methodology_version"],
        "horizon_minutes": raw["horizon_min"],
        "target_at": timestamp(raw["window_end_ms"]),
        "available_at": timestamp(raw["available_at_ms"]),
        "completeness": "COMPLETE" if complete else "INCOMPLETE",
        "metrics": metrics,
        "metric_units": {k: UNITS[raw["units"][k]] for k in metrics},
        "classification": {"NEUTRAL_MOVEMENT": "NEUTRAL", "UNCLASSIFIED": "N/A"}.get(
            raw["classification"], raw["classification"]
        ),
        "source_window_sha256": digest(raw["source_inputs"]) if complete else None,
    }


def envelope(
    history: History,
    event: dict[str, Any],
    episode: dict[str, Any],
    *,
    producer_ref: str,
    revision: int,
    kind: str,
    now_ms: int,
    outcomes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """No current settings/defaults or late enrichment enter a recorded source event."""
    result = {
        **confirmation(event),
        "schema_version": "cryptowatcher.market_event.v2",
        "producer_ref": producer_ref,
        "revision": revision,
        "delivery_kind": kind,
        "venue": "BYBIT",
        "market_type": MARKETS[event["market_type"]],
        "symbol": event["symbol"],
        "episode_id": event["episode_id"],
        "direction": event["direction"],
        "detected_at": timestamp(event["detection_ms"]),
        "produced_at": timestamp(now_ms),
        "interval_start": timestamp(event["market_observation_ms"] - int(event["timeframe"][:-1]) * 60000),
        "settings_snapshot": event["settings_snapshot"],
        "profile": event["profile"],
        "effective_parameters": event["effective_parameters"],
        "producer_source_revision": event["producer_source_revision"],
        "source_window_sha256": event["source_window_id"],
        "completeness": event["completeness"],
        "reference_price": float(event["reference_price"]),
        "reference_at": timestamp(event["reference_time_ms"]),
        "source_received_at": timestamp(event["receipt_ms"]) if event.get("receipt_ms") else None,
        "reference_atr14": event["reference_atr14"],
        "tick_size": event["tick_size"],
        "continuation_threshold_pct": event["continuation_threshold_pct"],
        "freshness_policy_version": "1",
        "episode": episode_snapshot(history, episode),
        "enrichment": enrichment_snapshot(event.get("enrichment", {})),
        "historical_outcomes": [outcome_snapshot(x) for x in outcomes or []],
        "trading_instruction": False,
        "trading_authorized": False,
        "model_invoked": False,
    }
    return result
