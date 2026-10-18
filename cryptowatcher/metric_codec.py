"""Exact outcome metric bytes; no decimal rounding or outcome recalculation."""

from __future__ import annotations

import math
import struct
from typing import Any

DIRECTED = ("price_return_pct", "directed_return_pct", "mfe_pct", "mae_pct")
NEUTRAL = ("price_return_pct", "max_rise_pct", "max_fall_pct", "range_pct")


def flip(raw: bytes) -> bytes:
    return bytes((raw[0] ^ 128,)) + raw[1:]


def pack(value: Any) -> bytes | None:
    """Unsupported shapes stay in the existing general-purpose record codec."""
    if not isinstance(value, dict):
        return None
    keys = DIRECTED if set(value) == set(DIRECTED) else NEUTRAL if set(value) == set(NEUTRAL) else ()
    if not keys or any(type(value[k]) not in (int, float) for k in keys):
        return None
    numbers = []
    flags = 0
    for index, key in enumerate(keys):
        number = value[key]
        try:
            converted = float(number)
        except (ValueError, OverflowError):
            return None
        if not math.isfinite(converted) or converted != number:
            return None
        if type(number) is int:
            flags |= 1 << index
        numbers.append(struct.pack(">d", converted))
    if keys == DIRECTED:
        if numbers[1] == numbers[0]:
            kind = 1
        elif numbers[1] == flip(numbers[0]):
            kind = 2
        else:
            return None
        return bytes((kind, flags)) + numbers[0] + numbers[2] + numbers[3]
    return bytes((3, flags)) + b"".join(numbers)


def unpack(raw: bytes) -> dict[str, Any]:
    if not isinstance(raw, bytes) or len(raw) < 2:
        raise ValueError("Invalid numeric metric frame")
    kind, flags = raw[:2]
    if kind not in (1, 2, 3) or flags & ~15 or len(raw) != (34 if kind == 3 else 26):
        raise ValueError("Invalid numeric metric layout")
    values = [raw[i : i + 8] for i in range(2, len(raw), 8)]
    if kind in (1, 2):
        values.insert(1, values[0] if kind == 1 else flip(values[0]))
    result: dict[str, Any] = {}
    for index, (key, bits) in enumerate(zip(NEUTRAL if kind == 3 else DIRECTED, values)):
        value = struct.unpack(">d", bits)[0]
        if not math.isfinite(value) or (flags & (1 << index) and not value.is_integer()):
            raise ValueError("Invalid numeric metric value")
        result[key] = int(value) if flags & (1 << index) else value
    if pack(result) != raw:
        raise ValueError("Noncanonical numeric metric frame")
    return result


def reflected(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != set(DIRECTED):
        raise ValueError("Direction view requires directed metric fields")
    return {
        "price_return_pct": value["price_return_pct"],
        "directed_return_pct": -value["directed_return_pct"],
        "mfe_pct": -value["mae_pct"],
        "mae_pct": -value["mfe_pct"],
    }


def restore_view(value: dict[str, Any], view: int) -> dict[str, Any]:
    if type(view) is not int or not 0 <= view <= 3:
        raise ValueError("Unknown metric view")
    result = reflected(value) if view in (1, 3) else dict(value)
    if view in (2, 3):
        if set(result) != set(DIRECTED):
            raise ValueError("Neutral view requires directed base fields")
        result = {
            "price_return_pct": result["price_return_pct"],
            "max_rise_pct": result["mfe_pct"],
            "max_fall_pct": result["mae_pct"],
            "range_pct": float(result["mfe_pct"]) - float(result["mae_pct"]),
        }
    return result


def canonical(value: Any) -> tuple[dict[str, Any], int] | None:
    """Share exact mirrored/neutral views only after a full public round trip.

    Non-derivable neutral ranges retain their complete original frame. No
    decimal rounding, source recalculation or range approximation is allowed.
    """
    raw = pack(value)
    if raw is None:
        return None
    if set(value) == set(DIRECTED):
        mirror = reflected(value)
        other = pack(mirror)
        if other is not None and other < raw:
            result, view = mirror, 1
        else:
            result, view = value, 0
    else:
        base = {
            "price_return_pct": value["price_return_pct"],
            "directed_return_pct": value["price_return_pct"],
            "mfe_pct": value["max_rise_pct"],
            "mae_pct": value["max_fall_pct"],
        }
        candidate = canonical(base)
        if candidate is not None:
            result, reflected_view = candidate
            view = 2 + reflected_view
            if pack(restore_view(result, view)) != raw:
                result, view = value, 0
        else:
            result, view = value, 0
    if pack(restore_view(result, view)) != raw:
        raise ValueError("Metric view changed public values")
    return result, view
