"""Logical domain reads, separate from opaque physical packet operations.

The legacy path is byte-identical. Shared descriptor expansion belongs here
when explicitly installed; physical append/relocate/read keep their contract.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import packet_store


def read(db: sqlite3.Connection, identity: int, cache: Any = None) -> bytes:
    return packet_store.read(db, identity)
