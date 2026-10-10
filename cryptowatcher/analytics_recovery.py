"""Bounded public backfill for referenced outcomes, including inactive instruments."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from typing import Any

from .events import History, identity
from .ingestion import BybitRadar
from .market import MarketError
from .outcomes import MINUTE, pending_key
from .sources import Candle, Sources
from .storage import Store, encode


class AnalyticsRecovery:
    def __init__(self, store: Store, market: BybitRadar) -> None:
        self.store, self.market, self.history = store, market, History(store)

    def request(self, stream: str, start: int, end: int) -> str | None:
        if start >= end:
            return "INVALID_REFERENCED_WINDOW"
        key = identity(f"analytics-gap/{stream}/{start}/{end}")
        job = self.history.get("analytics-gap:" + key)
        if job is not None and job["state"] != "COMPLETE":
            return job.get("reason", "REFERENCED_RECOVERY_PENDING")
        if job is None or job["state"] == "COMPLETE":
            now = self.market.server_now()
            pending = pending_key(now, key).replace("outcome-pending:", "analytics-gap-pending:", 1)
            with self.store.db:
                self.history.put(
                    "analytics-gap:" + key,
                    {
                        "id": key,
                        "stream": stream,
                        "start": start,
                        "end": end,
                        "cursor_end": end,
                        "state": "PENDING",
                        "next_attempt_ms": now,
                        "pending_key": pending,
                        "attempts": 0,
                    },
                )
                self.history.put(pending, {"id": key})
        return "REFERENCED_RECOVERY_PENDING"

    async def work(self, now: int) -> None:
        # Live gaps and bootstrap keep priority over historical analytics in the same REST budget.
        if any(v[0] < 20 for v in self.market._pending.values()) or self.market._working:
            return
        for _, binding in self.history.items("analytics-gap-pending:", 1):
            job = self.history.get("analytics-gap:" + binding["id"])
            if job["next_attempt_ms"] > now:
                return
            market, symbol, tf = job["stream"].split("/")
            step = int(tf[:-1]) * MINUTE
            try:
                response = await self.market._get(
                    "/v5/market/kline",
                    {
                        "category": market,
                        "symbol": symbol,
                        "interval": tf[:-1],
                        "limit": 1000,
                        "start": job["start"],
                        "end": job["cursor_end"] - 1,
                    },
                )
                candles = sorted(
                    (
                        Candle.parse(
                            row, tf, "rest-analytics", int(time.time() * 1000), int(response["time"])
                        )
                        for row in response["result"]["list"]
                    ),
                    key=lambda c: c.start,
                )
                if (
                    not candles
                    or candles[-1].end != job["cursor_end"]
                    or candles[0].start < job["start"]
                    or any(b.start - a.start != step for a, b in zip(candles, candles[1:]))
                ):
                    raise MarketError("incomplete_referenced_page")
                writes = []
                for candle in candles:
                    key = Sources.prefix(job["stream"]) + f"{candle.start:016d}"
                    existing = self.store.db.execute(
                        "SELECT payload_json FROM runtime WHERE key=?", (key,)
                    ).fetchone()
                    if existing is not None:
                        previous = Candle(**json.loads(existing[0]))
                        if previous.row() != candle.row():
                            raise MarketError("referenced_source_conflict")
                    else:
                        writes.append((key, encode(asdict(candle))))
                with self.store.db:
                    self.store.db.executemany("INSERT INTO runtime VALUES (?,?)", writes)
                    job["cursor_end"] = candles[0].start
                    job["state"] = "COMPLETE" if job["cursor_end"] == job["start"] else "PENDING"
                    job.pop("reason", None)
                    self.history.delete(job["pending_key"])
                    if job["state"] != "COMPLETE":
                        job["pending_key"] = pending_key(now, job["id"]).replace(
                            "outcome-pending:", "analytics-gap-pending:", 1
                        )
                        self.history.put(job["pending_key"], binding)
                    self.history.put("analytics-gap:" + job["id"], job)
            except (MarketError, KeyError, TypeError, ValueError, OverflowError) as exc:
                job.update(
                    state="RETRY",
                    attempts=job["attempts"] + 1,
                    reason=str(exc) if isinstance(exc, MarketError) else "INVALID_REFERENCED_RESPONSE",
                )
                job["next_attempt_ms"] = now + min(300, 2 ** min(job["attempts"], 8)) * 1000
                with self.store.db:
                    self.history.delete(job["pending_key"])
                    job["pending_key"] = pending_key(job["next_attempt_ms"], job["id"]).replace(
                        "outcome-pending:", "analytics-gap-pending:", 1
                    )
                    self.history.put(job["pending_key"], binding)
                    self.history.put("analytics-gap:" + job["id"], job)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "pending": self.history.count("analytics-gap-pending:"),
            "recent": [
                self.history.get("analytics-gap:" + v["id"])
                for _, v in self.history.items("analytics-gap-pending:", 3)
            ],
        }
