"""Explicit NR2 writer activation and a finite exact lease recompression pass."""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from . import numeric_block_codec as codec

COUNTS_CHECK = "typeof(counts)='blob' AND ((substr(counts,1,4)=x'4e523100' AND length(counts) BETWEEN 172 AND 1196) OR (substr(counts,1,4)=x'4e523200' AND length(counts) BETWEEN 58 AND 1196))"


def _layout(db: sqlite3.Connection) -> str:
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='market_numeric_block_refs'"
    ).fetchone()
    sql = " ".join(row[0].split()) if row is not None else ""
    if COUNTS_CHECK in sql:
        return "1"
    if "CHECK(typeof(counts)='blob' AND length(counts) BETWEEN 172 AND 1196)" in sql:
        # Unreleased local NB prototypes retain their frozen table and bytes.
        # Main541c41e has no NB table; ordinary production conversion creates
        # the NR2-capable table directly. Do not rebuild an old populated table.
        return "NR1_COMPAT"
    raise ValueError("Unknown numeric lease table; database preserved")


def installed(db: sqlite3.Connection) -> bool:
    row = db.execute("SELECT value FROM market_meta WHERE key='numeric_refs_compression'").fetchone()
    if row is not None and row[0] not in ("1", "NR1_COMPAT"):
        raise ValueError("Unknown numeric lease compression; database preserved")
    if row is not None:
        layout = db.execute("SELECT value FROM market_meta WHERE key='numeric_blocks'").fetchone()
        if layout is None or layout[0] != "3":
            raise ValueError("Missing numeric lease parent layout")
        if _layout(db) != row[0]:
            raise ValueError("Numeric lease table does not match writer mode")
    return row is not None


def writing(db: sqlite3.Connection) -> bool:
    return (
        installed(db)
        and db.execute("SELECT value FROM market_meta WHERE key='numeric_refs_compression'").fetchone()[0]
        == "1"
    )


def install(db: sqlite3.Connection) -> None:
    if installed(db):
        return
    layout = db.execute("SELECT value FROM market_meta WHERE key='numeric_blocks'").fetchone()
    if db.in_transaction or layout is None or layout[0] != "3":
        raise ValueError("Numeric lease installation requires the outer search-layout boundary")
    with db:
        db.execute("INSERT INTO market_meta VALUES('numeric_refs_compression',?)", (_layout(db),))


def header(db: sqlite3.Connection, raw: bytes) -> bytes:
    if raw[:4] == b"NR2\x00" and not writing(db):
        raise ValueError("NR2 requires explicit lease compression activation")
    return codec.lease_header(raw)


def validate(state: dict[str, Any]) -> None:
    if not isinstance(state, dict):
        raise ValueError("Invalid numeric lease checkpoint")
    if ("cursor" in state) != ("upper" in state):
        raise ValueError("Incomplete numeric lease boundary")
    for name in ("cursor", "upper"):
        if name in state and (type(state[name]) is not int or not -1 <= state[name] <= codec.MAX_BLOCK):
            raise ValueError("Invalid numeric lease boundary")
    if "upper" in state and state["cursor"] > state["upper"]:
        raise ValueError("Numeric lease cursor exceeds ceiling")
    for name in ("blocks_examined", "saved_bytes"):
        if name in state and (type(state[name]) is not int or not 0 <= state[name] < 2**63):
            raise ValueError("Invalid numeric lease progress")
    if "complete" in state and type(state["complete"]) is not bool:
        raise ValueError("Invalid numeric lease completion")
    if state.get("complete") and ("upper" not in state or state["cursor"] != state["upper"]):
        raise ValueError("Missing completed numeric lease boundary")
    if "format" in state and state["format"] not in ("NR2", "NR1_COMPAT"):
        raise ValueError("Unknown numeric lease checkpoint format")
    if state.get("complete") and "format" not in state:
        raise ValueError("Missing numeric lease checkpoint format")


def recode(db: sqlite3.Connection, block: int) -> int:
    from . import numeric_blocks
    from .candle_blocks import atomic

    if not installed(db):
        raise ValueError("Missing numeric lease writer")
    with atomic(db):
        before = numeric_blocks.read(db, block)
        row = db.execute("SELECT counts FROM market_numeric_block_refs WHERE id=?", (block,)).fetchone()
        if before is None or row is None:
            raise ValueError("Missing numeric lease recoding block")
        desired = codec.counts(block, before[1], before[2], compressed=writing(db))
        previous = bytes(row[0])
        if len(desired) >= len(previous):
            return 0
        numeric_blocks.put(db, block, *before[:3])
        after = numeric_blocks.read(db, block)
        actual = db.execute("SELECT counts FROM market_numeric_block_refs WHERE id=?", (block,)).fetchone()
        if after != before or actual is None or bytes(actual[0]) != desired:
            raise ValueError("Numeric lease recoding changed values or scalar leases")
        return len(previous) - len(desired)


def advance(db: sqlite3.Connection, state: dict[str, Any], batch: int, deadline: float) -> None:
    validate(state)
    if not db.in_transaction or not installed(db) or type(batch) is not int or not 0 < batch <= 256:
        raise ValueError("Numeric lease work requires a bounded outer writer transaction")
    mode = "NR2" if writing(db) else "NR1_COMPAT"
    if "format" in state and state["format"] != mode:
        raise ValueError("Numeric lease checkpoint differs from table format")
    state["format"] = mode
    if "upper" not in state:
        row = db.execute("SELECT id FROM market_numeric_blocks ORDER BY id DESC LIMIT 1").fetchone()
        state.update(cursor=-1, upper=row[0] if row else -1)
    for _ in range(batch):
        if state.get("complete") or time.monotonic() >= deadline:
            break
        row = db.execute(
            "SELECT id FROM market_numeric_blocks WHERE id>? AND id<=? ORDER BY id LIMIT 1",
            (state["cursor"], state["upper"]),
        ).fetchone()
        if row is None:
            state.update(cursor=state["upper"], complete=True)
            break
        saved = recode(db, row[0])
        state["cursor"] = row[0]
        state["blocks_examined"] = state.get("blocks_examined", 0) + 1
        state["saved_bytes"] = state.get("saved_bytes", 0) + saved
