"""Finite initial NF1 admission and a transactionally maintained live inbox.

The five-minute packing delay changes representation only: all values already
remain available from NB blocks. A full64-block group is eligible immediately.
No timer, larger cache, raw input retention or new analytics delay is introduced.
"""

from __future__ import annotations

import copy
import sqlite3
import struct
import time
from typing import Any

from . import numeric_block_codec as blocks
from . import numeric_frame_codec, numeric_frames, numeric_refs, numeric_search, owner_slots
from .packet_store import atomic

CATEGORY = "numeric_frame_pending"
DELAY_MS = 300_000
MAX_CLOCK = 2**63 - 1 - DELAY_MS


def eligible(b: str = "b", r: str = "r") -> str:
    return (
        f"{r}.detail_slots=0 AND {r}.unused=0 AND {r}.search_seal IS NOT NULL "
        f"AND {b}.id<=(SELECT issued/64-1 FROM market_numeric_totals WHERE id=1) "
        f"AND substr({b}.payload,1,4) IN(x'4e423100',x'4e423200')"
    )


def schema() -> dict[str, str]:
    result = {
        "market_numeric_frame_pending": f"""CREATE TABLE market_numeric_frame_pending(
            id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(typeof(id)='integer' AND id>0),
            block INTEGER NOT NULL UNIQUE REFERENCES market_numeric_blocks(id) ON DELETE CASCADE,
            queued_at INTEGER NOT NULL CHECK(typeof(queued_at)='integer' AND queued_at BETWEEN 0 AND {MAX_CLOCK}),
            eligible_at INTEGER NOT NULL CHECK(typeof(eligible_at)='integer' AND eligible_at=queued_at+{DELAY_MS}))""",
        "market_numeric_frame_age": "CREATE INDEX market_numeric_frame_age ON market_numeric_frame_pending(eligible_at,queued_at,id)",
    }
    insert = (
        "INSERT INTO market_numeric_frame_pending(block,queued_at,eligible_at) SELECT b.id,stamp.at_ms,stamp.at_ms+300000 "
        "FROM (SELECT cw_clock_ms() AS at_ms LIMIT 1) stamp CROSS JOIN market_numeric_blocks b "
        "JOIN market_numeric_block_refs r ON r.id=b.id WHERE b.id=NEW.id AND "
        + eligible()
        + " ON CONFLICT(block) DO NOTHING;"
    )
    remove = (
        "DELETE FROM market_numeric_frame_pending WHERE block=NEW.id AND NOT EXISTS("
        "SELECT 1 FROM market_numeric_blocks b JOIN market_numeric_block_refs r ON r.id=b.id "
        "WHERE b.id=NEW.id AND " + eligible() + ");"
    )
    for operation in ("INSERT", "UPDATE"):
        result["market_nframe_queue_refs_" + operation] = (
            "CREATE TRIGGER market_nframe_queue_refs_"
            + operation
            + " AFTER "
            + operation
            + " ON market_numeric_block_refs BEGIN "
            + insert
            + remove
            + " END"
        )
    result["market_nframe_queue_body"] = (
        "CREATE TRIGGER market_nframe_queue_body AFTER UPDATE OF payload ON market_numeric_blocks "
        "WHEN NEW.payload<>OLD.payload BEGIN " + insert + remove + " END"
    )
    result["market_nframe_queue_refs_delete"] = (
        "CREATE TRIGGER market_nframe_queue_refs_delete AFTER DELETE ON market_numeric_block_refs "
        "BEGIN DELETE FROM market_numeric_frame_pending WHERE block=OLD.id; END"
    )
    result["market_nframe_queue_clock"] = (
        "CREATE TRIGGER market_nframe_queue_clock BEFORE UPDATE ON market_numeric_frame_pending "
        "WHEN NEW.id<>OLD.id OR NEW.block<>OLD.block OR NEW.queued_at<>OLD.queued_at OR NEW.eligible_at<>OLD.eligible_at "
        "BEGIN SELECT RAISE(ABORT,'Immutable numeric frame admission clock'); END"
    )
    for operation in ("INSERT", "DELETE"):
        condition = "records>=0" if operation == "INSERT" else "records>0"
        change = "+1" if operation == "INSERT" else "-1"
        result["market_nframe_queue_count_guard_" + operation] = (
            "CREATE TRIGGER market_nframe_queue_count_guard_"
            + operation
            + " BEFORE "
            + operation
            + " ON market_numeric_frame_pending BEGIN SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM market_counts "
            f"WHERE category='{CATEGORY}' AND typeof(records)='integer' AND {condition} "
            "AND logical_bytes=0 AND inserted_bytes=0 AND deleted_bytes=0) "
            "THEN RAISE(ABORT,'Missing numeric frame inbox counter') END; END"
        )
        result["market_nframe_queue_count_" + operation] = (
            "CREATE TRIGGER market_nframe_queue_count_"
            + operation
            + " AFTER "
            + operation
            + f" ON market_numeric_frame_pending BEGIN UPDATE market_counts SET records=records{change} "
            f"WHERE category='{CATEGORY}'; END"
        )
    return result


