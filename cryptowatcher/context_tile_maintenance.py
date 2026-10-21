"""Finite initial context transfer and a durable indexed live inbox.

Run only inside the existing rotating repack worker. Sparse tails stay hot;
unrepresentable values stay hot and explicitly prevent capacity qualification.
"""

from __future__ import annotations

import copy
import sqlite3
import time
from typing import Any

from . import context_tile_codec as codec
from . import context_tiles as tiles
from . import indexed_frame
from .market_storage import DAY
from .storage import StateError, encode

SENTINEL: list[Any] = [0, "", -1]


def validate(state: dict[str, Any]) -> None:
    if (
        not isinstance(state, dict)
        or type(state.get("version", 1)) is not int
        or state.get("version", 1) != 1
    ):
        raise StateError("Unknown context tile checkpoint")
    if ("upper" in state) != ("cursor" in state):
        raise StateError("Incomplete context tile boundary")
    for field in ("upper", "cursor"):
        point = state.get(field, SENTINEL)
        if (
            not isinstance(point, list)
            or len(point) != 3
            or type(point[0]) is not int
            or not 0 <= point[0] < 2**63
            or not isinstance(point[1], str)
            or type(point[2]) is not int
            or not -(2**63) <= point[2] < 2**63
            or point != SENTINEL
            and (not point[0] or not point[1])
        ):
            raise StateError("Invalid context tile boundary")
    if "upper" in state and tuple(state["cursor"]) > tuple(state["upper"]):
        raise StateError("Context tile cursor exceeds boundary")
    if "initial_complete" in state and (type(state["initial_complete"]) is not bool or "upper" not in state):
        raise StateError("Missing context tile initial boundary")
    if state.get("initial_complete") and state["cursor"] != state["upper"]:
        raise StateError("Incomplete context tile initial scan")
    for field in ("rows_examined", "points_moved", "tiles_created", "fallback_points"):
        if field in state and (type(state[field]) is not int or not 0 <= state[field] < 2**63):
            raise StateError("Invalid context tile progress")


def status(db: sqlite3.Connection, now: int) -> dict[str, Any]:
    if not tiles.installed(db):
        return {"installed": False, "pending_work": False, "oldest_ms": None, "oldest_age_ms": None}
    pending = db.execute(
        "SELECT eligible_at,queued_at FROM market_context_tile_pending WHERE eligible_at IS NOT NULL "
        "ORDER BY eligible_at,queued_at,stream,kind,day LIMIT 1"
    ).fetchone()
    expiry = db.execute("SELECT due FROM market_context_tiles ORDER BY due,id LIMIT 1").fetchone()
    garbage = db.execute("SELECT 1 FROM market_context_bodies WHERE refs=0 LIMIT 1").fetchone() is not None
    blocked = (
        db.execute("SELECT 1 FROM market_context_tile_pending WHERE blocked=1 LIMIT 1").fetchone() is not None
    )
    newest = db.execute(
        "SELECT queued_at FROM market_context_tile_pending ORDER BY queued_at DESC LIMIT 1"
    ).fetchone()
    stamps = ([max(pending)] if pending is not None and pending[0] <= now else []) + (
        [expiry[0]] if expiry is not None and expiry[0] <= now else []
    )
    oldest = min(stamps) if stamps else None
    return {
        "installed": True,
        "pending_work": bool(stamps) or garbage,
        "body_gc_pending": garbage,
        "unsupported_pending": blocked,
        "oldest_ms": oldest,
        "oldest_age_ms": max(0, now - oldest) if oldest is not None else None,
        "next_eligible_ms": pending[0] if pending is not None else None,
        "clock_unknown": bool(newest is not None and newest[0] > now + 30000),
    }


def _schedule(db: sqlite3.Connection, key: tuple[int, str, int], now: int, *, blocked: bool = False) -> None:
    rows = db.execute(
        "SELECT at FROM market_context WHERE stream=? AND kind=? AND at>=? AND at<? ORDER BY at LIMIT 64",
        (key[0], key[1], key[2] * DAY, (key[2] + 1) * DAY),
    ).fetchall()
    if not rows or len(rows) < 8 and not blocked:
        db.execute("DELETE FROM market_context_tile_pending WHERE stream=? AND kind=? AND day=?", key)
        return
    eligible = now if len(rows) == 64 else max(now, (key[2] + 1) * DAY) if len(rows) >= 8 else None
    db.execute(
        "INSERT INTO market_context_tile_pending VALUES(?,?,?,?,?,?) ON CONFLICT(stream,kind,day) DO UPDATE "
        "SET eligible_at=CASE WHEN excluded.eligible_at IS NULL THEN NULL "
        "WHEN eligible_at IS NULL OR excluded.eligible_at>? THEN excluded.eligible_at "
        "ELSE min(eligible_at,excluded.eligible_at) END,blocked=excluded.blocked",
        (*key, now, None if blocked else eligible, int(blocked), now),
    )


