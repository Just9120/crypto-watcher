"""Known domain SQL sources over native cold ranges and checked mixed points.

No table, view, marker or migration is installed here. SQL aggregation/export
keeps its existing connection and snapshot, without collecting records in Python.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from . import catalogue_reader, record_blocks
from .storage import Store, encode


@contextmanager
def read_snapshot(store: Store) -> Iterator[None]:
    """Own only a newly opened read transaction; preserve any caller transaction."""
    opened = not store.db.in_transaction
    if opened:
        store.db.execute("BEGIN")
    try:
        yield
    finally:
        if opened:
            store.db.execute("ROLLBACK")


def public_key(kind: int, identity: bytes, revision: int, head: int) -> str:
    if (
        type(kind) is not int
        or not 0 <= kind < len(record_blocks.KINDS)
        or type(head) is not int
        or head not in (0, 1)
    ):
        raise ValueError("Invalid SQL domain coordinate")
    coordinate = record_blocks.Coordinate(record_blocks.KINDS[kind], str(uuid.UUID(bytes=identity)), revision)
    coordinate.validate()
    suffix = coordinate.key
    if head:
        suffix = (
            "decision:telegram:" if coordinate.kind == "decision" else coordinate.kind + "-head:"
        ) + coordinate.identity
    return catalogue_reader.PREFIX + suffix


def install_functions(store: Store) -> None:
    def payload(key: str | None) -> str | None:
        if key is None:
            return None
        missing = object()
        value = store.get_runtime(key, missing)
        return None if value is missing else encode(value)

    # Payload depends on the caller's current snapshot; never mark deterministic.
    store.db.create_function("cw_runtime_payload", 1, payload)
    store.db.create_function("cw_catalogue_public_key", 4, public_key, deterministic=True)


def source(store: Store, kind: str, *, head: bool = False) -> str:
    if kind not in ("event", "outcome", "episode", "comparison") or type(head) is not bool:
        raise ValueError("Unsupported SQL domain namespace")
    if not store.bounded or not catalogue_reader.reading(store.db):
        return "runtime"
    root = catalogue_reader.PREFIX + kind + ("-head:" if head else ":")
    # All SQL literals below come from the closed internal namespace dictionary.
    # Public filters and bounds remain parameters in each existing consumer.
    key = f"cw_catalogue_public_key(r.kind,r.public_uuid,r.revision,{int(head)})"
    current = " AND r.current=1" if head else ""
    return (
        "(SELECT h.key,cw_runtime_payload(h.key) AS payload_json FROM main.runtime h "
        f"WHERE h.key>='{root}' AND h.key<'{root}~' UNION ALL "
        f"SELECT {key},cw_runtime_payload({key}) FROM market_records r "
        f"WHERE r.kind={record_blocks.KINDS.index(kind)}{current} "
        f"AND NOT EXISTS(SELECT 1 FROM main.runtime h WHERE h.key={key}))"
    )
