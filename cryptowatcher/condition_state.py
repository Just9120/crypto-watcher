"""Bounded per-stream mutable detector state, with independent member expiry.

Immutable events and public interfaces keep their existing representation.
Legacy condition rows remain readable and migrate only in an observation's
writer transaction. One batch is ephemeral; no cache or background task is added.
"""

from __future__ import annotations

import copy
import heapq
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator

from .storage import StateError, encode

if TYPE_CHECKING:
    from .events import History

PREFIX = "radar:v1:"
GROUP = "condition-state:"
SCHEMA = "cryptowatcher.condition-state.v1"
MISSING = object()
MEMBERS = frozenset(
    f"{family}:{direction}"
    for family, directions in (
        ("price", ("UP", "DOWN")),
        ("turnover", ("NEUTRAL",)),
        ("breakout", ("UP", "DOWN")),
        ("volatility", ("NEUTRAL",)),
        ("composite", ("UP", "DOWN")),
    )
    for direction in directions
)


def coordinate(key: str) -> tuple[str, str] | None:
    if not key.startswith("condition:"):
        return None
    parts = key.split(":")
    if len(parts) != 4 or ":".join(parts[2:]) not in MEMBERS:
        return None
    stream = parts[1].split("/")
    if (
        len(stream) != 3
        or stream[0] not in ("spot", "linear")
        or not stream[1]
        or not stream[1].isascii()
        or not all(c.isupper() or c.isdigit() or c in "_-" for c in stream[1])
        or stream[2] not in ("1m", "3m", "5m", "15m")
    ):
        return None
    return parts[1], ":".join(parts[2:])


def supported(value: Any) -> bool:
    if not isinstance(value, dict) or not set(value) <= {"last_ms", "non_signal_bars", "blocked"}:
        return False
    return all(type(v) is bool if k == "blocked" else type(v) is int and v >= 0 for k, v in value.items())


def validate(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "states"}
        or value["schema"] != SCHEMA
        or not isinstance(value["states"], dict)
        or not 0 < len(value["states"]) <= len(MEMBERS)
    ):
        raise StateError("Unknown grouped condition state; data preserved")
    for member, record in value["states"].items():
        if (
            member not in MEMBERS
            or not isinstance(record, dict)
            or set(record) != {"value", "expires_ms"}
            or not supported(record["value"])
            or type(record["expires_ms"]) is not int
            or record["expires_ms"] < 0
        ):
            raise StateError("Invalid grouped condition member; data preserved")
    return value


def deadline(value: Any, *, stream: str | None = None) -> int:
    if stream is not None and coordinate(f"condition:{stream}:price:UP") is None:
        raise StateError("Invalid grouped condition owner; data preserved")
    return min(record["expires_ms"] for record in validate(value)["states"].values())


def read(history: History, stream: str) -> dict[str, Any] | None:
    if coordinate(f"condition:{stream}:price:UP") is None:
        raise StateError("Invalid grouped condition owner; data preserved")
    value = history._get_raw(GROUP + stream, MISSING)
    return validate(value) if value is not MISSING else None


class Batch:
    def __init__(self, history: History, stream: str) -> None:
        self.history, self.stream = history, stream
        self.disabled = False
        self.dirty = False
        self.reload()

    def reload(self) -> None:
        from .expiry_index import hot_binding

        self.base = read(self.history, self.stream)
        self.states = copy.deepcopy(self.base["states"]) if self.base else {}
        self.legacy: dict[str, tuple[Any, tuple[int, str, str | None]]] = {}
        self.deferred: set[str] = set()
        self.disabled = False
        prefix = f"condition:{self.stream}:"
        rows = list(self.history.store.scan_runtime(PREFIX + prefix, len(MEMBERS) + 1))
        self.legacy_rows = copy.deepcopy(rows)
        if len(rows) > len(MEMBERS):
            if self.base is not None:
                raise StateError("Unbounded mixed condition state; data preserved")
            self.disabled = True
            return
        for raw_key, raw in rows:
            key = raw_key[len(PREFIX) :]
            point = coordinate(key)
            value = self.history.resolve(key, raw)
            binding = hot_binding(self.history.store.db, raw_key)
            if point is None or not supported(value) or binding is None or binding[1:] != ("condition", None):
                if self.base is None:
                    self.disabled = True
                    return
                if point is not None:
                    if point[1] in self.states:
                        raise StateError("Conflicting mixed condition state")
                    self.deferred.add(point[1])
                continue
            member = point[1]
            if member in self.states:
                raise StateError("Conflicting mixed condition state")
            self.states[member] = {"value": value, "expires_ms": binding[0]}
            self.legacy[key] = (copy.deepcopy(value), binding)
        self.days = self.history.store.snapshot().settings.candle_retention_days
        self.dirty = False

    def put(self, member: str, value: Any) -> None:
        if not supported(value):
            raise StateError("Unsupported grouped condition update; data preserved")
        self.states[member] = {
            "value": copy.deepcopy(value),
            "expires_ms": self.history.store.clock_ms() + self.days * 86_400_000,
        }
        self.dirty = True

    def flush(self) -> None:
        from .expiry_index import hot_binding

        if self.disabled or not self.dirty:
            return
        if encode(read(self.history, self.stream)) != encode(self.base):
            raise StateError("Concurrent grouped condition update")
        current_rows = list(
            self.history.store.scan_runtime(PREFIX + f"condition:{self.stream}:", len(MEMBERS) + 1)
        )
        if encode(current_rows) != encode(self.legacy_rows):
            raise StateError("Concurrent legacy condition membership update")
        for key, (value, binding) in self.legacy.items():
            if (
                encode(self.history._get_raw(key)) != encode(value)
                or hot_binding(self.history.store.db, PREFIX + key) != binding
            ):
                raise StateError("Concurrent legacy condition update")
        group_key = GROUP + self.stream
        if self.states:
            value = {"schema": SCHEMA, "states": self.states}
            self.history._put_raw(group_key, value)
            self.base = copy.deepcopy(value)
        else:
            self.history._delete_raw(group_key)
            self.base = None
        for key in self.legacy:
            self.history._delete_raw(key)
        self.legacy_rows = [
            (key, value) for key, value in self.legacy_rows if key[len(PREFIX) :] not in self.legacy
        ]
        self.legacy.clear()
        self.dirty = False


