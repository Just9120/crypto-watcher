"""NS1 positive seals for retired numeric search entries; values remain retained.

Only closed ID blocks with no detail owners or unleased values may be sealed.
The seal binds actual value and lease bytes. A detail renewal reopens the full
search block in the same transaction; absence never means a valid seal.
"""

from __future__ import annotations

import hashlib
import sqlite3
import struct
import time
from typing import Any

from .numeric_block_codec import block_number

INDEXES = {
    "market_numeric_search_unknown": "CREATE INDEX market_numeric_search_unknown ON market_numeric_block_refs(id) WHERE detail_slots=-1",
    "market_numeric_search_due": "CREATE INDEX market_numeric_search_due ON market_numeric_block_refs(id) WHERE detail_slots=0 AND search_seal IS NULL AND unused=0",
}


def header(db: sqlite3.Connection) -> None:
    if not installed(db):
        raise ValueError("Missing numeric search layout")
    names = [r[1] for r in db.execute("PRAGMA table_info(market_numeric_block_refs)")]
    if names != ["id", "counts", "unused", "detail_slots", "search_seal"]:
        raise ValueError("Unknown numeric search lease columns")
    for name, expected in INDEXES.items():
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()
        if row is None or " ".join(row[0].split()) != expected:
            raise ValueError("Missing or unknown numeric search index")


def installed(db: sqlite3.Connection) -> bool:
    row = db.execute("SELECT value FROM market_meta WHERE key='numeric_blocks'").fetchone()
    if row is not None and row[0] not in ("1", "2", "3"):
        raise ValueError("Unknown numeric search layout; data preserved")
    return row is not None and row[0] == "3"


def _frame_signature(block_tag: bytes, payload: bytes, binding: bytes, counts: bytes) -> bytes:
    """Pure NS2 digest; callers validate the tag and authenticated entry first."""
    return hashlib.sha256(b"".join((b"NS2\x00", block_tag, payload, binding, counts))).digest()


def signature(block: int, payload: bytes, counts: bytes, *, binding: bytes | None = None) -> bytes:
    block_number(block)
    if payload.startswith(b"NF1\x00"):
        if not isinstance(binding, bytes) or len(binding) < 73:
            raise ValueError("Missing numeric frame value binding")
        return _frame_signature(struct.pack(">Q", block), payload, binding, counts)
    if binding is not None:
        raise ValueError("Unexpected numeric frame value binding")
    return hashlib.sha256(b"NS1\x00" + struct.pack(">Q", block) + payload + counts).digest()


def binding(db: sqlite3.Connection, block: int, payload: bytes) -> bytes | None:
    if not payload.startswith(b"NF1\x00"):
        return None
    row = db.execute("SELECT cw_numeric_frame_binding(?,?)", (block, payload)).fetchone()
    if row is None or row[0] is None:
        raise ValueError("Missing numeric frame value binding")
    return bytes(row[0])


def bound_signature(db: sqlite3.Connection, block: int, payload: bytes, counts: bytes) -> bytes:
    return signature(block, payload, counts, binding=binding(db, block, payload))


def fields(db: sqlite3.Connection, block: int) -> tuple[int, bytes | None]:
    if not installed(db):
        return -1, None
    row = db.execute(
        "SELECT detail_slots,search_seal FROM market_numeric_block_refs WHERE id=?", (block,)
    ).fetchone()
    if row is None:
        raise ValueError("Missing numeric search state")
    return row[0], row[1]


def checked(
    block: int,
    payload: bytes,
    counts: bytes,
    detail_slots: int,
    seal: bytes | None,
    details: list[int] | None = None,
    *,
    binding: bytes | None = None,
) -> bool:
    if type(detail_slots) is not int or not -1 <= detail_slots <= 64:
        raise ValueError("Invalid numeric search detail count")
    if details is not None and detail_slots >= 0 and detail_slots != sum(bool(v) for v in details):
        raise ValueError("Numeric search detail count mismatch")
    if seal is None:
        if payload.startswith(b"NF1\x00"):
            raise ValueError("Missing or corrupt positive numeric search seal")
        return False
    if (
        detail_slots != 0
        or not isinstance(seal, bytes)
        or len(seal) != 32
        or seal != signature(block, payload, counts, binding=binding)
    ):
        raise ValueError("Missing or corrupt positive numeric search seal")
    return True