def mode(db: sqlite3.Connection) -> str | None:
    row = db.execute("SELECT value FROM market_meta WHERE key='numeric_frame_queue'").fetchone()
    if row is not None and (row[0] not in ("1", "NB_COMPAT") or row[0] != numeric_frames.mode(db)):
        raise ValueError("Unknown numeric frame inbox layout; data preserved")
    return row[0] if row is not None else None


def header(db: sqlite3.Connection) -> str:
    current = mode(db)
    if current is None:
        raise ValueError("Missing numeric frame inbox checkpoint")
    if current == "1":
        for name, expected in schema().items():
            row = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
            if row is None or " ".join(row[0].split()) != " ".join(expected.split()):
                raise ValueError("Missing or unknown numeric frame inbox guard")
    elif db.execute("SELECT 1 FROM sqlite_master WHERE name='market_numeric_frame_pending'").fetchone():
        raise ValueError("Unexpected numeric frame inbox in compatible layout")
    return current


def install(db: sqlite3.Connection) -> None:
    if mode(db) is not None:
        header(db)
        return
    if db.in_transaction:
        raise ValueError("Numeric frame inbox installation requires an outer boundary")
    current = numeric_frames.header(db)
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='market_numeric_frame_pending'").fetchone():
        raise ValueError("Unmarked numeric frame inbox; data preserved")
    with db:
        db.execute("BEGIN IMMEDIATE")
        if current == "1":
            for statement in schema().values():
                db.execute(statement)
            db.execute("INSERT INTO market_counts VALUES(?,0,0,0,0)", (CATEGORY,))
        db.execute("INSERT INTO market_meta VALUES('numeric_frame_queue',?)", (current,))


def status(db: sqlite3.Connection, now_ms: int) -> dict[str, Any]:
    if type(now_ms) is not int or not 0 <= now_ms <= MAX_CLOCK:
        raise ValueError("Invalid numeric frame inbox clock")
    current = mode(db)
    if current != "1":
        return dict(
            installed=current is not None,
            mode=current,
            pending_work=False,
            count=0,
            oldest_ms=None,
            oldest_age_ms=None,
            next_eligible_ms=None,
            clock_unknown=False,
        )
    count = db.execute(
        "SELECT records,logical_bytes,inserted_bytes,deleted_bytes FROM market_counts WHERE category=?",
        (CATEGORY,),
    ).fetchone()
    rows = db.execute(
        "SELECT block,queued_at,eligible_at FROM market_numeric_frame_pending ORDER BY eligible_at,queued_at,id LIMIT 64"
    ).fetchall()
    if (
        count is None
        or any(type(v) is not int for v in count)
        or count[0] < 0
        or tuple(count[1:]) != (0, 0, 0)
        or (count[0] == 0) != (not rows)
        or count[0] < len(rows)
    ):
        raise ValueError("Missing or conflicting numeric frame inbox count")
    gc = db.execute(
        "SELECT gc_since FROM market_numeric_frames WHERE gc_since IS NOT NULL ORDER BY gc_since,id LIMIT 1"
    ).fetchone()
    latest = db.execute(
        "SELECT queued_at FROM market_numeric_frame_pending ORDER BY eligible_at DESC,queued_at DESC,id DESC LIMIT 1"
    ).fetchone()
    ready = min(rows[0][2], rows[63][1]) if len(rows) == 64 else rows[0][2] if rows else None
    due = ready is not None and ready <= now_ms
    stamps = ([ready] if due and ready is not None else []) + ([gc[0]] if gc is not None else [])
    oldest = min(stamps) if stamps else None
    return dict(
        installed=True,
        mode=current,
        count=count[0],
        pending_work=bool(due or gc is not None),
        oldest_ms=oldest,
        oldest_age_ms=max(0, now_ms - oldest) if oldest is not None else None,
        buffered_oldest_ms=rows[0][1] if rows else None,
        next_eligible_ms=ready if not due else None,
        clock_unknown=bool(
            latest and latest[0] > now_ms + 30000 or gc is not None and gc[0] > now_ms + 30000
        ),
    )


