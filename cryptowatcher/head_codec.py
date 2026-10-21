"""Frozen projected-head template. All query fields stay inline; full SHA256.

Fields added to a future public projection fall back to the general codec. Never
change these positions/defaults for CH1; introduce another magic instead.
"""

from __future__ import annotations

import hashlib
from typing import Any

from . import compact_json
from .storage import encode

MAGIC = b"CH1\x00"
SCHEMA = "cryptowatcher.history-head.v1"
MAXIMUM = 65536
KINDS = ("event", "outcome", "episode", "comparison")
FIELDS = (
    (
        "event_id",
        "revision",
        "episode_id",
        "market_observation_ms",
        "available_at_ms",
        "market_type",
        "detector_id",
        "detector_version",
        "symbol",
        "timeframe",
        "direction",
        "strength",
    ),
    (
        "outcome_id",
        "revision",
        "event_id",
        "event_revision",
        "available_at_ms",
        "state",
        "horizon_min",
        "methodology_version",
        "classification",
        "noise_threshold_pct",
        "metrics",
    ),
    (
        "episode_id",
        "revision",
        "instrument",
        "direction",
        "state",
        "started_ms",
        "last_observation_ms",
        "longest_tf_ms",
        "strength",
        "available_at_ms",
    ),
    ("comparison_id", "revision", "event_id", "available_at_ms", "state"),
)
DEFAULTS = {"revision": 1, "event_revision": 1, "detector_version": "1", "methodology_version": "1"}


def pack(value: Any) -> bytes | None:
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        return None
    key = value.get("revision_key")
    if not isinstance(key, str) or key.partition(":")[0] not in KINDS:
        return None
    kind = KINDS.index(key.partition(":")[0])
    names = FIELDS[kind]
    if set(value) - set(names) - {"schema", "revision_key"}:
        return None
    identity, revision = value.get(names[0]), value.get("revision")
    if not isinstance(identity, str) or type(revision) is not int or revision < 1:
        return None
    if key != f"{KINDS[kind]}:{identity}:{revision:010d}":
        return None
    raw = encode(value).encode()
    if len(raw) > MAXIMUM:
        return None
    mask = 0
    defaults = 0
    values = []
    for index, name in enumerate(names):
        if name in value:
            mask |= 1 << index
            if (
                name in DEFAULTS
                and type(value[name]) is type(DEFAULTS[name])
                and value[name] == DEFAULTS[name]
            ):
                defaults |= 1 << index
            else:
                values.append(value[name])
    try:
        body = compact_json.pack([kind, mask, defaults, values])
    except ValueError:
        return None
    return MAGIC + len(raw).to_bytes(4, "big") + hashlib.sha256(raw).digest() + body


def unpack_raw(frame: bytes) -> tuple[dict[str, Any], bytes]:
    if not isinstance(frame, bytes) or not frame.startswith(MAGIC) or not 44 <= len(frame) <= MAXIMUM + 4096:
        raise ValueError("Invalid projected head envelope")
    size = int.from_bytes(frame[4:8], "big")
    if not 0 < size <= MAXIMUM:
        raise ValueError("Invalid projected head size")
    body = compact_json.unpack(frame[40:], MAXIMUM)
    if not isinstance(body, list) or len(body) != 4:
        raise ValueError("Invalid projected head fields")
    kind, mask, defaults, values = body
    if (
        type(kind) is not int
        or not 0 <= kind < len(KINDS)
        or type(mask) is not int
        or not 0 <= mask < 1 << len(FIELDS[kind])
        or mask & 3 != 3
        or type(defaults) is not int
        or defaults < 0
        or defaults & ~mask
        or not isinstance(values, list)
        or len(values) != mask.bit_count() - defaults.bit_count()
    ):
        raise ValueError("Invalid projected head mask")
    result: dict[str, Any] = {"schema": SCHEMA}
    position = 0
    for index, name in enumerate(FIELDS[kind]):
        if defaults & (1 << index):
            if name not in DEFAULTS:
                raise ValueError("Invalid projected head default")
            result[name] = DEFAULTS[name]
        elif mask & (1 << index):
            result[name] = values[position]
            position += 1
    identity, revision = result[FIELDS[kind][0]], result["revision"]
    if not isinstance(identity, str) or type(revision) is not int or revision < 1:
        raise ValueError("Invalid projected head identity")
    result["revision_key"] = f"{KINDS[kind]}:{identity}:{revision:010d}"
    raw = encode(result).encode()
    if len(raw) != size or hashlib.sha256(raw).digest() != frame[8:40]:
        raise ValueError("Projected head checksum mismatch")
    return result, raw


def unpack(frame: bytes) -> dict[str, Any]:
    return unpack_raw(frame)[0]
