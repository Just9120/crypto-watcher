"""Finite logical-key source transfer; callers commit its checkpoint atomically."""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from . import candle_block_codec as codec
from . import candle_blocks as blocks


def advance(db: sqlite3.Connection, state: dict[str, Any], batch: int, deadline: float) -> bool:
    if not db.in_transaction:
        raise ValueError("Source transfer requires an outer checkpoint transaction")
    if type(batch) is not int or not 0 < batch <= 256:
        raise ValueError("Invalid source transfer batch")
    if state.get("source_transfer_complete"):
        return True
    if time.monotonic() >= deadline:
        return False
    if "source_upper" not in state:
        upper = db.execute(
            "SELECT stream,start FROM market_candles ORDER BY stream DESC,start DESC LIMIT 1"
        ).fetchone()
        state["source_upper"] = list(upper) if upper else None
        state["source_cursor"] = [0, 0]
    high = state["source_upper"]
    if high is None:
        state["source_transfer_complete"] = True
        return True
    low = state["source_cursor"]
    for key in (high, low):
        if not isinstance(key, list) or len(key) != 2:
            raise ValueError("Invalid finite source boundary")
        for value in key:
            codec.number(value)
    for _ in range(batch):
        if time.monotonic() >= deadline:
            return False
        row = db.execute(
            "SELECT stream,start FROM market_candles WHERE (stream,start)>(?,?) AND (stream,start)<=(?,?) ORDER BY stream,start LIMIT 1",
            (*low, *high),
        ).fetchone()
        if row is None:
            state["source_transfer_complete"] = True
            return True
        sid, start = row
        step = blocks.stream_step(db, sid)
        at = start // (codec.SLOTS * step) * (codec.SLOTS * step)
        try:
            moved = blocks.import_tile(db, sid, start)
            state["source_rows_moved"] = state.get("source_rows_moved", 0) + moved
            db.execute("DELETE FROM market_candle_fallbacks WHERE stream=? AND tile=?", (sid, at))
        except codec.Capacity as error:
            db.execute(
                "INSERT INTO market_candle_fallbacks VALUES(?,?,?) ON CONFLICT(stream,tile) DO NOTHING",
                (sid, at, str(error)),
            )
            state["source_fallbacks_observed"] = state.get("source_fallbacks_observed", 0) + 1
        # A tile is one immutable logical interval. VACUUM/DDL never turn this
        # cursor into a relocated rowid or extend the originally captured end.
        low = [sid, min(2**63 - 1, at + codec.SLOTS * step - 1)]
        state["source_cursor"] = low
        state["source_tiles_examined"] = state.get("source_tiles_examined", 0) + 1
    return False
