"""Finite typed service expiry and explicit parent retirement in the trial."""

from __future__ import annotations

from typing import Any, Callable

from .storage import StateError


def retire(repo: Any, event_id: str, terminal: Callable[[str, str], None]) -> int:
    repo.check(write=True)
    db = repo.store.db
    for (state,) in db.execute("SELECT state FROM subject_outcomes WHERE event_id=?", (event_id,)).fetchall():
        terminal("outcome-job", state)
    for channel, stage, state in db.execute(
        "SELECT channel,stage,state FROM subject_deliveries WHERE event_id=?", (event_id,)
    ).fetchall():
        if state in ("PENDING", "RETRY", "IN_FLIGHT", "BLOCKED"):
            terminal("agent" if channel == "agent" else "outbox", state)
    removed = 0
    for table in ("subject_attempts", "subject_deliveries", "subject_outcomes", "subject_sources"):
        removed += db.execute("DELETE FROM " + table + " WHERE event_id=?", (event_id,)).rowcount
    return removed


def work(repo: Any, now: int, batch: int = 64) -> int:
    """Service-only leases may end earlier than their source. Jobs end with source."""
    db = repo.store.db
    if not db.in_transaction:
        raise StateError("Typed expiry requires owning transaction")
    repo.check(write=True)
    removed = 0
    for table in ("subject_attempts", "subject_deliveries"):
        if table == "subject_deliveries":
            from .storage_lifecycle import BoundedRetention

            for channel, state in db.execute(
                "SELECT channel,state FROM subject_deliveries WHERE expires_ms<=? ORDER BY expires_ms,key LIMIT ?",
                (now, batch),
            ).fetchall():
                if state in ("PENDING", "RETRY", "IN_FLIGHT", "BLOCKED"):
                    BoundedRetention(repo.store)._terminal("agent" if channel == "agent" else "outbox", state)
        removed += db.execute(
            "DELETE FROM "
            + table
            + " WHERE key IN (SELECT key FROM "
            + table
            + " WHERE expires_ms<=? ORDER BY expires_ms,key LIMIT ?)",
            (now, batch),
        ).rowcount
    # Unused immutable anchors carry no body and cannot pin old details forever.
    removed += db.execute(
        "DELETE FROM subject_sources WHERE (event_id,revision) IN (SELECT s.event_id,s.revision FROM subject_sources s WHERE NOT EXISTS(SELECT 1 FROM subject_outcomes j WHERE j.event_id=s.event_id AND j.event_revision=s.revision) AND NOT EXISTS(SELECT 1 FROM subject_deliveries d WHERE d.event_id=s.event_id AND d.event_revision=s.revision) AND NOT EXISTS(SELECT 1 FROM subject_attempts a WHERE a.event_id=s.event_id AND a.event_revision=s.revision) LIMIT ?)",
        (batch,),
    ).rowcount
    repo.stats["expired"] += removed
    return removed
