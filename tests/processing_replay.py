"""Paced joint pipeline qualification with an independent, finite mock wire.

Application receivers, observation callbacks, calculations, publication, delivery,
retention and repack run together. The exchange clock has a fixed offset only:
there is no time acceleration. Reports distinguish seed work, offered arrivals,
source latency, due results and future horizons. This is not a live VPS test.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import queue
import sys
import tempfile
import threading
import time
from collections import Counter
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from cryptowatcher import frozen_codec_native
from cryptowatcher.bot import Controller
from cryptowatcher.config import TIMEFRAMES, Infrastructure, OwnerSettings
from cryptowatcher.ingestion import BybitRadar, shards
from cryptowatcher.market_storage import enable
from cryptowatcher.outcomes import MINUTE
from cryptowatcher.sources import Candle
from cryptowatcher.storage import Store, encode
from cryptowatcher.universe import classify

from .light_replay import ENV, NOW, SOURCE, ack, metadata, minute_row


def candle(end: int, tf: str) -> Candle:
    count = int(tf[:-1])
    parts = []
    for start in range(end - count * MINUTE, end, MINUTE):
        part = minute_row(start + 5 * MINUTE)
        if start == NOW - MINUTE:
            part[4], part[2], part[6] = 115.0, 116.0, 40000
        parts.append(part)
    return Candle.parse(
        [
            end - count * MINUTE,
            parts[0][1],
            max(r[2] for r in parts),
            min(r[3] for r in parts),
            parts[-1][4],
            count,
            sum(r[6] for r in parts),
        ],
        tf,
        "websocket",
        end,
        end,
    )


async def replay(
    path: Path,
    *,
    instruments: int = 32,
    seconds: int = 130,
    progress=None,
    direct_sqlite: bool = False,
    bounded_pool: bool = False,
) -> dict:
    if bounded_pool and not direct_sqlite:
        raise ValueError("The isolated pool uses the explicit direct SQLite repository")
    if path.exists() or type(instruments) is not int or not 1 <= instruments <= 256:
        raise ValueError("Qualification needs a new owned database and1..256 instruments per market")
    if type(seconds) is not int or not 4 <= seconds <= 190:
        raise ValueError("Qualification duration outside4..190 real seconds")
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
    wall_started, cpu_started = time.monotonic(), time.process_time()

    def stage(name):
        if progress is not None:
            progress(
                {
                    "stage": name,
                    "wall_seconds": time.monotonic() - wall_started,
                    "cpu_seconds": time.process_time() - cpu_started,
                }
            )

    clock = [NOW - 5 * MINUTE]
    steady_started = [None]

    def now():
        start = steady_started[0]
        return clock[0] if start is None else NOW + int((time.monotonic() - start) * 1000)

    # One fixed exchange/wall offset; the scheduler and independent arrival wire
    # keep their real monotonic clock. No global machine clock is changed.
    from .sqlite_repository import repository_context

    with (
        patch("time.time", lambda: now() / 1000),
        patch.dict(os.environ, ENV),
        repository_context(direct_sqlite) as finish_readers,
        closing(Store(path, infrastructure.owner_id)) as store,
    ):
        store.initialize(OwnerSettings())
        enable(store.db)
        store.bounded = True
        store.clock_ms = now
        if direct_sqlite:
            from .sqlite_repository import install

            install(store)
        agents = []

        rest_requests = []
        refresh_requests = []

        def transport(request):
            if str(request.url) == ENV["AGENT_EVENT_URL"]:
                assert not store.db.in_transaction
                agents.append(json.loads(request.content))
                return ack(request)
            if (
                bounded_pool
                and request.url.host == "api.bybit.com"
                and request.url.path in ("/v5/market/tickers", "/v5/market/instruments-info")
            ):
                category = request.url.params["category"]
                refresh_requests.append({"path": request.url.path, "category": category})
                rows = (
                    [metadata(symbol=symbol, market=category) for symbol in symbols]
                    if request.url.path.endswith("instruments-info")
                    else [
                        {"symbol": symbol, "turnover24h": "2000000", "lastPrice": "104"} for symbol in symbols
                    ]
                )
                return httpx.Response(
                    200, json={"retCode": 0, "time": now(), "result": {"list": rows, "nextPageCursor": ""}}
                )
            if request.url.host != "api.bybit.com" or request.url.path != "/v5/market/kline":
                raise AssertionError("Unexpected external request")
            query = dict(request.url.params)
            assert query["category"] in ("spot", "linear") and query["symbol"] in symbols
            step = int(query["interval"]) * MINUTE
            start, end = int(query["start"]), int(query["end"])
            assert start % step == 0 and start < end + 1 <= now()
            assert (end - start + 1) // step <= 1000
            rest_requests.append(
                {
                    "stream": query["category"] + "/" + query["symbol"] + "/" + query["interval"] + "m",
                    "start": start,
                    "end": end,
                }
            )
            rows = [candle(at + step, query["interval"] + "m").row() for at in range(start, end + 1, step)]
            return httpx.Response(
                200, json={"retCode": 0, "retMsg": "OK", "time": now(), "result": {"list": rows[::-1]}}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            market = BybitRadar(store, client)
            market.server_now = now
            controller = Controller(infrastructure, store, market)
            controller.agent.enable(True)
            pool = None
            global_trace = []
            if bounded_pool:
                from .bounded_pool import Pool

                pool = Pool(controller.outcomes, manual=("spot/BTCUSDT", "linear/BTCUSDT"))
                original_publish = controller.engine.on_event_snapshot

                def published(event, episode):
                    global_trace.append(signal_point(event))
                    original_publish(event, episode)

                controller.engine.on_event_snapshot = published
            bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
            context = SimpleNamespace(bot=bot)
            symbols = ["BTCUSDT"] + [f"SIM{i:03}USDT" for i in range(1, instruments)]
            streams = []
            seed_digest = hashlib.sha256(encode(store.snapshot().settings.payload()).encode())
            for category in ("spot", "linear"):
                for symbol in symbols:
                    instrument, reason = classify(category, metadata(symbol=symbol, market=category))
                    assert instrument is not None, reason
                    market.instruments[instrument.key] = instrument
                    market.tickers[instrument.key] = {"turnover24h": "2000000", "lastPrice": "104"}
                    for tf in TIMEFRAMES:
                        stream = instrument.key + "/" + tf
                        streams.append(stream)
                        market.streams.add(stream)
                        step = int(tf[:-1]) * MINUTE
                        prepared = [candle(NOW - i * step, tf) for i in range(24, 0, -1)]
                        seed_digest.update(encode([stream, [value.row() for value in prepared]]).encode())
                        market.sources.put(stream, prepared, recovery=True)
                    # Pre-existing due work, with complete closed inputs at T0.
                    # The original first observation is exactly five minutes old.
                    reference = NOW - 5 * MINUTE
                    rows = [candle(reference - i * MINUTE, "1m") for i in range(24, -1, -1)]
                    seed_digest.update(encode([instrument.key, [value.row() for value in rows]]).encode())
                    controller.engine.observe(
                        instrument.key + "/1m", rows, 2000000, instrument.tick_size, reference, live=True
                    )
                market.metadata_at[category] = market.tickers_at[category] = time.monotonic()
            if pool is not None:
                pool.tick(now())
                # Force exactly the next ordinary refresh to exercise its real
                # global persistence. The added cadence is explicit in report.
                for category in ("spot", "linear"):
                    market.metadata_at[category] -= 601
                    market.tickers_at[category] -= 301
            seed_seconds = time.monotonic() - wall_started
            seed_cpu = time.process_time() - cpu_started
            stage("SEED_COMPLETE")
            controller.maintenance_started_at = NOW / 1000
            market._scope_revision = store.snapshot().revision
            queues = {}
            arrivals = {}
            loop = asyncio.get_running_loop()
            groups = {}
            for category in ("spot", "linear"):
                for i, group in enumerate(shards([s for s in streams if s.startswith(category + "/")])):
                    name = f"{category}/{i}"
                    groups[name] = tuple(group)
                    queues[name] = queue.Queue(maxsize=4096)
                    arrivals[name] = asyncio.Event()
                    market.connections[name] = {
                        "state": "CONNECTED",
                        "requested": len(group),
                        "acknowledged": len(group),
                    }
            market._groups = groups
            source_lags = []
            accepted = Counter()
            original_accept = market.accept_message

            def accept(category, message, allowed, **kwargs):
                original_accept(category, message, allowed, **kwargs)
                if message["data"][0]["confirm"]:
                    parts = message["topic"].split(".")
                    stream = f"{category}/{parts[2]}/{parts[1]}m"
                    source = market.sources.windows[stream][message["data"][0]["start"]]
                    assert source.end == message["data"][0]["end"] + 1
                    assert source.close == message["data"][0]["close"]
                    accepted[stream] += 1
                    source_lags.append(max(0, now() - message["data"][0]["end"] - 1))

            market.accept_message = accept
            completed = []
            original_apply = controller.outcomes.apply

            def apply(job, event, binding, result, at, **options):
                original_apply(job, event, binding, result, at, **options)
                if result["state"] == "COMPLETE":
                    # Observe committed visibility on the next loop turn. This
                    # also covers an opt-in atomic group without assuming that
                    # returning from an individual apply has committed it.
                    def visible():
                        saved = controller.history.metadata("outcome-head:" + job["outcome_id"])
                        if saved is not None and saved["state"] == "COMPLETE":
                            completed.append((job["outcome_id"], max(0, now() - job["due_ms"])))

                    loop.call_soon(visible)

            controller.outcomes.apply = apply
            stop = threading.Event()
            offered = Counter()
            offered_hash = hashlib.sha256()
            producer_errors = []

            def message(stream, offset, confirmed):
                category, symbol, tf = stream.split("/")
                end = NOW + (offset // 60) * MINUTE
                if not confirmed:
                    end = NOW + (offset // (int(tf[:-1]) * 60) + 1) * int(tf[:-1]) * MINUTE
                value = candle(end, tf)
                return {
                    "topic": f"kline.{tf[:-1]}.{symbol}",
                    "ts": NOW + offset * 1000,
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
                            "confirm": confirmed,
                        }
                    ],
                }

            def wire():
                try:
                    for offset in range(0, seconds, 5):
                        if stop.wait(max(0, steady_started[0] + offset - time.monotonic())):
                            return
                        for name, group in groups.items():
                            for stream in group:
                                confirmed = offset % (int(stream.rsplit("/", 1)[1][:-1]) * 60) == 0
                                value = message(stream, offset, confirmed)
                                raw = encode(value)
                                offered_hash.update(encode([name, raw]).encode())
                                if confirmed:
                                    offered[stream] += 1
                                queues[name].put_nowait(raw)
                                loop.call_soon_threadsafe(arrivals[name].set)
                except BaseException as exc:
                    producer_errors.append(type(exc).__name__)

            class Wire:
                def __init__(self, name):
                    self.name = name

                async def recv(self):
                    while True:
                        try:
                            return queues[self.name].get_nowait()
                        except queue.Empty:
                            arrivals[self.name].clear()
                            if not queues[self.name].empty():
                                continue
                            await arrivals[self.name].wait()

            calls = Counter()
            errors = []
            tasks = []

            async def periodic(name, callback, interval, first):
                await asyncio.sleep(first)
                while not stop.is_set():
                    began = time.monotonic()
                    try:
                        await callback(context)
                        calls[name] += 1
                    except Exception as exc:
                        errors.append((name, type(exc).__name__))
                        raise
                    await asyncio.sleep(max(0.001, interval - (time.monotonic() - began)))

            async def diagnostics(_context):
                market.persist_diagnostics()

            async def global_refresh(_context):
                await market.refresh(store.snapshot().settings)

            samples = []

            async def pool_work(_context):
                pool.tick(now())
                # This bounded diagnostic is part of the measured workload.
                jobs = [j for _, j in controller.history.items("outcome-job:")]
                pending_bytes = sum(
                    store.db.execute(
                        "SELECT COALESCE(SUM(length(CAST(payload_json AS BLOB))),0) FROM runtime WHERE key>=? AND key<?",
                        ("radar:v1:" + prefix, "radar:v1:" + prefix + "~"),
                    ).fetchone()[0]
                    for prefix in ("pending:telegram:", "agent:intent-due:", "agent:send-due:")
                )
                pending_count = sum(
                    controller.history.count(prefix)
                    for prefix in ("pending:telegram:", "agent:intent-due:", "agent:send-due:")
                )
                if (
                    pending_count > pool.limits["delivery_pending"]
                    or pending_bytes > pool.limits["delivery_pending_bytes"]
                ):
                    raise RuntimeError("Global delivery work exceeded its explicit budget")
                samples.append(
                    {
                        "at_ms": now(),
                        "source_accepted": sum(accepted.values()),
                        "source_max_ms": max(source_lags, default=0),
                        "due": sum(j["due_ms"] <= now() for j in jobs),
                        "future": sum(j["due_ms"] > now() for j in jobs),
                        "oldest_due_ms": max(
                            (now() - j["due_ms"] for j in jobs if j["due_ms"] <= now()), default=0
                        ),
                        "telegram": controller.history.count("pending:telegram:"),
                        "agent_intents": controller.history.count("agent:intent-due:"),
                        "agent_send": controller.history.count("agent:send-due:"),
                        "delivery_pending_bytes": pending_bytes,
                    }
                )

            steady_started[0] = time.monotonic()
            phase_cpu = time.process_time()
            producer = threading.Thread(target=wire, name="finite-mock-wire", daemon=True)
            try:
                for name, group in groups.items():
                    task = asyncio.create_task(
                        market._receive_messages(Wire(name), name, name.split("/")[0], set(group))
                    )
                    market._sessions[name] = task
                for name, callback, interval, first in (
                    ("poll", controller.poll, infrastructure.engine_interval, 0),
                    ("warmup", controller.warmup_poll, 5, 2),
                    ("heartbeat", controller.heartbeat, 20, 1),
                    ("deliver", controller.deliver, 2, 2),
                    ("analytics", controller.analytics_job, 5, 5),
                    ("repack", controller.repack_job, 1, 3),
                    ("backfill", controller.backfill_job, 1, 4),
                    ("agent", controller.agent_job, 2, 3),
                    ("diagnostics", diagnostics, 1, 0),
                ):
                    tasks.append(
                        asyncio.create_task(periodic(name, callback, interval, first), name="fixture:" + name)
                    )
                if pool is not None:
                    tasks.append(asyncio.create_task(periodic("pool", pool_work, 2, 0), name="fixture:pool"))
                    tasks.append(
                        asyncio.create_task(
                            periodic("global_refresh", global_refresh, 300, 1), name="fixture:global-refresh"
                        )
                    )
                producer.start()
                await asyncio.sleep(seconds)
                stage("ARRIVAL_WINDOW_COMPLETE")
            finally:
                # Freeze the endpoint before cancellation/cleanup can service
                # any extra source messages or change the measured queues.
                finished = now()
                phase_seconds = time.monotonic() - steady_started[0]
                phase_used_cpu = time.process_time() - phase_cpu
                measured_accepted, measured_lags, measured_completed = (
                    dict(accepted),
                    list(source_lags),
                    list(completed),
                )
                measured_transports = (bot.send_message.await_count, len(agents))
                measured_calls = dict(calls)
                measured_timing = {key: dict(value) for key, value in market._work_timing.items()}
                stop.set()
                for task in market._sessions.values():
                    task.cancel()
                for task in tasks:
                    task.cancel()
                # There is one writer and no await while capturing this state.
                jobs = [value for _, value in controller.history.items("outcome-job:")]
                results = [
                    controller.history.metadata(key.removeprefix("radar:v1:"))
                    for key, _ in store.scan_runtime("radar:v1:outcome-head:")
                ]
                delivery_queues = {}
                for prefix in ("pending:telegram:", "agent:intent-due:", "agent:send-due:"):
                    first = controller.history.items(prefix, 1)
                    due = int(first[0][0][len(prefix) :].partition(":")[0]) if first else None
                    delivery_queues[prefix] = {
                        "pending_total": controller.history.count(prefix),
                        "first_due_ms": due,
                        "oldest_due_lag_ms": max(0, finished - due) if due is not None else 0,
                    }
                measured_freshness = market.confirmed_source_diagnostic(finished)
                measured_analytics = store.get_runtime("analytics", {})
                measured_pool = pool.report() if pool is not None else None
                stage("STOPPING_FIXTURE_WORKERS")
                producer.join(timeout=1)
                assert not producer.is_alive(), "Independent offered input did not terminate"
                for task in tasks:
                    task.cancel()
                done, pending = await asyncio.wait(tasks, timeout=5)
                if pending:
                    if progress is not None:
                        progress(
                            {
                                "stage": "CANCELLATION_PENDING",
                                "tasks": [
                                    {"name": task.get_name(), "await": str(task.get_coro())}
                                    for task in pending
                                ],
                            }
                        )
                    for task in pending:
                        task.cancel()
                    done, pending = await asyncio.wait(pending, timeout=5)
                    if pending:
                        raise RuntimeError("Fixture worker did not terminate after cancellation")
                await asyncio.gather(*tasks, return_exceptions=True)
                await controller.shared_records.close()
                receiver_errors = [
                    type(task.exception()).__name__
                    for task in market._sessions.values()
                    if task.done() and not task.cancelled() and task.exception() is not None
                ]
                await market.close()
                if finish_readers is not None:
                    await finish_readers()
                stage("FIXTURE_WORKERS_STOPPED")
            pending_due = [job for job in jobs if job["due_ms"] <= finished]
            result = {
                "schema": "cryptowatcher.processing-replay.v2",
                "environment": sys.platform,
                "instruments_per_market": instruments,
                "streams": len(streams),
                "real_seconds_requested": seconds,
                "phase_seconds": phase_seconds,
                "phase_cpu_seconds": phase_used_cpu,
                "seed_seconds": seed_seconds,
                "seed_cpu_seconds": seed_cpu,
                "seed_sha256": seed_digest.hexdigest(),
                "offered_sha256": offered_hash.hexdigest(),
                "offered_closed": sum(offered.values()),
                "accepted_closed": sum(measured_accepted.values()),
                "accepted_by_stream": measured_accepted,
                "producer_errors": producer_errors,
                "worker_errors": errors,
                "receiver_errors": receiver_errors,
                "max_source_lag_ms": max(measured_lags, default=None),
                "source_p95_ms": sorted(measured_lags)[math.ceil(len(measured_lags) * 0.95) - 1]
                if measured_lags
                else None,
                "completed": len(measured_completed),
                "max_completed_due_lag_ms": max((v[1] for v in measured_completed), default=None),
                "due_pending": len(pending_due),
                "oldest_due_pending_ms": max((finished - j["due_ms"] for j in pending_due), default=0),
                "future_jobs": sum(job["due_ms"] > finished for job in jobs),
                "outcome_states": dict(Counter(value["state"] for value in results)),
                "families": sorted(
                    {value["detector_id"] for _, value in store.scan_runtime("radar:v1:event-head:")}
                ),
                "telegram_attempts": measured_transports[0],
                "signed_agent_receipts": measured_transports[1],
                "scheduled_calls": measured_calls,
                "repack": store.get_runtime("storage_repack", {}),
                "referenced_rest_requests": rest_requests,
                "recovery_worker": store.get_runtime("analytics_recovery_worker", {}),
                "analytics": measured_analytics,
                "input_timing": measured_timing,
                "source_freshness_at_endpoint": measured_freshness,
                "delivery_queues_at_endpoint": delivery_queues,
                "native_backend": frozen_codec_native.available(),
                "comparison_jobs": controller.history.count("comparison-job:"),
                "numeric_sha256": hashlib.sha256(
                    encode(sorted((r["outcome_id"], r["state"], r["metrics"]) for r in results)).encode()
                ).hexdigest(),
                "working_bytes": sum(
                    Path(str(path) + suffix).stat().st_size
                    for suffix in ("", "-wal", "-shm")
                    if Path(str(path) + suffix).exists()
                ),
                "limitations": [
                    "Finite controlled exchange clock at real1:1 rate; mock wire/Telegram/signed agent, not live VPS",
                    "Seed and steady costs separated; future horizons are not backlog",
                    "Partial frames every5s, closed bursts on real TF cadence; no discovery HTTP/socket overhead",
                    "Independent wire buffer is finite4096/group; TCP/websocket buffer dynamics are not simulated",
                    "Due residence/source latency include competing writer, maintenance and callback scheduling",
                    "Native CI and unchanged shared-host30min/24h remain independent required gates",
                    "Endpoint captured before cancellation; file bytes are after join, not disk IO or measured peak",
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
            if direct_sqlite:
                from .sqlite_repository import stats

                result["direct_sqlite"] = stats(store)
            if pool is not None:
                expected = global_reference(path.with_name("oracle.sqlite3"), instruments, seconds)
                actual = sorted(encode(point) for point in global_trace)
                from cryptowatcher.events import identity

                selected_ids = set()
                for row in measured_pool["selected"]:
                    event = controller.history.get("event-head:" + row[1])
                    selected_ids.update(
                        identity(f"outcome/{row[0]}/{h}/2")
                        for h in event["settings_snapshot"]["outcome_horizons"]
                    )
                scheduled_ids = {r["outcome_id"] for r in results} | {j["outcome_id"] for j in jobs}
                result["bounded_pool"] = {
                    **measured_pool,
                    "global_expected": len(expected),
                    "global_actual": len(actual),
                    "global_expected_sha256": hashlib.sha256(encode(expected).encode()).hexdigest(),
                    "global_actual_sha256": hashlib.sha256(encode(actual).encode()).hexdigest(),
                    "samples": samples,
                    "selected_outcome_ids": sorted(selected_ids),
                    "actual_outcome_ids": sorted(scheduled_ids),
                    "global_refresh_requests": refresh_requests,
                    "agent_receipts_by_kind": dict(Counter(a["delivery_kind"] for a in agents)),
                    "coverage_limitations": [
                        "130s may not fill64 long-lived reservations; transition fixtures are separate",
                        "Global rich event publication remains unchanged",
                        "One added global metadata/ticker refresh; previous replay had no refresh calls",
                        "Oracle compares exact market values/types/formulas/origins; reception timestamps differ by scheduling. Each actual source-window SHA is verified separately",
                    ],
                    "capacity": {
                        "state": "UNPROVEN",
                        "general_hours": 72,
                        "detail_days": 30,
                        "aggregate_months": 12,
                        "working_limit_bytes": 35_000_000_000,
                        "model": "global candles72h + global signals/used-inputs30d + selected outcomes30d + unique annual aggregates + indexes/free-pages/journals/legacy debt; shared inputs counted once",
                        "missing": [
                            "steady global meaningful-signal incidence",
                            "mature per-population physical bytes",
                            "shared-VPS backup/temp reserve",
                        ],
                    },
                }
            return result


def signal_point(event):
    if hashlib.sha256(encode(event["source_inputs"]).encode()).hexdigest() != event["source_window_id"]:
        raise AssertionError("Source window hash does not match the saved exact inputs")
    point = {
        k: event[k]
        for k in (
            "market_type",
            "symbol",
            "timeframe",
            "detector_id",
            "direction",
            "strength",
            "market_observation_ms",
            "reference_price",
            "reference_time_ms",
            "metrics",
        )
    }
    point["source_data_sha256"] = hashlib.sha256(
        encode(
            [
                {k: v for k, v in row.items() if k not in ("received_ms", "server_ms")}
                for row in event["source_inputs"]
            ]
        ).encode()
    ).hexdigest()
    return point


def global_reference(path, instruments, seconds):
    """Independent ungated engine, two template instruments and frozen market time.

    Only symbol changes between identical fixture instruments. No transports or
    outcomes run here. This post-phase oracle still counts in the total limits.
    """
    from cryptowatcher.events import EventEngine

    points = []
    with closing(Store(path, 233382575)) as store:
        store.initialize(OwnerSettings())
        engine = EventEngine(store, source_revision=SOURCE, on_event=lambda e: points.append(signal_point(e)))
        for category in ("spot", "linear"):
            reference = NOW - 5 * MINUTE
            engine.observe(
                category + "/BTCUSDT/1m",
                [candle(reference - i * MINUTE, "1m") for i in range(24, -1, -1)],
                2000000,
                0.01,
                reference,
                live=True,
            )
            windows = {
                tf: [candle(NOW - i * int(tf[:-1]) * MINUTE, tf) for i in range(24, 0, -1)]
                for tf in TIMEFRAMES
            }
            for offset in range(0, seconds, 5):
                for tf in TIMEFRAMES:
                    if offset % (int(tf[:-1]) * 60):
                        continue
                    windows[tf].append(candle(NOW + (offset // 60) * MINUTE, tf))
                    engine.observe(
                        category + "/BTCUSDT/" + tf,
                        windows[tf],
                        2000000.0,
                        0.01,
                        NOW + offset * 1000,
                        live=True,
                    )
    for suffix in ("", "-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)
    symbols = ["BTCUSDT"] + [f"SIM{i:03}USDT" for i in range(1, instruments)]
    return sorted(encode({**point, "symbol": symbol}) for point in points for symbol in symbols)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instruments", type=int, default=32)
    parser.add_argument("--seconds", type=int, default=130)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists():
        parser.error("Report already exists")
    with tempfile.TemporaryDirectory(prefix="cw-processing-replay-") as temporary:
        result = asyncio.run(
            replay(
                Path(temporary) / "processing.sqlite3",
                instruments=args.instruments,
                seconds=args.seconds,
                progress=lambda value: print(json.dumps(value), file=sys.stderr, flush=True),
            )
        )
    result["temporary_database_removed"] = True
    args.report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "streams",
                    "offered_closed",
                    "accepted_closed",
                    "phase_seconds",
                    "phase_cpu_seconds",
                    "max_source_lag_ms",
                    "max_completed_due_lag_ms",
                    "due_pending",
                    "oldest_due_pending_ms",
                    "telegram_attempts",
                    "signed_agent_receipts",
                    "producer_errors",
                    "worker_errors",
                    "receiver_errors",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
