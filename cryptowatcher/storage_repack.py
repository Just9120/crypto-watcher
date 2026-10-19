"""Resumable in-place encoding changes, with bounded work and unchanged public bytes."""

from __future__ import annotations

import time
from typing import Any

from .storage import Store, encode, json_payload


def normalized_record(store: Store, key: str, value: Any) -> Any:
    """Rebind old inline coordinates without changing public values."""
    from .events import PREFIX, History
    from .history_codec import SCHEMA, SHARED_FIELDS, WINDOW_FIELDS
    from .market_storage import restore_window
    from .numeric_pool import encode_reference, resolve
    from .source_windows import intern

    history = History(store)
    if key.startswith((PREFIX + "outcome-job:", PREFIX + "comparison-job:")):
        from .queue_health import sync_job

        sync_job(history, key[len(PREFIX) :], value)
    if not key.startswith(tuple(PREFIX + kind + ":" for kind in ("event", "outcome", "comparison"))):
        return value
    from .expiry_index import expires

    expiry = expires(store.db, key)

    def visit(item: Any, depth: int = 0) -> Any:
        if depth > 32:
            raise ValueError("Source normalization nesting exceeds bound")
        if isinstance(item, list):
            return [visit(child, depth + 1) for child in item]
        if not isinstance(item, dict):
            return item
        result = {k: visit(v, depth + 1) if k != "_history_storage" else v for k, v in item.items()}
        storage = item.get("_history_storage")
        if storage is not None:
            if (
                not isinstance(storage, dict)
                or set(storage) != {"schema", "fields"}
                or storage.get("schema") != SCHEMA
                or not isinstance(storage.get("fields"), dict)
            ):
                raise ValueError("Invalid history storage fields")
            fields = dict(storage["fields"])
            for name, ref in fields.items():
                if name not in SHARED_FIELDS | WINDOW_FIELDS or not isinstance(ref, dict):
                    raise ValueError("Invalid history storage reference")
                if isinstance(ref, dict) and ref.get("mode") in ("source", "source-runs"):
                    if expiry is None:
                        raise ValueError("Source normalization requires a retained parent deadline")
                    restore_window(store.db, ref)  # Validate original SHA and every immutable coordinate.
                    fields[name] = intern(store.db, ref, expiry[0])
                elif name == "metrics" and ref.get("mode") in ("numeric", "numeric2"):
                    fields[name] = encode_reference(store.db, resolve(store.db, ref))
                elif name == "source_context" and ref.get("mode") == "contexts":
                    from .context_integrity import convert

                    fields[name] = convert(store.db, ref)
            result["_history_storage"] = {**storage, "fields": fields}
        if isinstance(result.get("metrics"), dict):
            reference = encode_reference(store.db, result["metrics"])
            if reference is not None:
                current = result.get("_history_storage", {"schema": SCHEMA, "fields": {}})
                if "metrics" in current["fields"]:
                    raise ValueError("Conflicting numeric metric storage")
                result["_history_storage"] = {
                    **current,
                    "fields": {**current["fields"], "metrics": reference},
                }
                del result["metrics"]
        return result

    return visit(value)


def scan_bounds(store: Store, state: dict[str, Any], kind: str) -> tuple[int, int]:
    """A finite physical interval; a schema rebuild invalidates physical IDs.

    Public identity and the pre-upgrade logical floor remain keys. Normal
    restart resumes next_rowid; DDL/VACUUM conservatively rechecks the retained
    logical remainder rather than trusting relocated physical positions.
    """
    table = {"records": "runtime", "decisions": "runtime", "quality": "market_quality_blocks"}[kind]
    version = store.db.execute("PRAGMA schema_version").fetchone()[0]
    if f"{kind}_rowid_upper" not in state or state.get(f"{kind}_schema_version") != version:
        if f"{kind}_rowid_upper" in state:
            state[f"{kind}_scan_restarts"] = state.get(f"{kind}_scan_restarts", 0) + 1
        low = store.db.execute(f"SELECT rowid FROM {table} ORDER BY rowid LIMIT 1").fetchone()
        high = store.db.execute(f"SELECT rowid FROM {table} ORDER BY rowid DESC LIMIT 1").fetchone()
        state[f"{kind}_next_rowid"] = low[0] if low else 1
        state[f"{kind}_rowid_upper"] = high[0] if high else 0
        state[f"{kind}_schema_version"] = version
    return state[f"{kind}_next_rowid"], state[f"{kind}_rowid_upper"]


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
    state.setdefault("decisions_key_floor", state.get("decision_cursor", prefix))
    next_rowid, upper = scan_bounds(store, state, "decisions")
    rows = (
        store.db.execute(
            "SELECT rowid,key,CASE WHEN key>? AND key<=? AND length(key)=? THEN payload_json END "
            "FROM runtime NOT INDEXED WHERE rowid>=? AND rowid<=? ORDER BY rowid LIMIT ?",
            (
                state["decisions_key_floor"],
                state["decision_upper"],
                len(prefix) + 36,
                next_rowid,
                upper,
                batch,
            ),
        )
        if next_rowid <= upper
        else ()
    )
    processed = 0
    exhausted = True
    for rid, key, payload in rows:
        if processed and time.monotonic() >= deadline:
            exhausted = False
            break
        state["decisions_next_rowid"] = rid + 1
        state["decisions_rows_scanned"] = state.get("decisions_rows_scanned", 0) + 1
        processed += 1
        if payload is None:
            continue
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
    state["decisions_complete"] = exhausted and (processed < batch or state["decisions_next_rowid"] > upper)


