"""Explicit source-format adoption and exact atomic row relocation.

No expiry policy or SQL trigger is disabled. A private synchronous authority
permits only the already checked old rows of one bounded rewrite transaction.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import math
import sqlite3
import time
from typing import Any

from . import candle_block_codec as codec
from . import candle_blocks as blocks
from .storage import encode

# Frozen normalized SQL of the existing layout1/2 source guards and counters.
# A different migration must qualify its own trigger set before relocation.
GUARDS = {
    "market_candle_block_delete": "ba37fc9da0869311eabf7fc1ad1eb8d7174cc82f8a0a99ec7ff08fde2b49cdb6",
    "market_candle_block_delete_guard": "8e1d8f219acdb4249a56118d75e22b7bfb6520702cd81bd4a56c9a849b82c14b",
    "market_candle_block_insert": "e69ef14dd7d52355397e12134b2c08edda0550b6062adf2b06c471ad6fffb8f8",
    "market_candle_block_insert_guard": "1260c9ae7e9e3756f54a759e2712388c3e37cfc8f076e486fc94dda9cb333bbe",
    "market_candle_block_update": "50a981ae7031bdcc4433eaeb3d749ec8167c56bacd311308286c031c26366e09",
    "market_candle_block_update_guard": "e91845753f9d32a4a8164c1fc0bd3c876d58ce898dac987fd7b4814b8bc3d09a",
    "market_candle_stream_binding": "e78a1cac950a363f0e2984b1ffb701c3d522284f09a5d6a72a6144f7b8b8673c",
}
KEY = "candle_column_repack"
_AUTHORITY: contextvars.ContextVar[tuple[sqlite3.Connection, frozenset[bytes]] | None] = (
    contextvars.ContextVar("source_column_rewrite", default=None)
)


def enabled(db: sqlite3.Connection) -> bool:
    row = db.execute("SELECT value FROM market_meta WHERE key='candle_blocks'").fetchone()
    if row is not None and row[0] not in ("1", "2"):
        raise ValueError("Unknown source tile layout")
    return row is not None and row[0] == "2"


def guards(db: sqlite3.Connection) -> None:
    if db.execute("PRAGMA user_version").fetchone()[0] != 2:
        raise ValueError("Source relocation requires the guarded source layout")
    for name, expected in GUARDS.items():
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
        if row is None or hashlib.sha256(" ".join(row[0].split()).encode()).hexdigest() != expected:
            raise ValueError("Unknown source relocation guard; database preserved")


def readable(db: sqlite3.Connection, raw: bytes) -> None:
    if raw.startswith(codec.COLUMN_MAGIC) and not enabled(db):
        raise ValueError("Source columns require explicit adoption")


def _authorized_delete(db: sqlite3.Connection, raw: bytes) -> bool:
    state = _AUTHORITY.get()
    return state is not None and state[0] is db and db.in_transaction and raw in state[1]


def checkpoint(db: sqlite3.Connection) -> dict[str, Any]:
    row = db.execute("SELECT value FROM market_meta WHERE key=?", (KEY,)).fetchone()
    if row is None:
        raise ValueError("Missing source column boundary")
    value = json.loads(row[0])
    if not isinstance(value, dict) or set(value) != {
        "version",
        "upper",
        "cursor",
        "initial_upper",
        "initial_complete",
        "examined",
        "rewritten",
        "saved_bytes",
    }:
        raise ValueError("Invalid source column checkpoint")
    if value["version"] != 1 or type(value["version"]) is not int:
        raise ValueError("Unknown source column checkpoint")
    for key in ("upper", "cursor", "initial_upper", "examined", "rewritten", "saved_bytes"):
        codec.number(value[key])
    if (
        type(value["initial_complete"]) is not bool
        or value["cursor"] > value["upper"]
        or value["initial_upper"] > value["upper"]
        or not value["initial_complete"]
        and value["cursor"] > value["initial_upper"]
        or value["rewritten"] > value["examined"]
    ):
        raise ValueError("Invalid source column boundary")
    return value


def enable(db: sqlite3.Connection) -> None:
    """Offline guarded adopter only; opening Store never calls this method."""
    if db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ValueError("Source adoption requires foreign key guards")
    guards(db)
    blocks.summary(db)
    if enabled(db):
        checkpoint(db)
        return
    marker = db.execute("SELECT value FROM market_meta WHERE key='candle_blocks'").fetchone()
    if marker is None or marker[0] != "1":
        raise ValueError("Source columns require existing source tiles")
    with blocks.atomic(db):
        if db.execute("SELECT 1 FROM market_meta WHERE key=?", (KEY,)).fetchone():
            raise ValueError("Unexpected source column boundary")
        upper = db.execute("SELECT coalesce(max(id),0) FROM market_candle_blocks").fetchone()[0]
        state = dict(
            version=1,
            upper=upper,
            cursor=0,
            initial_upper=upper,
            initial_complete=upper == 0,
            examined=0,
            rewritten=0,
            saved_bytes=0,
        )
        db.execute("INSERT INTO market_meta VALUES(?,?)", (KEY, encode(state)))
        if (
            db.execute("UPDATE market_meta SET value='2' WHERE key='candle_blocks' AND value='1'").rowcount
            != 1
        ):
            raise ValueError("Source adoption revision changed")


def status(db: sqlite3.Connection) -> dict[str, Any]:
    if not enabled(db):
        return {"enabled": False, "pending_work": False}
    state = checkpoint(db)
    return {"enabled": True, "pending_work": not state["initial_complete"], **state}


def rewrite(db: sqlite3.Connection, rows: list[tuple[int, bytes, bytes]]) -> int:
    """Delete a checked batch and reinsert exact values with the same rowids.

    Deleting the whole batch first releases complete leaf pages. BLOB updates
    alone leave their internal free bytes allocated in the working database.
    """
    if db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ValueError("Source rewrite requires foreign key guards")
    if (
        not enabled(db)
        or not isinstance(rows, list)
        or not 0 < len(rows) <= 8
        or _AUTHORITY.get() is not None
    ):
        raise ValueError("Invalid source rewrite scope")
    guards(db)
    with blocks.atomic(db):
        blocks.summary(db)
        before = tuple(db.execute("SELECT * FROM market_candle_block_totals WHERE id=1").fetchone())
        counter = tuple(db.execute("SELECT * FROM market_counts WHERE category='source_blocks'").fetchone())
        identities: set[int] = set()
        inserts = []
        saved = adjusted = 0
        for identity, old, new in rows:
            codec.number(identity, positive=True)
            if identity in identities or not isinstance(old, bytes) or not isinstance(new, bytes):
                raise ValueError("Invalid source rewrite identity")
            identities.add(identity)
            if not new.startswith(codec.COLUMN_MAGIC) or len(new) >= len(old):
                raise ValueError("Source rewrite must be a smaller exact column frame")
            original, replacement = codec.unpack(old), codec.unpack(new)
            if encode(original) != encode(replacement):
                raise ValueError("Source rewrite changed retained data")
            sid, tile, step, slots = original
            entries = [item for slot in slots for item in slot]
            value = (sid, tile, step, len(entries), min(item[2] for item in entries), old)
            actual = db.execute(
                "SELECT stream,tile,step,row_count,first_due,payload FROM market_candle_blocks WHERE id=?",
                (identity,),
            ).fetchone()
            if actual is None or tuple(actual) != value:
                raise ValueError("Source rewrite revision changed")
            inserts.append((identity, *value[:5], new))
            saved += len(old) - len(new)
            adjusted += len(new)
        token = _AUTHORITY.set((db, frozenset(row[1] for row in rows)))
        try:
            for identity, old, _ in rows:
                if (
                    db.execute(
                        "DELETE FROM market_candle_blocks WHERE id=? AND payload=?", (identity, old)
                    ).rowcount
                    != 1
                ):
                    raise ValueError("Source rewrite revision changed")
            db.executemany(
                "INSERT INTO market_candle_blocks(id,stream,tile,step,row_count,first_due,payload) VALUES(?,?,?,?,?,?,?)",
                inserts,
            )
        finally:
            _AUTHORITY.reset(token)
        # Match logical UPDATE accounting instead of charging relocation as new
        # source admission plus expiry. All trigger guards/counters ran above.
        if (
            db.execute(
                "UPDATE market_counts SET inserted_bytes=inserted_bytes-?,deleted_bytes=deleted_bytes-? "
                "WHERE category='source_blocks' AND inserted_bytes>=? AND deleted_bytes>=?",
                (adjusted, adjusted, adjusted, adjusted),
            ).rowcount
            != 1
        ):
            raise ValueError("Invalid source rewrite counters")
        actual_counter = tuple(
            db.execute("SELECT * FROM market_counts WHERE category='source_blocks'").fetchone()
        )
        actual_totals = tuple(db.execute("SELECT * FROM market_candle_block_totals WHERE id=1").fetchone())
        if actual_counter != (
            counter[0],
            counter[1],
            counter[2] - saved,
            counter[3],
            counter[4] + saved,
        ) or actual_totals != (*before[:3], before[3] - saved, *before[4:]):
            raise ValueError("Source rewrite counter mismatch")
        for row in inserts:
            actual = db.execute(
                "SELECT id,stream,tile,step,row_count,first_due,payload FROM market_candle_blocks WHERE id=?",
                (row[0],),
            ).fetchone()
            if actual is None or tuple(actual) != row:
                raise ValueError("Source rewrite readback mismatch")
        return saved


def advance(db: sqlite3.Connection, now: int, *, deadline: float, batch: int = 8) -> dict[str, Any]:
    """One bounded part of the existing rotating maintenance worker."""
    codec.number(now)
    if (
        type(batch) is not int
        or not 0 < batch <= 8
        or type(deadline) not in (int, float)
        or not math.isfinite(deadline)
    ):
        raise ValueError("Invalid source rewrite batch")
    if not enabled(db):
        return {"enabled": False}
    state = checkpoint(db)
    if time.monotonic() >= deadline:
        return {"enabled": True, **state}
    with blocks.atomic(db):
        if state["cursor"] == state["upper"]:
            state["cursor"] = 0
            state["upper"] = db.execute("SELECT coalesce(max(id),0) FROM market_candle_blocks").fetchone()[0]
            state["upper"] = max(state["upper"], state["initial_upper"])
        selected = db.execute(
            "SELECT id,tile,step,payload FROM market_candle_blocks WHERE id>? AND id<=? ORDER BY id LIMIT ?",
            (state["cursor"], state["upper"], batch),
        ).fetchall()
        candidates = []
        examined = []
        for identity, tile, step, raw in selected:
            if time.monotonic() >= deadline:
                break
            # Keep the live 256-candle calculation tail in its original fast
            # representation; dependency/history reads retain all older values.
            if tile + 32 * step <= now - 256 * step and not bytes(raw).startswith(codec.COLUMN_MAGIC):
                new = codec.compact(bytes(raw))
                if new != raw:
                    candidates.append((identity, bytes(raw), new))
            examined.append(identity)
        if candidates and time.monotonic() >= deadline:
            return {"enabled": True, **state}
        if candidates:
            state["saved_bytes"] += rewrite(db, candidates)
            state["rewritten"] += len(candidates)
        state["examined"] += len(examined)
        if examined:
            state["cursor"] = examined[-1]
        if len(selected) < batch and len(examined) == len(selected):
            state["cursor"] = state["upper"]
        if state["cursor"] >= state["initial_upper"]:
            state["initial_complete"] = True
        db.execute("UPDATE market_meta SET value=? WHERE key=?", (encode(state), KEY))
    return {"enabled": True, **state}
