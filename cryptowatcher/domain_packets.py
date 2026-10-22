"""Logical domain reads, separate from opaque physical packet operations.

The legacy path is byte-identical. Explicitly installed DS2 descriptors expand
here; physical append/relocate/read keep their opaque-byte contract.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import packet_store


def read(db: sqlite3.Connection, identity: int, cache: Any = None) -> bytes:
    raw = packet_store.read(db, identity)
    if raw.startswith(b"DS2\0"):
        from .shared_records import expand

        return expand(db, identity, raw, cache)
    return raw
