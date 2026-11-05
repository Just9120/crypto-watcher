"""Bounded trial accounting over canonical queues, without decoding payload bodies."""

from __future__ import annotations

from typing import Any


def pending_usage(store: Any) -> dict[str, int]:
    repo = store.services
    repo.check()
    legacy = sum(
        store.db.execute(
            "SELECT COALESCE(SUM(length(CAST(payload_json AS BLOB))),0) FROM runtime WHERE key>=? AND key<?",
            ("radar:v1:" + prefix, "radar:v1:" + prefix + "~"),
        ).fetchone()[0]
        for prefix in ("pending:telegram:", "agent:intent-due:", "agent:send-due:")
    )
    canonical, payload, mutable, missing = store.db.execute(
        "SELECT COALESCE(SUM(length(CAST(CASE WHEN d.stage='intent' "
        "THEN json_object('key',d.key) ELSE json_object('delivery_id',d.id) END AS BLOB))),0),"
        "COALESCE(SUM(length(CAST(p.payload AS BLOB))),0),"
        "COALESCE(SUM(length(CAST(d.mutable AS BLOB))),0),"
        "COALESCE(SUM(p.key IS NULL),0) "
        "FROM subject_deliveries d LEFT JOIN subject_delivery_payloads p USING(key) WHERE d.queued=1"
    ).fetchone()
    if missing:
        from .storage import StateError

        raise StateError("Pending subject delivery is missing its payload")
    return dict(
        binding_bytes=legacy + canonical,
        legacy_binding_bytes=legacy,
        canonical_binding_bytes=canonical,
        canonical_payload_bytes=payload,
        canonical_mutable_bytes=mutable,
    )


def final_ledger(store: Any, history: Any, now: int) -> dict[str, Any]:
    repo = store.services
    repo.check()
    states = [
        dict(channel=r[0], stage=r[1], state=r[2], queued=r[3], reason=r[4], count=r[5])
        for r in store.db.execute(
            "SELECT channel,stage,state,queued,COALESCE(json_extract(mutable,'$.reason'),''),COUNT(*) "
            "FROM subject_deliveries GROUP BY 1,2,3,4,5 ORDER BY 1,2,3,4,5"
        )
    ]
    due = [
        dict(id=r[0], state=r[1], pending=r[2], due_ms=r[3], next_ms=r[4], reason=r[5])
        for r in store.db.execute(
            "SELECT id,state,pending,due_ms,next_ms,COALESCE(json_extract(extra,'$.reason'),'') "
            "FROM subject_outcomes WHERE due_ms<=? ORDER BY due_ms,id LIMIT 64",
            (now,),
        )
    ]
    return dict(
        pending_usage=pending_usage(store),
        delivery_states=states,
        due_jobs=due,
        orphaned_pending=history.get("orphaned-pending-summary", {}),
    )
