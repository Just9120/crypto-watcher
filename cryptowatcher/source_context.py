"""Public ticker/instrument snapshots with explicit historical availability."""

from __future__ import annotations

import json
from typing import Any

from .storage import Store, encode


def prefix(kind: str, instrument: str) -> str:
    return f"source-context:v1:{kind}:{instrument}:"


def save(store: Store, kind: str, instrument: str, value: dict[str, Any]) -> None:
    if store.bounded:
        from .market_storage import save_context

        save_context(store.db, kind, instrument, value)
        return
    key = prefix(kind, instrument) + f"{value['available_at_ms']:016d}"
    previous = store.get_runtime(key)
    if previous is not None and encode(previous) != encode(value):
        raise ValueError("Conflicting public source snapshot")
    store.db.execute("INSERT OR IGNORE INTO runtime VALUES (?,?)", (key, encode(value)))


def at(store: Store, kind: str, instrument: str, cutoff_ms: int) -> dict[str, Any] | None:
    if store.bounded:
        from .market_storage import read_context

        return read_context(store.db, kind, instrument, cutoff_ms)
    start = prefix(kind, instrument)
    row = store.db.execute(
        "SELECT payload_json FROM runtime WHERE key>=? AND key<=? ORDER BY key DESC LIMIT 1",
        (start, start + f"{cutoff_ms:016d}"),
    ).fetchone()
    return json.loads(row[0]) if row else None


def context(store: Store, instrument: str, cutoff_ms: int) -> dict[str, Any]:
    return {kind: at(store, kind, instrument, cutoff_ms) for kind in ("ticker", "instrument")}
