"""Finite full-path replay; external transports are mocks, application code is real.

Logical market time is accelerated. Wall/CPU and filesystem bytes are measured
separately; this is neither a VPS capacity result nor a 24-hour stability test.
Run the identical file in two source snapshots to compare the same offered input.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx

from cryptowatcher.analytics import ComparisonWorker
from cryptowatcher.bot import Controller
from cryptowatcher.config import TIMEFRAMES, Infrastructure, OwnerSettings
from cryptowatcher.ingestion import BybitRadar
from cryptowatcher.market_storage import enable
from cryptowatcher.outcomes import MINUTE
from cryptowatcher.sources import Candle
from cryptowatcher.storage import Store, encode
from cryptowatcher.universe import classify

from .test_agent_delivery import ENV, SOURCE, ack
from .test_events import NOW
from .test_universe import metadata


def minute_row(start: int) -> list[Any]:
    offset = (start - NOW) // MINUTE
    if offset < -1:
        close, opening, high, low, turnover = 100.0, 100.0, 101.0, 99.0, 400
    elif offset == -1:
        close, opening, high, low, turnover = 104.0, 100.0, 110.0, 90.0, 20_000
    else:
        opening = 104.0 * (1.04 * 0.96) ** (offset // 2) * (0.96 if offset % 2 else 1.0)
        close = opening * (0.96 if offset % 2 == 0 else 1.04)
        high, low, turnover = max(opening, close) * 1.1, min(opening, close) * 0.9, 400
    return [start, opening, high, low, close, 1, turnover]


def candle(end: int, tf: str) -> Candle:
    count = int(tf[:-1])
    parts = [minute_row(start) for start in range(end - count * MINUTE, end, MINUTE)]
    row = [
        parts[0][0],
        parts[0][1],
        max(r[2] for r in parts),
        min(r[3] for r in parts),
        parts[-1][4],
        count,
        sum(r[6] for r in parts),
    ]
    return Candle.parse(row, tf, "websocket", end, end)


async def replay(path: Path, *, minutes: int = 30) -> dict[str, Any]:
    if path.exists() or not 15 <= minutes <= 60:
        raise ValueError("Replay requires a new temporary database and 15..60 logical minutes")
    infrastructure = Infrastructure.from_env(
        {
            "OWNER_USER_ID": "233382575",
            "EXPECTED_BOT_ID": "7523865123",
            "TELEGRAM_BOT_TOKEN": "7523865123:" + "test_only_not_a_secret" * 2,
            "DATABASE_FILE": str(path),
            "STATE_FILE": str(path.with_suffix(".json")),
            "SOURCE_REVISION": SOURCE,
        }
    )
    started, cpu_started = time.perf_counter(), time.process_time()
    stages: dict[str, Any] = defaultdict(lambda: {"calls": 0, "wall_seconds": 0.0, "cpu_seconds": 0.0})

    @contextmanager
    def measured(name):
        wall, cpu = time.perf_counter(), time.process_time()
        try:
            yield
        finally:
            stages[name]["calls"] += 1
            stages[name]["wall_seconds"] += time.perf_counter() - wall
            stages[name]["cpu_seconds"] += time.process_time() - cpu

    offered = hashlib.sha256()
    offers: Counter[str] = Counter()
    trace = []
    agents = []
    debt = []
    store = Store(path, infrastructure.owner_id)
    store.initialize(OwnerSettings())
    enable(store.db)
    store.bounded = True
    clock = [NOW]
    store.clock_ms = lambda: clock[0]

    def transport(request):
        if str(request.url) != ENV["AGENT_EVENT_URL"]:
            raise AssertionError("Replay attempted an unexpected external request")
        assert not store.db.in_transaction
        agents.append(json.loads(request.content))
        return ack(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        market = BybitRadar(store, client)
        market.server_now = lambda: clock[0]
        with patch.dict(os.environ, ENV):
            controller = Controller(infrastructure, store, market)
        controller.agent.enable(True)
        original = controller.engine.on_event

        def admitted(event):
            trace.append(
                {
                    k: event[k]
                    for k in (
                        "event_id",
                        "episode_id",
                        "market_type",
                        "timeframe",
                        "detector_id",
                        "reference_price",
                        "reference_time_ms",
                        "available_at_ms",
                    )
                }
            )
            original(event)

        controller.engine.on_event = admitted
        comparisons = ComparisonWorker(store)
        bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))

        async def work():
            with measured("outcomes"):
                for _ in range(64):
                    controller.outcomes.work(clock[0])
                    due = [
                        j
                        for _, j in controller.history.items("outcome-job:")
                        if j.get("state") != "COMPLETE" and j.get("due_ms", 2**63) <= clock[0]
                    ]
                    if not due:
                        break
                debt.append(
                    {
                        "at_ms": clock[0],
                        "due_outcomes": len(due),
                        "future_outcomes": sum(
                            j.get("due_ms", 0) > clock[0] for _, j in controller.history.items("outcome-job:")
                        ),
                    }
                )
            # Old main still admits N20; exercise its current worker too.
            with measured("comparisons"):
                comparisons.work(clock[0])
            with measured("telegram"):
                await controller.delivery.send_due(bot, clock[0])
            with measured("agent"):
                for _ in range(64):
                    await controller.agent.work(clock[0])
                    if not controller.history.items("agent:intent-due:", 1) and not controller.history.items(
                        "agent:send-due:", 1
                    ):
                        break

        try:
            for category in ("spot", "linear"):
                instrument, reason = classify(category, metadata(market=category))
                assert instrument is not None, reason
                market.instruments[instrument.key] = instrument
                market.tickers[instrument.key] = {"turnover24h": "2000000", "lastPrice": "104"}
                for tf in TIMEFRAMES:
                    stream = instrument.key + "/" + tf
                    market.streams.add(stream)
                    step = int(tf[:-1]) * MINUTE
                    market.sources.put(
                        stream, [candle(NOW - i * step, tf) for i in range(24, 0, -1)], recovery=True
                    )
            for minute in range(minutes + 1):
                clock[0] = NOW + minute * MINUTE
                with measured("offered_ingestion_and_publication"):
                    for category in ("spot", "linear"):
                        for tf in TIMEFRAMES:
                            if minute % int(tf[:-1]):
                                continue
                            value = candle(clock[0], tf)
                            stream = category + "/BTCUSDT/" + tf
                            message = {
                                "topic": "kline." + tf[:-1] + ".BTCUSDT",
                                "ts": clock[0],
                                "data": [
                                    {
                                        "interval": tf[:-1],
                                        "start": value.start,
                                        "end": value.end - 1,
                                        "open": value.open,
                                        "high": value.high,
                                        "low": value.low,
                                        "close": value.close,
                                        "volume": value.volume,
                                        "turnover": value.turnover,
                                        "confirm": True,
                                    }
                                ],
                            }
                            offered.update(encode([category, message]).encode())
                            offers[stream] += 1
                            market.accept_message(category, message, market.streams, received_ms=clock[0])
                            assert market.sources.windows[stream][value.start].row() == value.row()
                clock[0] += 2001
                controller.engine.tick(clock[0])
                await work()
            freshness = market.confirmed_source_diagnostic(clock[0])
            # Finish the finite offered interval; supply only the exact future 1m
            # inputs for its not-yet-due horizons. No new episodes are admitted here.
            with measured("future_inputs"):
                for category in ("spot", "linear"):
                    stream = category + "/BTCUSDT/1m"
                    market.sources.put(
                        stream, [candle(NOW + i * MINUTE, "1m") for i in range(minutes + 1, minutes + 61)]
                    )
            clock[0] = NOW + (minutes + 60) * MINUTE + 2001
            await work()
            outcomes = [r for _, r in controller.history.items("outcome-head:")]
            roots = {}
            for event in trace:
                roots.setdefault(event["episode_id"], event["event_id"])
            initial_ids = {
                e["event_id"]
                for e in trace
                if e["reference_time_ms"] == NOW and roots[e["episode_id"]] == e["event_id"]
            }
            initial_results = sorted(
                (r["event_id"], r["horizon_min"], r["metrics"])
                for r in outcomes
                if r["event_id"] in initial_ids
            )
            result = {
                "schema": "cryptowatcher.light-replay.v1",
                "environment": sys.platform,
                "logical_minutes": minutes,
                "offered_sha256": offered.hexdigest(),
                "offered_closed_candles": sum(offers.values()),
                "offers_by_stream": dict(offers),
                "persisted_and_confirmed": sum(offers.values()),
                "events": len(trace),
                "episodes": len(roots),
                "families": sorted({e["detector_id"] for e in trace}),
                "outcome_states": dict(Counter(r["state"] for r in outcomes)),
                "comparison_jobs": controller.history.count("comparison-job:"),
                "comparison_states": dict(
                    Counter(r["state"] for _, r in controller.history.items("comparison-head:"))
                ),
                "debt_checkpoints": debt,
                "source_freshness_at_offer_end": freshness,
                "telegram_attempts": bot.send_message.await_count,
                "agent_receipts": len(agents),
                "agent_kinds": dict(Counter(r["delivery_kind"] for r in agents)),
                "initial_root_result_count": len(initial_results),
                "initial_root_numeric_sha256": hashlib.sha256(encode(initial_results).encode()).hexdigest(),
                "sqlite_total_changes": store.db.total_changes,
                "stages": dict(stages),
                "wall_seconds": time.perf_counter() - started,
                "cpu_seconds": time.process_time() - cpu_started,
                "files": {
                    suffix or "database": Path(str(path) + suffix).stat().st_size
                    for suffix in ("", "-wal", "-shm")
                    if Path(str(path) + suffix).exists()
                },
                "limitations": [
                    "Synthetic finite input, accelerated logical clock; not sustainable VPS capacity",
                    "Real application producer/workers, mock Telegram and signed agent ACK; no live transport",
                    "Future horizons are reported separately; final drain supplies their exact future inputs",
                    "SQL changes and file bytes are not physical disk I/O measurements",
                ],
            }
            if sys.platform == "linux":
                import resource

                usage = resource.getrusage(resource.RUSAGE_SELF)
                result["process_usage"] = {
                    "peak_rss_bytes": usage.ru_maxrss * 1024,
                    "ru_inblock": usage.ru_inblock,
                    "ru_oublock": usage.ru_oublock,
                }
            else:
                result["process_usage"] = {"state": "UNAVAILABLE_NATIVE_RESOURCE_COUNTERS"}
            return result
        finally:
            await market.close()
            store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=int, default=30)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists():
        parser.error("Report already exists; do not overwrite evidence")
    with tempfile.TemporaryDirectory(prefix="cw-light-replay-") as temporary:
        report = asyncio.run(replay(Path(temporary) / "replay.sqlite3", minutes=args.minutes))
    report["temporary_database_removed"] = True
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "offered_closed_candles",
                    "events",
                    "episodes",
                    "outcome_states",
                    "telegram_attempts",
                    "agent_receipts",
                    "wall_seconds",
                    "cpu_seconds",
                )
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
