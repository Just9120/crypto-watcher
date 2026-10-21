"""Pure inline ownership summaries; database dependencies are never cached.

Only the existing Store byte cache may retain a summary. QF1 resolves and
authenticates its actual current container first. Each invocation validates
current dictionary bytes/shape; numeric leases remain the separate SQL owner's
responsibility, exactly as in the uncached extractor.
"""

from __future__ import annotations

import sqlite3
import struct
from typing import Any

from . import compact_json, indexed_frame, quality_vector
from .record_codec import MAX_BYTES


def _dictionary_count(db: sqlite3.Connection, identity: int, kind: str, binary_cache: Any) -> int:
    from .quality_packets import MAXIMUM, _template_value, packed, positive, signature

    found = db.execute(
        "SELECT kind,checksum,payload,parent,depth,hash FROM market_quality_dictionary WHERE id=?",
        (positive(identity),),
    ).fetchone()
    if found is None or found[0] != kind or found[3] is not None or found[4]:
        raise ValueError("Missing or invalid quality dictionary binding")
    raw = bytes(found[2])
    if signature(raw) != bytes(found[1]):
        raise ValueError("Corrupt quality dictionary bytes")

    def describe() -> bytes:
        value = compact_json.unpack(raw, MAXIMUM)
        if kind == "columns":
            if not isinstance(value, list):
                raise ValueError("Invalid quality columns")
            count = len(value)
        else:
            if (
                kind != "template2"
                or not isinstance(value, list)
                or len(value) != 2
                or not isinstance(value[1], list)
            ):
                raise ValueError("Invalid quality parameterized template")
            count = len(value[1])
            _template_value(value, None, 0, count)
        return signature(packed(value)) + struct.pack(">Q", count)

    # Only a pure validation result for these actual bytes is retained. Kind,
    # parent, depth, checksum, canonical hash and existence are current on every
    # call, including blob writes that do not alter Connection.total_changes.
    encoded = binary_cache("quality-dictionary-shape", kind, raw, describe)
    if not isinstance(encoded, bytes) or len(encoded) != 16 or encoded[:8] != bytes(found[5]):
        raise ValueError("Quality dictionary content mismatch")
    return struct.unpack(">Q", encoded[8:])[0]


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
    from .quality_packets import ownership, packet

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
    if summary["columns"] is not None:
        count = _dictionary_count(db, summary["columns"], "columns", binary_cache)
        if count != summary["n"]:
            raise ValueError("Invalid quality columns")
    for identity, count in summary["templates"]:
        if type(count) is not int or count != _dictionary_count(db, identity, "template2", binary_cache):
            raise ValueError("Invalid quality parameterized template")
    return {"pool": summary["pool"], "numeric": summary["numeric"]}
