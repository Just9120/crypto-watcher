"""Bounded, ephemeral provenance for a PR1 candidate; never a source lease.

Only a complete frozen outcome-v1 window may supply operands. Unknown methods
and unsupported/oversized shapes fall back. Actual retained-source checksum or
read errors propagate; optimization must not hide corrupt referenced data.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import metric_price_codec

MAX_CANDLES = 1440
MAX_BYTES = 2 * 1024 * 1024
MINUTE = 60000


class Source:
    def __init__(
        self,
        value: Any,
        read: Callable[[Any], bytes],
        db: sqlite3.Connection,
        cache: Any = None,
        *,
        reference: bool = False,
    ):
        self.value, self.read, self.db = value, read, db
        self.cache = cache
        self.reference = reference
        self.loaded = False
        self.rows: list[Any] | None = None

    def normalized(self, reference: dict[str, Any]) -> list[Any] | None:
        from .candle_access import Lookup
        from .market_storage import reference_points
        from .storage import encode

        if type(reference.get("stream")) is not int:
            raise ValueError("Invalid retained price source stream")
        points = reference_points(reference)
        if not isinstance(points, list) or not 0 < len(points) <= MAX_CANDLES:
            raise ValueError("Invalid retained price source count")
        previous = None
        for point in points:
            if (
                not isinstance(point, list)
                or len(point) != 2
                or any(type(n) is not int or not 0 <= n < 2**63 for n in point)
            ):
                raise ValueError("Invalid retained price source coordinate")
            if previous is not None and point[0] != previous + MINUTE:
                return None
            previous = point[0]
        cells = reference["mode"] == "source-cells"
        reader = Lookup(self.db, reference["stream"], self.cache, require_tiles=cells)
        checksum = hashlib.sha256(b"[")
        rows = []
        for index, point in enumerate(points):
            candle = reader.point(*point)
            if not cells:
                if index:
                    checksum.update(b",")
                checksum.update(encode(candle).encode())
            # No full source array/large provenance strings in a price context.
            rows.append({k: candle[k] for k in ("start", "step", "close", "high", "low")})
        checksum.update(b"]")
        if not cells and checksum.hexdigest() != reference["sha256"]:
            raise ValueError("Retained price source window checksum mismatch")
        return rows

    def bounded_blobs(self, identities: list[Any]) -> bool:
        total = 0
        for identity in identities:
            if not isinstance(identity, str) or len(identity) != 64:
                return False
            key = "historyblob:v1:" + identity
            row = self.db.execute("SELECT length(payload_json) FROM runtime WHERE key=?", (key,)).fetchone()
            if row is None:
                raise ValueError("Missing retained price source body")
            if row[0] > 65536:
                return False
            payload = self.db.execute("SELECT payload_json FROM runtime WHERE key=?", (key,)).fetchone()
            if payload is None:
                raise ValueError("Missing retained price source body")
            metadata = json.loads(payload[0])
            size = metadata.get("bytes") if isinstance(metadata, dict) else None
            if type(size) is not int or not 0 <= size <= MAX_BYTES:
                return False
            total += size
            if total > MAX_BYTES:
                return False
        return True

    def candles(self) -> list[Any] | None:
        if self.loaded:
            return self.rows
        value = self.value
        if isinstance(value, dict):
            if not self.reference:
                return None
            if value.get("mode") == "source-ref":
                identity = value.get("id")
                if type(identity) is not int or identity <= 0:
                    return None
                row = self.db.execute(
                    "SELECT length(payload) FROM market_source_windows WHERE id=?", (identity,)
                ).fetchone()
                if row is not None and row[0] > 65536:
                    return None
                from .source_windows import load

                value = load(self.db, identity)
            if value.get("mode") in ("source-runs", "source-cells"):
                runs = value.get("runs")
                if not isinstance(runs, list) or len(runs) > MAX_CANDLES:
                    return None
                count = 0
                for run in runs:
                    if not isinstance(run, list) or len(run) != 4 or type(run[2]) is not int or run[2] < 1:
                        return None
                    count += run[2]
                    if count > MAX_CANDLES:
                        return None
                value = self.normalized(value)
            elif value.get("mode") in ("window", "value", "value2"):
                ids = value.get("ids", [value.get("id")])
                if not isinstance(ids, list) or len(ids) > MAX_CANDLES // 16 + 1:
                    return None
                if not self.bounded_blobs(ids):
                    return None
                raw = self.read(self.value)
                if len(raw) > MAX_BYTES:
                    return None
                value = json.loads(raw)
            else:
                # The old packed-point shape remains readable through its
                # existing codec; this optional candidate does not expand it.
                return None
        self.loaded = True
        if isinstance(value, list) and 0 < len(value) <= MAX_CANDLES:
            self.rows = value
        return self.rows


@dataclass(frozen=True)
class Context:
    method: str | None = None
    future: Source | None = None


def source(
    db: sqlite3.Connection,
    value: dict[str, Any],
    name: str,
    read: Callable[[Any], bytes],
    *,
    cache: Any = None,
) -> Source | None:
    if name in value:
        return Source(value[name], read, db, cache)
    storage = value.get("_history_storage")
    if not isinstance(storage, dict) or storage.get("schema") != "cryptowatcher.shared-fields.v1":
        return None
    fields = storage.get("fields")
    return (
        Source(fields[name], read, db, cache, reference=True)
        if isinstance(fields, dict) and name in fields
        else None
    )


def context(
    db: sqlite3.Connection,
    value: dict[str, Any],
    parent: Context,
    read: Callable[[Any], bytes],
    *,
    cache: Any = None,
) -> Context:
    method = value.get("methodology_version", parent.method)
    if not isinstance(method, str):
        method = None
    future = source(db, value, "future_inputs", read, cache=cache)
    return Context(method, future if future is not None else parent.future)


def candidate(
    db: sqlite3.Connection,
    value: dict[str, Any],
    original: bytes,
    current: Context,
    read: Callable[[Any], bytes],
    *,
    cache: Any = None,
) -> bytes | None:
    if value.get("methodology_version", current.method) != "1" or value.get("state") != "COMPLETE":
        return None
    start, end = value.get("window_start_ms"), value.get("window_end_ms")
    if type(start) is not int or type(end) is not int or not 0 <= start < end < 2**63:
        return None
    if (end - start) % MINUTE or not 0 < (end - start) // MINUTE <= MAX_CANDLES:
        return None
    if "reference_time_ms" in value and (
        type(value["reference_time_ms"]) is not int or value["reference_time_ms"] != start
    ):
        return None
    reference = value.get("reference_price")
    try:
        if (
            not isinstance(reference, (int, float))
            or type(reference) not in (int, float)
            or reference <= 0
            or not math.isfinite(reference)
        ):
            return None
    except OverflowError:
        return None
    own = source(db, value, "source_inputs", read, cache=cache)
    provider = own if own is not None else current.future
    rows = provider.candles() if provider is not None else None
    if rows is None:
        return None
    chosen = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or type(row.get("start")) is not int
            or row.get("step") != MINUTE
            or type(row.get("step")) is not int
        ):
            return None
        if start <= row["start"] and row["start"] + MINUTE <= end:
            chosen.append(row)
    if len(chosen) != (end - start) // MINUTE:
        return None
    if own is not None and len(chosen) != len(rows):
        return None
    for index, row in enumerate(chosen):
        if row["start"] != start + index * MINUTE:
            return None
        try:
            if any(
                type(row.get(key)) not in (int, float) or row[key] <= 0 or not math.isfinite(row[key])
                for key in ("close", "high", "low")
            ):
                return None
        except OverflowError:
            return None
    prices = [
        reference,
        chosen[-1]["close"],
        max(row["high"] for row in chosen),
        min(row["low"] for row in chosen),
    ]
    return metric_price_codec.encode(original, prices)
