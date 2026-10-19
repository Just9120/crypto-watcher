"""Explicit finite mixed-domain transfer; callers own activation/checkpoint commits.

Opening a Store never calls this module. The initial transfer is separate from
future steady compaction; completing a finite ceiling does not claim zero hot
rows or qualify application capacity.
"""

from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass
from typing import Any

import zstandard

from . import (
    catalogue_aliases,
    catalogue_lifecycle,
    catalogue_reader,
    packet_store,
    record_blocks,
    record_catalog,
)
from .events import DECISION_HEAD, HEAD_SCHEMA, PREFIX, History
from .expiry_index import hot_binding
from .indexed_frame import FrameSizeError
from .storage import StateError, Store, encode, json_payload

VERSION = 1
MAX_BUFFER = 2 * 1024 * 1024
MAX_RECORD = packet_store.MAXIMUM - 65536
# Individual records retain their conservative admission bound. A group may
# use the existing DB2 expanded bound; compressed overflow is split atomically.
MAX_GROUP = record_blocks.MAX_EXPANDED
PHASES = ("bodies", "heads", "aliases")


@dataclass
class Prepared:
    coordinate: record_blocks.Coordinate
    body: dict[str, Any]
    projection: dict[str, Any] | None
    expires: int
    parent: tuple[str, int] | None
    raw: str | bytes
    head: tuple[str, str | bytes] | None


def validate(state: dict[str, Any]) -> None:
    if (
        not isinstance(state, dict)
        or state.get("version", VERSION) != VERSION
        or type(state.get("version", VERSION)) is not int
    ):
        raise StateError("Unknown catalogue transfer checkpoint; data preserved")
    if state.get("phase", "bodies") not in (*PHASES, "complete"):
        raise StateError("Unknown catalogue transfer phase; data preserved")
    for name in PHASES:
        present = {suffix for suffix in ("schema", "next", "upper") if name + "_" + suffix in state}
        if present and len(present) != 3:
            raise StateError("Partial catalogue scan boundary; data preserved")
        for suffix in ("schema", "next", "upper"):
            value = state.get(name + "_" + suffix)
            lower = 0 if suffix == "schema" else -(2**63)
            upper = 2**63 if suffix == "next" else 2**63 - 1
            if value is not None and (type(value) is not int or not lower <= value <= upper):
                raise StateError("Invalid finite catalogue boundary; data preserved")
        if name + "_complete" in state and type(state[name + "_complete"]) is not bool:
            raise StateError("Invalid catalogue phase completion; data preserved")
    index = (
        PHASES.index(state.get("phase", "bodies")) if state.get("phase", "bodies") in PHASES else len(PHASES)
    )
    if any(
        state.get(phase + "_complete") is not True
        or any(phase + "_" + suffix not in state for suffix in ("schema", "next", "upper"))
        for phase in PHASES[:index]
    ):
        raise StateError("Missing completed catalogue phase; data preserved")
    for name in ("scan_generations", "heads_retired", "bodies_moved", "aliases_moved", "rows_examined"):
        value = state.get(name, 0)
        if type(value) is not int or value < 0:
            raise StateError("Invalid catalogue progress counter; data preserved")
    visits = state.get("fallback_visits", {})
    if not isinstance(visits, dict) or any(
        not isinstance(key, str) or type(value) is not int or value < 0 for key, value in visits.items()
    ):
        raise StateError("Invalid catalogue fallback accounting; data preserved")


def _large_envelope(raw: str | bytes) -> bool:
    from .record_codec import BINARY_MAGIC, MARKER, TYPED_MAGIC

    if isinstance(raw, bytes):
        if raw.startswith(TYPED_MAGIC) and len(raw) >= 40:
            return int.from_bytes(raw[4:8], "big") > MAX_RECORD
        if raw.startswith(BINARY_MAGIC):
            try:
                return zstandard.get_frame_parameters(raw[36:]).content_size > MAX_RECORD
            except zstandard.ZstdError as error:
                raise StateError("Invalid transfer record envelope; data preserved") from error
    elif MARKER in raw:
        value = json.loads(raw)
        wrapper = value.get(MARKER) if isinstance(value, dict) else None
        if isinstance(wrapper, dict) and type(wrapper.get("bytes")) is int:
            return wrapper["bytes"] > MAX_RECORD
    return False


