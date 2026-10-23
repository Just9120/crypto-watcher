"""Closed candles, provenance and bounded windows in SQLite source:v1 namespaces.

The physical schema remains v1. Source rows are independently addressable;
backup includes them without a second DB or a rewrite of owner configuration.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Callable

from . import market_storage
from .config import TIMEFRAMES
from .market import MarketError, finite
from .storage import RUNTIME_UPSERT, Store, encode

WINDOW_SIZE = 256


@dataclass(frozen=True)
class Candle:
    start: int
    step: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float
    source: str
    received_ms: int
    server_ms: int

    @property
    def end(self) -> int:
        return self.start + self.step

    def row(self) -> list[Any]:
        return [self.start, self.open, self.high, self.low, self.close, self.volume, self.turnover]

    @classmethod
    def parse(cls, row: list[Any], tf: str, source: str, received_ms: int, server_ms: int) -> Candle:
        if tf not in TIMEFRAMES or len(row) != 7:
            raise MarketError("invalid_candle")
        start_value = finite(row[0])
        step = int(tf[:-1]) * 60_000
        if not start_value.is_integer() or start_value < 0 or start_value % step:
            raise MarketError("invalid_candle_boundary")
        values = [finite(x, positive=index < 4) for index, x in enumerate(row[1:])]
        open_, high, low, close, volume, turnover = values
        if high < max(open_, close) or low > min(open_, close) or low > high or min(volume, turnover) < 0:
            raise MarketError("invalid_candle_values")
        if int(start_value) + step > server_ms:
            raise MarketError("unclosed_candle")
        return cls(
            int(start_value), step, open_, high, low, close, volume, turnover, source, received_ms, server_ms
        )


class Sources:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.windows: dict[str, dict[int, Candle]] = {}
        self.live: dict[str, int] = {}
        self.blocked: dict[str, str] = {}

    @staticmethod
    def prefix(stream: str) -> str:
        return f"source:v1:candle:{stream}:"

    def load(self, stream: str) -> None:
        if stream in self.windows:
            return
        prefix = self.prefix(stream)
        rows = self.store.db.execute(
            "SELECT payload_json FROM runtime WHERE key>=? AND key<? ORDER BY key DESC LIMIT ?",
            (prefix, prefix + "~", WINDOW_SIZE),
        ).fetchall()
        if self.store.bounded:
            rows = [
                (encode(v),)
                for v in market_storage.read(
                    self.store.db, stream, limit=WINDOW_SIZE, cache=self.store.cached_bytes
                )
            ]
        window: dict[int, Candle] = {}
        try:
            for row in rows:
                candle = Candle(**json.loads(row[0]))
                validated = Candle.parse(
                    candle.row(),
                    stream.rsplit("/", 1)[1],
                    candle.source,
                    candle.received_ms,
                    candle.server_ms,
                )
                if candle != validated:
                    raise MarketError("invalid_persisted_candle")
                window[candle.start] = candle
        except (ValueError, TypeError, KeyError) as exc:
            raise MarketError("invalid_persisted_candle") from exc
        self.windows[stream] = window
        # Persisted sources may assist recovery; they never re-enter the live channel.

    def put(
        self,
        stream: str,
        candles: list[Candle],
        *,
        live: bool = False,
        recovery: bool = False,
        after_write: Callable[[Sources, bool], None] | None = None,
    ) -> bool:
        """Commit sources and an optional synchronous consumer atomically.

        The internal consumer must join this transaction without committing or
        awaiting. Only the prospective view sees new RAM before a successful
        commit; a failed consumer leaves the published window unchanged.
        """
        self.load(stream)
        window = self.windows[stream]
        previous = max(window, default=-1)
        writes = []
        incoming: dict[int, Candle] = {}
        for candle in candles:
            if candle.start in incoming and incoming[candle.start].row() != candle.row():
                raise MarketError("conflicting_recovery_candle")
            incoming[candle.start] = candle
            existing = window.get(candle.start)
            if existing and existing.row() != candle.row() and not recovery:
                self.blocked[stream] = "conflicting_closed_candle"
                self.live.pop(stream, None)
                raise MarketError("conflicting_closed_candle")
            if existing and existing.row() == candle.row():
                continue
            writes.append((self.prefix(stream) + f"{candle.start:016d}", encode(asdict(candle))))
        prospective = dict(window)
        for candle in candles:
            existing = prospective.get(candle.start)
            if existing is None or existing.row() != candle.row():
                prospective[candle.start] = candle
        while len(prospective) > WINDOW_SIZE:
            del prospective[min(prospective)]
        ordered = sorted(prospective)
        latest = max(prospective, default=-1)
        advanced = latest > previous
        next_live = None if recovery else self.live.get(stream)
        next_blocked = None if recovery else self.blocked.get(stream)
        if live and advanced and latest == max((c.start for c in candles), default=-1):
            next_live = latest
        if any(b - a != prospective[b].step for a, b in zip(ordered, ordered[1:])):
            next_blocked, next_live = "candle_gap", None
        with self.store.db:
            if self.store.bounded:
                market_storage.admit_many(
                    self.store.db,
                    stream,
                    [json.loads(payload) for _, payload in writes],
                    cache=self.store.cached_bytes,
                )
            else:
                self.store.db.executemany(RUNTIME_UPSERT, writes)
            if after_write is not None:
                candidate = Sources(self.store)
                candidate.windows[stream] = prospective
                if next_live is not None:
                    candidate.live[stream] = next_live
                if next_blocked is not None:
                    candidate.blocked[stream] = next_blocked
                after_write(candidate, advanced)
        window.clear()
        window.update(prospective)
        if next_live is None:
            self.live.pop(stream, None)
        else:
            self.live[stream] = next_live
        if next_blocked is None:
            self.blocked.pop(stream, None)
        else:
            self.blocked[stream] = next_blocked
        return advanced

    def rows(self, stream: str, count: int) -> list[list[Any]]:
        self.load(stream)
        if count > WINDOW_SIZE:
            raise MarketError("lookback_exceeds_source_window")
        return [self.windows[stream][key].row() for key in sorted(self.windows[stream], reverse=True)[:count]]

    def remove_window(self, stream: str) -> None:
        self.windows.pop(stream, None)
        self.live.pop(stream, None)
        self.blocked.pop(stream, None)
