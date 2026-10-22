"""Bounded exact source columns inside the authenticated SB3 tile envelope.

The outer tile binds all bytes and coordinates. Decimal coefficients are derived
from Python's shortest round-trip float text; original int/float types and signed
zero are kept. Unsupported/oversize inputs retain the existing source codecs.
"""

from __future__ import annotations

import json
import math
import struct
from decimal import Decimal
from typing import Any

import zstandard

from . import source_slot_json
from .storage import encode

SLOTS = 32
MAX_ROWS = 256
MAX_BYTES = 512 * 1024
MAX_INTEGER_BITS = 8192
HEADER = 6


def integer(value: Any) -> int:
    if type(value) is not int or value.bit_length() > MAX_INTEGER_BITS:
        raise ValueError("Source column integer bound")
    return value


def column(values: list[Any]) -> tuple[str, bytes]:
    if all(type(v) is int for v in values):
        return "i", encode([values[0]] + [b - a for a, b in zip(values, values[1:])]).encode()
    if all(type(v) is float for v in values):
        parts = [Decimal(repr(v)).as_tuple() for v in values]
        scale = min(int(v.exponent) for v in parts)
        coefficients = [
            (-1 if v.sign else 1) * int("".join(map(str, v.digits))) * 10 ** (int(v.exponent) - scale)
            for v in parts
        ]
        for value in coefficients:
            integer(value)
        delta = [coefficients[0]] + [b - a for a, b in zip(coefficients, coefficients[1:])]
        negative_zero = sum(1 << i for i, v in enumerate(parts) if v.sign and coefficients[i] == 0)
        return "d", encode([scale, delta, negative_zero]).encode()
    return "v", encode(values).encode()


def pack(slots: list[Any]) -> bytes | None:
    if (
        not isinstance(slots, list)
        or len(slots) != SLOTS
        or not all(source_slot_json.supported(slot) for slot in slots)
    ):
        return None
    counts = list(map(len, slots))
    count = sum(counts)
    if not 0 < count <= MAX_ROWS:
        return None
    flat = [row[:3] + row[3] for slot in slots for row in slot]
    columns = [column([row[i] for row in flat]) for i in range(12)]
    metadata = encode([counts, [[kind, len(raw)] for kind, raw in columns]]).encode()
    raw = struct.pack(">I", len(metadata)) + metadata + b"".join(value for _, value in columns)
    if len(raw) > MAX_BYTES:
        return None
    body = zstandard.ZstdCompressor(level=3, write_checksum=True, threads=0).compress(raw)
    result = struct.pack(">IH", len(raw), count) + body
    return result if len(result) <= MAX_BYTES - 60 else None


def layout(frame: bytes) -> tuple[int, int, bytes]:
    if not isinstance(frame, bytes) or not HEADER < len(frame) <= MAX_BYTES - 60:
        raise ValueError("Source columns allocation bound")
    size, count = struct.unpack(">IH", frame[:HEADER])
    if not 0 < size <= MAX_BYTES or not 0 < count <= MAX_ROWS:
        raise ValueError("Source columns allocation bound")
    body = frame[HEADER:]
    try:
        parameters = zstandard.get_frame_parameters(body)
    except zstandard.ZstdError as error:
        raise ValueError("Invalid source columns compression") from error
    if (
        parameters.content_size != size
        or parameters.window_size > MAX_BYTES
        or parameters.dict_id
        or not parameters.has_checksum
    ):
        raise ValueError("Source columns decompression bound")
    return size, count, body


def json_value(raw: bytes) -> Any:
    try:
        return json.loads(
            raw, parse_constant=source_slot_json.invalid, object_pairs_hook=source_slot_json.invalid
        )
    except (ValueError, RecursionError, UnicodeError) as error:
        raise ValueError("Invalid source columns JSON") from error


def cumulative(values: Any, count: int) -> list[int]:
    if not isinstance(values, list) or len(values) != count:
        raise ValueError("Source column cardinality mismatch")
    result = []
    total = 0
    for value in values:
        total = integer(total + integer(value))
        result.append(total)
    return result


def decoded_column(kind: str, block: bytes, count: int) -> list[Any]:
    value = json_value(block)
    if kind == "i":
        return cumulative(value, count)
    if kind == "d":
        if not isinstance(value, list) or len(value) != 3:
            raise ValueError("Invalid decimal source column")
        scale, delta, negative_zero = value
        if (
            type(scale) is not int
            or not -324 <= scale <= 308
            or type(negative_zero) is not int
            or not 0 <= negative_zero < 1 << count
        ):
            raise ValueError("Source decimal allocation bound")
        result = []
        suffix = "e" + str(scale)
        for i, coefficient in enumerate(cumulative(delta, count)):
            text = str(coefficient)
            if negative_zero and negative_zero & (1 << i):
                if coefficient:
                    raise ValueError("Invalid source negative-zero mask")
                text = "-" + text
            number = float(text + suffix)
            if not math.isfinite(number):
                raise ValueError("Non-finite source decimal")
            result.append(number)
        return result
    if kind != "v" or not isinstance(value, list) or len(value) != count:
        raise ValueError("Invalid source value column")
    return value


def data(frame: bytes, cache: Any = None) -> tuple[int, bytes]:
    size, count, body = layout(frame)

    def read() -> bytes:
        try:
            # The pinned C backend forwards this argument to libzstd in bytes.
            # Authenticate actual frame window/content independently in layout.
            return zstandard.ZstdDecompressor(max_window_size=MAX_BYTES).decompress(
                body, max_output_size=size, allow_extra_data=False
            )
        except zstandard.ZstdError as error:
            raise ValueError("Invalid source columns compression") from error

    raw = read() if cache is None else cache("source-column-data", "", frame, read)
    if not isinstance(raw, bytes) or len(raw) != size:
        raise ValueError("Invalid pure source columns cache result")
    return count, raw


def all_values(frame: bytes, cache: Any = None) -> list[Any]:
    count, raw = data(frame, cache)
    if len(raw) < 4:
        raise ValueError("Truncated source columns")
    metadata_size = struct.unpack(">I", raw[:4])[0]
    if not 0 < metadata_size <= 8192 or 4 + metadata_size >= len(raw):
        raise ValueError("Invalid source columns metadata bound")
    metadata = json_value(raw[4 : 4 + metadata_size])
    if not isinstance(metadata, list) or len(metadata) != 2:
        raise ValueError("Invalid source columns metadata")
    counts, descriptors = metadata
    if (
        not isinstance(counts, list)
        or len(counts) != SLOTS
        or any(type(n) is not int or not 0 <= n <= MAX_ROWS for n in counts)
        or sum(counts) != count
        or not isinstance(descriptors, list)
        or len(descriptors) != 12
    ):
        raise ValueError("Source columns cardinality mismatch")
    at = 4 + metadata_size
    columns = []
    for descriptor in descriptors:
        if not isinstance(descriptor, list) or len(descriptor) != 2:
            raise ValueError("Invalid source column descriptor")
        kind, length = descriptor
        if kind not in ("i", "d", "v") or type(length) is not int or not 0 < length <= len(raw) - at:
            raise ValueError("Invalid source column descriptor")
        columns.append(decoded_column(kind, raw[at : at + length], count))
        at += length
    if at != len(raw):
        raise ValueError("Trailing source column bytes")
    rows = [[row[0], row[1], row[2], list(row[3:])] for row in zip(*columns)]
    slots = []
    at = 0
    for n in counts:
        slot = rows[at : at + n]
        if not source_slot_json.supported(slot):
            raise ValueError("Invalid source column slot")
        slots.append(slot)
        at += n
    return slots