def validate(state: dict[str, Any]) -> None:
    if (
        not isinstance(state, dict)
        or type(state.get("version", 1)) is not int
        or state.get("version", 1) != 1
    ):
        raise ValueError("Unknown numeric frame checkpoint")
    if ("cursor" in state) != ("upper" in state):
        raise ValueError("Incomplete numeric frame boundary")
    if (
        any(
            name in state
            for name in (
                "version",
                "mode",
                "upper",
                "cursor",
                "initial_complete",
                "rows_examined",
                "blocks_packed",
                "frames_created",
                "groups_skipped",
                "gc_removed",
                "queue_upper",
            )
        )
        and not {"version", "mode", "upper", "cursor"} <= state.keys()
    ):
        raise ValueError("Missing numeric frame initial boundary")
    for name in ("cursor", "upper"):
        if name in state and (type(state[name]) is not int or not -1 <= state[name] <= blocks.MAX_BLOCK):
            raise ValueError("Invalid numeric frame boundary")
    if "upper" in state and state["cursor"] > state["upper"]:
        raise ValueError("Numeric frame cursor exceeds boundary")
    if "mode" in state and state["mode"] not in ("1", "NB_COMPAT"):
        raise ValueError("Unknown numeric frame checkpoint mode")
    if "initial_complete" in state and (
        type(state["initial_complete"]) is not bool or "upper" not in state or "mode" not in state
    ):
        raise ValueError("Missing numeric frame initial boundary")
    if state.get("initial_complete") and state["cursor"] != state["upper"]:
        raise ValueError("Incomplete numeric frame initial scan")
    if "queue_upper" in state and (
        type(state["queue_upper"]) is not int
        or not 0 <= state["queue_upper"] < 2**63
        or state.get("cursor") != state.get("upper")
    ):
        raise ValueError("Invalid numeric frame admission boundary")
    if state.get("initial_complete") and "queue_upper" not in state:
        raise ValueError("Missing completed numeric frame admission boundary")
    for name in ("rows_examined", "blocks_packed", "frames_created", "groups_skipped", "gc_removed"):
        if name in state and (type(state[name]) is not int or not 0 <= state[name] < 2**63):
            raise ValueError("Invalid numeric frame progress")


def advance(
    db: sqlite3.Connection,
    state: dict[str, Any],
    batch: int,
    deadline: float,
    *,
    now_ms: int,
    cache: Any = None,
) -> bool:
    validate(state)
    current = mode(db)
    if not db.in_transaction or type(batch) is not int or not 0 < batch <= 256 or current is None:
        raise ValueError("Numeric frame work requires a bounded installed outer writer")
    if type(now_ms) is not int or not 0 <= now_ms <= MAX_CLOCK:
        raise ValueError("Invalid numeric frame work clock")
    if state.get("mode", current) != current:
        raise ValueError("Changed numeric frame work mode")
    if state.get("initial_complete") and not through(db, state["queue_upper"]):
        raise ValueError("Unfinished initial numeric frame admission boundary")
    if time.monotonic() >= deadline:
        return state.get("initial_complete") is True
    if current == "1" and status(db, now_ms)["clock_unknown"]:
        raise ValueError("Unknown numeric frame work clock")
    working = copy.deepcopy(state)
    with atomic(db):
        if "upper" not in working:
            row = db.execute("SELECT id FROM market_numeric_blocks ORDER BY id DESC LIMIT 1").fetchone()
            working.update(version=1, mode=current, cursor=-1, upper=row[0] if row else -1)
        if current == "NB_COMPAT":
            working.update(cursor=working["upper"], queue_upper=0, initial_complete=True)
        else:
            removed = numeric_frames.collect(db, deadline, cache=cache)
            working["gc_removed"] = working.get("gc_removed", 0) + removed
            if time.monotonic() < deadline:
                _advance(db, working, batch, deadline, now_ms, cache)
    state.clear()
    state.update(working)
    return state.get("initial_complete") is True


