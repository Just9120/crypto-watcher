"""Closed candles, provenance and bounded windows in SQLite source:v1 namespaces.

The physical schema remains v1. Source rows are independently addressable;
backup includes them without a second DB or a rewrite of owner configuration.
"""

from __future__ import annotations

import hashlib
import json
from array import array
from dataclasses import asdict, dataclass
from typing import Any

from . import market_storage
from .config import TIMEFRAMES
from .market import MarketError, finite
from .storage import RUNTIME_UPSERT, Store, encode

WINDOW_SIZE = 256


@dataclass(frozen=True, slots=True)
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
        # Native uint64 coordinates, in sorted-window order. A second Python
        # dict/long object per candle would undo the slot-backed memory saving.
        self.revisions: dict[str, array[int]] = {}
        self.live: dict[str, int] = {}
        self.blocked: dict[str, str] = {}

    @staticmethod
    def prefix(stream: str) -> str:
        return f"source:v1:candle:{stream}:"

    def load(self, stream: str) -> None:
        if stream in self.windows:
            return
        revisions: array[int] = array("Q")
        if self.store.bounded:
            points = market_storage.read_points(
                self.store.db, stream, limit=WINDOW_SIZE, cache=self.store.cached_bytes
            )
            rows = [(encode(value),) for _, _, value in points]
            revisions = array("Q", (revision for _, revision, _ in sorted(points, key=lambda p: p[0])))
        else:
            prefix = self.prefix(stream)
            rows = self.store.db.execute(
                "SELECT payload_json FROM runtime WHERE key>=? AND key<? ORDER BY key DESC LIMIT ?",
                (prefix, prefix + "~", WINDOW_SIZE),
            ).fetchall()
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
        self.revisions[stream] = revisions
        # Persisted sources may assist recovery; they never re-enter the live channel.

    def put(self, stream: str, candles: list[Candle], *, live: bool = False, recovery: bool = False) -> bool:
        self.load(stream)
        window = self.windows[stream]
        previous = max(window, default=-1)
        old_coordinates = self.revisions.get(stream, array("Q"))
        revisions = (
            dict(zip(sorted(window), old_coordinates, strict=True))
            if len(old_coordinates) == len(window)
            else {}
        )
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
        # Save before exposing to the detector/Telegram consumer.
        admitted: list[tuple[int, int, int]] = []
        with self.store.db:
            if self.store.bounded:
                admitted = market_storage.admit_many(
                    self.store.db,
                    stream,
                    [json.loads(payload) for _, payload in writes],
                    cache=self.store.cached_bytes,
                )
            else:
                self.store.db.executemany(RUNTIME_UPSERT, writes)
        written = {start: revision for _, start, revision in admitted}
        for candle in candles:
            existing = window.get(candle.start)
            if existing is None or existing.row() != candle.row():
                window[candle.start] = candle
                if self.store.bounded:
                    revisions[candle.start] = written[candle.start]
        while len(window) > WINDOW_SIZE:
            oldest = min(window)
            del window[oldest]
            revisions.pop(oldest, None)
        ordered = sorted(window)
        self.revisions[stream] = (
            array("Q", (revisions[start] for start in ordered))
            if self.store.bounded and all(start in revisions for start in ordered)
            else array("Q")
        )
        if recovery:
            self.live.pop(stream, None)
            self.blocked.pop(stream, None)
        latest = max(window, default=-1)
        if live and latest > previous and latest == max((c.start for c in candles), default=-1):
            self.live[stream] = latest
        if any(b - a != window[b].step for a, b in zip(ordered, ordered[1:])):
            self.blocked[stream] = "candle_gap"
            self.live.pop(stream, None)
        return latest > previous

    def rows(self, stream: str, count: int) -> list[list[Any]]:
        self.load(stream)
        if count > WINDOW_SIZE:
            raise MarketError("lookback_exceeds_source_window")
        return [self.windows[stream][key].row() for key in sorted(self.windows[stream], reverse=True)[:count]]

    def snapshot_reference(self, stream: str, *, through: int | None = None) -> dict[str, Any] | None:
        """Freeze the actual memory window, without admitting data or extending leases.

        A durable consumer must retain an input hold while this reference is pending.
        A later correction or independent backfill must not replace the revisions
        that the producer actually held when the observation was admitted.
        """
        references = self._snapshot_references(stream, through=through, chunk_size=WINDOW_SIZE)
        return references[0] if references else None

    def snapshot_chunks(self, stream: str, *, through: int | None = None) -> list[dict[str, Any]]:
        """Freeze all coordinates in independently authenticated finite chunks.

        Consumers needing a calculation tail can restore complete chunks without
        reading or weakening the hash of the unused prefix. The full window is
        still captured, including its original revisions and provenance.
        """
        return self._snapshot_references(stream, through=through, chunk_size=16)

    def _snapshot_references(
        self, stream: str, *, through: int | None, chunk_size: int
    ) -> list[dict[str, Any]]:
        if not self.store.bounded:
            raise ValueError("Source coordinates require bounded storage")
        if through is not None and (type(through) is not int or through < 0):
            raise ValueError("Invalid observation cutoff")
        self.load(stream)
        window = self.windows[stream]
        ordered = sorted(window)
        starts = [start for start in ordered if through is None or start <= through]
        if not starts:
            return []
        sid = market_storage.stream_id(self.store.db, stream)
        if sid is None:
            raise MarketError("missing_source_stream")
        revisions = self.revisions.get(stream, array("Q"))
        if len(revisions) != len(ordered):
            raise MarketError("missing_source_revision")
        coordinates = [
            [start, revision]
            for start, revision in zip(ordered, revisions, strict=True)
            if through is None or start <= through
        ]
        references = []
        for offset in range(0, len(starts), chunk_size):
            selected = starts[offset : offset + chunk_size]
            raw = encode([asdict(window[start]) for start in selected]).encode()
            references.append(
                market_storage.compact_reference(
                    sid,
                    coordinates[offset : offset + chunk_size],
                    hashlib.sha256(raw).hexdigest(),
                )
            )
        return references

    def remove_window(self, stream: str) -> None:
        self.windows.pop(stream, None)
        self.revisions.pop(stream, None)
        self.live.pop(stream, None)
        self.blocked.pop(stream, None)