def record_rows(store: Store, state: dict[str, Any], batch: int, deadline: float) -> None:
    from .head_codec import MAGIC as HEAD_MAGIC
    from .head_codec import SCHEMA as HEAD_SCHEMA
    from .record_codec import TYPED_MAGIC, pack_binary, sql_payload

    if not state.get("records_complete"):
        prefix = "radar:v1:"
        if "records_upper" not in state:
            row = store.db.execute(
                "SELECT key FROM runtime WHERE key>=? AND key<? ORDER BY key DESC LIMIT 1",
                (prefix, prefix + "~"),
            ).fetchone()
            state["records_upper"] = row[0] if row else prefix
        state.setdefault("records_key_floor", state["cursor"] or prefix)
        next_rowid, upper = scan_bounds(store, state, "records")
        # Bound the SQL read itself, including excluded headers. CASE avoids
        # loading large shared bodies which never belonged to this repack.
        rows = (
            store.db.execute(
                "SELECT rowid,key,CASE WHEN key>? AND key<=? THEN payload_json END "
                "FROM runtime NOT INDEXED WHERE rowid>=? AND rowid<=? ORDER BY rowid LIMIT ?",
                (state["records_key_floor"], state["records_upper"], next_rowid, upper, batch),
            )
            if next_rowid <= upper
            else ()
        )
        processed = 0
        exhausted = True
        for rid, key, payload in rows:
            if processed and time.monotonic() >= deadline:
                exhausted = False
                break
            state["records_next_rowid"] = rid + 1
            state["records_rows_scanned"] = state.get("records_rows_scanned", 0) + 1
            processed += 1
            if payload is None:
                continue
            size = len(payload.encode() if isinstance(payload, str) else payload)
            if size <= 1024 * 1024:
                raw = sql_payload(payload)
                original = json_payload(payload)
                normalized = normalized_record(store, key, original)
                changed = encode(normalized) != encode(original)
                target = encode(normalized) if changed else raw
                # Validate the target envelope, but do not parse/compress it
                # a second time. New writes already choose this codec.
                packed = (
                    payload
                    if not changed
                    and isinstance(payload, bytes)
                    and (
                        payload.startswith(HEAD_MAGIC)
                        or payload.startswith(TYPED_MAGIC)
                        and not (isinstance(original, dict) and original.get("schema") == HEAD_SCHEMA)
                    )
                    else pack_binary(target)
                )
                # A shared-view reference can grow by a byte while removing a
                # retained duplicate numeric body and its index entries. Keep
                # the exact signed payload delta separate from incoming flow.
                if changed or len(packed) < size:
                    if sql_payload(packed) != target:
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
        state["records_complete"] = exhausted and (processed < batch or state["records_next_rowid"] > upper)


def quality_rows(store: Store, state: dict[str, Any], batch: int, deadline: float) -> None:
    from .quality_layout import drain as drain_quality
    from .quality_packets import MAGIC, pack, unpack
    from .quality_summary import provenance_links

    quality_moved = drain_quality(store.db, batch=1) if time.monotonic() < deadline else 1
    if (
        state.get("records_complete")
        and quality_moved == 0
        and not state.get("quality_complete")
        and time.monotonic() < deadline
    ):
        state.setdefault("quality_key_floor", state.get("quality_cursor", [-1, -1, -1]))
        next_rowid, upper = scan_bounds(store, state, "quality")
        rows = (
            store.db.execute(
                "SELECT b.rowid,b.day,b.cohort,b.chunk,CASE WHEN (b.day,b.cohort,b.chunk)>(?,?,?) THEN b.facts END,c.external,p.payload "
                "FROM market_quality_blocks b NOT INDEXED JOIN market_quality_cohorts c ON c.id=b.cohort "
                "JOIN market_quality_provenance p ON p.id=b.provenance WHERE b.rowid>=? AND b.rowid<=? ORDER BY b.rowid LIMIT ?",
                (*state["quality_key_floor"], next_rowid, upper, batch),
            )
            if next_rowid <= upper
            else ()
        )
        processed = 0
        exhausted = True
        for rid, day, cohort, chunk, payload, external, provenance in rows:
            if processed and time.monotonic() >= deadline:
                exhausted = False
                break
            state["quality_next_rowid"] = rid + 1
            state["quality_rows_scanned"] = state.get("quality_rows_scanned", 0) + 1
            processed += 1
            if payload is None:
                continue
            provenance_links(store, day, external, chunk, json_payload(provenance))
            if not bytes(payload).startswith(MAGIC):
                facts = unpack(store.db, bytes(payload))
                packed = pack(store.db, facts, str(cohort))
                if encode(unpack(store.db, packed)) != encode(facts):
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
        state["quality_complete"] = exhausted and (processed < batch or state["quality_next_rowid"] > upper)
    state["quality_legacy_empty"] = quality_moved == 0


