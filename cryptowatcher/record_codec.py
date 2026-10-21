"""Lossless envelopes for old immutable JSON records; no market behavior changes."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any

import zstandard

from .storage import StateError, encode

MARKER = "_history_record_storage"
SCHEMA = "cryptowatcher.compressed-record.v1"
MAX_BYTES = 32 * 1024 * 1024
FIELDS = {"schema", "bytes", "sha256", "zstd"}
CANDLE_MARKER = "_cw_candle_v1"
CANDLE_FIELDS = (
    "close",
    "high",
    "low",
    "open",
    "received_ms",
    "server_ms",
    "source",
    "start",
    "step",
    "turnover",
    "volume",
)


def pack_candle(payload: str) -> str:
    """Elide fixed candle field names only for an exactly canonical known object."""
    value = json.loads(payload)
    if not isinstance(value, dict) or set(value) != set(CANDLE_FIELDS):
        return payload
    if encode(value) != payload:
        return payload
    return encode({CANDLE_MARKER: [value[name] for name in CANDLE_FIELDS]})


def sql_payload(payload: Any) -> Any:
    """Expose original JSON to the existing SQLite history filters and aggregates."""
    if isinstance(payload, bytes):
        return binary_payload(payload)
    if not isinstance(payload, str) or (MARKER not in payload and CANDLE_MARKER not in payload):
        return payload
    value = json.loads(payload)
    if isinstance(value, dict) and CANDLE_MARKER in value:
        data = value[CANDLE_MARKER]
        if set(value) != {CANDLE_MARKER} or not isinstance(data, list) or len(data) != len(CANDLE_FIELDS):
            raise StateError("Invalid compact candle record")
        return encode(dict(zip(CANDLE_FIELDS, data, strict=True)))
    raw = unpack_bytes(value)
    return payload if raw is None else raw.decode("utf-8")


def pack(payload: str) -> str:
    """Preserve the exact original bytes; caller selects validated immutable records."""
    raw = payload.encode("utf-8")
    if not 0 < len(raw) <= MAX_BYTES:
        raise StateError("Compressed record size refused")
    encoded = zstandard.ZstdCompressor(level=1, write_checksum=True, threads=0).compress(raw)
    return encode(
        {
            MARKER: {
                "schema": SCHEMA,
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "zstd": base64.b64encode(encoded).decode("ascii"),
            }
        }
    )


def unpack_bytes(value: Any) -> bytes | None:
    """Check every encoded byte, bounds and identity before returning original JSON."""
    if not isinstance(value, dict) or MARKER not in value:
        return None
    descriptor = value[MARKER]
    if (
        set(value) != {MARKER}
        or not isinstance(descriptor, dict)
        or set(descriptor) != FIELDS
        or descriptor.get("schema") != SCHEMA
    ):
        raise StateError("Invalid compressed record schema")
    size, fingerprint, text = descriptor["bytes"], descriptor["sha256"], descriptor["zstd"]
    if (
        type(size) is not int
        or not 0 < size <= MAX_BYTES
        or not isinstance(fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
        or not isinstance(text, str)
        or len(text) > (MAX_BYTES + 65536) * 4 // 3 + 4
    ):
        raise StateError("Invalid compressed record bounds")
    try:
        encoded = base64.b64decode(text, validate=True)
        parameters = zstandard.get_frame_parameters(encoded)
        if (
            parameters.content_size != size
            or parameters.window_size > MAX_BYTES
            or parameters.dict_id
            or not parameters.has_checksum
        ):
            raise StateError("Invalid compressed record frame")
        raw = zstandard.ZstdDecompressor(max_window_size=MAX_BYTES // 1024).decompress(
            encoded, max_output_size=size, allow_extra_data=False
        )
    except (ValueError, zstandard.ZstdError) as error:
        raise StateError("Invalid compressed record encoding") from error
    if len(raw) != size or hashlib.sha256(raw).hexdigest() != fingerprint:
        raise StateError("Compressed record identity mismatch")
    return raw


# Frozen raw-content dictionary v2. Changes require a new magic/version; old
# immutable records always decode with the original bytes below.
BINARY_MAGIC = b"CW2\x00"
BINARY_DICTIONARY = zstandard.ZstdCompressionDict(
    b"""{"schema":"cryptowatcher.event.v1","event_id":"","revision":1,"episode_id":"","episode_revision":1,"venue":"BYBIT","symbol":"BTCUSDT","market_type":"spot","timeframe":"1m","detector_id":"composite","detector_version":"1","settings_revision":3,"settings_snapshot":{"profile":"custom","alert_universe_mode":"top"},"effective_parameters":{},"direction":"NEUTRAL","strength":1.0,"metrics":{"price_return_pct":0.0,"directed_return_pct":0.0,"mfe_pct":0.0,"mae_pct":0.0},"units":{},"market_observation_ms":0,"available_at_ms":0,"reference_price":0,"reference_time_ms":0,"source_inputs":[],"completeness":"COMPLETE","trading_authorized":false,"model_invoked":false}
{"schema":"cryptowatcher.outcome.v1","outcome_id":"","event_id":"","event_revision":1,"methodology_version":"1","horizon_min":15,"state":"COMPLETE","classification":"UNCLASSIFIED","expected_candles":15,"observed_candles":15,"noise_threshold_pct":0.0,"metrics":{},"source_inputs":[],"reference_time_ms":0,"reference_price":0,"available_at_ms":0}
{"schema":"cryptowatcher.history-head.v1","revision_key":"event:","event_id":"","outcome_id":"","episode_id":"","comparison_id":"","revision":1,"state":"COMPLETE"}
{"schema":"cryptowatcher.episode.v1","episode_id":"","instrument":"spot/BTCUSDT","direction":"UP","state":"ACTIVE","started_ms":0,"last_observation_ms":0,"longest_tf_ms":60000,"confirmations":{},"conditions":[],"last_notified":null,"pending_notification":null,"notification_sequence":1,"strength":1.0,"available_at_ms":0}
{"_history_storage":{"schema":"cryptowatcher.shared-fields.v1","fields":{"settings_snapshot":{"mode":"value2","id":""},"source_inputs":{"mode":"source","stream":1,"points":"","sha256":""}}}}
{"outcome_id":"","event_id":"","event_revision":1,"horizon_min":15,"methodology_version":"1","stream":"spot/BTCUSDT/1m","state":"PENDING","next_attempt_ms":0,"pending_key":"outcome-pending:","hold_key":"hold:"}
""",
    dict_type=zstandard.DICT_TYPE_RAWCONTENT,
)


def pack_binary_v2(payload: str) -> bytes:
    raw = payload.encode("utf-8")
    if not 0 < len(raw) <= MAX_BYTES:
        raise StateError("Compressed record size refused")
    frame = zstandard.ZstdCompressor(
        level=3, dict_data=BINARY_DICTIONARY, write_checksum=True, threads=0
    ).compress(raw)
    return BINARY_MAGIC + hashlib.sha256(raw).digest() + frame


TYPED_MAGIC = b"CW3\x00"


def pack_binary(payload: str) -> bytes:
    """Typed canonical records; noncanonical legacy bytes keep their v2 codec."""
    from .compact_json import pack as typed_pack

    raw = payload.encode("utf-8")
    if not 0 < len(raw) <= MAX_BYTES:
        raise StateError("Compressed record size refused")
    value = json.loads(payload)
    if encode(value) != payload:
        return pack_binary_v2(payload)
    try:
        typed = typed_pack(value)
    except ValueError:
        return pack_binary_v2(payload)
    if len(typed) >= len(raw):
        return pack_binary_v2(payload)
    frame = zstandard.ZstdCompressor(level=3, write_checksum=True, threads=0).compress(typed)
    ordinary = TYPED_MAGIC + len(raw).to_bytes(4, "big") + hashlib.sha256(raw).digest() + frame
    from .head_codec import pack as head

    projected = head(value)
    return projected if projected is not None and len(projected) < len(ordinary) else ordinary


def _typed_value(payload: bytes) -> tuple[Any, bytes]:
    """Validate the existing CW3 envelope once; retain exact JSON for SQL callers."""
    from .compact_json import unpack as typed_unpack

    if len(payload) < 46:
        raise StateError("Invalid typed record envelope")
    size = int.from_bytes(payload[4:8], "big")
    try:
        parameters = zstandard.get_frame_parameters(payload[40:])
        if (
            not 0 < size <= MAX_BYTES
            or not 0 < parameters.content_size <= MAX_BYTES
            or parameters.window_size > MAX_BYTES
            or parameters.dict_id
            or not parameters.has_checksum
        ):
            raise StateError("Invalid typed record bounds")
        typed = zstandard.ZstdDecompressor(max_window_size=MAX_BYTES // 1024).decompress(
            payload[40:], max_output_size=parameters.content_size, allow_extra_data=False
        )
        value = typed_unpack(typed, MAX_BYTES)
        raw = encode(value).encode()
        if len(raw) != size or hashlib.sha256(raw).digest() != payload[8:40]:
            raise StateError("Typed record identity mismatch")
        return value, raw
    except (zstandard.ZstdError, ValueError, UnicodeError, RecursionError) as exc:
        raise StateError("Invalid typed record encoding") from exc


def _head_value(payload: bytes) -> tuple[Any, bytes]:
    from .head_codec import unpack_raw

    try:
        return unpack_raw(payload)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise StateError("Invalid projected head encoding") from exc


def json_value(payload: Any) -> Any:
    """Avoid a second JSON parse after fully validating and decoding typed data."""
    if isinstance(payload, bytes) and payload.startswith(TYPED_MAGIC):
        return _typed_value(payload)[0]
    from .head_codec import MAGIC

    if isinstance(payload, bytes) and payload.startswith(MAGIC):
        return _head_value(payload)[0]
    return json.loads(sql_payload(payload))


def binary_payload(payload: bytes) -> str:
    if payload.startswith(TYPED_MAGIC):
        return _typed_value(payload)[1].decode("utf-8")
    from .head_codec import MAGIC

    if payload.startswith(MAGIC):
        return _head_value(payload)[1].decode()
    if not payload.startswith(BINARY_MAGIC) or len(payload) < 42:
        raise StateError("Invalid binary record envelope")
    fingerprint = payload[4:36]
    frame = payload[36:]
    try:
        parameters = zstandard.get_frame_parameters(frame)
        if (
            not 0 < parameters.content_size <= MAX_BYTES
            or parameters.window_size > MAX_BYTES
            or parameters.dict_id
            or not parameters.has_checksum
        ):
            raise StateError("Invalid binary record bounds")
        raw = zstandard.ZstdDecompressor(
            dict_data=BINARY_DICTIONARY, max_window_size=MAX_BYTES // 1024
        ).decompress(frame, max_output_size=parameters.content_size, allow_extra_data=False)
        if len(raw) != parameters.content_size or hashlib.sha256(raw).digest() != fingerprint:
            raise StateError("Binary record identity mismatch")
        return raw.decode("utf-8")
    except (zstandard.ZstdError, ValueError, UnicodeError) as exc:
        raise StateError("Invalid binary record encoding") from exc