def _advance(
    db: sqlite3.Connection, state: dict[str, Any], batch: int, deadline: float, now_ms: int, cache: Any
) -> None:
    initial = not state.get("initial_complete")

    def initial_pending() -> list[Any]:
        return (
            db.execute(
                "SELECT id,block FROM market_numeric_frame_pending WHERE id<=? ORDER BY id LIMIT 64",
                (state["queue_upper"],),
            ).fetchall()
            if "queue_upper" in state
            else db.execute(
                "SELECT id,block FROM market_numeric_frame_pending ORDER BY id LIMIT 64"
            ).fetchall()
        )

    pending = initial_pending() if initial else []
    if initial and state["cursor"] < state["upper"] and len(pending) < 64:
        rows = db.execute(
            "SELECT b.id,r.id FROM market_numeric_blocks b LEFT JOIN market_numeric_block_refs r ON r.id=b.id WHERE b.id>? AND b.id<=? ORDER BY b.id LIMIT ?",
            (state["cursor"], state["upper"], batch),
        ).fetchall()
        for block, lease in rows:
            if lease is None:
                raise ValueError("Missing numeric frame initial lease binding")
        if rows:
            last = rows[-1][0]
            db.execute(
                "INSERT INTO market_numeric_frame_pending(block,queued_at,eligible_at) SELECT b.id,?,? FROM market_numeric_blocks b JOIN market_numeric_block_refs r ON r.id=b.id WHERE b.id>? AND b.id<=? AND "
                + eligible()
                + " ON CONFLICT(block) DO NOTHING",
                (now_ms, now_ms + DELAY_MS, state["cursor"], last),
            )
            state["cursor"] = last
            state["rows_examined"] = state.get("rows_examined", 0) + len(rows)
        else:
            state["cursor"] = state["upper"]
        pending = initial_pending()
    if initial and state["cursor"] == state["upper"] and "queue_upper" not in state:
        row = db.execute("SELECT id FROM market_numeric_frame_pending ORDER BY id DESC LIMIT 1").fetchone()
        state["queue_upper"] = row[0] if row else 0
        pending = initial_pending()
    if time.monotonic() >= deadline:
        return
    if initial:
        ready = len(pending) >= 64 or state["cursor"] == state["upper"]
        ids = [row[1] for row in pending[: min(batch, 64)]] if ready else []
    else:
        pending = db.execute(
            "SELECT block,eligible_at FROM market_numeric_frame_pending ORDER BY eligible_at,queued_at,id LIMIT 64"
        ).fetchall()
        ready = len(pending) >= 64 or bool(pending and pending[0][1] <= now_ms)
        ids = [row[0] for row in pending[: min(batch, 64)]] if ready else []
    if ids:
        sources = []
        for block in ids:
            row = db.execute(
                "SELECT b.payload FROM market_numeric_blocks b JOIN market_numeric_block_refs r ON r.id=b.id WHERE b.id=? AND "
                + eligible(),
                (block,),
            ).fetchone()
            if row is None:
                raise ValueError("Stale or missing numeric frame inbox binding")
            sources.append((block, bytes(row[0])))
        identity = numeric_frames.publish(db, sources, cache)
        if identity is None:
            state["groups_skipped"] = state.get("groups_skipped", 0) + 1
        else:
            state["frames_created"] = state.get("frames_created", 0) + 1
            state["blocks_packed"] = state.get("blocks_packed", 0) + len(sources)
        db.executemany("DELETE FROM market_numeric_frame_pending WHERE block=?", [(block,) for block in ids])
    if initial and state["cursor"] == state["upper"]:
        state["initial_complete"] = through(db, state["queue_upper"])


def through(db: sqlite3.Connection, upper: int) -> bool:
    if type(upper) is not int or not 0 <= upper < 2**63:
        raise ValueError("Invalid numeric frame admission boundary")
    current = mode(db)
    if current is None:
        raise ValueError("Missing numeric frame admission checkpoint")
    if current == "NB_COMPAT":
        return upper == 0
    return (
        db.execute("SELECT 1 FROM market_numeric_frame_pending WHERE id<=? LIMIT 1", (upper,)).fetchone()
        is None
    )


