"""Resumable in-place encoding changes, with bounded work and unchanged public bytes."""

from __future__ import annotations

import time
from typing import Any

from .storage import Store, encode, json_payload

VERSION = 22


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
    from . import metric_price_context as price_context
    from .numeric_blocks import price_writing

    root_context: Any = price_context.Context() if price_writing(store.db) else False

    def visit(item: Any, depth: int = 0, inherited: Any = root_context) -> Any:
        if depth > 32:
            raise ValueError("Source normalization nesting exceeds bound")
        if isinstance(item, list):
            return [visit(child, depth + 1, inherited) for child in item]
        if not isinstance(item, dict):
            return item
        price_state = (
            price_context.context(
                store.db, item, inherited, history.codec.field_raw, cache=store.cached_bytes
            )
            if inherited is not False
            else False
        )

        def price(frame: bytes) -> bytes | None:
            if not isinstance(price_state, price_context.Context):
                return None
            return price_context.candidate(
                store.db, item, frame, price_state, history.codec.field_raw, cache=store.cached_bytes
            )

        result = {
            k: visit(v, depth + 1, price_state) if k != "_history_storage" else v for k, v in item.items()
        }
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
                if ref.get("mode") in ("source", "source-runs", "source-ref"):
                    from .candle_access import writing
                    from .source_cells import convert as convert_cells

                    tiled_writer = writing(store.db)
                    if ref["mode"] == "source-ref" and not tiled_writer:
                        continue
                    if expiry is None:
                        raise ValueError("Source normalization requires a retained parent deadline")
                    cells = convert_cells(store.db, ref, cache=store.cached_bytes) if tiled_writer else None
                    if cells is not None:
                        fields[name] = cells
                    elif ref["mode"] != "source-ref":
                        restore_window(store.db, ref, cache=store.cached_bytes)
                        fields[name] = intern(store.db, ref, expiry[0])
                elif name == "metrics" and ref.get("mode") in ("numeric", "numeric2"):
                    fields[name] = encode_reference(
                        store.db, resolve(store.db, ref), price=price if price_state is not False else None
                    )
                elif name == "source_context" and ref.get("mode") == "contexts":
                    from .context_integrity import convert

                    fields[name] = convert(store.db, ref)
            result["_history_storage"] = {**storage, "fields": fields}
        if isinstance(result.get("metrics"), dict):
            reference = encode_reference(
                store.db, result["metrics"], price=price if price_state is not False else None
            )
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


async def async_work(
    store: Store, now_ms: int, slices: int = 8, *, defer_compaction: bool = False
) -> dict[str, Any]:
    """Advance a finite queue without one long transaction or event-loop stall.

    Each existing40ms slice commits its own checkpoint. Eight slices add at
    most320ms of intended work per maintenance tick, with a scheduler yield
    between slices; SQL/commit I/O can overrun an individual time check.
    """
    import asyncio

    if type(defer_compaction) is not bool:
        raise ValueError("Invalid compaction priority")
    state = store.get_runtime("storage_repack", {})
    for _ in range(slices):
        state = work(store, now_ms, defer_compaction=True) if defer_compaction else work(store, now_ms)
        from .catalogue_repack import busy

        if not busy(state):
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
                numeric_categories = ("numeric_details", "quality_numeric")
                has_numeric = key.startswith(
                    tuple("radar:v1:" + kind + ":" for kind in ("event", "outcome", "comparison"))
                )
                numeric_before = (
                    dict(
                        store.db.execute(
                            "SELECT category,logical_bytes FROM market_counts WHERE category IN (?,?)",
                            numeric_categories,
                        )
                    )
                    if has_numeric
                    else {}
                )
                normalized = normalized_record(store, key, original)
                if has_numeric:
                    numeric_after = dict(
                        store.db.execute(
                            "SELECT category,logical_bytes FROM market_counts WHERE category IN (?,?)",
                            numeric_categories,
                        )
                    )
                    for numeric_category in numeric_categories:
                        saved = numeric_before.get(numeric_category, 0) - numeric_after.get(
                            numeric_category, 0
                        )
                        if saved:
                            state["saved_payload_bytes"] = state.get("saved_payload_bytes", 0) + saved
                            saved_categories = state.setdefault("saved_categories", {})
                            saved_categories[numeric_category] = (
                                saved_categories.get(numeric_category, 0) + saved
                            )
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
    from .quality_packets import FRAME_MAGIC, MAGIC, repack, unpack
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
            if not bytes(payload).startswith((MAGIC, FRAME_MAGIC)):
                facts = unpack(store.db, bytes(payload))
                packed = repack(store.db, bytes(payload), str(cohort))
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


