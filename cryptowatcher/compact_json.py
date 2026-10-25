"""Versioned lossless typed JSON; dictionaries and float bits are part of v3.

No Python objects/code, implicit numeric rounding or external dictionaries. The
SQL/public interface remains canonical JSON. Old record codecs remain readable.
"""

from __future__ import annotations

import math
import re
import struct
from typing import Any

from . import frozen_codec_native

# Frozen v3 token dictionary. Never change/reorder without a new format version.
WORDS = tuple(
    """schema event_id outcome_id episode_id comparison_id revision event_revision
revision_key episode_revision venue symbol market_type timeframe detector_id detector_version
settings_revision settings_snapshot effective_parameters direction strength metrics units
market_observation_ms available_at_ms reference_price reference_time_ms receipt_ms
source_inputs future_inputs source_context enrichment completeness trading_authorized model_invoked
state horizon_min methodology_version classification classification_reason noise_threshold_pct
expected_candles observed_candles missing_ranges window_start_ms window_end_ms
price_return_pct directed_return_pct mfe_pct mae_pct max_rise_pct max_fall_pct range_pct
profile alert_universe_mode instrument started_ms last_observation_ms longest_tf_ms
confirmations conditions last_notified pending_notification notification_sequence
next_attempt_ms pending_key hold_key stream start end id attempts reason cursor_end completed_ms
_history_storage fields mode value2 source contexts points sha256 _cw_revision
cryptowatcher.event.v1 cryptowatcher.outcome.v1 cryptowatcher.episode.v1
cryptowatcher.comparison.v1 cryptowatcher.history-head.v1 cryptowatcher.shared-fields.v1
BYBIT spot linear UP DOWN NEUTRAL PENDING RETRY COMPLETE COMPLETE_LIMITED INCOMPLETE
ACTIVE CLOSED HISTORICAL SUPPRESSED UNCLASSIFIED CONTINUATION REVERSAL NEUTRAL_MOVEMENT
EXPIRED_RETENTION_INCOMPLETE LOCAL_CLOSED_DATA FROZEN_EVENT_THRESHOLD NEUTRAL_EVENT
COMPARISON_UNAVAILABLE custom top 1m 3m 5m 15m %
previous_state noise comparator_metrics comparator_sets limitations sample_size population_size
offset count observed_candles expected_candles reason quality_provenance
settings_sha256 detector_definition outcome_definition comparison_version comparison_definition
comparison_versions comparison_definitions
FEWER_THAN20_MATCHING_COMPLETE_NONOVERLAPPING_WINDOWS PARTIAL28DAY_SOURCE_HISTORY
INCOMPLETE_OR_UNCALCULABLE_CANDIDATES INCOMPLETE_WINDOW MISSING_REFERENCE_CONTEXT
FROZEN_DETECTOR_VERSION_UNAVAILABLE REFERENCED_RECOVERY_PENDING""".split()
)
WORD_IDS = {word: i for i, word in enumerate(WORDS)}
MAX_NODES = 1_000_000
MAX_DEPTH = 64


