"""Frozen reversible v3 integer-token transform, never numeric rounding.

Only a canonical positive zigzag integer divisible by120000 may become tag12.
The inverse recreates its original tag3/LEB bytes. Other scalar bytes, key order,
float bits and noncanonical old integer encodings pass through unchanged.
"""

from __future__ import annotations

from .compact_json import MAX_DEPTH, MAX_NODES, integer

SCALE = 120000
MINIMUM = 2_000_000_000_000
MAX_INTEGER = 8190


def transform(raw: bytes, maximum: int, *, inverse: bool = False) -> bytes:
    if (
        not isinstance(raw, bytes)
        or type(maximum) is not int
        or not 0 < len(raw) <= maximum
        or type(inverse) is not bool
    ):
        raise ValueError("Invalid scalar unit input")
    pos = nodes = 0
    out = bytearray()

    def append(value: bytes) -> None:
        if len(value) > maximum - len(out):
            raise ValueError("Scalar unit expansion bound")
        out.extend(value)

    def number() -> int:
        nonlocal pos
        value = 0
        for shift in range(0, 8192, 7):
            if pos >= len(raw):
                raise ValueError("Truncated scalar unit integer")
            byte = raw[pos]
            pos += 1
            value |= (byte & 127) << shift
            if byte < 128:
                if (value // 2).bit_length() > MAX_INTEGER:
                    raise ValueError("Scalar unit integer bound")
                return value
        raise ValueError("Scalar unit integer bound")

    def node(depth: int) -> tuple[int, int | None]:
        nonlocal pos, nodes
        nodes += 1
        if depth > MAX_DEPTH or nodes > MAX_NODES or pos >= len(raw):
            raise ValueError("Scalar unit token bound")
        start = pos
        tag = raw[pos]
        pos += 1
        if tag in (3, 12):
            number_start = pos
            value = number()
            canonical = integer(value)
            if tag == 12:
                expanded = value * SCALE
                if (
                    not inverse
                    or expanded < MINIMUM
                    or (expanded // 2).bit_length() > MAX_INTEGER
                    or raw[number_start:pos] != canonical
                    or len(canonical) >= len(integer(expanded))
                ):
                    raise ValueError("Invalid scalar unit coordinate")
                append(b"\x03" + integer(expanded))
            elif (
                not inverse and value >= MINIMUM and value % SCALE == 0 and raw[number_start:pos] == canonical
            ):
                encoded = integer(value // SCALE)
                append(b"\x0c" + encoded if len(encoded) < len(canonical) else raw[start:pos])
            else:
                append(raw[start:pos])
        elif tag in (0, 1, 2):
            append(raw[start:pos])
        elif tag in (4, 9, 10):
            pos += 8 if tag == 4 else 16 if tag == 9 else 32
            if pos > len(raw):
                raise ValueError("Truncated scalar unit value")
            append(raw[start:pos])
        elif tag in (5, 8):
            count = number()
            if tag == 5:
                pos += count
            if pos > len(raw):
                raise ValueError("Truncated scalar unit text")
            append(raw[start:pos])
        elif tag in (6, 7, 11):
            count = number()
            if count > len(raw):
                raise ValueError("Scalar unit collection bound")
            append(raw[start:pos])
            if tag == 11:
                key_tag, key_count = node(depth + 1)
                if key_tag != 6 or not key_count:
                    raise ValueError("Scalar unit column schema")
                for _ in range(key_count):
                    node(depth + 1)
            else:
                for _ in range(count * (2 if tag == 7 else 1)):
                    node(depth + 1)
            return tag, count
        else:
            raise ValueError("Unknown scalar unit token")
        return tag, None

    node(0)
    if pos != len(raw):
        raise ValueError("Trailing scalar unit tokens")
    return bytes(out)
