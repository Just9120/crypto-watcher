"""Resumable in-place encoding changes, with bounded work and unchanged public bytes."""

from __future__ import annotations

import time
from typing import Any

from .storage import Store, encode, json_payload


async def async_work(store: Store, now_ms: int, slices: int = 8) -> dict[str, Any]:
    """Advance a finite queue without one long transaction or event-loop stall.

    Each existing40ms slice commits its own checkpoint. Eight slices add at
    most320ms of intended work per maintenance tick, with a scheduler yield
    between slices; SQL/commit I/O can overrun an individual time check.
    """
    import asyncio

    state = store.get_runtime("storage_repack", {})
    for _ in range(slices):
        state = work(store, now_ms)
        if state.get("state") in ("COMPLETE", "N/A_LEGACY"):
            break
        await asyncio.sleep(0)
    return state


def decision_heads(store: Store, state: dict[str, Any], batch: int, deadline: float) -> None:
    from .events import DECISION_HEAD, PREFIX, History

    prefix = PREFIX + "decision:telegram:"
    if "decision_upper" not in state:
        row = store.db.execute(
            "SELECT key FROM runtime WHERE key>=? AND key<? AND length(key)=? ORDER BY key DESC LIMIT 1",
            (prefix, prefix + "~", len(prefix) + 36),
        ).fetchone()
        state["decision_upper"] = row[0] if row else prefix
    rows = store.db.execute(
        "SELECT key,payload_json FROM runtime WHERE key>? AND key<=? AND length(key)=? ORDER BY key LIMIT ?",
        (state.get("decision_cursor", prefix), state["decision_upper"], len(prefix) + 36, batch),
    ).fetchall()
    processed = 0
    for key, payload in rows:
        if time.monotonic() >= deadline:
            break
        value = json_payload(payload)
        revision = value.get("revision") if isinstance(value, dict) else None
        if type(revision) is int and revision > 0:
            raw = encode(value)
            old = store.get_runtime(key + f":{revision:010d}")
            if encode(old) != raw:
                raise ValueError("Decision head differs from retained revision")
            pointer = {DECISION_HEAD: revision}
            if encode(History(store).resolve(key[len(PREFIX) :], pointer)) != raw:
                raise ValueError("Decision reference changed public bytes")
            packed = encode(pointer)
            size = len(payload.encode() if isinstance(payload, str) else payload)
            if len(packed) < size:
                store.db.execute("UPDATE runtime SET payload_json=? WHERE key=?", (packed, key))
                state["saved_payload_bytes"] += size - len(packed)
                state["saved_categories"]["services"] = (
                    state["saved_categories"].get("services", 0) + size - len(packed)
                )
                state["decision_heads_compacted"] = state.get("decision_heads_compacted", 0) + 1
        state["decision_cursor"] = key
        state["decision_heads_scanned"] = state.get("decision_heads_scanned", 0) + 1
        processed += 1
    state["decisions_complete"] = len(rows) < batch and processed == len(rows)