def _process(
    db: sqlite3.Connection, state: dict[str, Any], keys: list[Any], now: int, *, initial: bool
) -> None:
    if not keys:
        return
    day = keys[0][2] // DAY
    group = (keys[0][0], keys[0][1], day)
    allowed: list[tuple[int, str, int]] = []
    charge = 0
    fallback = False
    for key in keys:
        if tuple(key[:2]) != group[:2] or key[2] // DAY != day:
            break
        state["rows_examined"] = state.get("rows_examined", 0) + 1
        # A late hot point in an existing interval remains indexed and readable.
        interval = db.execute(
            "SELECT last_at FROM market_context_tiles WHERE stream=? AND kind=? AND first_at<=? ORDER BY first_at DESC LIMIT 1",
            (key[0], key[1], key[2]),
        ).fetchone()
        if interval is not None and interval[0] >= key[2]:
            if allowed:
                break
            continue
        if (
            allowed
            and db.execute(
                "SELECT 1 FROM market_context_tiles WHERE stream=? AND kind=? AND first_at>? AND first_at<=? LIMIT 1",
                (key[0], key[1], allowed[-1][2], key[2]),
            ).fetchone()
        ):
            break
        row = db.execute(
            "SELECT stream,kind,at,body,timing,needed_anchor,purge_after,integrity FROM market_context "
            "WHERE stream=? AND kind=? AND at=?",
            tuple(key),
        ).fetchone()
        if row is None:
            raise StateError("Missing context tile transfer source")
        point = tiles.hot_point(db, row)
        try:
            # Same codec admission as publication; no coercion of old values.
            codec.pack(1, [point])
        except (ValueError, indexed_frame.FrameSizeError):
            fallback = True
            state["fallback_points"] = state.get("fallback_points", 0) + 1
            continue
        cost = len(encode([point.stream, point.kind, point.at, point.stable, point.timing]).encode()) + 1
        if charge + cost > codec.MAX_EXPANDED - 2:
            break
        charge += cost
        allowed.append(tuple(key))
    moved = len(allowed) >= 8 and (len(allowed) == 64 or (day + 1) * DAY <= now)
    if moved:
        tiles.publish(db, allowed)
        state["points_moved"] = state.get("points_moved", 0) + len(allowed)
        state["tiles_created"] = state.get("tiles_created", 0) + 1
    _schedule(db, group, now, blocked=fallback)
    if not initial and not moved:
        # A late point inside a sealed interval remains a hot fallback; do not
        # repeatedly select the same ineligible gap. A later insert requeues it.
        eligible = max(now, (day + 1) * DAY) if len(allowed) >= 8 else None
        db.execute(
            "UPDATE market_context_tile_pending SET eligible_at=? WHERE stream=? AND kind=? AND day=?",
            (None if fallback else eligible, *group),
        )


def advance(
    db: sqlite3.Connection, state: dict[str, Any], batch: int, deadline: float, *, now_ms: int
) -> bool:
    validate(state)
    if (
        not db.in_transaction
        or not tiles.installed(db)
        or type(batch) is not int
        or not 0 < batch <= 256
        or type(now_ms) is not int
        or now_ms < 0
    ):
        raise StateError("Context tile transfer requires a bounded outer writer")
    if time.monotonic() >= deadline:
        return state.get("initial_complete") is True
    working = copy.deepcopy(state)
    with tiles._atomic(db):
        if "upper" not in working:
            upper = db.execute(
                "SELECT stream,kind,at FROM market_context ORDER BY stream DESC,kind DESC,at DESC LIMIT 1"
            ).fetchone()
            working.update(version=1, upper=list(upper) if upper else list(SENTINEL), cursor=list(SENTINEL))
        tiles.expire(db, now_ms, batch=1)
        garbage = db.execute(
            "SELECT id FROM market_context_bodies WHERE refs=0 LIMIT ?", (min(batch, 64),)
        ).fetchall()
        db.executemany("DELETE FROM market_context_bodies WHERE id=? AND refs=0", garbage)
        for _ in range(min(batch, 64)):
            if time.monotonic() >= deadline or not _step(db, working, batch, now_ms):
                break
        if time.monotonic() < deadline:
            garbage = db.execute(
                "SELECT id FROM market_context_bodies WHERE refs=0 LIMIT ?", (min(batch, 64),)
            ).fetchall()
            db.executemany("DELETE FROM market_context_bodies WHERE id=? AND refs=0", garbage)
    state.clear()
    state.update(working)
    return state.get("initial_complete") is True


def _step(db: sqlite3.Connection, working: dict[str, Any], batch: int, now_ms: int) -> bool:
    initial = not working.get("initial_complete")
    if initial:
        keys = db.execute(
            "SELECT stream,kind,at FROM market_context WHERE (stream,kind,at)>(?,?,?) "
            "AND (stream,kind,at)<=(?,?,?) ORDER BY stream,kind,at LIMIT ?",
            (*working["cursor"], *working["upper"], min(64, batch)),
        ).fetchall()
        # Stop before crossing a native cohort/day; later calls continue at
        # the last examined source key, never at a newly discovered ceiling.
        if keys:
            first = keys[0]
            keys = [key for key in keys if key[:2] == first[:2] and key[2] // DAY == first[2] // DAY]
        _process(db, working, keys, now_ms, initial=True)
        if keys:
            working["cursor"] = list(keys[-1])
        else:
            working["cursor"] = list(working["upper"])
        if working["cursor"] == working["upper"]:
            working["initial_complete"] = True
    else:
        pending = db.execute(
            "SELECT stream,kind,day FROM market_context_tile_pending WHERE eligible_at<=? "
            "ORDER BY eligible_at,queued_at,stream,kind,day LIMIT 1",
            (now_ms,),
        ).fetchone()
        if pending is None:
            return False
        if pending is not None:
            keys = db.execute(
                "SELECT stream,kind,at FROM market_context WHERE stream=? AND kind=? AND at>=? AND at<? ORDER BY at LIMIT 64",
                (pending[0], pending[1], pending[2] * DAY, (pending[2] + 1) * DAY),
            ).fetchall()
            if keys:
                _process(db, working, keys, now_ms, initial=False)
            else:
                db.execute(
                    "DELETE FROM market_context_tile_pending WHERE stream=? AND kind=? AND day=?",
                    tuple(pending),
                )
    return True