def work(store: Store, now_ms: int, batch: int = 256, seconds: float = 0.04) -> dict[str, Any]:
    from .cohort_meter import drain as drain_cohorts
    from .cohort_meter import ready as cohort_ready
    from .expiry_index import drain
    from .key_registry import drain as drain_registry
    from .quality_meter import drain as drain_quality_meter
    from .quality_meter import read as quality_totals
    from .reference_layout import drain as drain_references
    from .runtime_meter import drain as drain_namespaces
    from .runtime_meter import read as namespace_snapshot

    if not store.bounded:
        return {"state": "N/A_LEGACY"}
    state = store.get_runtime("storage_repack", {})
    if (
        state.get("version") == 9
        and state.get("state") == "COMPLETE"
        and state.get("decisions_complete")
        and state.get("namespace_meter_complete")
        and state.get("reference_legacy_empty")
        and state.get("quality_meter_complete")
        and state.get("key_registry_complete")
        and state.get("cohort_meter_complete")
        and cohort_ready(store.db)
        and quality_totals(store.db) is not None
        and namespace_snapshot(store.db) is not None
    ):
        return state
    if state.get("version") in (3, 4, 5, 6, 7, 8):
        state = {
            "previous_version": state["version"],
            "previous_run": {
                k: state[k] for k in ("state", "records", "cursor", "records_rowid_upper") if k in state
            },
            "saved_payload_bytes": state.get("saved_payload_bytes", 0),
            "saved_categories": state.get("saved_categories", {}),
        }
    state = {"version": 9, "cursor": "", "records": 0, "saved_payload_bytes": 0, **state}
    state.setdefault("saved_categories", {})
    deadline = time.monotonic() + seconds
    first = state.get("next_phase", 0)
    with store.db:
        # One shared deadline, with a durable rotating first phase. A slow SQL
        # read/commit may overrun a slice, but cannot always starve the same tail.
        for offset in range(9):
            if time.monotonic() >= deadline:
                break
            phase = (first + offset) % 9
            state["next_phase"] = (phase + 1) % 9
            if phase == 0 and not state.get("records_complete"):
                record_rows(store, state, batch, deadline)
            elif phase == 1 and not state.get("decisions_complete"):
                decision_heads(store, state, batch, deadline)
            elif phase == 2 and not state.get("expiry_legacy_empty"):
                result = drain(store.db, batch=batch, seconds=max(0, deadline - time.monotonic()))
                state["expiry_legacy_empty"] = result["legacy_empty"]
            elif phase == 3:
                quality_rows(store, state, batch, deadline)
            elif phase == 4:
                state["namespace_meter_complete"] = drain_namespaces(store.db, batch=batch, deadline=deadline)
            elif phase == 5:
                state["reference_legacy_empty"] = drain_references(store.db, batch, deadline)
            elif phase == 6:
                state["quality_meter_complete"] = drain_quality_meter(store.db, batch, deadline)
            elif phase == 7:
                state["key_registry_complete"] = drain_registry(store.db, state, batch, deadline)
            elif phase == 8:
                state["cohort_meter_complete"] = drain_cohorts(store.db, batch, deadline)
        state.update(
            state="COMPLETE"
            if all(
                state.get(k)
                for k in (
                    "records_complete",
                    "decisions_complete",
                    "expiry_legacy_empty",
                    "quality_complete",
                    "quality_legacy_empty",
                    "namespace_meter_complete",
                    "reference_legacy_empty",
                    "quality_meter_complete",
                    "key_registry_complete",
                    "cohort_meter_complete",
                )
            )
            else "IN_PROGRESS",
            at_ms=now_ms,
        )
        store.put_runtime("storage_repack", state)
    if store.db.execute("PRAGMA freelist_count").fetchone()[0] > 256:
        store.db.execute("PRAGMA incremental_vacuum(32)").fetchall()
    return state
