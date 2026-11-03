"""Bounded opt-in observation of the existing replay; no production activation.

Timers are exclusive synchronous main-thread CPU/wall measurements. SQL parameters,
transport headers, secrets and full delivery bodies are never recorded. Instrumented
throughput is diagnostic evidence, not acceptance or a performance improvement.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import threading
import time
from collections import Counter
from contextlib import ExitStack, closing, contextmanager
from functools import wraps
from pathlib import Path
from unittest.mock import patch

MAX_ACTIONS = 32768
MAX_EVENTS = 65536
MAX_REPORT = 32 * 1024**2


def point_difference(reference, actual):
    missing = Counter(reference) - Counter(actual)
    extra = Counter(actual) - Counter(reference)
    return {
        "missing_count": sum(missing.values()),
        "extra_count": sum(extra.values()),
        "missing": [{"count": count, "point": json.loads(p)} for p, count in sorted(missing.items())],
        "extra": [{"count": count, "point": json.loads(p)} for p, count in sorted(extra.items())],
    }


class Trace:
    def __init__(self, enabled=False):
        self.enabled = enabled
        self.owner = threading.get_ident()
        self.phase = "SEED"
        self.actions = []
        self.events = []
        self.agent = []
        self.timing = {}
        self.stack = []
        self.current = None
        self.freezing = None
        self.truncated = False
        self.patches = ExitStack()

    def active(self):
        return self.enabled and self.phase in ("SEED", "PHASE") and threading.get_ident() == self.owner

    @contextmanager
    def scope(self, name):
        if not self.active():
            yield
            return
        key = self.phase + (":global:" if self.current is not None else ":other:") + name
        value = self.timing.setdefault(
            key, {"calls": 0, "failures": 0, "cpu_seconds": 0.0, "wall_seconds": 0.0}
        )
        started, cpu = time.monotonic(), time.thread_time()
        children = [0.0, 0.0]
        self.stack.append(children)
        try:
            yield
        except BaseException as exc:
            if not isinstance(exc, StopIteration):
                value["failures"] += 1
            raise
        finally:
            wall, used = time.monotonic() - started, time.thread_time() - cpu
            self.stack.pop()
            if self.stack:
                self.stack[-1][0] += wall
                self.stack[-1][1] += used
            value["calls"] += 1
            value["wall_seconds"] += max(0.0, wall - children[0])
            value["cpu_seconds"] += max(0.0, used - children[1])

    def append(self, target, value, limit):
        if len(target) >= limit:
            self.truncated = True
            raise RuntimeError("DIAGNOSTIC_TRACE_BOUND_EXCEEDED")
        target.append(value)

    def stage(self, value):
        if value["stage"] == "SEED_COMPLETE":
            self.phase = "PHASE"
        elif value["stage"] == "STOPPING_FIXTURE_WORKERS":
            self.phase = "STOPPED"

    def published(self, event, point):
        if self.active():
            self.append(
                self.events,
                {
                    "action": self.current,
                    "event_id": event["event_id"],
                    "episode_id": event["episode_id"],
                    "live": event["live"],
                    "reason": event["observation_reason"],
                    "detection_ms": event["detection_ms"],
                    "fresh_until_ms": event["fresh_until_ms"],
                    "point": point,
                },
                MAX_EVENTS,
            )

    def timed(self, name, function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with self.scope(name):
                return function(*args, **kwargs)

        return wrapped

    def __enter__(self):
        if not self.enabled:
            return self
        from cryptowatcher.agent_delivery import AgentDelivery
        from cryptowatcher.events import EventEngine
        from cryptowatcher.storage import encode
        from tests.sqlite_repository import History

        trace = self

        class Cursor(sqlite3.Cursor):
            category = "read"

            def execute(self, sql, parameters=()):
                verb = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
                self.category = (
                    "read"
                    if verb in ("SELECT", "WITH")
                    else "pragma"
                    if verb == "PRAGMA"
                    else "transaction"
                    if verb in ("BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE")
                    else "write"
                )
                with trace.scope("sql_" + self.category):
                    return super().execute(sql, parameters)

            def executemany(self, sql, parameters):
                with trace.scope("sql_write"):
                    return super().executemany(sql, parameters)

            def fetchone(self):
                with trace.scope("sql_" + self.category):
                    return super().fetchone()

            def fetchall(self):
                with trace.scope("sql_" + self.category):
                    return super().fetchall()

            def fetchmany(self, size=None):
                with trace.scope("sql_" + self.category):
                    return super().fetchmany() if size is None else super().fetchmany(size)

            def __next__(self):
                with trace.scope("sql_" + self.category):
                    return super().__next__()

        class Connection(sqlite3.Connection):
            def cursor(self, factory=Cursor):
                return super().cursor(factory)

            def execute(self, sql, parameters=()):
                return self.cursor().execute(sql, parameters)

            def executemany(self, sql, parameters):
                return self.cursor().executemany(sql, parameters)

            def executescript(self, script):
                with trace.scope("sql_script"):
                    return super().executescript(script)

            def commit(self):
                with trace.scope("sql_transaction"):
                    return super().commit()

            def rollback(self):
                with trace.scope("sql_transaction"):
                    return super().rollback()

            def __exit__(self, *args):
                with trace.scope("sql_transaction"):
                    return super().__exit__(*args)

        connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            if "factory" in kwargs or len(args) > 5:
                raise ValueError("Diagnostic cannot replace a custom SQLite factory")
            return connect(*args, factory=Connection, **kwargs)

        self.patches.enter_context(patch.object(sqlite3, "connect", traced_connect))
        for module in list(sys.modules.values()):
            if getattr(module, "__name__", "").startswith(("cryptowatcher.", "tests.sqlite_repository")):
                if getattr(module, "encode", None) is encode:
                    self.patches.enter_context(patch.object(module, "encode", self.timed("encode", encode)))
        for name, label in (
            ("_row", "revision_read_check"),
            ("_record", "revision_rehydrate_check"),
            ("version", "revision_mutation"),
        ):
            self.patches.enter_context(patch.object(History, name, self.timed(label, getattr(History, name))))
        self.patches.enter_context(
            patch.object(History, "checksum", staticmethod(self.timed("checksum", History.checksum)))
        )
        original_observe, original_tick = EventEngine.observe, EventEngine.tick
        original_init = EventEngine.__init__

        def initialize(engine, *args, **kwargs):
            original_init(engine, *args, **kwargs)
            engine.detector = trace.timed("detector", engine.detector)

        self.patches.enter_context(patch.object(EventEngine, "__init__", initialize))

        def observe(engine, stream, candles, liquidity, tick_size, now_ms, **kwargs):
            previous = trace.current
            if trace.active():
                trace.current = len(trace.actions)
                trace.append(
                    trace.actions,
                    (
                        "observe",
                        stream,
                        tuple(candles),
                        liquidity,
                        tick_size,
                        now_ms,
                        kwargs["live"],
                        trace.phase,
                    ),
                    MAX_ACTIONS,
                )
            try:
                with trace.scope("observe_other"):
                    return original_observe(engine, stream, candles, liquidity, tick_size, now_ms, **kwargs)
            finally:
                trace.current = previous

        def tick(engine, now_ms):
            if trace.active():
                trace.append(trace.actions, ("tick", now_ms, trace.phase), MAX_ACTIONS)
            with trace.scope("episode_tick_other"):
                return original_tick(engine, now_ms)

        self.patches.enter_context(patch.object(EventEngine, "observe", observe))
        self.patches.enter_context(patch.object(EventEngine, "tick", tick))
        original_get, original_put = History.get, History.put

        def get(history, key, *args, **kwargs):
            value = original_get(history, key, *args, **kwargs)
            if trace.freezing is not None and isinstance(value, dict):
                if key.startswith("agent:intent:"):
                    trace.freezing.update(
                        intent_key=key,
                        due_ms=value["due_ms"],
                        event_id=value["event_id"],
                        historical_intent=value["historical"],
                        replay=value["replay"],
                    )
                elif key.startswith("event:"):
                    trace.freezing.update(
                        event_live=value["live"],
                        event_observation_ms=value["market_observation_ms"],
                        fresh_until_ms=value["fresh_until_ms"],
                        event_settings_revision=value["settings_revision"],
                    )
            return value

        def put(history, key, value):
            if trace.freezing is not None and key.startswith("agent:outbox:"):
                trace.freezing.update(
                    kind=value["kind"], delivery_id=value["delivery_id"], outbox_state=value["state"]
                )
            return original_put(history, key, value)

        freeze = AgentDelivery._freeze

        def freezing(agent, now):
            if not trace.active():
                return freeze(agent, now)
            trace.freezing = {
                "at_ms": now,
                "phase": trace.phase,
                "settings_revision": agent.store.snapshot().revision,
            }
            try:
                with trace.scope("agent_freeze_other"):
                    return freeze(agent, now)
            finally:
                trace.append(trace.agent, trace.freezing, MAX_EVENTS)
                trace.freezing = None

        self.patches.enter_context(patch.object(History, "get", get))
        self.patches.enter_context(patch.object(History, "put", put))
        self.patches.enter_context(patch.object(AgentDelivery, "_freeze", freezing))
        return self

    def __exit__(self, *args):
        return self.patches.__exit__(*args)

    def report(self, path: Path, expected, actual, signal_point):
        """Post-phase independent ungated engine with the actual action clock/order.

        This supplement never replaces the frozen oracle or any qualification gate.
        Candle objects are frozen. Only finite trace metadata, not their bodies, is
        exported; the frozen wire and exact source-window hashes identify inputs.
        """
        from cryptowatcher.config import OwnerSettings
        from cryptowatcher.events import EventEngine
        from cryptowatcher.market_storage import enable
        from cryptowatcher.storage import Store, encode
        from tests.sqlite_repository import install

        self.phase = "ORACLE"
        aligned, actions = [], []
        started, cpu = time.monotonic(), time.process_time()
        with closing(Store(path, 233382575)) as store:
            store.initialize(OwnerSettings())
            enable(store.db)
            store.bounded = True
            install(store)
            engine = EventEngine(
                store, source_revision="UNSET", on_event=lambda e: aligned.append(signal_point(e))
            )
            for action in self.actions:
                if action[0] == "tick":
                    _, at, phase = action
                    store.clock_ms = lambda: at
                    engine.tick(at)
                    actions.append({"kind": "tick", "at_ms": at, "phase": phase})
                    continue
                _, stream, candles, liquidity, tick_size, at, live, phase = action
                store.clock_ms = lambda: at
                engine.observe(stream, list(candles), liquidity, tick_size, at, live=live)
                actions.append(
                    {
                        "kind": "observe",
                        "stream": stream,
                        "at_ms": at,
                        "phase": phase,
                        "live_requested": live,
                        "candle_end_ms": candles[-1].end if candles else None,
                        "window_count": len(candles),
                        "numeric_window_sha256": hashlib.sha256(
                            encode([c.row() for c in candles]).encode()
                        ).hexdigest(),
                    }
                )
        for suffix in ("", "-wal", "-shm"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)
        aligned_encoded = sorted(encode(p) for p in aligned)
        actual_encoded = sorted(encode(p) for p in actual)

        value = {
            "schema": "cryptowatcher.processing-trace.v1",
            "mode": "DIAGNOSTIC_NOT_ACCEPTANCE",
            "complete": not self.truncated,
            "limits": {"actions": MAX_ACTIONS, "events": MAX_EVENTS, "bytes": MAX_REPORT},
            "timing": self.timing,
            "actions": actions,
            "events": self.events,
            "agent_selections": self.agent,
            "frozen_oracle_difference": point_difference(expected, actual_encoded),
            "aligned_oracle_difference": point_difference(aligned_encoded, actual_encoded),
            "aligned_expected_count": len(aligned_encoded),
            "aligned_expected_sha256": hashlib.sha256(encode(aligned_encoded).encode()).hexdigest(),
            "aligned_oracle_cpu_seconds": time.process_time() - cpu,
            "aligned_oracle_wall_seconds": time.monotonic() - started,
            "limitations": [
                "All trace overhead included; no throughput acceptance or causal speedup claim",
                "SQL timing includes SQLite execution/fetch and callbacks, not isolated physical IO latency",
                "Aligned oracle changes only diagnostic clock/order; frozen gates remain unchanged",
                "Trace contains synthetic fixture metadata only; no transport bodies, headers or credentials",
            ],
        }
        size = len(json.dumps(value, indent=2).encode())
        if size > MAX_REPORT:
            raise RuntimeError("DIAGNOSTIC_REPORT_BOUND_EXCEEDED")
        value["serialized_bytes_before_size_field"] = size
        return value
