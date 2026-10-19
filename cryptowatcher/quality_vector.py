"""Bounded exact numeric2 reference vectors, including repeated values and views."""

from __future__ import annotations

from typing import Any

MAXIMUM = 4096


def pack(values: list[Any], *, delta: bool) -> list[Any] | None:
    from .quality_packets import metric_reference

    if len(values) > MAXIMUM:
        raise ValueError("Quality comparison vector exceeds bound")
    for value in values:
        metric_reference(value, views=True)
    if not values:
        return None
    if delta and all(type(value) is int for value in values):
        previous = 0
        differences = []
        for value in values:
            differences.append(value - previous)
            previous = value
        return [1, differences]
    return [0, values]


def unpack(value: Any) -> list[Any]:
    from .quality_packets import metric_reference

    if value is None:
        return []
    if (
        not isinstance(value, list)
        or len(value) != 2
        or type(value[0]) is not int
        or value[0] not in (0, 1)
        or not isinstance(value[1], list)
        or len(value[1]) > MAXIMUM
    ):
        raise ValueError("Invalid quality comparison vector")
    mode, values = value
    if mode == 0:
        result = values
    else:
        result = []
        previous = 0
        for difference in values:
            if type(difference) is not int:
                raise ValueError("Invalid quality comparison delta")
            previous += difference
            metric_reference(previous, views=True)
            result.append(previous)
    for reference in result:
        metric_reference(reference, views=True)
    return result
