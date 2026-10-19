"""Central mixed source access; installed tiles never hide legacy revisions."""

from __future__ import annotations

import heapq
import sqlite3
from typing import Any, Generator

from . import candle_block_codec as codec
from . import candle_blocks as blocks
from .market_storage import public
from .storage import encode


def installed(db: sqlite3.Connection) -> bool:
    row = db.execute("SELECT value FROM market_meta WHERE key='candle_blocks'").fetchone()
    if row is not None and row[0] != "1":
        raise ValueError("Unknown source tile layout")
    return row is not None


def writing(db: sqlite3.Connection) -> bool:
    row = db.execute("SELECT value FROM market_meta WHERE key='candle_block_runtime'").fetchone()
    if row is not None and (row[0] != "1" or not installed(db)):
        raise ValueError("Unknown source writer state")
    return row is not None


def admit_many(
    db: sqlite3.Connection, stream: str, values: list[dict[str, Any]], anchor: int = 0, *, cache: Any = None
) -> list[tuple[int, int, int]]:
    """Transfer eligible tiles and publish a whole validated input batch atomically."""
    from .market_storage import stream_id

    codec.number(anchor)
    tf = stream.rsplit("/", 1)[-1]
    if tf not in ("1m", "3m", "5m", "15m"):
        raise ValueError("Unsupported source stream timeframe")
    step = int(tf[:-1]) * 60000
    groups = blocks.validated_groups(step, values)
    result = [(0, 0, 0)] * len(values)
    with blocks.atomic(db):
        sid = stream_id(db, stream, create=True)
        assert sid is not None
        for at, items in groups.items():
            try:
                with blocks.atomic(db):
                    blocks.import_tile(db, sid, at)
                    coordinates = blocks._admit_groups(db, sid, {at: items}, anchor, cache=cache)
                    db.execute("DELETE FROM market_candle_fallbacks WHERE stream=? AND tile=?", (sid, at))
            except codec.Capacity as error:
                # The failed speculative transfer/admission was rolled back in
                # full. Every old revision remains addressable in its old table.
                coordinates = {index: _legacy_admit(db, sid, values[index], anchor) for index, _, _ in items}
                db.execute(
                    "INSERT INTO market_candle_fallbacks VALUES(?,?,?) ON CONFLICT(stream,tile) DO NOTHING",
                    (sid, at, str(error)),
                )
            for index, _, _ in items:
                result[index] = coordinates[index]
    return result