def _head_key(coordinate: record_blocks.Coordinate) -> str:
    return (
        PREFIX
        + ("decision:telegram:" if coordinate.kind == "decision" else coordinate.kind + "-head:")
        + coordinate.identity
    )


def _fallback(state: dict[str, Any], reason: str) -> None:
    # These are cumulative visits, not per-key indexes or a remaining gauge.
    counts = state.setdefault("fallback_visits", {})
    counts[reason] = counts.get(reason, 0) + 1


def _drop(store: Store, key: str, raw: str | bytes) -> None:
    from .storage_lifecycle import forget

    forget(store, key)
    if store.db.execute("DELETE FROM runtime WHERE key=? AND payload_json=?", (key, raw)).rowcount != 1:
        raise StateError("Concurrent hot record change during transfer")


def _projection(coordinate: record_blocks.Coordinate, body: dict[str, Any]) -> dict[str, Any] | None:
    if coordinate.kind == "decision":
        return None
    # Bounded HEAD_FIELDS consist only of frozen scalar metadata. Numeric metrics
    # are deliberately excluded from bounded outcome heads by the existing API.
    return History._head(coordinate.kind, body, coordinate.key, bounded=True)


def prepare(store: Store, key: str, raw: str | bytes, state: dict[str, Any]) -> Prepared | None:
    selected = catalogue_reader.point(key)
    if selected is None or selected.revision is None:
        return None
    coordinate = record_blocks.Coordinate(selected.kind, selected.identity, selected.revision)
    if _large_envelope(raw):
        _fallback(state, "oversized_expansion")
        return None
    body = json_payload(raw)
    if not isinstance(body, dict):
        raise StateError("Invalid immutable domain body; data preserved")
    if (
        body.get(coordinate.field) != coordinate.identity
        or type(body.get("revision")) is not int
        or body["revision"] != coordinate.revision
    ):
        _fallback(state, "unrecognized_domain_identity")
        return None
    record_blocks.compact(coordinate, body)
    if len(encode(body).encode()) + 128 > MAX_RECORD:
        _fallback(state, "oversized_record")
        return None
    if coordinate.kind == "episode" and not catalogue_lifecycle.renewable(body):
        _fallback(state, "episode_external_lease")
        return None
    binding = hot_binding(store.db, key)
    if binding is None:
        _fallback(state, "missing_hot_lease")
        return None
    expires, category, event_id = binding
    if category != coordinate.kind:
        _fallback(state, "unknown_lease_category")
        return None
    parent = None
    if event_id is not None:
        root = History(store).metadata("event-head:" + event_id)
        if (
            root is None
            or root.get("event_id") != event_id
            or type(root.get("market_observation_ms")) is not int
        ):
            _fallback(state, "missing_parent_metadata")
            return None
        try:
            record_catalog.binary(event_id)
        except ValueError:
            _fallback(state, "legacy_parent_identity")
            return None
        root_lease = catalogue_lifecycle.parent_expiry(store.db, event_id)
        if root_lease is None:
            saved = hot_binding(store.db, PREFIX + "event-head:" + event_id)
            root_lease = saved[0] if saved is not None else None
        if root_lease != expires or body.get("event_id") != event_id:
            _fallback(state, "distinct_parent_lease")
            return None
        parent = event_id, root["market_observation_ms"]
    elif coordinate.kind != "episode":
        _fallback(state, "unbound_domain_record")
        return None
    projection = None
    head = None
    head_key = _head_key(coordinate)
    saved_head = store.db.execute("SELECT payload_json FROM runtime WHERE key=?", (head_key,)).fetchone()
    if saved_head is not None:
        value = json_payload(saved_head[0])
        number = catalogue_reader.revision(
            value, catalogue_reader.Point(coordinate.kind, coordinate.identity, None)
        )
        if number == coordinate.revision:
            if coordinate.kind == "decision":
                if value != {DECISION_HEAD: coordinate.revision} and not catalogue_reader.equal_public(
                    store.db, value, body, store.cached_bytes
                ):
                    raise StateError("Conflicting retained decision head")
            elif value.get("schema") == HEAD_SCHEMA:
                projection = _projection(coordinate, body)
                if encode(value) != encode(projection):
                    raise StateError("Conflicting retained head projection")
            elif catalogue_reader.equal_public(store.db, value, body, store.cached_bytes):
                projection = _projection(coordinate, body)
            else:
                raise StateError("Conflicting retained full head")
            head_lease = hot_binding(store.db, head_key)
            if head_lease is not None and head_lease[0] == expires:
                head = head_key, saved_head[0]
            else:
                _fallback(state, "distinct_head_lease")
    old = record_catalog.row(store.db, coordinate)
    if old is not None:
        previous = record_catalog.read(store.db, coordinate, store.cached_bytes)
        if encode(previous) != encode(body):
            if not catalogue_reader.equal_public(store.db, previous, body, store.cached_bytes):
                raise StateError("Conflicting immutable transfer retry")
            _fallback(state, "existing_private_variant")
            return None
        previous_projection = record_blocks.projected(
            packet_store.read(store.db, old[0]), old[1], coordinate, store.cached_bytes
        )
        if encode(previous_projection) != encode(projection):
            projection = previous_projection
            head = None
            _fallback(state, "existing_projection_variant")
    cost = len(encode(body).encode()) + len(encode(projection).encode()) + 128
    if cost > MAX_RECORD:
        _fallback(state, "oversized_projection")
        return None
    return Prepared(coordinate, body, projection, expires, parent, raw, head)


