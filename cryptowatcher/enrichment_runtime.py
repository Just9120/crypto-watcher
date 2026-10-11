"""Bounded optional enrichment using the shared public budget and causal source history."""

from __future__ import annotations

import asyncio
from typing import Any

from .analytics import as_of
from .enrichment import DEFINITION, VERSION, match, snapshot
from .events import History, identity
from .ingestion import BybitRadar
from .market import MarketError, finite
from .source_context import at, save
from .storage import StateError, Store
from .universe import classify


class Enrichment:
    def __init__(self, store: Store, market: BybitRadar) -> None:
        self.store, self.market, self.history = store, market, History(store)
        self.lock = asyncio.Lock()
        with store.db:
            previous = self.history.get("enrichment-method:" + VERSION)
            if previous is not None and previous != DEFINITION:
                raise StateError("Enrichment methodology changed without a new version")
            if previous is None:
                self.history.put("enrichment-method:" + VERSION, DEFINITION)

    def waiting_for_recovery(self) -> bool:
        return bool(self.market._working or any(v[0] < 20 for v in self.market._pending.values()))

    def _mapping(self, key: str, now: int) -> dict[str, Any]:
        spot = self.market.instruments[key]
        perpetual, reason = match(spot, self.market.instruments)
        mapping: dict[str, Any] = {
            "mapping_id": identity("mapping/" + key),
            "spot_instrument": key,
            "perpetual_instrument": perpetual.key if perpetual else None,
            "reason": reason,
            "spot_metadata": spot.metadata,
            "perpetual_metadata": perpetual.metadata if perpetual else None,
            "candidate_keys": sorted(
                i.key
                for i in self.market.instruments.values()
                if i.market == "linear" and i.base == spot.base
            ),
        }
        head = self.history.get("mapping-head:" + mapping["mapping_id"])
        if head is None or any(head[k] != v for k, v in mapping.items()):
            mapping.update(revision=1 if head is None else head["revision"] + 1, available_at_ms=now)
            self.history.version("mapping", mapping)
        return mapping

    async def work(self, now: int, batch: int = 4) -> None:
        async with self.lock:
            owner = self.store.snapshot()
            if not owner.settings.enrichment_enabled or now <= 0:
                return
            if self.waiting_for_recovery():
                self.store.put_runtime("enrichment", {"state": "WAITING_FOR_CORE_RECOVERY", "at_ms": now})
                return
            errors: dict[str, str] = {}
            for market in ("spot", "linear"):
                if self.waiting_for_recovery():
                    return
                try:
                    await self.market.refresh_tickers(market, max_age=60)
                except Exception as exc:
                    errors[market + "/ticker"] = type(exc).__name__
                if self.store.snapshot().revision != owner.revision:
                    return
            now = self.market.server_now()
            active = sorted({s.rsplit("/", 1)[0] for s in self.market.streams if s.startswith("spot/")})
            pairs = set()
            with self.store.db:
                for key in active:
                    if key in self.market.instruments:
                        mapping = self._mapping(key, now)
                        if mapping["perpetual_instrument"]:
                            pairs.add(mapping["perpetual_instrument"])
            due = []
            for key in pairs:
                attempt = self.history.get("oi-attempt:" + key, {})
                due.append((attempt.get("next_ms", 0), key))
                if attempt.get("reason"):
                    errors[key + "/oi"] = attempt["reason"]
            due.sort()
            for next_ms, key in due[:batch]:
                if next_ms > now or self.waiting_for_recovery():
                    break
                try:
                    response = await self.market._get(
                        "/v5/market/open-interest",
                        {
                            "category": "linear",
                            "symbol": key.split("/", 1)[1],
                            "intervalTime": "5min",
                            "limit": 2,
                        },
                    )
                    result = response["result"]
                    rows = result["list"]
                    server_ms = finite(response["time"], positive=True)
                    if (
                        result.get("category") != "linear"
                        or result.get("symbol") != key.split("/", 1)[1]
                        or not server_ms.is_integer()
                        or not isinstance(rows, list)
                        or not 1 <= len(rows) <= 2
                    ):
                        raise MarketError("invalid_oi_identity_or_page")
                    points = []
                    for row in rows:
                        timestamp = finite(row["timestamp"])
                        value = finite(row["openInterest"])
                        if not timestamp.is_integer() or timestamp < 0 or timestamp > server_ms or value < 0:
                            raise MarketError("invalid_oi_point")
                        points.append({"timestamp": int(timestamp), "open_interest": value})
                    if len({p["timestamp"] for p in points}) != len(points):
                        raise MarketError("duplicate_oi_timestamp")
                    if self.store.snapshot().revision != owner.revision:
                        return
                    received = self.market.server_now()
                    with self.store.db:
                        save(
                            self.store,
                            "oi",
                            key,
                            {
                                "schema": "cryptowatcher.oi-source.v1",
                                "points": points,
                                "available_at_ms": received,
                                "response_server_ms": int(server_ms),
                                "source": "BYBIT_REST_OPEN_INTEREST_5MIN",
                                "unit": "BASE",
                            },
                        )
                    reason = None
                    errors.pop(key + "/oi", None)
                except Exception as exc:
                    reason = type(exc).__name__
                    errors[key + "/oi"] = reason
                if self.store.snapshot().revision != owner.revision:
                    return
                self.store.put_runtime(
                    "radar:v1:oi-attempt:" + key,
                    {
                        "at_ms": now,
                        "next_ms": now + 300_000,
                        "reason": reason,
                    },
                )
            self.store.put_runtime(
                "enrichment",
                {
                    "state": "PARTIAL" if errors else "OK",
                    "at_ms": self.market.server_now(),
                    "configuration_revision": owner.revision,
                    "mapped_perpetuals": len(pairs),
                    "errors": errors,
                },
            )

    def context(self, key: str, cutoff: int) -> dict[str, Any]:
        if not self.store.snapshot().settings.enrichment_enabled:
            return {"state": "DISABLED", "optional": True, "detector_input": False}
        if not key.startswith("spot/"):
            return {"state": "N/A", "reason": "INDEPENDENT_PERPETUAL", "detector_input": False}
        mapping = as_of(self.history, "mapping", identity("mapping/" + key), cutoff)
        if not mapping or not mapping["perpetual_instrument"]:
            return {
                "state": "MISSING",
                "reason": mapping["reason"] if mapping else "MAPPING_NOT_AVAILABLE_AT_CUTOFF",
                "optional": True,
                "detector_input": False,
                "cutoff_ms": cutoff,
            }
        spot, _ = classify("spot", mapping["spot_metadata"])
        perp, _ = classify("linear", mapping["perpetual_metadata"])
        if not spot or not perp:
            return {"state": "INVALID", "reason": "INVALID_MAPPING_METADATA", "detector_input": False}
        value = snapshot(
            spot,
            perp,
            at(self.store, "ticker", key, cutoff),
            at(self.store, "ticker", perp.key, cutoff),
            at(self.store, "oi", perp.key, cutoff),
            cutoff,
        )
        value.update(
            mapping_revision=mapping["revision"],
            mapping_available_at_ms=mapping["available_at_ms"],
            cutoff_ms=cutoff,
        )
        return value

    def diagnostics(self) -> dict[str, Any]:
        errors: dict[str, str] = {}
        error_count = 0
        for key, value in self.history.scan("oi-attempt:"):
            if value.get("reason"):
                error_count += 1
                if len(errors) < 50:
                    errors[key] = value["reason"]
        return {
            "enabled": self.store.snapshot().settings.enrichment_enabled,
            "ticker_cadence_ms": 60_000,
            "oi_cadence_ms": 300_000,
            "worker": self.store.get_runtime("enrichment", {"state": "PENDING"}),
            "mapping_count": self.history.count("mapping-head:"),
            "oi_error_count": error_count,
            "oi_errors": errors,
            "oi_errors_truncated": error_count > 50,
            "optional": True,
            "detector_input": False,
        }
