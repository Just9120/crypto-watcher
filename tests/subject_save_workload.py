"""Deterministic complete-save case from the frozen130s input, not a latency test.

Use all confirmed waves and fresh identities, actual Controller consumers, due
outcomes, signed transports and storage service. Logical time advances explicitly;
only CPU/work is comparable here. The paced Linux qualification stays unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import Counter
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from cryptowatcher.bot import Controller
from cryptowatcher.config import TIMEFRAMES, Infrastructure, OwnerSettings
from cryptowatcher.ingestion import BybitRadar
from cryptowatcher.market_storage import enable
from cryptowatcher.outcomes import MINUTE
from cryptowatcher.storage import Store, encode
from cryptowatcher.universe import classify
from tests.light_replay import ENV, NOW, SOURCE, ack, metadata
from tests.processing_replay import candle, global_reference, signal_point
from tests.subject_save_prototype import installed


async def run(path, enabled, *, instruments=4, audit=False):
    if path.exists():
        raise ValueError("Use a new owned disposable SQLite")
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
    clock = [NOW - 5 * MINUTE]
    stages = {}
    audit_counts = Counter()
    agents = []
    began, cpu = time.perf_counter(), time.process_time()

    def now():
        return clock[0]

    def measured(name, callback):
        start = time.process_time()
        try:
            return callback()
        finally:
            stages[name] = stages.get(name, 0) + time.process_time() - start

    def transport(request):
        if str(request.url) != ENV["AGENT_EVENT_URL"]:
            raise AssertionError("Unexpected network in subject-save narrow case")
        agents.append(json.loads(request.content))
        return ack(request)

    with (
        installed(enabled) as subject_stats,
        patch.dict(os.environ, ENV),
        patch("time.time", lambda: now() / 1000),
        closing(Store(path, infrastructure.owner_id, writer_connection=True)) as store,
    ):
        store.initialize(OwnerSettings())
        enable(store.db)
        store.bounded = True
        store.clock_ms = now
        if audit:

            def counted(key, new):
                audit_counts[
                    ("new:" if new else "update:") + key.removeprefix("radar:v1:").split(":")[0]
                ] += 1
                return 0

            store.db.create_function("cw_subject_mix", 2, counted)
            for operation, new in [("INSERT", 1), ("UPDATE", 0)]:
                store.db.execute(
                    f"CREATE TEMP TRIGGER cw_mix_{operation.lower()} AFTER {operation} ON main.runtime WHEN NEW.key LIKE 'radar:v1:%' BEGIN SELECT cw_subject_mix(NEW.key,{new}); END"
                )
            store.db.commit()
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            market = BybitRadar(store, client)
            market.server_now = now
            controller = Controller(infrastructure, store, market)
            controller.agent.enable(True)
            bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
            symbols = ["BTCUSDT"] + [f"SIM{i:03}USDT" for i in range(1, instruments)]
            streams = []
            for category in ("spot", "linear"):
                for symbol in symbols:
                    item, reason = classify(category, metadata(symbol=symbol, market=category))
                    assert item is not None, reason
                    market.instruments[item.key] = item
                    market.tickers[item.key] = {"turnover24h": "2000000", "lastPrice": "104"}
                    for tf in TIMEFRAMES:
                        stream = item.key + "/" + tf
                        streams.append(stream)
                        market.streams.add(stream)
                        step = int(tf[:-1]) * MINUTE
                        market.sources.put(
                            stream, [candle(NOW - i * step, tf) for i in range(24, 0, -1)], recovery=True
                        )
                    reference = NOW - 5 * MINUTE
                    measured(
                        "save_operation_cpu",
                        lambda: controller.engine.observe(
                            item.key + "/1m",
                            [candle(reference - i * MINUTE, "1m") for i in range(24, -1, -1)],
                            2000000,
                            item.tick_size,
                            reference,
                            live=True,
                        ),
                    )

            # Same pre-arrival reservation as the frozen full replay.
            controller.analytics_pool.tick(now())

            async def services():
                start = time.process_time()
                controller.analytics_pool.tick(now())
                for _ in range(16):
                    controller.outcomes.work(now())
                    jobs = controller.history.items("outcome-job:")
                    if not any(
                        j["state"] not in ("COMPLETE", "EXPIRED_RETENTION_INCOMPLETE")
                        and j["due_ms"] <= now()
                        for _, j in jobs
                    ):
                        break
                for _ in range(64):
                    await controller.delivery.send_due(bot, now())
                    pending = controller.history.items("pending:telegram:", 1)
                    if (
                        not pending
                        or int(pending[0][0][len("pending:telegram:") :].partition(":")[0]) > now()
                    ):
                        break
                for _ in range(64):
                    await controller.agent.work(now())
                    intents = controller.history.items("agent:intent-due:", 1)
                    sends = controller.history.items("agent:send-due:", 1)
                    if not intents and not sends:
                        break

                    def due(row, prefix):
                        return int(row[0][0][len(prefix) :].partition(":")[0]) if row else 2**63

                    if min(due(intents, "agent:intent-due:"), due(sends, "agent:send-due:")) > now():
                        break
                stages["outcomes_and_deliveries_cpu"] = (
                    stages.get("outcomes_and_deliveries_cpu", 0) + time.process_time() - start
                )

            offers = Counter()
            try:
                for offset in (0, 60, 120):
                    clock[0] = NOW + offset * 1000
                    for stream in streams:
                        category, symbol, tf = stream.split("/")
                        if offset % (int(tf[:-1]) * 60):
                            continue
                        value = candle(NOW + (offset // 60) * MINUTE, tf)
                        message = {
                            "topic": f"kline.{tf[:-1]}.{symbol}",
                            "ts": now(),
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
                        measured(
                            "save_operation_cpu",
                            lambda: market.accept_message(
                                category, message, market.streams, received_ms=now()
                            ),
                        )
                        assert market.sources.windows[stream][value.start].row() == value.row()
                        offers[stream] += 1
                    clock[0] += 2001
                    controller.engine.tick(now())
                    await services()
                # Include actual storage servicing; no prototype work is deferred.
                start = time.process_time()
                controller.retention.work(now())
                from cryptowatcher.storage_repack import work as repack

                maintenance_states = [repack(store, now(), batch=64, seconds=1) for _ in range(2)]
                stages["maintenance_cpu"] = time.process_time() - start
                facts = [signal_point(value) for _, value in controller.history.items("event:")]
                jobs = controller.history.items("outcome-job:")
                outcomes = [
                    controller.history.metadata(key.removeprefix("radar:v1:"))
                    for key, _ in store.scan_runtime("radar:v1:outcome-head:")
                ]
                result = {
                    "mode": "DETERMINISTIC_COMPLETE_SAVE_NOT_ARRIVAL_ACCEPTANCE",
                    "enabled": enabled,
                    "instruments_per_market": instruments,
                    "offered_closed": sum(offers.values()),
                    "facts": len(facts),
                    "families": sorted({point["detector_id"] for point in facts}),
                    "markets": sorted({point["market_type"] for point in facts}),
                    "timeframes": sorted({point["timeframe"] for point in facts}),
                    "facts_sha": hashlib.sha256(
                        encode(sorted(encode(f) for f in facts)).encode()
                    ).hexdigest(),
                    "due_pending": sum(
                        j["state"] not in ("COMPLETE", "EXPIRED_RETENTION_INCOMPLETE")
                        and j["due_ms"] <= now()
                        for _, j in jobs
                    ),
                    "future_jobs": sum(j["due_ms"] > now() for _, j in jobs),
                    "telegram": bot.send_message.await_count,
                    "agent": len(agents),
                    "agent_kinds": dict(Counter(r["delivery_kind"] for r in agents)),
                    "queues": {
                        p: controller.history.count(p)
                        for p in ("pending:telegram:", "agent:intent-due:", "agent:send-due:")
                    },
                    "source_blocks": dict(market.sources.blocked),
                    "outcomes_sha": hashlib.sha256(encode(outcomes).encode()).hexdigest(),
                    "completed_outcomes": sum(value["state"] == "COMPLETE" for value in outcomes),
                    "agent_payloads_sha": hashlib.sha256(
                        encode(sorted(encode(v) for v in agents)).encode()
                    ).hexdigest(),
                    "telegram_texts_sha": hashlib.sha256(
                        encode(
                            sorted(
                                call.kwargs.get("text", call.args[1] if len(call.args) > 1 else "")
                                for call in bot.send_message.call_args_list
                            )
                        ).encode()
                    ).hexdigest(),
                    "subject": dict(subject_stats),
                    "stages": stages,
                    "cpu_seconds": time.process_time() - cpu,
                    "wall_seconds": time.perf_counter() - began,
                    "sqlite_changes": store.db.total_changes,
                    "maintenance_states": [state["state"] for state in maintenance_states],
                    "files": {
                        suffix or "database": Path(str(path) + suffix).stat().st_size
                        for suffix in ("", "-wal", "-shm")
                        if Path(str(path) + suffix).exists()
                    },
                    "raw_mix": dict(audit_counts),
                    "audit_instrumentation": audit,
                }
                assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
                assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            finally:
                await controller.shared_records.close()
                await market.close()
    result["workload_cpu_with_close"] = time.process_time() - cpu
    oracle = global_reference(path.with_name("oracle.sqlite3"), instruments, 130)
    result["oracle_sha"] = hashlib.sha256(encode(oracle).encode()).hexdigest()
    result["oracle_equal"] = result["facts_sha"] == result["oracle_sha"]
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    result["cpu_seconds"] = time.process_time() - cpu
    result["wall_seconds"] = time.perf_counter() - began
    result["cpu_scope"] = "complete workload, teardown, independent oracle and owned fixture cleanup"
    return result
