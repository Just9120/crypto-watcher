"""Frozen PR1 operands for exactly reversible outcome-v1 metric frames.

This is a private codec candidate, not an outcome calculation or source lookup.
Admission requires complete byte equality to the original metric frame. The
operands are self-contained and outlive source retention. NB1 remains frozen;
choosing PR1 requires a separate block format and whole-block size comparison.
"""

from __future__ import annotations

import math
import struct
from decimal import Decimal
from typing import Any

from . import metric_codec
from .compact_json import integer

MAXIMUM = 33


def zig(value: int) -> int:
    return 2 * value if value >= 0 else -2 * value - 1


def unzig(value: int) -> int:
    return value // 2 if value % 2 == 0 else -(value // 2) - 1


def read_integer(raw: bytes, position: int) -> tuple[int, int]:
    start, value = position, 0
    for shift in range(0, 70, 7):
        if position >= len(raw):
            raise ValueError("Truncated price operand")
        byte = raw[position]
        position += 1
        value |= (byte & 127) << shift
        if not byte & 128:
            if value >= 2**64 or integer(value) != raw[start:position]:
                raise ValueError("Noncanonical price operand")
            return value, position
    raise ValueError("Price operand integer overflow")


def restored(kind: int, prices: list[float]) -> dict[str, Any]:
    """Frozen PR1 arithmetic/order. Do not import a future calculation method."""
    reference, close, high, low = prices
    change = 100 * (close / reference - 1)
    rise = 100 * (high / reference - 1)
    fall = 100 * (low / reference - 1)
    if kind == 3:
        return dict(zip(metric_codec.NEUTRAL, (change, max(0, rise), min(0, fall), rise - fall)))
    sign = 1 if kind == 1 else -1
    return dict(
        zip(
            metric_codec.DIRECTED,
            (
                change,
                change * sign,
                max(0, rise if sign == 1 else -fall),
                min(0, fall if sign == 1 else -rise),
            ),
        )
    )


def unpack(raw: bytes) -> dict[str, Any]:
    if not isinstance(raw, bytes) or not 6 <= len(raw) <= MAXIMUM or raw[0] not in (17, 18, 19):
        raise ValueError("Invalid PR1 metric frame")
    values = []
    position = 1
    for _ in range(5):
        value, position = read_integer(raw, position)
        values.append(value)
    if position != len(raw):
        raise ValueError("Trailing price operand bytes")
    exponent, reference = unzig(values[0]), values[1]
    if not -324 <= exponent <= 308 or not 0 < reference < 2**63:
        raise ValueError("Invalid price operand scale/reference")
    coefficients = [reference] + [reference + unzig(n) for n in values[2:]]
    if any(not 0 < n < 2**63 for n in coefficients):
        raise ValueError("Invalid price operand coefficient")
    prices = [float(str(n) + "e" + str(exponent)) for n in coefficients]
    if any(not math.isfinite(n) or n <= 0 for n in prices):
        raise ValueError("Invalid reconstructed price operand")
    result = restored(raw[0] - 16, prices)
    if metric_codec.pack(result) is None:
        raise ValueError("Non-finite reconstructed metric")
    return result


def frame(raw: bytes) -> bytes:
    """The original26/34byte canonical frame, suitable for the existing FP/ID."""
    if isinstance(raw, bytes) and raw and raw[0] in (1, 2, 3):
        metric_codec.unpack(raw)
        return raw
    result = metric_codec.pack(unpack(raw))
    if result is None:
        raise ValueError("Invalid reconstructed metric frame")
    return result


def encode(original: bytes, prices: Any) -> bytes | None:
    """Return a smaller exact candidate; unsupported operands keep the raw frame.

    Producers must also qualify frozen method/input provenance. This codec
    decides neither source completeness nor whether a block should be recoded.
    """
    metric_codec.unpack(original)
    if not isinstance(prices, (list, tuple)) or len(prices) != 4:
        return None
    try:
        for price in prices:
            if type(price) not in (int, float) or not math.isfinite(price) or price <= 0:
                return None
        pieces = [Decimal(str(price)).as_tuple() for price in prices]
        if any(type(piece.exponent) is not int for piece in pieces):
            return None
        exponent = min(int(piece.exponent) for piece in pieces)
        if not -324 <= exponent <= 308:
            return None
        coefficients = []
        for piece in pieces:
            difference = int(piece.exponent) - exponent
            if difference < 0 or len(piece.digits) + difference > 19:
                return None
            coefficient = int("".join(map(str, piece.digits))) * 10**difference
            if not 0 < coefficient < 2**63:
                return None
            coefficients.append(coefficient)
        reconstructed = [float(str(n) + "e" + str(exponent)) for n in coefficients]
        if any(
            struct.pack(">d", a) != struct.pack(">d", b) for a, b in zip(prices, reconstructed, strict=True)
        ):
            return None
        raw = bytes((original[0] + 16,)) + integer(zig(exponent)) + integer(coefficients[0])
        raw += b"".join(integer(zig(n - coefficients[0])) for n in coefficients[1:])
        if len(raw) >= len(original) or frame(raw) != original:
            return None
        return raw
    except (ValueError, OverflowError):
        return None
