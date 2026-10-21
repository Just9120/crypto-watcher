"""Explicit bounded boundary counts for domain rows outside runtime.

Logical coordinates are reported separately from payload/page bytes. They must
not be added again to DBSTAT or file sizes. The caller owns snapshot/deadline.
"""

from __future__ import annotations

from typing import Any

from . import catalogue_lifecycle, catalogue_overlap, catalogue_queue, record_blocks, record_masks
from .storage import StateError, Store


def measure(store: Store, hot: dict[str, dict[str, int]]) -> dict[str, Any]:
    if not catalogue_lifecycle.active(store.db):
        return {"state": "N/A_INACTIVE_CATALOGUE"}
    actual = dict(
        (kind, (n, heads))
        for kind, n, heads in store.db.execute(
            "SELECT kind,count(*),sum(current) FROM market_records GROUP BY kind"
        )
    )
    counters = store.db.execute("SELECT kind,n,heads FROM market_catalogue_counts ORDER BY kind").fetchall()
    if [row[0] for row in counters] != list(range(5)) or any(
        any(type(value) is not int for value in row) or tuple(row[1:]) != actual.get(row[0], (0, 0))
        for row in counters
    ):
        raise StateError("Conflicting actual catalogue domain count")
    n = sum(row[1] for row in counters)
    row = store.db.execute(
        "SELECT records,logical_bytes FROM market_counts WHERE category='record_catalog'"
    ).fetchone()
    if (row is None and n) or (row is not None and tuple(row) != (n, n * 56)):
        raise StateError("Conflicting catalogue coordinate byte count")
    indexes = record_masks.tallies(store.db, "record_parents")
    record_masks.tallies(store.db, "record_owners")
    record_masks.tallies(store.db, "record_aliases")
    observed = {
        "parents": store.db.execute("SELECT count(*) FROM market_record_parents").fetchone()[0],
        "owners": store.db.execute("SELECT count(*) FROM market_record_owners").fetchone()[0],
        "history": store.db.execute(
            "SELECT count(*) FROM market_record_parents WHERE history_published=1"
        ).fetchone()[0],
        "membership": store.db.execute("SELECT count(*) FROM market_record_members").fetchone()[0],
    }
    if observed != indexes:
        raise StateError("Conflicting actual catalogue index count")
    overlaps = catalogue_overlap.header(store.db)
    public = {name: values["records"] for name, values in hot.items()}
    for kind, count, heads in counters:
        name = record_blocks.KINDS[kind]
        for suffix, amount in (("", count), ("-head", heads)):
            label = name + suffix
            namespace = "decision" if name == "decision" else label
            if not 0 <= overlaps[label] <= amount:
                raise StateError("Conflicting catalogue overlap occupancy")
            public[namespace] = public.get(namespace, 0) + amount - overlaps[label]
    for label, field in (("history", "history"), ("episode-event", "membership")):
        if not 0 <= overlaps[label] <= observed[field]:
            raise StateError("Conflicting catalogue alias overlap")
        public[label] = public.get(label, 0) + observed[field] - overlaps[label]
    pending: dict[str, Any] = {"state": "N/A_NOT_INSTALLED"}
    if store.db.execute("SELECT 1 FROM market_meta WHERE key='catalogue_queue'").fetchone():
        pending = {"state": "MEASURED", **catalogue_queue.verify(store.db)}
    bundles: dict[str, Any] = {"state": "N/A_NOT_INSTALLED"}
    if store.db.execute("SELECT 1 FROM market_meta WHERE key='packet_bundles'").fetchone():
        from . import packet_bundles

        packet_bundles.validate(store.db)
        bundles = {"state": "MEASURED", **packet_bundles.verify(store.db)}
    return {
        "state": "MEASURED",
        "method": "NATIVE_DOMAIN_AND_INDEX_COUNTS_WITH_CHECKED_TRANSACTIONAL_OVERLAPS",
        "domain_namespaces": [tuple(row) for row in counters],
        "indexes": observed,
        "overlaps": overlaps,
        "pending": pending,
        "bundles": bundles,
        "public_namespace_records": public,
        "public_records_accounted": sum(public.values()),
        "limitations": [
            "Coordinate counts are not physical bytes; payloads and full integrity use separate checks"
        ],
    }
