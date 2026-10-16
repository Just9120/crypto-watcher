"""Resumable in-place encoding changes, with bounded work and unchanged public bytes."""

from __future__ import annotations

import time
from typing import Any

from .storage import Store, encode


def work(store: Store, now_ms: int, batch: int = 256, seconds: float = 0.04) -> dict[str, Any]:
    from .expiry_index import drain
    from .market_storage import decoded
    from .quality_layout import drain as drain_quality
    from .quality_summary import encode_facts
    from .record_codec import pack_binary, sql_payload

    if not store.bounded:
        return {"state": "N/A_LEGACY"}
    state = store.get_runtime("storage_repack", {})
    if state.get("version") == 3 and state.get("state") == "COMPLETE":
        return state
    state = {"version": 3, "cursor": "", "records": 0, "saved_payload_bytes": 0, **state}
    state.setdefault("saved_categories", {})
    deadline = time.monotonic() + seconds
    with store.db:
        # The old expiry table itself is the durable remaining-work queue. A row
        # moves only in the transaction that inserts its checked replacement.
        expiry = drain(store.db, batch=batch, seconds=seconds / 3)
        if not state.get("records_complete"):
            prefix = "radar:v1:"
            rows = store.db.execute(
                "SELECT key,payload_json FROM runtime WHERE key>? AND key<? ORDER BY key LIMIT ?",
                (state["cursor"] or prefix, prefix + "~", batch),
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
                    packed = pack_binary(raw)
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
            else "IN_PROGRESS",
            at_ms=now_ms,
            expiry_legacy_empty=expiry["legacy_empty"],
        )
        store.put_runtime("storage_repack", state)
    # Return freed pages incrementally; never a full VACUUM or a second database.
    if store.db.execute("PRAGMA freelist_count").fetchone()[0] > 256:
        store.db.execute("PRAGMA incremental_vacuum(32)").fetchall()
    return state
