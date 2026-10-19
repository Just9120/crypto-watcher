"""Map arbitrary public keyset cursors to native composite index bounds."""

from __future__ import annotations

import uuid
from bisect import bisect_left
from typing import Any

HEX = "0123456789abcdef"
DECIMAL = "0123456789"
UUID = tuple("-" if i in (8, 13, 18, 23) else HEX for i in range(36))
FORMATS = {"uuid": UUID, "time": (DECIMAL,) * 16, "revision": (DECIMAL,) * 10}


def ceiling(text: str, pattern: tuple[str, ...]) -> tuple[str | None, bool]:
    """First canonical fixed-width spelling >= text, without scanning records."""
    result: list[str] = []

    def finish() -> str:
        return "".join(result) + "".join(chars[0] for chars in pattern[len(result) :])

    def increment() -> str | None:
        while result:
            index = len(result) - 1
            previous = result.pop()
            allowed = pattern[index]
            position = allowed.index(previous) + 1
            if position < len(allowed):
                result.append(allowed[position])
                return finish()
        return None

    for index, allowed in enumerate(pattern):
        if index == len(text):
            return finish(), False
        position = bisect_left(allowed, text[index])
        if position == len(allowed):
            return increment(), False
        chosen = allowed[position]
        result.append(chosen)
        if chosen != text[index]:
            return finish(), False
    if len(text) == len(pattern):
        return "".join(result), True
    return increment(), False


def keyset(
    root: str,
    fields: tuple[tuple[str, str], ...],
    after: str | None,
    before: str | None,
) -> tuple[str, tuple[Any, ...]]:
    """Exclusive public boundaries become UUID/int scalar or tuple comparisons.

    Noncanonical old cursors, partial prefixes and Unicode compare exactly as
    public TEXT keys. No computed-key SQL sort/filter or row-by-row skip is used.
    """
    formats = [FORMATS[kind] for _, kind in fields]
    pattern = tuple(
        character for number, part in enumerate(formats) for character in ((":",) if number else ()) + part
    )
    clauses = []
    values: list[Any] = []
    names = ",".join(field for field, _ in fields)
    target = names if len(fields) == 1 else "(" + names + ")"
    placeholders = "?" if len(fields) == 1 else "(" + ",".join("?" for _ in fields) + ")"
    for bound, lower in ((after, True), (before, False)):
        if bound is None:
            continue
        if not isinstance(bound, str):
            raise ValueError("Invalid catalogue keyset cursor")
        if not bound.startswith(root):
            if (bound < root) == lower:
                continue
            return "0", ()
        candidate, exact = ceiling(bound[len(root) :], pattern)
        if candidate is None:
            if lower:
                return "0", ()
            continue
        operation = (">" if exact else ">=") if lower else "<"
        clauses.append(target + operation + placeholders)
        for part, (_, kind) in zip(candidate.split(":"), fields, strict=True):
            values.append(uuid.UUID(part).bytes if kind == "uuid" else int(part))
    return " AND ".join(clauses) or "1", tuple(values)
