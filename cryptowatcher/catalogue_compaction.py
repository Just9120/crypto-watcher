"""Bounded parent bundles and dead-slot trimming in the existing repack worker.

The finite upgrade visits the original packet ID ceiling. Later arrivals retain
their own enqueue clocks and use the same worker, without delaying hot transfer.
Oversized parent bundles remain readable and are recorded as explicit fallbacks.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from . import owner_slots, packet_bundles, packet_store, record_blocks, record_catalog, record_relocation
from .indexed_frame import FrameSizeError
from .storage import StateError, Store


def validate(state: dict[str, Any]) -> None:
    if (
        not isinstance(state, dict)
        or state.get("version", 1) != 1
        or type(state.get("version", 1)) is not int
    ):
        raise StateError("Unknown bundle compaction checkpoint")
    for key in (
        "upper",
        "packets_examined",
        "records_moved",
        "packets_relocated",
        "arena_steps",
        "fallbacks",
    ):
        if key in state and (type(state[key]) is not int or not 0 <= state[key] < 2**63):
            raise StateError("Invalid bundle compaction boundary or progress")
    if "initial_complete" in state and type(state["initial_complete"]) is not bool:
        raise StateError("Invalid bundle compaction completion")
    if state.get("initial_complete") and "upper" not in state:
        raise StateError("Missing bundle compaction ceiling")


@dataclass(frozen=True)
class _Seed:
    coordinate: record_blocks.Coordinate
    binding: record_relocation.Binding
    expires: int


def _live(store: Store, packet: int) -> list[_Seed]:
    raw = packet_store.read(store.db, packet)
    records = []
    for slot, (coordinate, _) in enumerate(record_blocks.projections(raw)):
        saved = record_catalog.row(store.db, coordinate)
        if saved is None or saved[0] != packet:
            continue
        if saved[1] != slot:
            raise StateError("Conflicting retained packet slot")
        expires = saved[3]
        if saved[2] is not None:
            parent = store.db.execute(
                "SELECT expires FROM market_record_parents WHERE id=?", (saved[2],)
            ).fetchone()
            if parent is None:
                raise StateError("Missing retained seed parent")
            expires = parent[0]
        if type(expires) is not int:
            raise StateError("Missing retained seed deadline")
        records.append(_Seed(coordinate, tuple(saved), expires))
    row = store.db.execute("SELECT refs FROM market_packets WHERE id=?", (packet,)).fetchone()
    if row is None or row[0] != len(records):
        raise StateError("Conflicting retained packet count")
    return records


def _parent(store: Store, parent: int) -> list[record_relocation.Record] | None:
    masks = store.db.execute(
        "SELECT packet,slots,checksum FROM market_record_owners WHERE parent=? ORDER BY packet LIMIT 65",
        (parent,),
    ).fetchall()
    if not masks:
        raise StateError("Missing retained bundle parent")
    if len(masks) > 64:
        return None
    count = 0
    for packet, slots, checksum in masks:
        if owner_slots.signature(parent, packet, slots) != checksum:
            raise StateError("Corrupt bundle owner mask")
        n = owner_slots.bits(slots).bit_count()
        if not n:
            raise StateError("Empty retained bundle owner mask")
        count += n
    if count > 64:
        return None
    result = []
    size = 0
    for packet, slots, _ in masks:
        projections = record_blocks.projections(packet_store.read(store.db, packet))
        if owner_slots.bits(slots) >> len(projections):
            raise StateError("Bundle owner slot exceeds its packet")
        for slot, (coordinate, _) in enumerate(projections):
            if not owner_slots.has(slots, slot):
                continue
            record = record_relocation.capture(store, coordinate)
            if record.binding[:3] != (packet, slot, parent):
                raise StateError("Stale bundle owner binding")
            size += record.charge
            if size > record_blocks.MAX_EXPANDED:
                return None
            result.append(record)
    if len(result) != count:
        raise StateError("Incomplete retained bundle")
    return result


def compact(store: Store, packet: int) -> tuple[int, int]:
    """Return moved records/fallbacks, preserving every unselected live slot."""
    with packet_store.atomic(store.db):
        seed = _live(store, packet)
        if not seed:
            packet_bundles.checked(store.db, packet)
            return 0, 0
        expanded: list[record_relocation.Record] = []
        seen: set[int] = set()
        size = 0
        fallback = 0
        day = seed[0].expires // 86400000
        # A complete parent is preferred. A parent exceeding either existing
        # limit keeps its seed subset; no hidden larger memory allowance.
        for record in seed:
            parent = record.binding[2]
            if parent is not None:
                if parent in seen:
                    continue
                seen.add(parent)
                group = _parent(store, parent)
                if group is None:
                    fallback += 1
                    group = [
                        record_relocation.capture(store, r.coordinate) for r in seed if r.binding[2] == parent
                    ]
            else:
                group = [record_relocation.capture(store, record.coordinate)]
            charge = sum(r.charge for r in group)
            if (
                all(r.expires // 86400000 == day for r in group)
                and len(expanded) + len(group) <= record_blocks.MAX_RECORDS
                and size + charge <= record_blocks.MAX_EXPANDED
            ):
                expanded.extend(group)
                size += charge
        if not expanded:
            raise StateError("No bounded seed bundle")
        original_slots = len(record_blocks.projections(packet_store.read(store.db, packet)))
        other_packets = {r.binding[0] for r in expanded} - {packet}
        # An intact packet with no fragmented parents is already settled. Do
        # not churn it solely to change ordering or rewrite identical bodies.
        if not other_packets and original_slots == len(seed):
            packet_bundles.checked(store.db, packet)
            return 0, fallback
        try:
            published = record_relocation.relocate(store, expanded)
        except FrameSizeError:
            # Existing packet bound can reject an otherwise valid parent group.
            # Keep its original layout, visible in the fallback telemetry.
            packet_bundles.checked(store.db, packet)
            return 0, fallback + 1
        packet_bundles.checked(store.db, published)
        return len(expanded), fallback


def evacuate(store: Store) -> int | None:
    """Move one exact packet out of a sparse closed arena; keep packet identity.

    Inspect at most32 indexed rows, collecting unreferenced rows in that prefix.
    No full scan, new payload/index copy, or reference gap is introduced.
    """
    with packet_store.atomic(store.db):
        arena = packet_bundles.sparse(store.db)
        if arena is None:
            return None
        rows = store.db.execute(
            "SELECT id,refs FROM market_packets WHERE arena=? ORDER BY id LIMIT 32", (arena,)
        ).fetchall()
        if not rows:
            raise StateError("Missing sparse arena packet")
        moved = False
        for identity, refs in rows:
            if refs == 0:
                store.db.execute("DELETE FROM market_packets WHERE id=? AND refs=0", (identity,))
            elif not moved:
                packet_store.relocate(store.db, identity)
                moved = True
        return int(moved)


def advance(store: Store, state: dict[str, Any], batch: int, deadline: float) -> None:
    validate(state)
    if store.readonly or not store.db.in_transaction or type(batch) is not int or not 0 < batch <= 256:
        raise StateError("Bundle work requires a bounded outer writer transaction")
    packet_bundles.header(store.db)
    if "upper" not in state:
        row = store.db.execute("SELECT id FROM market_packets ORDER BY id DESC LIMIT 1").fetchone()
        state.update(version=1, upper=row[0] if row is not None else 0)
    upper = state["upper"]
    if not state.get("initial_complete"):
        packet_bundles.tag(store.db, upper, min(batch, 256))
    for _ in range(min(batch, 32)):
        if time.monotonic() >= deadline:
            break
        if state.get("initial_complete"):
            pending = store.db.execute(
                "SELECT id FROM market_packets WHERE bundle_checked=0 AND refs>0 "
                "ORDER BY bundle_since,id LIMIT 1"
            ).fetchone()
        else:
            pending = store.db.execute(
                "SELECT id FROM market_packets WHERE bundle_checked=0 AND refs>0 AND id<=? ORDER BY id LIMIT 1",
                (upper,),
            ).fetchone()
        if pending is None:
            break
        moved, fallbacks = compact(store, pending[0])
        state["packets_examined"] = state.get("packets_examined", 0) + 1
        state["records_moved"] = state.get("records_moved", 0) + moved
        state["fallbacks"] = state.get("fallbacks", 0) + fallbacks
    if not state.get("initial_complete"):
        unknown = store.db.execute(
            "SELECT 1 FROM market_packets WHERE bundle_checked IS NULL AND id<=? LIMIT 1", (upper,)
        ).fetchone()
        pending = store.db.execute(
            "SELECT 1 FROM market_packets WHERE bundle_checked=0 AND refs>0 AND id<=? LIMIT 1", (upper,)
        ).fetchone()
        state["initial_complete"] = unknown is None and pending is None
    current = packet_bundles.header(store.db)
    if state.get("initial_complete") and not current["count"] and not current["unknown"]:
        for _ in range(min(batch, 32)):
            if time.monotonic() >= deadline:
                break
            moved_packets = evacuate(store)
            if moved_packets is None:
                break
            state["packets_relocated"] = state.get("packets_relocated", 0) + moved_packets
            state["arena_steps"] = state.get("arena_steps", 0) + 1