@contextmanager
def batch(history: History, stream: str) -> Iterator[None]:
    if not history.store.bounded or coordinate(f"condition:{stream}:price:UP") is None:
        yield
        return
    if history._condition_batch is not None:
        raise StateError("Nested condition batch requires an exposed callback boundary")
    if not history.store.db.in_transaction:
        history.store.db.execute("BEGIN IMMEDIATE")
    current = Batch(history, stream)
    history._condition_batch = current
    try:
        yield
    except BaseException:
        raise
    else:
        current.flush()
    finally:
        history._condition_batch = None


@contextmanager
def exposed(history: History) -> Iterator[None]:
    """Synchronous callbacks see all prior state and can safely mutate it."""
    current = history._condition_batch
    if current is None or current.disabled:
        yield
        return
    current.flush()
    history._condition_batch = None
    try:
        yield
    except BaseException:
        raise
    else:
        current.reload()
    finally:
        history._condition_batch = current


def get(history: History, key: str, default: Any) -> Any:
    point = coordinate(key)
    if point is None:
        return MISSING
    stream, member = point
    current = history._condition_batch
    if current is not None and current.stream == stream:
        if current.disabled or member in current.deferred:
            return MISSING
        record = current.states.get(member)
        return copy.deepcopy(record["value"]) if record else default
    value = read(history, stream)
    if value is None or member not in value["states"]:
        return MISSING
    if history._get_raw(key, MISSING) is not MISSING:
        raise StateError("Conflicting mixed condition state")
    return copy.deepcopy(value["states"][member]["value"])


def put(history: History, key: str, value: Any) -> bool:
    point = coordinate(key)
    if point is None:
        return False
    stream, member = point
    current = history._condition_batch
    if current is not None and current.stream == stream:
        if current.disabled or member in current.deferred:
            return False
        current.put(member, value)
        return True
    if read(history, stream) is None:
        return False
    with exposed(history), batch(history, stream):
        return put(history, key, value)


def delete(history: History, key: str) -> bool:
    point = coordinate(key)
    if point is None:
        return False
    stream, member = point
    current = history._condition_batch
    if current is not None and current.stream == stream:
        if current.disabled or member in current.deferred:
            return False
        if member in current.states:
            del current.states[member]
            current.dirty = True
        return True
    if read(history, stream) is None:
        return False
    with exposed(history), batch(history, stream):
        return delete(history, key)


def scan(history: History, prefix: str, reverse: bool = False) -> Iterator[tuple[str, Any]]:
    if history._condition_batch is not None:
        history._condition_batch.flush()

    def legacy() -> Iterator[tuple[str, Any]]:
        for key, value in history.store.scan_runtime(PREFIX + prefix, reverse=reverse):
            key = key[len(PREFIX) :]
            if not key.startswith(GROUP):
                yield key, history.resolve(key, value)

    def groups() -> Iterator[tuple[str, Any]]:
        stem = prefix[len("condition:") :].split(":", 1)[0] if prefix.startswith("condition:") else ""
        for key, value in history.store.scan_runtime(PREFIX + GROUP + stem, reverse=reverse):
            stream = key[len(PREFIX + GROUP) :]
            if coordinate(f"condition:{stream}:price:UP") is None:
                raise StateError("Invalid grouped condition owner; data preserved")
            value = validate(history.resolve(key[len(PREFIX) :], value))
            for member, record in sorted(value["states"].items(), reverse=reverse):
                logical = f"condition:{stream}:{member}"
                if logical.startswith(prefix):
                    yield logical, copy.deepcopy(record["value"])

    previous = None
    for key, value in heapq.merge(legacy(), groups(), key=lambda row: row[0], reverse=reverse):
        if key == previous:
            raise StateError("Conflicting mixed condition state")
        previous = key
        yield key, value


def expire(history: History, key: str, now_ms: int) -> tuple[int, bool]:
    if not key.startswith(GROUP):
        raise StateError("Invalid grouped condition expiry owner; data preserved")
    stream = key[len(GROUP) :]
    value = read(history, stream)
    if value is None:
        history._delete_raw(key)
        return 0, False
    remaining = {k: v for k, v in value["states"].items() if v["expires_ms"] > now_ms}
    removed = len(value["states"]) - len(remaining)
    if remaining:
        history._put_raw(key, {"schema": SCHEMA, "states": remaining})
    else:
        history._delete_raw(key)
    return removed, not remaining
