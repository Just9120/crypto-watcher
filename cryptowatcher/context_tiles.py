"""Explicit lossless context tiles and indexed mixed hot/cold point access.

The public key remains (stream, kind, available_at_ms). Tiles never overlap;
late points can remain hot inside a tile's interval. No layout is installed by
opening a Store, and publication/expiry never commit a caller's transaction.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

from . import context_tile_codec as codec
from .market_storage import DAY
from .storage import StateError, encode

TABLE = "market_context_tiles"
FIELDS = "id,stream,kind,day,payload,leases,frozen,seal"
META = "n,first_at,last_at,due"


def installed(db: sqlite3.Connection) -> bool:
    marker = db.execute("SELECT value FROM market_meta WHERE key='context_tiles'").fetchone()
    names = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('market_context_tiles','market_context_tile_pending')"
        )
    }
    if marker is None:
        if names:
            raise StateError("Unmarked context tile layout; database preserved")
        return False
    if marker[0] != "1" or len(names) != 2:
        raise StateError("Unknown or incomplete context tile layout; database preserved")
    return True


def _tile(row: Any) -> codec.Tile:
    return codec.Tile(*row[:4], *(bytes(value) for value in row[4:8]))


def _policy(db: sqlite3.Connection) -> tuple[int, int]:
    import json

    row = db.execute("SELECT settings_json FROM owner_settings LIMIT 1").fetchone()
    settings = json.loads(row[0])
    return settings.get("candle_retention_days", 35), settings.get("detail_retention_days", 30)


def _now(db: sqlite3.Connection) -> int:
    return db.execute("SELECT cw_clock_ms()").fetchone()[0]


@contextmanager
def _atomic(db: sqlite3.Connection) -> Iterator[None]:
    db.execute("SAVEPOINT context_tile_change")
    try:
        yield
        db.execute("RELEASE context_tile_change")
    except BaseException:
        db.execute("ROLLBACK TO context_tile_change")
        db.execute("RELEASE context_tile_change")
        raise


def install_functions(db: sqlite3.Connection) -> None:
    def valid(*values: Any) -> int:
        tile = _tile(values)
        stats = codec.stats(tile)
        return int(
            tuple(values[8:]) == tuple(stats[key] for key in ("count", "first_at", "last_at", "first_due"))
        )

    def change(*values: Any) -> int:
        old, new = _tile(values[:8]), _tile(values[8:])
        if (old.identity, old.stream, old.kind, old.day) != (new.identity, new.stream, new.kind, new.day):
            return 0
        original, updated = codec.points(old), codec.points(new)
        if old.payload == new.payload and len(original) == len(updated):
            for before, after in zip(original, updated):
                if after.anchor < before.anchor or after.purge < before.purge:
                    return 0
                if before.integrity is not None and after.integrity != before.integrity:
                    return 0
            return 1
        candle_days, detail_days = _policy(db)
        retained = {point.at: point for point in updated}
        if set(retained) - {point.at for point in original}:
            return 0
        now = _now(db)
        for before in original:
            retained_point = retained.get(before.at)
            due = max(before.purge, before.at + candle_days * DAY, before.anchor + detail_days * DAY)
            if retained_point is None:
                if due > now:
                    return 0
            elif (
                encode(codec.public(before)) != encode(codec.public(retained_point))
                or before.integrity != retained_point.integrity
                or retained_point.anchor < before.anchor
                or retained_point.purge < max(due, retained_point.anchor + detail_days * DAY)
            ):
                return 0
        return 1

    def removable(*values: Any) -> int:
        candle_days, detail_days = _policy(db)
        return int(
            codec.expire(_tile(values), _now(db), candle_days=candle_days, detail_days=detail_days) is None
        )

    def contains(payload: bytes, at: int) -> int:
        from . import indexed_frame

        return int(any(row[2] == at for row in indexed_frame.all_values(bytes(payload))))

    def times(payload: bytes) -> str:
        from . import indexed_frame

        return encode([row[2] for row in indexed_frame.all_values(bytes(payload))])

    db.create_function("cw_context_tile_valid", 12, valid, deterministic=True)
    db.create_function("cw_context_tile_change", 16, change)
    db.create_function("cw_context_tile_removable", 8, removable)
    db.create_function("cw_context_tile_contains", 2, contains, deterministic=True)
    db.create_function("cw_context_tile_times", 1, times, deterministic=True)


def install(db: sqlite3.Connection) -> None:
    if installed(db):
        return
    marker = db.execute("SELECT value FROM market_meta WHERE key='context_integrity'").fetchone()
    if db.in_transaction or marker is None or marker[0] != "1":
        raise StateError("Context tiles require a known outer integrity boundary")
    install_functions(db)
    new = ",".join("NEW." + field for field in FIELDS.split(","))
    old = ",".join("OLD." + field for field in FIELDS.split(","))
    size = "(length({a}.payload)+length({a}.leases)+length({a}.frozen)+length({a}.seal))"
    try:
        db.executescript(f"""
            BEGIN IMMEDIATE;
            CREATE TABLE market_context_tiles(
                id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(id>0 AND id<144115188075855872),
                stream INTEGER NOT NULL REFERENCES market_streams(id),
                kind TEXT NOT NULL CHECK(kind IN ('instrument','ticker')),
                day INTEGER NOT NULL CHECK(typeof(day)='integer' AND day>=0),
                payload BLOB NOT NULL,leases BLOB NOT NULL,
                frozen BLOB NOT NULL CHECK(typeof(frozen)='blob' AND length(frozen)=8),
                seal BLOB NOT NULL CHECK(typeof(seal)='blob' AND length(seal)=32 AND cw_owner_digest(stream,id,frozen)=seal),
                n INTEGER NOT NULL CHECK(n BETWEEN 1 AND 64),
                first_at INTEGER NOT NULL,last_at INTEGER NOT NULL,due INTEGER NOT NULL);
            CREATE UNIQUE INDEX market_context_tile_point ON market_context_tiles(stream,kind,first_at);
            CREATE INDEX market_context_tile_due ON market_context_tiles(due,id);
            CREATE TABLE market_context_tile_pending(
                stream INTEGER NOT NULL REFERENCES market_streams(id),kind TEXT NOT NULL,
                day INTEGER NOT NULL CHECK(typeof(day)='integer' AND day>=0),
                queued_at INTEGER NOT NULL CHECK(typeof(queued_at)='integer' AND queued_at>=0),
                eligible_at INTEGER CHECK(eligible_at IS NULL OR (typeof(eligible_at)='integer' AND eligible_at>=0)),
                blocked INTEGER NOT NULL DEFAULT 0 CHECK(blocked IN (0,1)),
                PRIMARY KEY(stream,kind,day)) WITHOUT ROWID;
            CREATE INDEX market_context_tile_age ON market_context_tile_pending(eligible_at,queued_at,stream,kind,day)
                WHERE eligible_at IS NOT NULL;
            CREATE INDEX market_context_tile_blocked ON market_context_tile_pending(stream,kind,day) WHERE blocked=1;
            CREATE INDEX market_context_tile_clock ON market_context_tile_pending(queued_at);
            CREATE TRIGGER market_context_tile_validate_insert BEFORE INSERT ON market_context_tiles BEGIN
                SELECT CASE WHEN cw_context_tile_valid({new},NEW.n,NEW.first_at,NEW.last_at,NEW.due)<>1
                    THEN RAISE(ABORT,'Invalid context tile metadata') END;
                SELECT CASE WHEN COALESCE((SELECT last_at FROM market_context_tiles WHERE stream=NEW.stream
                    AND kind=NEW.kind AND first_at<=NEW.last_at ORDER BY first_at DESC LIMIT 1),-1)>=NEW.first_at
                    THEN RAISE(ABORT,'Overlapping context tile') END;
                SELECT CASE WHEN EXISTS(SELECT 1 FROM json_each(cw_context_tile_times(NEW.payload)) p
                    JOIN market_context c ON c.stream=NEW.stream AND c.kind=NEW.kind AND c.at=p.value)
                    THEN RAISE(ABORT,'Duplicate hot/cold context ownership') END;
            END;
            CREATE TRIGGER market_context_tile_validate_update BEFORE UPDATE ON market_context_tiles BEGIN
                SELECT CASE WHEN cw_context_tile_valid({new},NEW.n,NEW.first_at,NEW.last_at,NEW.due)<>1
                    OR cw_context_tile_change({old},{new})<>1
                    THEN RAISE(ABORT,'Immutable context tile values or leases') END;
            END;
            CREATE TRIGGER market_context_tile_delete BEFORE DELETE ON market_context_tiles BEGIN
                SELECT CASE WHEN cw_context_tile_removable({old})<>1
                    THEN RAISE(ABORT,'Retained context tile') END;
            END;
            INSERT INTO market_meta VALUES('context_tiles','1');
        """)
        # Hot writers cannot accidentally re-create a point already owned by a
        # tile, including when a connection was opened before explicit install.
        for event in ("INSERT", "UPDATE OF stream,kind,at"):
            label = "insert" if event == "INSERT" else "update"
            db.execute(f"""CREATE TRIGGER market_context_tile_hot_{label} BEFORE {event} ON market_context BEGIN
                SELECT CASE WHEN COALESCE((SELECT cw_context_tile_contains(payload,NEW.at)
                    FROM market_context_tiles WHERE stream=NEW.stream AND kind=NEW.kind
                    AND first_at<=NEW.at ORDER BY first_at DESC LIMIT 1),0)=1
                    THEN RAISE(ABORT,'Context point already tiled') END;
                END""")
        for event in ("INSERT", "UPDATE", "DELETE"):
            ns, os = size.format(a="NEW"), size.format(a="OLD")
            if event == "INSERT":
                body = f"""INSERT INTO market_counts VALUES('context_tiles',NEW.n,{ns},{ns},0)
                    ON CONFLICT(category) DO UPDATE SET records=records+NEW.n,
                    logical_bytes=logical_bytes+{ns},inserted_bytes=inserted_bytes+{ns};"""
            elif event == "DELETE":
                body = f"""UPDATE market_counts SET records=records-OLD.n,logical_bytes=logical_bytes-{os},
                    deleted_bytes=deleted_bytes+{os} WHERE category='context_tiles';"""
            else:
                body = f"""UPDATE market_counts SET records=records+NEW.n-OLD.n,logical_bytes=logical_bytes+{ns}-{os},
                    inserted_bytes=inserted_bytes+max(0,{ns}-{os}),deleted_bytes=deleted_bytes+max(0,{os}-{ns})
                    WHERE category='context_tiles';"""
            db.execute(
                f"CREATE TRIGGER market_context_tile_count_{event} AFTER {event} ON market_context_tiles BEGIN {body} END"
            )
        hot_count = """(SELECT count(*) FROM (SELECT at FROM market_context
            WHERE stream=NEW.stream AND kind=NEW.kind AND at>=NEW.at/86400000*86400000
                AND at<(NEW.at/86400000+1)*86400000 LIMIT 64))"""
        db.execute(f"""CREATE TRIGGER market_context_tile_enqueue AFTER INSERT ON market_context
            WHEN NEW.at>=0 AND NEW.kind IN ('instrument','ticker') BEGIN
            INSERT INTO market_context_tile_pending(stream,kind,day,queued_at,eligible_at)
                VALUES(NEW.stream,NEW.kind,NEW.at/86400000,cw_clock_ms(),
                    CASE WHEN {hot_count}>=64 THEN cw_clock_ms() WHEN {hot_count}>=8
                         THEN max(cw_clock_ms(),(NEW.at/86400000+1)*86400000) ELSE NULL END)
                ON CONFLICT(stream,kind,day) DO UPDATE SET blocked=0,
                    eligible_at=CASE WHEN eligible_at IS NULL THEN excluded.eligible_at
                        WHEN excluded.eligible_at IS NULL THEN eligible_at ELSE min(eligible_at,excluded.eligible_at) END;
            END""")
        for event in ("INSERT", "DELETE"):
            sql = (
                "INSERT INTO market_counts VALUES('context_tile_pending',1,48,48,0) "
                "ON CONFLICT(category) DO UPDATE SET records=records+1,logical_bytes=logical_bytes+48,inserted_bytes=inserted_bytes+48;"
                if event == "INSERT"
                else "UPDATE market_counts SET records=records-1,logical_bytes=logical_bytes-48,deleted_bytes=deleted_bytes+48 WHERE category='context_tile_pending';"
            )
            db.execute(
                f"CREATE TRIGGER market_context_tile_pending_{event} AFTER {event} ON market_context_tile_pending BEGIN {sql} END"
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def hot_point(db: sqlite3.Connection, row: Any) -> codec.Point:
    from .market_storage import context_public, decoded

    value = context_public(db, row[3:5])
    timing = decoded(bytes(row[4]), 8192)
    point = codec.Point(
        row[0],
        row[1],
        row[2],
        {key: value for key, value in value.items() if key not in timing},
        timing,
        row[5],
        row[6],
        bytes(row[7]) if row[7] is not None else None,
    )
    # Validate before removing any source, including an old frozen SHA.
    if point.integrity is not None and codec.digest(point) != point.integrity:
        raise ValueError("Frozen context hash mismatch")
    return point


def cold(
    db: sqlite3.Connection, stream: int, kind: str, at: int, *, exact: bool = True
) -> tuple[codec.Tile, int] | None:
    if not installed(db):
        return None
    row = db.execute(
        f"SELECT {FIELDS} FROM {TABLE} WHERE stream=? AND kind=? AND first_at<=? ORDER BY first_at DESC LIMIT 1",
        (stream, kind, at),
    ).fetchone()
    if row is None:
        return None
    tile = _tile(row)
    rows = codec.points(tile)
    eligible = [slot for slot, point in enumerate(rows) if point.at == at or (not exact and point.at <= at)]
    return (tile, eligible[-1]) if eligible else None


def lookup(
    db: sqlite3.Connection, stream: int, kind: str, at: int, *, exact: bool = True
) -> codec.Point | None:
    row = db.execute(
        "SELECT stream,kind,at,body,timing,needed_anchor,purge_after,integrity FROM market_context "
        + (
            "WHERE stream=? AND kind=? AND at=?"
            if exact
            else "WHERE stream=? AND kind=? AND at<=? ORDER BY at DESC LIMIT 1"
        ),
        (stream, kind, at),
    ).fetchone()
    hot = hot_point(db, row) if row is not None else None
    found = cold(db, stream, kind, at, exact=exact)
    tiled = codec.get(*found) if found is not None else None
    if hot is not None and tiled is not None and hot.at == tiled.at:
        raise StateError("Duplicate hot/cold context ownership")
    return hot if hot is not None and (tiled is None or hot.at > tiled.at) else tiled


def _values(tile: codec.Tile) -> tuple[Any, ...]:
    stats = codec.stats(tile)
    return (
        tile.identity,
        tile.stream,
        tile.kind,
        tile.day,
        tile.payload,
        tile.leases,
        tile.frozen,
        tile.seal,
        stats["count"],
        stats["first_at"],
        stats["last_at"],
        stats["first_due"],
    )


def _update(db: sqlite3.Connection, old: codec.Tile, new: codec.Tile) -> None:
    if old == new:
        return
    result = db.execute(
        f"UPDATE {TABLE} SET payload=?,leases=?,frozen=?,seal=?,n=?,first_at=?,last_at=?,due=? "
        "WHERE id=? AND payload=? AND leases=? AND frozen=? AND seal=?",
        (*_values(new)[4:], old.identity, old.payload, old.leases, old.frozen, old.seal),
    )
    if result.rowcount != 1:
        raise StateError("Stale context tile revision")


def renew(db: sqlite3.Connection, found: tuple[codec.Tile, int], *, anchor: int, purge: int) -> None:
    old, slot = found
    _update(db, old, codec.renew(old, slot, anchor=anchor, purge=purge))


def freeze(db: sqlite3.Connection, found: tuple[codec.Tile, int], expected: bytes) -> None:
    old, slot = found
    _update(db, old, codec.freeze(old, slot, expected))


def publish(db: sqlite3.Connection, keys: list[tuple[int, str, int]]) -> int:
    """Atomically replace 1..64 exact hot rows; unsupported inputs remain hot."""
    if not installed(db) or not 0 < len(keys) <= 64 or len(set(keys)) != len(keys):
        raise StateError("Invalid context tile publication boundary")
    with _atomic(db):
        source = []
        for key in keys:
            row = db.execute(
                "SELECT stream,kind,at,body,timing,needed_anchor,purge_after,integrity FROM market_context "
                "WHERE stream=? AND kind=? AND at=?",
                key,
            ).fetchone()
            if row is None:
                raise StateError("Missing hot context publication source")
            source.append(tuple(row))
        seq = db.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (TABLE,)).fetchone()
        identity = (seq[0] if seq is not None else 0) + 1
        expected = [hot_point(db, row) for row in source]
        tile = codec.pack(identity, expected)
        for row in source:
            result = db.execute(
                "DELETE FROM market_context WHERE stream=? AND kind=? AND at=? AND body=? AND timing=? "
                "AND needed_anchor=? AND purge_after=? AND integrity IS ?",
                row,
            )
            if result.rowcount != 1:
                raise StateError("Stale hot context publication source")
        db.execute(
            f"INSERT INTO {TABLE}({FIELDS},{META}) VALUES ({','.join('?' for _ in range(12))})", _values(tile)
        )
        actual = db.execute(f"SELECT {FIELDS} FROM {TABLE} WHERE id=?", (identity,)).fetchone()
        if actual is None or codec.signature(codec.points(_tile(actual))) != codec.signature(expected):
            raise StateError("Context tile publication changed source")
        return identity


def expire(db: sqlite3.Connection, now: int, *, batch: int = 1) -> int:
    if not installed(db):
        return 0
    if type(now) is not int or now < 0 or type(batch) is not int or not 0 < batch <= 64:
        raise StateError("Context tile expiry requires the current bounded clock")
    candle_days, detail_days = _policy(db)
    removed = 0
    with _atomic(db):
        candidates = db.execute(
            f"SELECT {FIELDS} FROM {TABLE} WHERE due<=? ORDER BY due,id LIMIT ?",
            (now, batch),
        ).fetchall()
        if candidates and now > _now(db):
            raise StateError("Context tile expiry requires the current bounded clock")
        for row in candidates:
            old = _tile(row)
            new = codec.expire(old, now, candle_days=candle_days, detail_days=detail_days)
            removed += codec.stats(old)["count"] - (codec.stats(new)["count"] if new is not None else 0)
            if new is not None:
                _update(db, old, new)
            elif (
                db.execute(
                    f"DELETE FROM {TABLE} WHERE id=? AND payload=? AND leases=? AND frozen=? AND seal=?",
                    (old.identity, old.payload, old.leases, old.frozen, old.seal),
                ).rowcount
                != 1
            ):
                raise StateError("Stale context tile expiry source")
    return removed
