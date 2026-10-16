"""Optional v2 market layout: one immutable candle value and shared window references.

Enabled only by the explicit fresh-history reset, never while opening an old DB.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from typing import Any

DAY = 86_400_000
FIELDS = (
    "start",
    "step",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "source",
    "received_ms",
    "server_ms",
)


def enable(db: sqlite3.Connection) -> None:
    if db.execute("SELECT 1 FROM runtime LIMIT 1").fetchone():
        raise ValueError("Fresh market layout requires empty runtime")
    # Only the empty, newly prepared reset candidate reaches this path.
    # Incremental vacuum later releases expired pages in small bounded batches.
    db.execute("PRAGMA auto_vacuum=INCREMENTAL")
    db.execute("VACUUM")
    # Large immutable JSON envelopes use the ordinary rowid table. WITHOUT
    # ROWID's quarter-page local payload threshold otherwise creates a mostly
    # empty overflow page per1..2KiB record. Narrow normalized tables remain
    # WITHOUT ROWID; this choice is qualified by the storage measurement fixture.
    db.executescript("""
        CREATE TABLE market_streams(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
        CREATE TABLE market_candles(
            stream INTEGER NOT NULL REFERENCES market_streams(id),
            start INTEGER NOT NULL, revision INTEGER NOT NULL,
            payload BLOB NOT NULL, needed_until INTEGER NOT NULL DEFAULT 0, purge_after INTEGER NOT NULL,
            PRIMARY KEY(stream,start,revision)) WITHOUT ROWID;
        CREATE INDEX market_candle_expiry ON market_candles(purge_after,stream,start,revision);
        CREATE TABLE market_context_bodies(id INTEGER PRIMARY KEY,digest BLOB NOT NULL UNIQUE,payload BLOB NOT NULL,refs INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX market_context_garbage ON market_context_bodies(refs,id);
        CREATE TABLE market_context(
            stream INTEGER NOT NULL REFERENCES market_streams(id), kind TEXT NOT NULL,
            at INTEGER NOT NULL, body INTEGER NOT NULL REFERENCES market_context_bodies(id),
            timing BLOB NOT NULL, needed_anchor INTEGER NOT NULL DEFAULT 0, purge_after INTEGER NOT NULL,
            PRIMARY KEY(stream,kind,at)) WITHOUT ROWID;
        CREATE INDEX market_context_body ON market_context(body);
        CREATE INDEX market_context_expiry ON market_context(purge_after,stream,kind,at);
        CREATE TABLE market_holds(key TEXT PRIMARY KEY, stream INTEGER NOT NULL,
            start INTEGER NOT NULL,end INTEGER NOT NULL,expires INTEGER NOT NULL) WITHOUT ROWID;
        CREATE INDEX market_hold_stream ON market_holds(stream,start,end);
        CREATE TABLE market_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
        CREATE TABLE market_lifetimes(
            key TEXT PRIMARY KEY, expires INTEGER NOT NULL, category TEXT NOT NULL,event_id TEXT) WITHOUT ROWID;
        CREATE INDEX market_lifetime_event ON market_lifetimes(event_id,key);
        CREATE INDEX market_lifetime_expiry ON market_lifetimes(expires,key);
        CREATE TABLE market_body_counts(body TEXT PRIMARY KEY,refs INTEGER NOT NULL DEFAULT 0) WITHOUT ROWID;
        CREATE INDEX market_body_garbage ON market_body_counts(refs,body);
        CREATE TRIGGER market_blob_insert AFTER INSERT ON runtime WHEN NEW.key GLOB 'historyblob:v1:*' BEGIN
            INSERT OR IGNORE INTO market_body_counts VALUES (substr(NEW.key,16),0);
        END;
        CREATE TABLE market_body_links(
            owner TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(owner,body)) WITHOUT ROWID;
        CREATE INDEX market_body_reverse ON market_body_links(body,owner);
        CREATE TABLE market_counts(category TEXT PRIMARY KEY, records INTEGER NOT NULL,
            logical_bytes INTEGER NOT NULL) WITHOUT ROWID;
        CREATE TRIGGER market_link_insert AFTER INSERT ON market_body_links BEGIN
            UPDATE market_body_counts SET refs=refs+1 WHERE body=NEW.body;
        END;
        CREATE TRIGGER market_link_delete AFTER DELETE ON market_body_links BEGIN
            UPDATE market_body_counts SET refs=refs-1 WHERE body=OLD.body;
        END;
        CREATE TRIGGER market_context_insert AFTER INSERT ON market_context BEGIN
            UPDATE market_context_bodies SET refs=refs+1 WHERE id=NEW.body;
        END;
        CREATE TRIGGER market_context_delete AFTER DELETE ON market_context BEGIN
            UPDATE market_context_bodies SET refs=refs-1 WHERE id=OLD.body;
        END;
        PRAGMA user_version=2;
    """)

    from .quality_summary import create
    from .storage_meter import create as create_meter

    create(db)
    create_meter(db)


def active(db: sqlite3.Connection) -> bool:
    return db.execute("PRAGMA user_version").fetchone()[0] == 2


def encoded(value: Any) -> bytes:
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    compressed = zlib.compress(raw, 1)
    return b"Z" + compressed if len(compressed) < len(raw) else b"J" + raw


def decoded(payload: bytes, maximum: int = 8192) -> Any:
    if not payload or payload[:1] not in (b"J", b"Z"):
        raise ValueError("Invalid compact candle encoding")
    if payload[:1] == b"Z":
        reader = zlib.decompressobj()
        raw = reader.decompress(payload[1:], maximum + 1)
        if not reader.eof or reader.unused_data or reader.unconsumed_tail:
            raise ValueError("Invalid compact candle frame")
    else:
        raw = payload[1:]
    if len(raw) > maximum:
        raise ValueError("Compact candle exceeds decoder bound")
    return json.loads(raw)


def stream_id(db: sqlite3.Connection, name: str, *, create: bool = False) -> int | None:
    if create:
        db.execute("INSERT OR IGNORE INTO market_streams(name) VALUES (?)", (name,))
    row = db.execute("SELECT id FROM market_streams WHERE name=?", (name,)).fetchone()
    return row[0] if row else None


def admit(
    db: sqlite3.Connection, stream: str, candle: dict[str, Any], needed_until: int = 0
) -> tuple[int, int, int]:
    from .sources import Candle

    # Validate before any source becomes addressable by a history reference.
    c = Candle(**candle)
    if c != Candle.parse(c.row(), stream.rsplit("/", 1)[1], c.source, c.received_ms, c.server_ms):
        raise ValueError("Invalid normalized candle")
    sid = stream_id(db, stream, create=True)
    assert sid is not None
    value = encoded([candle[f] for f in FIELDS])
    settings = json.loads(db.execute("SELECT settings_json FROM owner_settings LIMIT 1").fetchone()[0])
    expiry = max(
        c.end + settings.get("candle_retention_days", 35) * DAY,
        needed_until + settings.get("detail_retention_days", 30) * DAY,
    )
    previous = db.execute(
        "SELECT revision,payload,needed_until FROM market_candles WHERE stream=? AND start=? ORDER BY revision DESC",
        (sid, c.start),
    ).fetchall()
    for row in previous:
        if bytes(row[1]) == value:
            if needed_until > row[2]:
                db.execute(
                    "UPDATE market_candles SET needed_until=?,purge_after=max(purge_after,?) WHERE stream=? AND start=? AND revision=?",
                    (needed_until, expiry, sid, c.start, row[0]),
                )
            return int(sid), c.start, row[0]
    revision = previous[0][0] + 1 if previous else 0
    db.execute(
        "INSERT INTO market_candles VALUES (?,?,?,?,?,?)",
        (sid, c.start, revision, value, needed_until, expiry),
    )
    return int(sid), c.start, revision


def public(payload: bytes) -> dict[str, Any]:
    values = decoded(payload)
    if not isinstance(values, list) or len(values) != len(FIELDS):
        raise ValueError("Invalid compact candle fields")
    return dict(zip(FIELDS, values))


def read(
    db: sqlite3.Connection, stream: str, start: int = 0, end: int = 2**63 - 1, limit: int | None = None
) -> list[dict[str, Any]]:
    sid = stream_id(db, stream)
    if sid is None:
        return []
    suffix = " ORDER BY c.start DESC" if limit is not None else " ORDER BY c.start"
    query = (
        """SELECT c.payload FROM market_candles c WHERE c.stream=? AND c.start>=? AND c.start<?
        AND c.revision=(SELECT MAX(v.revision) FROM market_candles v WHERE v.stream=c.stream AND v.start=c.start)"""
        + suffix
    )
    args: tuple[Any, ...] = (sid, start, end)
    if limit is not None:
        query += " LIMIT ?"
        args += (limit,)
    return [public(bytes(r[0])) for r in db.execute(query, args)]


def window(
    db: sqlite3.Connection, stream: str, candles: list[dict[str, Any]], expires: int
) -> dict[str, Any]:
    points = [admit(db, stream, c, expires) for c in candles]
    raw = json.dumps(
        candles, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    # Coordinates only; no OHLC arrays or overlapping window bodies are copied.
    import base64

    return {
        "mode": "source",
        "stream": points[0][0],
        "points": base64.b64encode(encoded([[p[1], p[2]] for p in points])).decode(),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def restore_window(db: sqlite3.Connection, reference: dict[str, Any]) -> bytes:
    import base64

    if (
        set(reference) != {"mode", "stream", "points", "sha256"}
        or type(reference["stream"]) is not int
        or not isinstance(reference["points"], str)
        or len(reference["points"]) > 65536
    ):
        raise ValueError("Invalid source window reference")
    points = decoded(base64.b64decode(reference["points"], validate=True), 131072)
    if not isinstance(points, list) or not 0 < len(points) <= 4096:
        raise ValueError("Invalid source window coordinates")
    candles = []
    for point in points:
        if not isinstance(point, list) or len(point) != 2 or any(type(v) is not int or v < 0 for v in point):
            raise ValueError("Invalid candle coordinate")
        row = db.execute(
            "SELECT payload FROM market_candles WHERE stream=? AND start=? AND revision=?",
            (reference["stream"], *point),
        ).fetchone()
        if row is None:
            raise ValueError("Missing retained candle revision")
        candles.append(public(bytes(row[0])))
    raw = json.dumps(
        candles, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
        raise ValueError("Source window hash mismatch")
    return raw


def context_public(db: sqlite3.Connection, row: Any) -> dict[str, Any]:
    body = db.execute("SELECT payload FROM market_context_bodies WHERE id=?", (row[0],)).fetchone()
    if body is None:
        raise ValueError("Missing context body")
    return {**decoded(bytes(body[0]), 131072), **decoded(bytes(row[1]), 8192)}


def save_context(
    db: sqlite3.Connection, kind: str, instrument: str, value: dict[str, Any], anchor: int = 0
) -> tuple[int, str, int]:
    sid = stream_id(db, instrument, create=True)
    assert sid is not None
    when = value["available_at_ms"]
    timing = {k: v for k, v in value.items() if k in ("available_at_ms", "market_observation_ms")}
    stable = {k: v for k, v in value.items() if k not in timing}
    raw = encoded(stable)
    digest = hashlib.sha256(
        json.dumps(
            stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).digest()
    db.execute("INSERT OR IGNORE INTO market_context_bodies(digest,payload) VALUES (?,?)", (digest, raw))
    body = db.execute("SELECT id,payload FROM market_context_bodies WHERE digest=?", (digest,)).fetchone()
    if decoded(bytes(body[1]), 131072) != stable:
        raise ValueError("Conflicting context digest")
    previous = db.execute(
        "SELECT body,timing FROM market_context WHERE stream=? AND kind=? AND at=?", (sid, kind, when)
    ).fetchone()
    if previous is not None and context_public(db, previous) != value:
        raise ValueError("Conflicting public source snapshot")
    settings = json.loads(db.execute("SELECT settings_json FROM owner_settings LIMIT 1").fetchone()[0])
    expires = max(
        when + settings.get("candle_retention_days", 35) * DAY,
        anchor + settings.get("detail_retention_days", 30) * DAY,
    )
    db.execute(
        "INSERT INTO market_context VALUES (?,?,?,?,?,?,?) ON CONFLICT(stream,kind,at) DO UPDATE SET needed_anchor=max(needed_anchor,excluded.needed_anchor),purge_after=max(purge_after,excluded.purge_after)",
        (sid, kind, when, body[0], encoded(timing), anchor, expires),
    )
    return sid, kind, when


def read_context(db: sqlite3.Connection, kind: str, instrument: str, cutoff: int) -> dict[str, Any] | None:
    sid = stream_id(db, instrument)
    row = db.execute(
        "SELECT body,timing FROM market_context WHERE stream=? AND kind=? AND at<=? ORDER BY at DESC LIMIT 1",
        (sid, kind, cutoff),
    ).fetchone()
    return context_public(db, row) if row else None


def context_window(db: sqlite3.Connection, stream: str, value: dict[str, Any], anchor: int) -> dict[str, Any]:
    points = {
        k: list(save_context(db, k, stream.rsplit("/", 1)[0], v, anchor)) if v is not None else None
        for k, v in value.items()
    }
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    return {"mode": "contexts", "points": points, "sha256": hashlib.sha256(raw).hexdigest()}


def restore_context(db: sqlite3.Connection, ref: dict[str, Any]) -> bytes:
    if (
        set(ref) != {"mode", "points", "sha256"}
        or not isinstance(ref["points"], dict)
        or set(ref["points"]) - {"ticker", "instrument"}
    ):
        raise ValueError("Invalid context reference")
    result: dict[str, Any] = {}
    for kind, point in ref["points"].items():
        if point is None:
            result[kind] = None
            continue
        if (
            not isinstance(point, list)
            or len(point) != 3
            or type(point[0]) is not int
            or type(point[2]) is not int
            or point[1] != kind
        ):
            raise ValueError("Invalid context coordinate")
        row = db.execute(
            "SELECT body,timing FROM market_context WHERE stream=? AND kind=? AND at=?", point
        ).fetchone()
        if row is None:
            raise ValueError("Missing retained context")
        result[kind] = context_public(db, row)
    raw = json.dumps(
        result, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
        raise ValueError("Context hash mismatch")
    return raw