def _flush(store: Store, entries: list[Prepared], state: dict[str, Any]) -> None:
    if not entries:
        return
    owners = {
        entry.coordinate: record_catalog.parent(store.db, entry.parent[0], entry.parent[1], entry.expires)
        if entry.parent is not None
        else None
        for entry in entries
    }
    try:
        record_catalog.append(
            store.db,
            [(entry.coordinate, entry.body, entry.projection) for entry in entries],
            bucket=entries[0].expires // 86400000,
            owners=owners,
            deadlines={
                entry.coordinate: None if entry.parent is not None else entry.expires for entry in entries
            },
            current=[entry.coordinate for entry in entries if entry.head is not None],
        )
    except FrameSizeError:
        if len(entries) == 1:
            raise
        middle = len(entries) // 2
        _flush(store, entries[:middle], state)
        _flush(store, entries[middle:], state)
        return
    for entry in entries:
        actual = record_catalog.read(store.db, entry.coordinate, store.cached_bytes)
        if encode(actual) != encode(entry.body):
            raise StateError("Catalogue transfer changed immutable private values")
        _drop(store, PREFIX + entry.coordinate.key, entry.raw)
        if entry.head is not None:
            _drop(store, *entry.head)
            state["heads_retired"] = state.get("heads_retired", 0) + 1
        state["bodies_moved"] = state.get("bodies_moved", 0) + 1


def _bounds(store: Store, state: dict[str, Any], phase: str) -> tuple[int, int]:
    generation = store.db.execute("PRAGMA schema_version").fetchone()[0]
    if state.get(phase + "_schema") != generation:
        low = store.db.execute("SELECT rowid FROM runtime ORDER BY rowid LIMIT 1").fetchone()
        high = store.db.execute("SELECT rowid FROM runtime ORDER BY rowid DESC LIMIT 1").fetchone()
        state[phase + "_next"] = low[0] if low else 1
        state[phase + "_upper"] = high[0] if high else 0
        state[phase + "_schema"] = generation
        state["scan_generations"] = state.get("scan_generations", 0) + 1
    return state[phase + "_next"], state[phase + "_upper"]


