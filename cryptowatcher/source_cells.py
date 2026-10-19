"""Checked frozen coordinates into immutable authenticated source tiles."""

from __future__ import annotations

import sqlite3
from typing import Any


def convert(db: sqlite3.Connection, reference: dict[str, Any], *, cache: Any = None) -> dict[str, Any] | None:
    """Validate the entire old window hash before replacing its descriptor."""
    from .candle_access import Lookup, installed
    from .market_storage import compact_reference, reference_points, restore_window
    from .source_windows import load
    from .storage import encode

    if not installed(db):
        return None
    if reference.get("mode") == "source-ref":
        if set(reference) != {"mode", "id"}:
            raise ValueError("Invalid source descriptor reference")
        reference = load(db, reference["id"])
    if reference.get("mode") not in ("source", "source-runs"):
        raise ValueError("Unsupported source conversion reference")
    before = restore_window(db, reference, cache=cache)
    points = reference_points(reference)
    reader = Lookup(db, reference["stream"], cache, require_tiles=True)
    values = []
    for start, revision in points:
        value = reader.frozen(start, revision)
        if value is None:
            # Supported legacy correction fallback keeps its old full hash and
            # descriptor. Missing objects/corruption in the old window already
            # failed above; neither is silently called a fallback.
            return None
        values.append(value)
    if encode(values).encode() != before:
        raise ValueError("Source cell conversion changed the frozen window")
    result = compact_reference(reference["stream"], points, reference["sha256"])
    return {"mode": "source-cells", "stream": result["stream"], "runs": result["runs"]}
