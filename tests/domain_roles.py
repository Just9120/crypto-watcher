"""Bounded independent fixture network role, identical offered market bytes.

The child only generates network input. It owns no SQLite handle or credentials.
Unconfirmed frames are still generated and hashed, then rejected at ingress.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import threading
import time

from cryptowatcher.storage import encode

PENDING = {}
LOCK = threading.Lock()


def watermark(now):
    with LOCK:
        return min([now, *PENDING.values()])


class Arrived(str):
    def __new__(cls, raw, received_ms):
        value = super().__new__(cls, raw)
        value.received_ms = received_ms
        return value


class Heartbeat:
    def __init__(self, received_ms):
        self.received_ms = received_ms


class FrozenDigest:
    def __init__(self, value):
        self.value = value

    def hexdigest(self):
        return self.value


def generate(connection, groups, seconds, started, now):
    from cryptowatcher.outcomes import MINUTE
    from tests.processing_replay import candle, frozen_group_label

    try:
        sha = hashlib.sha256()
        offered, partials = {}, 0
        for offset in range(0, seconds, 5):
            time.sleep(max(0, started + offset - time.monotonic()))
            for name, group in groups.items():
                frames = []
                for stream in group:
                    category, symbol, tf = stream.split("/")
                    confirmed = offset % (int(tf[:-1]) * 60) == 0
                    end = now + (offset // 60) * MINUTE
                    if not confirmed:
                        end = now + (offset // (int(tf[:-1]) * 60) + 1) * int(tf[:-1]) * MINUTE
                    value = candle(end, tf)
                    message = {
                        "topic": f"kline.{tf[:-1]}.{symbol}",
                        "ts": now + offset * 1000,
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
                    raw = encode(message)
                    sha.update(encode([frozen_group_label(name), raw]).encode())
                    if confirmed:
                        offered[stream] = offered.get(stream, 0) + 1
                        frames.append((raw, now + int((time.monotonic() - started) * 1000)))
                    else:
                        partials += 1
                if frames:
                    connection.send(("frames", name, frames))
                else:
                    connection.send(("heartbeat", name, now + int((time.monotonic() - started) * 1000)))
        connection.send(("done", sha.hexdigest(), offered, partials, time.process_time()))
    except BaseException as exc:
        connection.send(("error", type(exc).__name__, str(exc)[:200]))
    finally:
        connection.close()


def wire(groups, seconds, started, now, stop, queues, arrivals, loop, offered, stats):
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    process = ctx.Process(
        target=generate, args=(child, groups, seconds, started, now), name="bounded-domain-ingress"
    )
    process.start()
    child.close()
    completed = None
    try:
        while not stop.is_set():
            if not parent.poll(0.1):
                if not process.is_alive():
                    raise RuntimeError("Ingress exited without completion")
                continue
            value = parent.recv()
            if value[0] == "frames":
                _, name, frames = value
                if len(frames) > 128 or sum(len(raw.encode()) for raw, _ in frames) > 1048576:
                    raise RuntimeError("Ingress frame batch exceeded bound")
                for raw, receipt in frames:
                    arrived = Arrived(raw, receipt)
                    with LOCK:
                        if len(PENDING) >= 3300:
                            raise RuntimeError("Bounded ingress work exceeded3300 closed frames")
                        PENDING[id(arrived)] = receipt
                    queues[name].put_nowait(arrived)
                loop.call_soon_threadsafe(arrivals[name].set)
            elif value[0] == "heartbeat":
                _, name, receipt = value
                queues[name].put_nowait(Heartbeat(receipt))
                loop.call_soon_threadsafe(arrivals[name].set)
            elif value[0] == "done":
                _, sha, counts, partials, cpu = value
                offered.update(counts)
                stats.update(
                    state="COMPLETE",
                    child_cpu_seconds=cpu,
                    filtered_partial_frames=partials,
                    offered_sha256=sha,
                )
                completed = FrozenDigest(sha)
                break
            else:
                raise RuntimeError("Independent ingress failed: " + str(value))
    finally:
        parent.close()
        process.join(timeout=0.5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=0.5)
        if process.is_alive():
            process.kill()
            process.join(timeout=0.5)
        stats["joined"] = not process.is_alive()
        stats["exitcode"] = process.exitcode
    if completed is None:
        raise RuntimeError("Ingress stopped before full offered input was accounted")
    return completed


async def receive(self, websocket, name, market, allowed):
    started, processed = time.monotonic(), 0
    while True:
        async with asyncio.timeout(65):
            raw = await websocket.recv()
        if isinstance(raw, Heartbeat):
            self.connections[name]["last_received_ms"] = raw.received_ms
            await asyncio.sleep(0)
            continue
        if not isinstance(raw, Arrived):
            raise RuntimeError("Independent ingress receipt missing")
        try:
            self.accept_message(market, json.loads(raw), allowed, received_ms=raw.received_ms)
        finally:
            with LOCK:
                PENDING.pop(id(raw), None)
        self.connections[name]["last_received_ms"] = raw.received_ms
        processed += 1
        if processed >= 16 or time.monotonic() - started >= 0.005:
            await asyncio.sleep(0)
            started, processed = time.monotonic(), 0