def _legacy_admit(
    db: sqlite3.Connection, sid: int, value: dict[str, Any], anchor: int
) -> tuple[int, int, int]:
    """A capacity fallback shares one revision space with the frozen tile."""
    from .market_storage import FIELDS, encoded

    start, step = value["start"], value["step"]
    expected = encoded([value[k] for k in FIELDS])
    old = blocks.tile(db, sid, start, step)
    slots = codec.unpack(old)[3] if old is not None else None
    slot = start // step % codec.SLOTS
    cold = slots[slot] if slots is not None else []
    tail = [value[k] for k in codec.TAIL]
    matching_cold = next((r for r in reversed(cold) if encode(r[3]) == encode(tail)), None)
    highest_cold = cold[-1][0] if cold else -1
    highest_legacy = -1
    matching_legacy = None
    cursor = db.execute(
        "SELECT revision,payload,needed_until,purge_after FROM market_candles WHERE stream=? AND start=? ORDER BY revision DESC",
        (sid, start),
    )
    try:
        for row in cursor:
            highest_legacy = max(highest_legacy, row[0])
            same = next((r for r in cold if r[0] == row[0]), None)
            if same is not None:
                frozen = {"start": start, "step": step, **dict(zip(codec.TAIL, same[3], strict=True))}
                if encode(frozen) != encode(public(bytes(row[1]))):
                    raise ValueError("Conflicting immutable source revision")
            if bytes(row[1]) == expected:
                matching_legacy = row
                break
    finally:
        cursor.close()
    candle_days, detail_days = blocks.policy(db)
    expiry = max(start + step + candle_days * 86400000, anchor + detail_days * 86400000)
    if matching_cold is not None and (matching_legacy is None or matching_cold[0] > matching_legacy[0]):
        revision = matching_cold[0]
        needed, purge = max(anchor, matching_cold[1]), max(expiry, matching_cold[2])
        if (needed, purge) != (matching_cold[1], matching_cold[2]):
            matching_cold[1:3] = [needed, purge]
            assert slots is not None and old is not None
            blocks.put(db, codec.pack(sid, start // (32 * step) * (32 * step), step, slots), old)
    elif matching_legacy is not None:
        revision = matching_legacy[0]
        if anchor > matching_legacy[2] or expiry > matching_legacy[3]:
            db.execute(
                "UPDATE market_candles SET needed_until=max(needed_until,?),purge_after=max(purge_after,?) WHERE stream=? AND start=? AND revision=?",
                (anchor, expiry, sid, start, revision),
            )
    else:
        revision = max(highest_legacy, highest_cold) + 1
        db.execute(
            "INSERT INTO market_candles VALUES(?,?,?,?,?,?)", (sid, start, revision, expected, anchor, expiry)
        )
    return sid, start, revision


class Lookup:
    """One synchronous window keeps at most one authenticated source tile."""

    def __init__(
        self, db: sqlite3.Connection, stream: int, cache: Any = None, *, require_tiles: bool = False
    ):
        self.db = db
        self.stream = stream
        self.cache = cache
        self.step = blocks.stream_step(db, stream) if installed(db) else None
        if require_tiles and self.step is None:
            raise ValueError("Source cells require authenticated tiles")
        self.require_tiles = require_tiles
        self.all_frozen = self.step is not None
        self.at: int | None = None
        self.reader: codec.Reader | None = None

    def frozen(self, start: int, revision: int) -> tuple[dict[str, Any], int, int] | None:
        codec.number(start)
        codec.number(revision)
        if self.step is None:
            return None
        at = start // (codec.SLOTS * self.step) * (codec.SLOTS * self.step)
        if self.at != at:
            raw = blocks.tile(self.db, self.stream, start, self.step)
            self.reader = codec.Reader(raw, self.cache, canonical=True) if raw is not None else None
            self.at = at
        return self.reader.point(self.stream, start, revision) if self.reader is not None else None

    def point(self, start: int, revision: int) -> dict[str, Any]:
        codec.number(start)
        codec.number(revision)
        legacy = self.db.execute(
            "SELECT payload,needed_until,purge_after FROM market_candles WHERE stream=? AND start=? AND revision=?",
            (self.stream, start, revision),
        ).fetchone()
        value = public(bytes(legacy[0])) if legacy is not None else None
        frozen = self.frozen(start, revision)
        if frozen is None:
            self.all_frozen = False
        if frozen is None and self.require_tiles:
            raise ValueError("Missing authenticated source cell")
        if frozen is not None:
            if value is not None and encode(value) != encode(frozen[0]):
                raise ValueError("Conflicting immutable source revision")
            if legacy is not None and (frozen[1] < legacy[1] or frozen[2] < legacy[2]):
                self.all_frozen = False
            value = frozen[0]
        if value is None:
            raise ValueError("Missing retained candle revision")
        if value["start"] != start or self.step is not None and value["step"] != self.step:
            raise ValueError("Source candle coordinate mismatch")
        return value


def read(
    db: sqlite3.Connection,
    sid: int,
    start: int,
    end: int,
    limit: int | None,
    cache: Any = None,
) -> list[dict[str, Any]]:
    """Merge two indexed ordered cursors, choosing the actual newest revision."""
    if limit is not None and (type(limit) is not int or limit < 0):
        raise ValueError("Invalid source read limit")
    if limit == 0 or end <= start:
        return []
    step = blocks.stream_step(db, sid)
    reverse = limit is not None
    order = "DESC" if reverse else "ASC"

    def legacy() -> Generator[tuple[int, int, dict[str, Any]], None, None]:
        cursor = db.execute(
            "SELECT c.start,c.revision,c.payload FROM market_candles c WHERE c.stream=? AND c.start>=? AND c.start<? "
            "AND c.revision=(SELECT MAX(v.revision) FROM market_candles v WHERE v.stream=c.stream AND v.start=c.start) "
            f"ORDER BY c.start {order}",
            (sid, start, end),
        )
        try:
            for when, revision, raw in cursor:
                value = public(bytes(raw))
                if (value["start"], value["step"]) != (when, step):
                    raise ValueError("Source candle coordinate mismatch")
                yield when, revision, value
        finally:
            cursor.close()

    def cold() -> Generator[tuple[int, int, dict[str, Any]], None, None]:
        floor = max(0, start) // (codec.SLOTS * step) * (codec.SLOTS * step)
        cursor = db.execute(
            "SELECT stream,tile,step,payload FROM market_candle_blocks WHERE stream=? AND tile>=? AND tile<? "
            f"ORDER BY tile {order}",
            (sid, floor, end),
        )
        try:
            for stream, tile, saved_step, payload in cursor:
                raw = bytes(payload)
                if codec.header(raw)[:3] != (stream, tile, saved_step) or saved_step != step:
                    raise ValueError("Source tile index binding mismatch")
                yield from codec.latest(raw, start, end, reverse=reverse, cache=cache)
        finally:
            cursor.close()

    readers = legacy(), cold()
    ordered = heapq.merge(*readers, key=lambda row: row[0], reverse=reverse)
    result: list[dict[str, Any]] = []
    current: tuple[int, int, dict[str, Any]] | None = None
    try:
        for row in ordered:
            if current is not None and row[0] != current[0]:
                result.append(current[2])
                if limit is not None and len(result) == limit:
                    return result
                current = None
            if current is None or row[1] > current[1]:
                current = row
            elif row[1] == current[1] and encode(row[2]) != encode(current[2]):
                raise ValueError("Conflicting immutable source revision")
        if current is not None:
            result.append(current[2])
        return result
    finally:
        for reader in readers:
            reader.close()
