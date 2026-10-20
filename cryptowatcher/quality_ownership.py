"""Pure inline ownership summaries; database dependencies are never cached.

Only the existing Store byte cache may retain a summary. QF1 resolves and
authenticates its actual current container first. Each invocation validates
current dictionary bytes/shape; numeric leases remain the separate SQL owner's
responsibility, exactly as in the uncached extractor.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import compact_json, indexed_frame, quality_vector
from .record_codec import MAX_BYTES


def _summary(rows: Any, columns: Any) -> dict[str, Any]:
    from .quality_packets import metric_reference, positive

    if not isinstance(rows, list) or not 0 < len(rows) <= 128:
        raise ValueError("Invalid quality packet")
    templates = set()
    numbers = set()
    pool = set()
    if columns is not None:
        pool.add(positive(columns))
    for value in rows:
        if not isinstance(value, list) or len(value) != (4 if columns is not None else 6):
            raise ValueError("Invalid quality fact row")
        template, own, comparisons, slots = value[:4]
        template = positive(template)
        pool.add(template)
        if not isinstance(slots, list):
            raise ValueError("Invalid quality parameterized template")
        templates.add((template, len(slots)))
        references = quality_vector.unpack(comparisons)
        metric_reference(own, views=True)
        for reference in (own, *references):
            if type(reference) is int:
                numbers.add(reference // 4)
    return {
        "n": len(rows),
        "columns": columns,
        "templates": [list(value) for value in sorted(templates)],
        "pool": sorted(pool),
        "numeric": sorted(numbers),
    }


def sql_ownership(db: sqlite3.Connection, raw: bytes, binary_cache: Any = None) -> dict[str, list[int]]:
    from .quality_frames import MAGIC as FRAME_MAGIC
    from .quality_frames import checked_entry, entry_bytes
    from .quality_packets import MAGIC as INLINE_MAGIC
    from .quality_packets import _template_count, ownership, packet, row

    if binary_cache is None or not raw.startswith((INLINE_MAGIC, FRAME_MAGIC)):
        return ownership(db, raw)
    framed = raw.startswith(FRAME_MAGIC)
    source = entry_bytes(db, raw, binary_cache) if framed else raw

    def extract() -> bytes:
        if framed:
            value = checked_entry(compact_json.unpack(source, indexed_frame.MAX_BYTES))
            rows, columns = value[3]
        else:
            rows, columns = packet(source)
        return compact_json.pack(_summary(rows, columns))

    encoded = binary_cache("quality-inline-owners", "QF1" if framed else "CQ5", source, extract)
    summary = compact_json.unpack(encoded, MAX_BYTES)
    current: dict[Any, Any] = {}
    if summary["columns"] is not None:
        values, parent, depth = row(db, summary["columns"], "columns", current)
        if parent is not None or depth or not isinstance(values, list) or len(values) != summary["n"]:
            raise ValueError("Invalid quality columns")
    for identity, count in summary["templates"]:
        _template_count(db, identity, count, current)
    return {"pool": summary["pool"], "numeric": summary["numeric"]}