def sealed(db: sqlite3.Connection, block: int) -> bool:
    if not installed(db):
        return False
    row = db.execute(
        "SELECT b.payload,r.counts,r.detail_slots,r.search_seal FROM market_numeric_blocks b "
        "JOIN market_numeric_block_refs r ON r.id=b.id WHERE b.id=?",
        (block,),
    ).fetchone()
    if row is None:
        raise ValueError("Missing numeric search state")
    raw = bytes(row[0])
    return checked(block, raw, bytes(row[1]), row[2], row[3], binding=binding(db, block, raw))


def index_bytes(n: int, state: tuple[int, bytes | None]) -> int | None:
    return None if state[0] < 0 else 40 if state[1] is not None else 8 + 16 * n


def install(db: sqlite3.Connection) -> None:
    if installed(db):
        header(db)
        return
    row = db.execute("SELECT value FROM market_meta WHERE key='numeric_blocks'").fetchone()
    if row is None or row[0] != "2" or db.in_transaction:
        raise ValueError("Numeric search upgrade requires the outer price-layout boundary")
    with db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "ALTER TABLE market_numeric_block_refs ADD COLUMN detail_slots INTEGER NOT NULL "
            "DEFAULT -1 CHECK(typeof(detail_slots)='integer' AND detail_slots BETWEEN -1 AND 64)"
        )
        db.execute(
            "ALTER TABLE market_numeric_block_refs ADD COLUMN search_seal BLOB "
            "CHECK(search_seal IS NULL OR (typeof(search_seal)='blob' AND length(search_seal)=32))"
        )
        for statement in INDEXES.values():
            db.execute(statement)
        db.execute("UPDATE market_meta SET value='3' WHERE key='numeric_blocks'")


def reaccount(db: sqlite3.Connection, before: tuple[int, ...], after: tuple[int, ...]) -> None:
    from . import numeric_blocks as blocks

    current = blocks.state(db)
    updated = (current[0], *(current[i + 1] + after[i] - before[i] for i in range(5)))
    checksum = blocks.digest(updated)
    blocks.account(db, "numeric_details", before[1], before[3], after[1], after[3])
    blocks.account(db, "quality_numeric", before[2], before[4], after[2], after[4])
    db.execute(
        "UPDATE market_numeric_totals SET live=?,details=?,quality=?,detail_bytes=?,quality_bytes=?,checksum=? WHERE id=1",
        (*updated[1:], checksum),
    )


def qualify(db: sqlite3.Connection, block: int) -> None:
    from . import numeric_blocks as blocks
    from .candle_blocks import atomic

    with atomic(db):
        value = blocks.read(db, block)
        if value is None:
            raise ValueError("Missing numeric search qualification block")
        frames, details, quality, payload = value
        old = fields(db, block)
        if old[1] is not None:
            return
        counts = db.execute("SELECT counts FROM market_numeric_block_refs WHERE id=?", (block,)).fetchone()[0]
        n = sum(bool(f) for f in frames)
        before = blocks.allocation(
            block,
            frames,
            details,
            quality,
            len(payload),
            index_bytes=index_bytes(n, old),
            counts_bytes=len(counts),
        )
        detail_slots = sum(bool(v) for v in details)
        eligible = (
            (block + 1) * 64 <= blocks.state(db)[0]
            and not detail_slots
            and all(not f or quality[i] > 0 for i, f in enumerate(frames))
        )
        seal = None
        if eligible:
            coordinates = [(blocks.signature(f), block * 64 + i + 1) for i, f in enumerate(frames) if f]
            for _, identity in coordinates:
                blocks.lookup(db, identity, frames[(identity - 1) % 64])
            deleted = db.executemany(
                "DELETE FROM market_numeric_block_lookup WHERE fingerprint=? AND id=?", coordinates
            ).rowcount
            if deleted != len(coordinates):
                raise ValueError("Numeric search retirement lost a coordinate")
            seal = bound_signature(db, block, payload, bytes(counts))
        after = blocks.allocation(
            block,
            frames,
            details,
            quality,
            len(payload),
            index_bytes=index_bytes(n, (detail_slots, seal)),
            counts_bytes=len(counts),
        )
        db.execute(
            "UPDATE market_numeric_block_refs SET detail_slots=?,search_seal=? WHERE id=?",
            (detail_slots, seal, block),
        )
        reaccount(db, before, after)


