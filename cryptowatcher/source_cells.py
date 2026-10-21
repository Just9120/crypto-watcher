"""Checked frozen coordinates into immutable authenticated source tiles."""

from __future__ import annotations

import sqlite3
from typing import Any


def convert(db: sqlite3.Connection, reference: dict[str, Any], *, cache: Any = None) -> dict[str, Any] | None:
    """Validate the entire old window hash before replacing its descriptor."""
    from .candle_access import installed
    from .market_storage import checked_window, compact_reference, reference_points
    from .source_windows import load

    if not installed(db):
        return None
    if reference.get("mode") == "source-ref":
        if set(reference) != {"mode", "id"}:
            raise ValueError("Invalid source descriptor reference")
        reference = load(db, reference["id"])
    if reference.get("mode") not in ("source", "source-runs"):
        raise ValueError("Unsupported source conversion reference")
    _, all_frozen = checked_window(db, reference, cache=cache)
    points = reference_points(reference)
    if not all_frozen:
        # A complete old SHA was checked over actual values first. A supported
        # legacy correction keeps the original descriptor; missing/corrupt old
        # data has already failed. No DB-existence proof survives this call.
        return None
    result = compact_reference(reference["stream"], points, reference["sha256"])
    return {"mode": "source-cells", "stream": result["stream"], "runs": result["runs"]}