def integer(value: int) -> bytes:
    out = bytearray()
    while value >= 128:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def pack(value: Any) -> bytes:
    native = frozen_codec_native.typed_pack(value, WORD_IDS)
    if native is not None:
        return native
    out = bytearray()
    nodes = 0

    def put(v: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if depth > MAX_DEPTH or nodes > MAX_NODES:
            raise ValueError("Typed JSON bound")
        if v is None:
            out.append(0)
        elif isinstance(v, bool):
            out.append(2 if v else 1)
        elif isinstance(v, int):
            if v.bit_length() > 8190:
                raise ValueError("Typed integer bound")
            out.append(3)
            out.extend(integer(v * 2 if v >= 0 else -v * 2 - 1))
        elif isinstance(v, float):
            if not math.isfinite(v):
                raise ValueError("Non-finite typed JSON")
            out.append(4)
            out.extend(struct.pack(">d", v))
        elif isinstance(v, str):
            if v in WORD_IDS:
                out.append(8)
                out.extend(integer(WORD_IDS[v]))
            elif len(v) in (36, 64) and re.fullmatch(
                r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{64}", v
            ):
                out.append(9 if len(v) == 36 else 10)
                out.extend(bytes.fromhex(v.replace("-", "")))
            else:
                raw = v.encode("utf-8")
                out.append(5)
                out.extend(integer(len(raw)))
                out.extend(raw)
        elif isinstance(v, list):
            keys = list(v[0]) if v and isinstance(v[0], dict) else []
            if len(v) >= 4 and keys and all(isinstance(row, dict) and list(row) == keys for row in v):
                out.append(11)
                out.extend(integer(len(v)))
                put(keys, depth + 1)
                for key in keys:
                    put([row[key] for row in v], depth + 1)
            else:
                out.append(6)
                out.extend(integer(len(v)))
                for child in v:
                    put(child, depth + 1)
        elif isinstance(v, dict):
            out.append(7)
            out.extend(integer(len(v)))
            for key, child in v.items():
                if not isinstance(key, str):
                    raise ValueError("Typed JSON key")
                put(key, depth + 1)
                put(child, depth + 1)
        else:
            raise ValueError("Unsupported typed JSON value")

    put(value, 0)
    return bytes(out)


def unpack(raw: bytes, maximum: int) -> Any:
    length = len(raw)
    if not 0 < length <= maximum:
        raise ValueError("Typed JSON size")
    native = frozen_codec_native.typed_unpack(raw, WORDS)
    if native is not NotImplemented:
        return native
    collection_limit = min(MAX_NODES, length)
    pos = 0
    nodes = 0

    def take(n: int) -> bytes:
        nonlocal pos
        if n < 0 or n > length - pos:
            raise ValueError("Truncated typed JSON")
        result = raw[pos : pos + n]
        pos += n
        return result

    def number() -> int:
        nonlocal pos
        v = 0
        for shift in range(0, 8192, 7):
            if pos >= length:
                raise ValueError("Truncated typed JSON")
            byte = raw[pos]
            pos += 1
            v |= (byte & 127) << shift
            if byte < 128:
                return v
        raise ValueError("Typed integer bound")

    def get(depth: int) -> Any:
        nonlocal nodes, pos
        nodes += 1
        if depth > MAX_DEPTH or nodes > MAX_NODES:
            raise ValueError("Typed JSON bound")
        if pos >= length:
            raise ValueError("Truncated typed JSON")
        tag = raw[pos]
        pos += 1
        if tag < 3:
            return (None, False, True)[tag]
        if tag == 3:
            n = number()
            return n // 2 if n % 2 == 0 else -(n // 2) - 1
        if tag == 4:
            value = struct.unpack(">d", take(8))[0]
            if not math.isfinite(value):
                raise ValueError("Non-finite typed JSON")
            return value
        if tag == 5:
            return take(number()).decode("utf-8")
        if tag == 8:
            index = number()
            if index >= len(WORDS):
                raise ValueError("Unknown typed token")
            return WORDS[index]
        if tag in (9, 10):
            value = take(16 if tag == 9 else 32).hex()
            return (
                "-".join((value[:8], value[8:12], value[12:16], value[16:20], value[20:]))
                if tag == 9
                else value
            )
        if tag not in (6, 7, 11):
            raise ValueError("Unknown typed JSON tag")
        n = number()
        if n > collection_limit:
            raise ValueError("Typed collection bound")
        if tag == 6:
            return [get(depth + 1) for _ in range(n)]
        if tag == 7:
            result = {}
            for _ in range(n):
                key = get(depth + 1)
                if not isinstance(key, str) or key in result:
                    raise ValueError("Invalid typed JSON key")
                result[key] = get(depth + 1)
            return result
        keys = get(depth + 1)
        if (
            not isinstance(keys, list)
            or not keys
            or not all(isinstance(key, str) for key in keys)
            or len(set(keys)) != len(keys)
            or n * len(keys) > MAX_NODES
        ):
            raise ValueError("Typed column schema bound")
        columns = [get(depth + 1) for _ in keys]
        if any(not isinstance(column, list) or len(column) != n for column in columns):
            raise ValueError("Typed column shape")
        return [{key: columns[j][i] for j, key in enumerate(keys)} for i in range(n)]

    result = get(0)
    if pos != length:
        raise ValueError("Trailing typed JSON bytes")
    return result