def validate(state: dict[str, Any]) -> None:
    from .numeric_block_codec import MAX_BLOCK

    if ("numeric_search_cursor" in state) != ("numeric_search_upper" in state):
        raise ValueError("Incomplete numeric search boundary")
    for name in ("numeric_search_cursor", "numeric_search_upper"):
        if name in state and (type(state[name]) is not int or not -1 <= state[name] <= MAX_BLOCK):
            raise ValueError("Invalid numeric search boundary")
    if (
        "numeric_search_cursor" in state
        and "numeric_search_upper" in state
        and state["numeric_search_cursor"] > state["numeric_search_upper"]
    ):
        raise ValueError("Numeric search cursor exceeds finite boundary")
    if "numeric_search_complete" in state and type(state["numeric_search_complete"]) is not bool:
        raise ValueError("Invalid numeric search completion")
    if state.get("numeric_search_complete") and (
        "numeric_search_upper" not in state
        or state.get("numeric_search_cursor", -2) < state["numeric_search_upper"]
    ):
        raise ValueError("Missing completed numeric search boundary")


def pending(db: sqlite3.Connection) -> bool:
    if not installed(db):
        return True
    if db.execute("SELECT 1 FROM market_numeric_block_refs WHERE detail_slots=-1 LIMIT 1").fetchone():
        return True
    ceiling = blocks_ceiling(db)
    return (
        db.execute(
            "SELECT 1 FROM market_numeric_block_refs WHERE detail_slots=0 "
            "AND search_seal IS NULL AND unused=0 AND id<=? LIMIT 1",
            (ceiling,),
        ).fetchone()
        is not None
    )


def blocks_ceiling(db: sqlite3.Connection) -> int:
    from .numeric_blocks import state

    return state(db)[0] // 64 - 1


def work(db: sqlite3.Connection, state: dict[str, Any], batch: int, deadline: float) -> bool:
    validate(state)
    if type(batch) is not int or not 0 < batch <= 256:
        raise ValueError("Invalid numeric search batch")
    if not installed(db) or not db.in_transaction:
        raise ValueError("Numeric search work requires an installed writer boundary")
    if db.execute("SELECT 1 FROM market_numeric LIMIT 1").fetchone():
        return False
    if "numeric_search_upper" not in state:
        row = db.execute("SELECT id FROM market_numeric_blocks ORDER BY id DESC LIMIT 1").fetchone()
        state.update(numeric_search_upper=row[0] if row else -1, numeric_search_cursor=-1)
    for _ in range(batch):
        if time.monotonic() >= deadline:
            break
        if not state.get("numeric_search_complete"):
            row = db.execute(
                "SELECT id FROM market_numeric_blocks WHERE id>? AND id<=? ORDER BY id LIMIT 1",
                (state["numeric_search_cursor"], state["numeric_search_upper"]),
            ).fetchone()
            if row is None:
                state.update(
                    numeric_search_complete=True, numeric_search_cursor=state["numeric_search_upper"]
                )
                continue
            qualify(db, row[0])
            state["numeric_search_cursor"] = row[0]
        else:
            row = db.execute(
                "SELECT id FROM market_numeric_block_refs WHERE detail_slots=-1 ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                row = db.execute(
                    "SELECT id FROM market_numeric_block_refs WHERE detail_slots=0 "
                    "AND search_seal IS NULL AND unused=0 AND id<=? ORDER BY id LIMIT 1",
                    (blocks_ceiling(db),),
                ).fetchone()
            if row is None:
                break
            qualify(db, row[0])
    return state.get("numeric_search_complete", False)