def work(
    store: Store,
    now_ms: int,
    batch: int = 256,
    seconds: float = 0.04,
    *,
    defer_compaction: bool = False,
) -> dict[str, Any]:
    from . import (
        catalogue_compaction,
        catalogue_repack,
        context_tile_maintenance,
        context_tiles,
        numeric_frame_work,
        numeric_frames,
        numeric_refs,
        numeric_search,
        packet_bundles,
        parent_affinity,
        quality_frames,
        source_column_work,
    )
    from .candle_access import installed as source_installed
    from .candle_access import writing as source_writing
    from .candle_blocks import install as install_source_tiles
    from .candle_blocks import summary as source_summary
    from .cohort_meter import drain as drain_cohorts
    from .cohort_meter import ready as cohort_ready
    from .expiry_index import drain
    from .key_registry import drain as drain_registry
    from .numeric_blocks import install as install_numeric_blocks
    from .numeric_blocks import price_layout, price_writing, upgrade_price
    from .numeric_blocks import summary as numeric_summary
    from .numeric_blocks import transfer as transfer_numeric
    from .numeric_blocks import writing as numeric_writing
    from .quality_meter import drain as drain_quality_meter
    from .quality_meter import read as quality_totals
    from .reference_layout import drain as drain_references
    from .runtime_meter import drain as drain_namespaces
    from .runtime_meter import read as namespace_snapshot
    from .source_transfer import advance as transfer_sources

    if type(defer_compaction) is not bool:
        raise ValueError("Invalid compaction priority")
    if not store.bounded:
        return {"state": "N/A_LEGACY"}
    state = store.get_runtime("storage_repack", {})
    if (
        not isinstance(state, dict)
        or ("version" in state and type(state["version"]) is not int)
        or state.get("version")
        not in (None, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, VERSION)
    ):
        raise ValueError("Unknown storage repack checkpoint; database preserved")
    phases = (
        20
        if state.get("version") in (21, VERSION)
        else 19
        if state.get("version") == 20
        else 18
        if state.get("version") == 19
        else 17
        if state.get("version") == 18
        else 16
        if state.get("version") == 17
        else 15
        if state.get("version") == 16
        else 14
    )
    if "next_phase" in state and (
        type(state["next_phase"]) is not int or not 0 <= state["next_phase"] < phases
    ):
        raise ValueError("Invalid storage repack phase; database preserved")
    catalogue_repack.validate(store, state, VERSION)
    quality_frames.validate(state.get("quality_frames", {}))
    context_tile_maintenance.validate(state.get("context_tiles", {}))
    parent_affinity.validate(state.get("parent_affinity", {}))
    affinity_layout = parent_affinity.header(store.db)
    if state.get("version") in (20, 21, VERSION) and not affinity_layout:
        raise ValueError("Missing parent affinity writer checkpoint; database preserved")
    if (
        state.get("version") in (20, 21, VERSION)
        and state.get("state") == "COMPLETE"
        and state.get("parent_affinity", {}).get("initial_complete") is not True
    ):
        raise ValueError("Missing completed parent affinity boundary; database preserved")
    context_layout = context_tiles.installed(store.db)
    if state.get("version") in (19, 20, 21, VERSION) and not context_layout:
        raise ValueError("Missing context tile writer checkpoint; database preserved")
    if (
        state.get("version") in (19, 20, 21, VERSION)
        and state.get("state") == "COMPLETE"
        and state.get("context_tiles", {}).get("initial_complete") is not True
    ):
        raise ValueError("Missing completed context tile boundary; database preserved")
    if state.get("version") in (18, 19, 20, 21, VERSION) and not quality_frames.installed(store.db):
        raise ValueError("Missing quality frame writer checkpoint; database preserved")
    if (
        state.get("version") in (18, 19, 20, 21, VERSION)
        and state.get("state") == "COMPLETE"
        and state.get("quality_frames", {}).get("initial_complete") is not True
    ):
        raise ValueError("Missing completed quality frame boundary; database preserved")
    catalogue_compaction.validate(state.get("packet_bundles", {}))
    if state.get("version") in (16, 17, 18, 19, 20, 21, VERSION) and not packet_bundles.installed(store.db):
        raise ValueError("Missing packet bundle checkpoint; database preserved")
    if packet_bundles.installed(store.db):
        packet_bundles.validate(store.db)
    if (
        state.get("version") in (16, 17, 18, 19, 20, 21, VERSION)
        and state.get("state") == "COMPLETE"
        and not state.get("packet_bundles", {}).get("initial_complete")
    ):
        raise ValueError("Missing completed packet bundle boundary; database preserved")
    numeric_search.validate(state)
    if state.get("version") in (15, 16, 17, 18, 19, 20, 21, VERSION) and not numeric_search.installed(
        store.db
    ):
        raise ValueError("Missing numeric search checkpoint; database preserved")
    numeric_refs.validate(state.get("numeric_refs", {}))
    if state.get("version") in (17, 18, 19, 20, 21, VERSION) and not numeric_refs.installed(store.db):
        raise ValueError("Missing numeric lease writer checkpoint; database preserved")
    if (
        state.get("version") in (17, 18, 19, 20, 21, VERSION)
        and state.get("state") == "COMPLETE"
        and not state.get("numeric_refs", {}).get("complete")
    ):
        raise ValueError("Missing completed numeric lease boundary; database preserved")
    numeric_frame_work.validate(state.get("numeric_frames", {}))
    if state.get("version") == VERSION and not quality_frames.column_writing(store.db):
        raise ValueError("Missing quality column writer checkpoint; database preserved")
    if state.get("version") in (21, VERSION):
        numeric_frames.header(store.db)
        numeric_frame_work.header(store.db)
        if (
            state.get("state") == "COMPLETE"
            and state.get("numeric_frames", {}).get("initial_complete") is not True
        ):
            raise ValueError("Missing completed numeric frame boundary; database preserved")
        if state.get("state") == "COMPLETE" and not numeric_frame_work.through(
            store.db, state["numeric_frames"]["queue_upper"]
        ):
            raise ValueError("Unfinished initial numeric frame admission boundary; database preserved")
    source_active, numeric_active, price_active = (
        source_writing(store.db),
        numeric_writing(store.db),
        price_writing(store.db),
    )
    if (
        state.get("version") in (10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, VERSION)
        and not source_active
    ):
        raise ValueError("Missing source writer checkpoint; database preserved")
    if state.get("version") in (11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, VERSION) and not numeric_active:
        raise ValueError("Missing numeric writer checkpoint; database preserved")
    if state.get("version") in (12, 13, 14, 15, 16, 17, 18, 19, 20, 21, VERSION) and not price_active:
        raise ValueError("Missing numeric price writer checkpoint; database preserved")
    if not source_installed(store.db):
        install_source_tiles(store.db, store.cached_text)
    source_summary(store.db)
    install_numeric_blocks(store.db, store.cached_bytes)
    numeric_summary(store.db)
    if not price_layout(store.db):
        upgrade_price(store.db)
    numeric_search.install(store.db)
    catalogue_repack.install(store)
    packet_bundles.install(store.db)
    numeric_refs.install(store.db)
    numeric_frames.install(store.db, store.cached_bytes)
    numeric_frame_work.install(store.db)
    quality_frames.install(store.db)
    context_tiles.install(store.db, store.cached_bytes)
    parent_affinity.install(store.db, store.cached_bytes)
    maintenance = catalogue_repack.status(store, now_ms)
    frame_maintenance = quality_frames.status(store.db, now_ms)
    context_maintenance = context_tile_maintenance.status(store.db, now_ms)
    numeric_maintenance = numeric_frame_work.status(store.db, now_ms)
    column_maintenance = source_column_work.status(store.db)
    if (
        state.get("version") == VERSION
        and state.get("state") == "COMPLETE"
        and state.get("decisions_complete")
        and state.get("namespace_meter_complete")
        and state.get("reference_legacy_empty")
        and state.get("quality_meter_complete")
        and state.get("key_registry_complete")
        and state.get("cohort_meter_complete")
        and state.get("source_transfer_complete")
        and state.get("numeric_transfer_complete")
        and state.get("numeric_search_complete")
        and state.get("catalogue_complete")
        and state.get("initial_reclaim_complete")
        and state.get("quality_frame_initial_reclaim_complete")
        and state.get("context_tile_initial_reclaim_complete")
        and state.get("parent_affinity_initial_reclaim_complete")
        and state.get("numeric_frames", {}).get("initial_complete")
        and state.get("numeric_frame_initial_reclaim_complete")
        and not maintenance["pending_work"]
        and not frame_maintenance["pending_work"]
        and not context_maintenance["pending_work"]
        and not numeric_maintenance["pending_work"]
        and not column_maintenance["pending_work"]
        and (not column_maintenance["enabled"] or state.get("source_column_initial_reclaim_complete") is True)
        and cohort_ready(store.db)
        and quality_totals(store.db) is not None
        and namespace_snapshot(store.db) is not None
        and not numeric_search.pending(store.db)
    ):
        # Persisting the previous checkpoint can itself consume free pages.
        # Clear a stale busy result without another checkpoint write, which
        # would again change the allocation being measured. Other idle returns
        # retain their existing checkpoint identity/timestamp.
        if state.get("catalogue_maintenance", {}).get("pending_work"):
            previous = state["catalogue_maintenance"].get("last_progress_ms")
            if previous is not None:
                maintenance["last_progress_ms"] = previous
            state = {**state, "catalogue_maintenance": maintenance}
        if state.get("quality_frame_maintenance", {}).get("pending_work"):
            state = {**state, "quality_frame_maintenance": frame_maintenance}
        if state.get("context_tile_maintenance", {}).get("pending_work"):
            state = {**state, "context_tile_maintenance": context_maintenance}
        if state.get("numeric_frame_maintenance", {}).get("pending_work"):
            state = {**state, "numeric_frame_maintenance": numeric_maintenance}
        return state
    if state.get("version") == 13:
        # CQ5 needs one fresh finite quality pass. Preserve the original domain
        # and inbox boundaries: this upgrade does not restart catalogue transfer.
        state = dict(state)
        state["previous_version"] = 13
        state["state"] = "IN_PROGRESS"
        for key in (
            "quality_complete",
            "quality_cursor",
            "quality_key_floor",
            "quality_next_rowid",
            "quality_rowid_upper",
            "quality_schema_version",
            "quality_rows_scanned",
            "quality_scan_restarts",
            "initial_reclaim_complete",
        ):
            state.pop(key, None)
    elif state.get("version") in (14, 15, 16):
        state = {**state, "previous_version": state["version"], "state": "IN_PROGRESS"}
        state.pop("initial_reclaim_complete", None)
    elif state.get("version") in (17, 18, 19, 20, 21):
        # Keep the qualified body/reclaim boundary so retention priority can
        # drain old events while the independent finite frame pass proceeds.
        state = {**state, "previous_version": state["version"], "state": "IN_PROGRESS"}
    elif state.get("version") in (3, 4, 5, 6, 7, 8, 9, 10, 11, 12):
        state = {
            "previous_version": state["version"],
            "previous_run": {
                k: state[k] for k in ("state", "records", "cursor", "records_rowid_upper") if k in state
            },
            "saved_payload_bytes": state.get("saved_payload_bytes", 0),
            "saved_categories": state.get("saved_categories", {}),
        }
    state = {"cursor": "", "records": 0, "saved_payload_bytes": 0, **state, "version": VERSION}
    state.setdefault("saved_categories", {})
    deadline = time.monotonic() + seconds
    first = state.get("next_phase", 9)
    with store.db:
        if not store.db.in_transaction:
            store.db.execute("BEGIN")
        store.db.execute(
            "INSERT INTO market_meta VALUES('candle_block_runtime','1') ON CONFLICT(key) DO NOTHING"
        )
        store.db.execute(
            "INSERT INTO market_meta VALUES('numeric_block_runtime','1') ON CONFLICT(key) DO NOTHING"
        )
        store.db.execute(
            "INSERT INTO market_meta VALUES('numeric_price_runtime','1') ON CONFLICT(key) DO NOTHING"
        )
        catalogue_repack.activate(store)
        # One shared deadline, with a durable rotating first phase. A slow SQL
        # read/commit may overrun a slice, but cannot always starve the same tail.
        for offset in range(20):
            if time.monotonic() >= deadline:
                break
            phase = (first + offset) % 20
            state["next_phase"] = (phase + 1) % 20
            if phase in (14, 16, 17, 18, 19) and defer_compaction and state.get("initial_reclaim_complete"):
                # Keep all queues/checkpoints pending; resume their bounded
                # passes once the due retention batch drains.
                continue
            if (
                phase == 0
                and state.get("source_transfer_complete")
                and state.get("numeric_transfer_complete")
                and not state.get("records_complete")
            ):
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
            elif phase == 9 and not state.get("source_transfer_complete"):
                categories = ("candles", "source_blocks", "source_block_indexes")

                def source_bytes() -> dict[str, int]:
                    return dict(
                        store.db.execute(
                            "SELECT category,logical_bytes FROM market_counts WHERE category IN (?,?,?)",
                            categories,
                        )
                    )

                before = source_bytes()
                transfer_sources(store.db, state, min(batch, 256), deadline)
                after = source_bytes()
                for category in categories:
                    saved = before.get(category, 0) - after.get(category, 0)
                    if saved:
                        state["saved_payload_bytes"] += saved
                        state["saved_categories"][category] = (
                            state["saved_categories"].get(category, 0) + saved
                        )
            elif phase == 10 and not state.get("numeric_transfer_complete"):
                numeric_categories = ("numeric_details", "quality_numeric")
                before = dict(
                    store.db.execute(
                        "SELECT category,logical_bytes FROM market_counts WHERE category IN (?,?)",
                        numeric_categories,
                    )
                )
                transfer_numeric(store.db, state, min(batch, 256), deadline)
                after = dict(
                    store.db.execute(
                        "SELECT category,logical_bytes FROM market_counts WHERE category IN (?,?)",
                        numeric_categories,
                    )
                )
                for category in numeric_categories:
                    saved = before.get(category, 0) - after.get(category, 0)
                    if saved:
                        state["saved_payload_bytes"] += saved
                        state["saved_categories"][category] = (
                            state["saved_categories"].get(category, 0) + saved
                        )
            elif phase == 11:
                catalogue_repack.initial(store, state, batch, deadline)
            elif phase == 12:
                catalogue_repack.maintenance(store, state, now_ms, batch, deadline)
            elif phase == 13 and state.get("numeric_transfer_complete"):
                search_categories = ("numeric_details", "quality_numeric")
                before = dict(
                    store.db.execute(
                        "SELECT category,logical_bytes FROM market_counts WHERE category IN (?,?)",
                        search_categories,
                    )
                )
                state["numeric_search_complete"] = numeric_search.work(
                    store.db, state, min(batch, 256), deadline
                )
                after = dict(
                    store.db.execute(
                        "SELECT category,logical_bytes FROM market_counts WHERE category IN (?,?)",
                        search_categories,
                    )
                )
                for category in search_categories:
                    saved = before.get(category, 0) - after.get(category, 0)
                    if saved:
                        state["saved_payload_bytes"] += saved
                        state["saved_categories"][category] = (
                            state["saved_categories"].get(category, 0) + saved
                        )
            elif (
                phase == 14
                and all(state.get(k) for k in (*catalogue_repack.PREREQUISITES, "catalogue_complete"))
                and state.get("parent_affinity", {}).get("initial_complete")
            ):
                catalogue_compaction.advance(
                    store, state.setdefault("packet_bundles", {}), min(batch, 256), deadline
                )
            elif (
                phase == 15
                and state.get("numeric_transfer_complete")
                and state.get("numeric_search_complete")
            ):
                numeric_refs.advance(
                    store.db, state.setdefault("numeric_refs", {}), min(batch, 256), deadline
                )
            elif phase == 16 and state.get("quality_complete") and state.get("quality_legacy_empty"):
                quality_frames.advance(
                    store.db,
                    state.setdefault("quality_frames", {}),
                    min(batch, 256),
                    deadline,
                    now_ms=now_ms,
                )
            elif phase == 17:
                context_tile_maintenance.advance(
                    store.db,
                    state.setdefault("context_tiles", {}),
                    min(batch, 256),
                    deadline,
                    now_ms=now_ms,
                )
            elif phase == 18:
                parent_affinity.advance(
                    store.db,
                    state.setdefault("parent_affinity", {}),
                    min(batch, 256),
                    deadline,
                    store.cached_bytes,
                )
            elif (
                phase == 19
                and state.get("numeric_search_complete")
                and state.get("numeric_refs", {}).get("complete")
            ):
                numeric_frame_work.advance(
                    store.db,
                    state.setdefault("numeric_frames", {}),
                    min(batch, 256),
                    deadline,
                    now_ms=now_ms,
                    cache=store.cached_bytes,
                )
            if (
                phase == 9
                and time.monotonic() < deadline
                and column_maintenance["enabled"]
                and (not defer_compaction or not state.get("source_column_initial_reclaim_complete"))
            ):
                before_columns = source_column_work.checkpoint(store.db)["saved_bytes"]
                after_columns = source_column_work.advance(
                    store.db, now_ms, deadline=deadline, batch=min(batch, 8)
                )
                saved_columns = after_columns["saved_bytes"] - before_columns
                state["saved_payload_bytes"] += saved_columns
                state["saved_categories"]["source_blocks"] = (
                    state["saved_categories"].get("source_blocks", 0) + saved_columns
                )
        column_maintenance = source_column_work.status(store.db)
        state["source_column_maintenance"] = column_maintenance
        body_complete = (
            all(state.get(k) for k in (*catalogue_repack.PREREQUISITES, "catalogue_complete"))
            and state.get("packet_bundles", {}).get("initial_complete")
            and state.get("numeric_refs", {}).get("complete")
        )
        state.update(
            state="COMPLETE"
            if (
                body_complete
                and state.get("quality_frames", {}).get("initial_complete")
                and state.get("context_tiles", {}).get("initial_complete")
                and state.get("parent_affinity", {}).get("initial_complete")
                and state.get("numeric_frames", {}).get("initial_complete")
                and not column_maintenance["pending_work"]
            )
            else "IN_PROGRESS",
            at_ms=now_ms,
        )
        if store.db.execute("PRAGMA freelist_count").fetchone()[0] > 256:
            store.db.execute("PRAGMA incremental_vacuum(32)").fetchall()
        if "catalogue_transfer" in state:
            maintenance = catalogue_repack.status(store, now_ms)
            previous = state.get("catalogue_maintenance", {}).get("last_progress_ms")
            if previous is not None:
                maintenance["last_progress_ms"] = previous
            state["catalogue_maintenance"] = maintenance
            if body_complete and not maintenance["pending_work"]:
                state["initial_reclaim_complete"] = True
        state["quality_frame_maintenance"] = quality_frames.status(store.db, now_ms)
        if (
            state.get("quality_frames", {}).get("initial_complete")
            and not state["quality_frame_maintenance"]["pending_work"]
            and not maintenance["pending_work"]
        ):
            state["quality_frame_initial_reclaim_complete"] = True
        state["context_tile_maintenance"] = context_tile_maintenance.status(store.db, now_ms)
        if (
            state.get("context_tiles", {}).get("initial_complete")
            and not state["context_tile_maintenance"]["pending_work"]
            and not maintenance["pending_work"]
        ):
            state["context_tile_initial_reclaim_complete"] = True
        if state.get("parent_affinity", {}).get("initial_complete") and not maintenance["pending_work"]:
            state["parent_affinity_initial_reclaim_complete"] = True
        state["numeric_frame_maintenance"] = numeric_frame_work.status(store.db, now_ms)
        if (
            state.get("numeric_frames", {}).get("initial_complete")
            and not state["numeric_frame_maintenance"]["pending_work"]
            and not maintenance["pending_work"]
        ):
            state["numeric_frame_initial_reclaim_complete"] = True
        if (
            column_maintenance["enabled"]
            and not column_maintenance["pending_work"]
            and not maintenance["pending_work"]
        ):
            state["source_column_initial_reclaim_complete"] = True
        store.put_runtime("storage_repack", state)
    return state