def measure(db: sqlite3.Connection, deadline: float, cache: Any = None) -> dict[str, Any]:
    """Explicit snapshot counts and actual NF owners/seals, not a market replay.

    The caller owns the read transaction and SQL progress deadline. Bodies are
    decoded once per frame; at most64 parent rows are retained at a time.
    """
    current = mode(db)
    if current != "1":
        return {"state": "N/A_COMPAT" if current else "N/A_NOT_INSTALLED", "mode": current}
    header(db)
    numeric_frames.header(db)
    cursor = frames = payload_bytes = owned = 0
    last = db.execute("SELECT id FROM market_numeric_frames ORDER BY id DESC LIMIT 1").fetchone()
    upper = last[0] if last else 0
    result: dict[str, Any] = {
        "state": "PARTIAL_BUDGET",
        "mode": current,
        "upper": upper,
        "method": "FINITE_ACTUAL_CONTAINER_POINTER_OWNER_SEAL_AND_COUNTER_RECONCILIATION",
        "limitations": [
            "Structural owner/seal counts are separate from full metric/method/replay equality and physical bytes."
        ],
    }
    try:
        while cursor < upper and time.monotonic() < deadline:
            row = db.execute(
                "SELECT id,payload,slots,checksum FROM market_numeric_frames WHERE id>? AND id<=? ORDER BY id LIMIT 1",
                (cursor, upper),
            ).fetchone()
            if row is None:
                cursor = upper
                break
            identity, payload, mask, checksum = row[0], bytes(row[1]), bytes(row[2]), bytes(row[3])
            if owner_slots.signature(3, identity, mask) != checksum:
                raise ValueError("Conflicting actual numeric frame owner checksum")
            entries = numeric_frame_codec.raw_entries(payload, cache)
            if owner_slots.bits(mask) >> len(entries):
                raise ValueError("Actual numeric frame mask exceeds container")
            ids = [struct.unpack(">Q", entry[:8])[0] for entry in entries]
            values = [identity for identity in ids if identity != numeric_frame_codec.DEAD]
            bindings = (
                {
                    r[0]: r
                    for r in db.execute(
                        "SELECT b.id,b.payload,r.counts,r.detail_slots,r.search_seal FROM market_numeric_blocks b LEFT JOIN market_numeric_block_refs r ON r.id=b.id WHERE b.id IN("
                        + ",".join("?" for _ in values)
                        + ")",
                        values,
                    )
                }
                if values
                else {}
            )
            for slot, (block, entry) in enumerate(zip(ids, entries, strict=True)):
                found = bindings.get(block)
                pointer = numeric_frames.pointer(identity, slot)
                retained = found is not None and bytes(found[1]) == pointer
                if retained != owner_slots.has(mask, slot):
                    raise ValueError("Conflicting actual numeric frame owner binding")
                if retained and found is not None:
                    if found[2] is None:
                        raise ValueError("Missing actual numeric frame lease")
                    counts = bytes(found[2])
                    body = numeric_refs.header(db, counts)
                    if struct.unpack(">Q", body[:8])[0] != block:
                        raise ValueError("Conflicting actual numeric frame lease identity")
                    if not numeric_search.checked(block, pointer, counts, found[3], found[4], binding=entry):
                        raise ValueError("Missing actual numeric frame value seal")
                    owned += 1
            frames += 1
            payload_bytes += len(payload)
            cursor = identity
        if cursor == upper and time.monotonic() < deadline:
            # The guarded table admits exactly NF1 at length13; NB is >=109.
            # length() avoids reading every large hot NB payload in this scan.
            parents = db.execute(
                "SELECT COUNT(*) FROM market_numeric_blocks WHERE length(payload)=13"
            ).fetchone()[0]
            if parents != owned:
                raise ValueError("Unaccounted actual numeric frame parent")
            count = db.execute(
                "SELECT records,logical_bytes,inserted_bytes,deleted_bytes FROM market_counts WHERE category=?",
                (numeric_frames.CATEGORY,),
            ).fetchone()
            if (
                count is None
                or tuple(count[:2]) != (frames, payload_bytes)
                or count[2] - count[3] != payload_bytes
            ):
                raise ValueError("Conflicting actual numeric frame byte counter")
            pending = db.execute("SELECT COUNT(*) FROM market_numeric_frame_pending").fetchone()[0]
            declared = status(db, db.execute("SELECT cw_clock_ms()").fetchone()[0])
            if declared["count"] != pending:
                raise ValueError("Conflicting actual numeric frame pending counter")
            result.update(
                state="MEASURED",
                frames=frames,
                payload_bytes=payload_bytes,
                owned_blocks=owned,
                pending_rows=pending,
            )
    except sqlite3.OperationalError as error:
        if getattr(error, "sqlite_errorcode", None) != sqlite3.SQLITE_INTERRUPT:
            raise
    result.update(frames_visited=frames, owned_blocks_visited=owned, cursor=cursor)
    return result