def work(store: Store, now_ms: int, batch: int = 256, seconds: float = 0.04) -> dict[str, Any]:
    from .expiry_index import drain
    from .market_storage import decoded
    from .quality_layout import drain as drain_quality
    from .quality_summary import encode_facts
    from .record_codec import TYPED_MAGIC, pack_binary, sql_payload

    if not store.bounded:
        return {"state": "N/A_LEGACY"}
    state = store.get_runtime("storage_repack", {})
    if state.get("version") == 3 and state.get("state") == "COMPLETE" and state.get("decisions_complete"):
        return state
    state = {"version": 3, "cursor": "", "records": 0, "saved_payload_bytes": 0, **state}
    state.setdefault("saved_categories", {})
    deadline = time.monotonic() + seconds
    with store.db:
        if not state.get("decisions_complete"):
            decision_heads(store, state, batch, min(deadline, time.monotonic() + seconds / 4))
        # The old expiry table itself is the durable remaining-work queue. A row
        # moves only in the transaction that inserts its checked replacement.
        expiry = drain(store.db, batch=batch, seconds=seconds / 3)
        if not state.get("records_complete"):
            prefix = "radar:v1:"
            if "records_upper" not in state:
                row = store.db.execute(
                    "SELECT key FROM runtime WHERE key>=? AND key<? ORDER BY key DESC LIMIT 1",
                    (prefix, prefix + "~"),
                ).fetchone()
                state["records_upper"] = row[0] if row else prefix
            rows = store.db.execute(
                "SELECT key,payload_json FROM runtime WHERE key>? AND key<=? ORDER BY key LIMIT ?",
                (state["cursor"] or prefix, state["records_upper"], batch),
            )
            processed = 0
            exhausted = True
            for key, payload in rows:
                if time.monotonic() >= deadline:
                    exhausted = False
                    break
                size = len(payload.encode() if isinstance(payload, str) else payload)
                if size <= 1024 * 1024:
                    raw = sql_payload(payload)
                    # Validate the target envelope, but do not parse/compress it
                    # a second time. New writes already choose this codec.
                    packed = (
                        payload
                        if isinstance(payload, bytes) and payload.startswith(TYPED_MAGIC)
                        else pack_binary(raw)
                    )
                    if len(packed) < size:
                        if sql_payload(packed) != raw:
                            raise ValueError("Repack changed public record bytes")
                        store.db.execute("UPDATE runtime SET payload_json=? WHERE key=?", (packed, key))
                        state["saved_payload_bytes"] += size - len(packed)
                        category = (
                            "details"
                            if key.startswith(
                                tuple(
                                    "radar:v1:" + p
                                    for p in (
                                        "event",
                                        "outcome:",
                                        "outcome-head:",
                                        "episode:",
                                        "episode-head:",
                                        "comparison:",
                                        "comparison-head:",
                                    )
                                )
                            )
                            else "services"
                        )
                        state["saved_categories"][category] = (
                            state["saved_categories"].get(category, 0) + size - len(packed)
                        )
                else:
                    state["large_records_skipped"] = state.get("large_records_skipped", 0) + 1
                state["cursor"] = key
                state["records"] += 1
                processed += 1
            state["records_complete"] = exhausted and processed < batch
        quality_moved = drain_quality(store.db, batch=1) if time.monotonic() < deadline else 1
        if (
            state.get("records_complete")
            and quality_moved == 0
            and not state.get("quality_complete")
            and time.monotonic() < deadline
        ):
            cursor = state.get("quality_cursor", [-1, -1, -1])
            rows = store.db.execute(
                "SELECT b.day,b.cohort,b.chunk,b.facts FROM market_quality_blocks b WHERE (b.day,b.cohort,b.chunk)>(?,?,?) ORDER BY b.day,b.cohort,b.chunk LIMIT ?",
                (*cursor, batch),
            )
            processed = 0
            exhausted = True
            for day, cohort, chunk, payload in rows:
                if time.monotonic() >= deadline:
                    exhausted = False
                    break
                facts = decoded(bytes(payload), 1024 * 1024)
                packed = encode_facts(facts)
                if len(packed) < len(payload):
                    if encode(decoded(packed, 1024 * 1024)) != encode(facts):
                        raise ValueError("Repack changed quality facts")
                    store.db.execute(
                        "UPDATE market_quality_blocks SET facts=? WHERE day=? AND cohort=? AND chunk=?",
                        (packed, day, cohort, chunk),
                    )
                    state["saved_payload_bytes"] += len(payload) - len(packed)
                    state["saved_categories"]["quality"] = (
                        state["saved_categories"].get("quality", 0) + len(payload) - len(packed)
                    )
                state["quality_cursor"] = [day, cohort, chunk]
                processed += 1
            state["quality_complete"] = exhausted and processed < batch
        state.update(
            state="COMPLETE"
            if expiry["legacy_empty"]
            and quality_moved == 0
            and state.get("records_complete")
            and state.get("quality_complete")
            and state.get("decisions_complete")
            else "IN_PROGRESS",
            at_ms=now_ms,
            expiry_legacy_empty=expiry["legacy_empty"],
        )
        store.put_runtime("storage_repack", state)
    # Return freed pages incrementally; never a full VACUUM or a second database.
    if store.db.execute("PRAGMA freelist_count").fetchone()[0] > 256:
        store.db.execute("PRAGMA incremental_vacuum(32)").fetchall()
    return state