def advance(store: Store, state: dict[str, Any], batch: int, deadline: float) -> bool:
    validate(state)
    if type(batch) is not int or not 1 <= batch <= 256 or not store.db.in_transaction:
        raise StateError("Catalogue transfer requires a bounded outer writer checkpoint")
    if not catalogue_lifecycle.active(store.db):
        raise StateError("Catalogue transfer requires the complete explicit runtime adapter")
    if state.get("phase") == "complete":
        return True
    if time.monotonic() >= deadline:
        return False
    working = copy.deepcopy(state)
    working.setdefault("version", VERSION)
    working.setdefault("phase", "bodies")
    with packet_store.atomic(store.db):
        # Later phases keep the same finite admission boundary. Only a schema
        # rebuild recaptures unfinished intervals after physical row relocation.
        for phase in PHASES:
            if not working.get(phase + "_complete"):
                _bounds(store, working, phase)
        _advance(store, working, batch, deadline)
    state.clear()
    state.update(working)
    return state["phase"] == "complete"


def _advance(store: Store, state: dict[str, Any], batch: int, deadline: float) -> None:
    phase = state["phase"]
    low, high = _bounds(store, state, phase)
    metadata = (
        store.db.execute(
            "SELECT rowid,CASE WHEN length(key)<=8192 THEN key END,length(CAST(payload_json AS BLOB)) FROM runtime NOT INDEXED WHERE rowid>=? AND rowid<=? ORDER BY rowid LIMIT ?",
            (low, high, batch),
        ).fetchall()
        if low <= high
        else []
    )
    entries: list[Prepared] = []
    buffered = consumed = visited = 0
    for rid, key, size in metadata:
        if visited and time.monotonic() >= deadline:
            break
        selected = catalogue_reader.point(key) if key is not None else None
        alias = catalogue_aliases.point(key) if key is not None else None
        eligible = (
            (phase == "bodies" and selected is not None and selected.revision is not None)
            or (phase == "heads" and selected is not None and selected.revision is None)
            or (phase == "aliases" and alias is not None)
        )
        if eligible and consumed + size > MAX_BUFFER:
            if not visited:
                _fallback(state, "oversized_envelope")
            else:
                break
        elif eligible:
            saved = store.db.execute(
                "SELECT payload_json FROM runtime WHERE rowid=? AND key=?", (rid, key)
            ).fetchone()
            if saved is None:
                raise StateError("Concurrent catalogue scan change")
            raw = saved[0]
            consumed += size
            if phase == "bodies":
                entry = prepare(store, key, raw, state)
                if entry is not None:
                    cost = len(encode(entry.body).encode()) + len(encode(entry.projection).encode()) + 128
                    if entries and (
                        len(entries) == record_blocks.MAX_RECORDS
                        or buffered + cost > MAX_GROUP
                        or entries[0].expires // 86400000 != entry.expires // 86400000
                    ):
                        _flush(store, entries, state)
                        entries, buffered = [], 0
                    entries.append(entry)
                    buffered += cost
            elif phase == "aliases":
                _flush(store, entries, state)
                entries, buffered = [], 0
                if catalogue_aliases.publish(store.db, key, json_payload(raw), store.cached_bytes):
                    _drop(store, key, raw)
                    state["aliases_moved"] = state.get("aliases_moved", 0) + 1
                else:
                    _fallback(state, "unsupported_binding")
            else:
                # A body transferred before a separately published head may
                # have no frozen projection. Keep that small head hot exactly.
                _fallback(state, "retained_hot_head")
        visited += 1
        state[phase + "_next"] = rid + 1
        state["rows_examined"] = state.get("rows_examined", 0) + 1
    _flush(store, entries, state)
    if not metadata or (
        visited == len(metadata) and (len(metadata) < batch or state[phase + "_next"] > high)
    ):
        state[phase + "_complete"] = True
        index = PHASES.index(phase)
        state["phase"] = PHASES[index + 1] if index + 1 < len(PHASES) else "complete"
