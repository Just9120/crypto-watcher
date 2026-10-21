"""Bounded private regrouping with unchanged logical records and retention.

Capture never changes state. Relocation requires the caller's writer transaction
and checks every actual binding/value/head/anchor again before any deletion.
Existing SQL guards own reference release/admission; no GC runs in the gap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import packet_store, record_blocks, record_catalog
from .catalogue_sql import read_snapshot
from .storage import StateError, Store, encode

Binding = tuple[int, int, int | None, int | None, int]


@dataclass(frozen=True)
class Record:
    coordinate: record_blocks.Coordinate
    binding: Binding
    expires: int
    value: dict[str, Any]
    projection: dict[str, Any] | None

    @property
    def charge(self) -> int:
        return len(encode(self.value).encode()) + len(encode(self.projection).encode()) + 128


def capture(store: Store, coordinate: record_blocks.Coordinate) -> Record:
    with read_snapshot(store):
        saved = record_catalog.row(store.db, coordinate)
        if saved is None:
            raise StateError("Missing relocation record; data preserved")
        packet, slot, parent, expires, current = saved
        raw = packet_store.read(store.db, packet)
        value = record_blocks.read(raw, slot, coordinate, store.cached_bytes)
        projected = record_blocks.projections(raw)[slot][1]
        if parent is not None:
            root = store.db.execute(
                "SELECT expires FROM market_record_parents WHERE id=?", (parent,)
            ).fetchone()
            if root is None:
                raise StateError("Missing relocation parent; data preserved")
            expires = root[0]
        if type(expires) is not int:
            raise StateError("Missing relocation deadline; data preserved")
        return Record(coordinate, tuple(saved), expires, value, projected)


def _same(store: Store, expected: Record) -> None:
    actual = capture(store, expected.coordinate)
    if (
        actual.binding != expected.binding
        or actual.expires != expected.expires
        or encode(actual.value) != encode(expected.value)
        or encode(actual.projection) != encode(expected.projection)
    ):
        raise StateError("Stale or changed relocation snapshot; data preserved")


def relocate(store: Store, records: list[Record]) -> int:
    """Return the new/existing packet ID; an unchanged fresh retry has no DML.

    Failed validation/deletion/publication rolls back only this savepoint. The
    caller can preserve its other changes. Obsolete snapshots must be recaptured.
    """
    if store.readonly or not store.db.in_transaction:
        raise StateError("Relocation requires an outer writer transaction")
    if not 0 < len(records) <= record_blocks.MAX_RECORDS:
        raise StateError("Relocation record cardinality bound")
    if len({r.coordinate for r in records}) != len(records):
        raise StateError("Duplicate relocation coordinate")
    charge = 0
    for record in records:
        record.coordinate.validate()
        binding = record.binding
        if (
            len(binding) != 5
            or type(binding[0]) is not int
            or not 0 < binding[0] < 2**63
            or type(binding[1]) is not int
            or not 0 <= binding[1] < 64
            or type(binding[4]) is not int
            or binding[4] not in (0, 1)
            or (binding[2] is None) == (binding[3] is None)
            or binding[2] is not None
            and (type(binding[2]) is not int or not 0 < binding[2] < 2**63)
            or binding[3] is not None
            and type(binding[3]) is not int
            or type(record.expires) is not int
        ):
            raise StateError("Invalid relocation binding; data preserved")
        charge += record.charge
        if charge > record_blocks.MAX_EXPANDED:
            raise StateError("Relocation expanded group bound")
    buckets = {r.expires // 86400000 for r in records}
    if len(buckets) != 1:
        raise StateError("Relocation crosses retention day")
    entries = [(r.coordinate, r.value, r.projection) for r in records]
    proposed = record_blocks.pack(entries)
    with packet_store.atomic(store.db):
        for record in records:
            _same(store, record)
        packets = {r.binding[0] for r in records}
        if len(packets) == 1:
            packet = next(iter(packets))
            if packet_store.read(store.db, packet) == proposed:
                return packet
        # Deletion releases each logical numeric/window lease and clears its
        # old owner bit through existing guards. Keep parents/membership intact.
        for record in records:
            coordinate, binding = record.coordinate, record.binding
            deleted = store.db.execute(
                "DELETE FROM market_records WHERE kind=? AND public_uuid=? AND revision=? "
                "AND packet=? AND slot=? AND current=? AND expires IS ?",
                (
                    record_blocks.KINDS.index(coordinate.kind),
                    record_catalog.binary(coordinate.identity),
                    coordinate.revision,
                    binding[0],
                    binding[1],
                    binding[4],
                    binding[3],
                ),
            ).rowcount
            if deleted != 1:
                raise StateError("Concurrent relocation change; data preserved")
        added = record_catalog.append(
            store.db,
            entries,
            bucket=next(iter(buckets)),
            owners={r.coordinate: r.binding[2] for r in records},
            deadlines={r.coordinate: r.binding[3] for r in records},
            current=[r.coordinate for r in records if r.binding[4]],
        )
        if added != len(records):
            raise StateError("Incomplete relocation publication; data preserved")
        published: int | None = None
        for previous in records:
            actual = capture(store, previous.coordinate)
            if published is None:
                published = actual.binding[0]
            if (
                actual.binding[0] != published
                or actual.binding[2:] != previous.binding[2:]
                or actual.expires != previous.expires
                or encode(actual.value) != encode(previous.value)
                or encode(actual.projection) != encode(previous.projection)
            ):
                raise StateError("Relocation readback mismatch; data preserved")
        assert published is not None
        if packet_store.read(store.db, published) != proposed:
            raise StateError("Relocation frame changed; data preserved")
        return published
