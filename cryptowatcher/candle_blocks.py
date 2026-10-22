"""Atomic SB1 tile storage, before runtime activation or legacy transfer.

Only supported, already validated coordinates enter a tile. SQL guards bind
its header/count/expiry, preserve every old source value and qualify removals
against the same current per-entry policy/holds as the legacy source table.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from . import candle_block_codec as codec
from .market_storage import candle_deadline
from .storage import encode


def policy(db: sqlite3.Connection) -> tuple[int, int]:
    row = db.execute("SELECT settings_json FROM owner_settings").fetchone()
    if row is None:
        raise ValueError("Missing source retention policy")
    value = json.loads(row[0])
    days = value.get("candle_retention_days", 35), value.get("detail_retention_days", 30)
    if any(type(v) is not int or v < 1 for v in days):
        raise ValueError("Invalid source retention policy")
    return days


def install_functions(db: sqlite3.Connection, cache: Any = None, binary_cache: Any = None) -> None:
    if (
        db.execute("SELECT 1 FROM sqlite_master WHERE name='market_candle_gc'").fetchone()
        and db.execute("SELECT 1 FROM market_candle_gc LIMIT 1").fetchone()
    ):
        raise ValueError("Unfinished source expiry authority")

    marker = db.execute("SELECT value FROM market_meta WHERE key='candle_blocks'").fetchone()
    if (marker is None or marker[0] != "2") and db.execute(
        "SELECT 1 FROM market_meta WHERE key='candle_column_repack'"
    ).fetchone():
        raise ValueError("Source column boundary lacks explicit adoption")
    if marker is not None and marker[0] == "2":
        from .source_column_work import checkpoint, guards

        guards(db)
        checkpoint(db)

    def stats(raw: bytes) -> str:
        if isinstance(raw, bytes) and raw.startswith(codec.COLUMN_MAGIC):
            from .source_column_work import readable

            readable(db, raw)

        def read() -> str:
            return encode(codec.stats(raw, cache=binary_cache))

        return read() if cache is None else str(cache("source-tile-stats", "", raw, read))

    def change(old: bytes, new: bytes | None) -> int:
        if new is None:
            from .source_column_work import _authorized_delete

            if _authorized_delete(db, old):
                return 1
        removed = (
            codec.rows(old, cache=binary_cache)
            if new is None
            else codec.preserved(old, new, cache=binary_cache)
        )
        if removed:
            permit = db.execute("SELECT cutoff FROM market_candle_gc WHERE id=1").fetchone()
            if permit is None:
                raise ValueError("Source deletion requires qualified expiry")
            candle_days, detail_days = policy(db)
            for sid, start, revision, anchor, purge, value in removed:
                if (
                    candle_deadline(
                        db, sid, start, value["step"], anchor, permit[0], candle_days, detail_days
                    )
                    > permit[0]
                ):
                    raise ValueError("Retained source candle")
        return 1

    db.create_function("cw_candle_block_stats", 1, stats)
    db.create_function("cw_candle_block_change", 2, change)
    db.create_function("cw_candle_stream_step", 1, lambda sid: stream_step(db, sid))


def install(db: sqlite3.Connection, cache: Any = None, binary_cache: Any = None) -> None:
    if db.execute("PRAGMA user_version").fetchone()[0] != 2:
        raise ValueError("Source tiles require the normalized source layout")
    install_functions(db, cache, binary_cache)
    marker = db.execute("SELECT value FROM market_meta WHERE key='candle_blocks'").fetchone()
    if marker is not None:
        if marker[0] not in ("1", "2") or db.execute("SELECT 1 FROM market_candle_gc LIMIT 1").fetchone():
            raise ValueError("Unknown source tile state; database preserved")
        return
    if db.in_transaction:
        raise ValueError("Source tile installation requires an outer boundary")
    try:
        db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE market_candle_blocks(
                id INTEGER PRIMARY KEY,stream INTEGER NOT NULL REFERENCES market_streams(id),
                tile INTEGER NOT NULL,step INTEGER NOT NULL,row_count INTEGER NOT NULL,
                first_due INTEGER NOT NULL,payload BLOB NOT NULL CHECK(typeof(payload)='blob'),UNIQUE(stream,tile));
            CREATE INDEX market_candle_block_due ON market_candle_blocks(first_due,id);
            CREATE TABLE market_candle_fallbacks(
                stream INTEGER NOT NULL REFERENCES market_streams(id),tile INTEGER NOT NULL,
                reason TEXT NOT NULL,PRIMARY KEY(stream,tile)) WITHOUT ROWID;
            CREATE TABLE market_candle_block_totals(
                id INTEGER PRIMARY KEY CHECK(id=1),blocks INTEGER NOT NULL CHECK(typeof(blocks)='integer' AND blocks>=0),
                versions INTEGER NOT NULL CHECK(typeof(versions)='integer' AND versions>=0),
                payload_bytes INTEGER NOT NULL CHECK(typeof(payload_bytes)='integer' AND payload_bytes>=0),
                fallbacks INTEGER NOT NULL CHECK(typeof(fallbacks)='integer' AND fallbacks>=0),
                index_bytes INTEGER NOT NULL CHECK(typeof(index_bytes)='integer' AND index_bytes>=0));
            INSERT INTO market_candle_block_totals VALUES(1,0,0,0,0,0);
            CREATE TABLE market_candle_gc(id INTEGER PRIMARY KEY CHECK(id=1),cutoff INTEGER NOT NULL CHECK(typeof(cutoff)='integer' AND cutoff>=0));
            CREATE TRIGGER market_candle_block_insert_guard BEFORE INSERT ON market_candle_blocks BEGIN
                SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM market_candle_block_totals WHERE id=1)
                    THEN RAISE(ABORT,'Missing source tile counter') END;
                SELECT CASE WHEN NEW.step IS NOT cw_candle_stream_step(NEW.stream)
                    OR NEW.stream IS NOT json_extract(cw_candle_block_stats(NEW.payload),'$[0]')
                    OR NEW.tile IS NOT json_extract(cw_candle_block_stats(NEW.payload),'$[1]')
                    OR NEW.step IS NOT json_extract(cw_candle_block_stats(NEW.payload),'$[2]')
                    OR NEW.row_count IS NOT json_extract(cw_candle_block_stats(NEW.payload),'$[3]')
                    OR NEW.first_due IS NOT json_extract(cw_candle_block_stats(NEW.payload),'$[4]')
                    THEN RAISE(ABORT,'Source tile metadata mismatch') END;
            END;
            CREATE TRIGGER market_candle_block_update_guard BEFORE UPDATE ON market_candle_blocks BEGIN
                SELECT CASE WHEN NEW.stream<>OLD.stream OR NEW.tile<>OLD.tile OR NEW.step<>OLD.step
                    OR cw_candle_block_change(OLD.payload,NEW.payload)<>1
                    THEN RAISE(ABORT,'Immutable source tile binding') END;
                SELECT CASE WHEN NEW.step IS NOT cw_candle_stream_step(NEW.stream)
                    OR NEW.stream IS NOT json_extract(cw_candle_block_stats(NEW.payload),'$[0]')
                    OR NEW.tile IS NOT json_extract(cw_candle_block_stats(NEW.payload),'$[1]')
                    OR NEW.step IS NOT json_extract(cw_candle_block_stats(NEW.payload),'$[2]')
                    OR NEW.row_count IS NOT json_extract(cw_candle_block_stats(NEW.payload),'$[3]')
                    OR NEW.first_due IS NOT json_extract(cw_candle_block_stats(NEW.payload),'$[4]')
                    THEN RAISE(ABORT,'Source tile metadata mismatch') END;
                SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM market_candle_block_totals WHERE id=1
                    AND blocks>0 AND versions>=OLD.row_count AND payload_bytes>=length(OLD.payload))
                    OR NOT EXISTS(SELECT 1 FROM market_counts WHERE category='source_blocks' AND records>0 AND logical_bytes>=length(OLD.payload))
                    THEN RAISE(ABORT,'Missing source tile counter') END;
            END;
            CREATE TRIGGER market_candle_block_delete_guard BEFORE DELETE ON market_candle_blocks BEGIN
                SELECT CASE WHEN cw_candle_block_change(OLD.payload,NULL)<>1
                    OR NOT EXISTS(SELECT 1 FROM market_candle_block_totals WHERE id=1 AND blocks>0 AND versions>=OLD.row_count AND payload_bytes>=length(OLD.payload))
                    OR NOT EXISTS(SELECT 1 FROM market_counts WHERE category='source_blocks' AND records>0 AND logical_bytes>=length(OLD.payload))
                    THEN RAISE(ABORT,'Missing source tile counter or expiry authority') END;
            END;
            CREATE TRIGGER market_candle_block_insert AFTER INSERT ON market_candle_blocks BEGIN
                UPDATE market_candle_block_totals SET blocks=blocks+1,versions=versions+NEW.row_count,payload_bytes=payload_bytes+length(NEW.payload) WHERE id=1;
                INSERT INTO market_counts VALUES('source_blocks',1,length(NEW.payload),length(NEW.payload),0)
                    ON CONFLICT(category) DO UPDATE SET records=records+1,logical_bytes=logical_bytes+excluded.logical_bytes,inserted_bytes=inserted_bytes+excluded.inserted_bytes;
            END;
            CREATE TRIGGER market_candle_block_update AFTER UPDATE ON market_candle_blocks BEGIN
                UPDATE market_candle_block_totals SET versions=versions+NEW.row_count-OLD.row_count,payload_bytes=payload_bytes+length(NEW.payload)-length(OLD.payload) WHERE id=1;
                UPDATE market_counts SET logical_bytes=logical_bytes+length(NEW.payload)-length(OLD.payload),
                    inserted_bytes=inserted_bytes+max(0,length(NEW.payload)-length(OLD.payload)),
                    deleted_bytes=deleted_bytes+max(0,length(OLD.payload)-length(NEW.payload)) WHERE category='source_blocks';
            END;
            CREATE TRIGGER market_candle_block_delete AFTER DELETE ON market_candle_blocks BEGIN
                UPDATE market_candle_block_totals SET blocks=blocks-1,versions=versions-OLD.row_count,payload_bytes=payload_bytes-length(OLD.payload) WHERE id=1;
                UPDATE market_counts SET records=records-1,logical_bytes=logical_bytes-length(OLD.payload),deleted_bytes=deleted_bytes+length(OLD.payload) WHERE category='source_blocks';
            END;
            CREATE TRIGGER market_candle_stream_binding BEFORE UPDATE OF id,name ON market_streams
                WHEN (NEW.id<>OLD.id OR NEW.name<>OLD.name) AND EXISTS(SELECT 1 FROM market_candle_blocks WHERE stream=OLD.id)
                BEGIN SELECT RAISE(ABORT,'Retained source stream binding'); END;
            CREATE TRIGGER market_candle_fallback_insert AFTER INSERT ON market_candle_fallbacks BEGIN
                SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM market_candle_block_totals WHERE id=1)
                    THEN RAISE(ABORT,'Missing source fallback counter') END;
                UPDATE market_candle_block_totals SET fallbacks=fallbacks+1,index_bytes=index_bytes+16+length(CAST(NEW.reason AS BLOB)) WHERE id=1;
                INSERT INTO market_counts VALUES('source_block_indexes',1,16+length(CAST(NEW.reason AS BLOB)),16+length(CAST(NEW.reason AS BLOB)),0)
                    ON CONFLICT(category) DO UPDATE SET records=records+1,logical_bytes=logical_bytes+excluded.logical_bytes,inserted_bytes=inserted_bytes+excluded.inserted_bytes;
            END;
            CREATE TRIGGER market_candle_fallback_update BEFORE UPDATE ON market_candle_fallbacks
                WHEN NEW.stream<>OLD.stream OR NEW.tile<>OLD.tile OR NEW.reason<>OLD.reason
                BEGIN SELECT RAISE(ABORT,'Immutable source fallback'); END;
            CREATE TRIGGER market_candle_fallback_delete AFTER DELETE ON market_candle_fallbacks BEGIN
                SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM market_candle_block_totals WHERE id=1 AND fallbacks>0 AND index_bytes>=16+length(CAST(OLD.reason AS BLOB)))
                    OR NOT EXISTS(SELECT 1 FROM market_counts WHERE category='source_block_indexes' AND records>0 AND logical_bytes>=16+length(CAST(OLD.reason AS BLOB)))
                    THEN RAISE(ABORT,'Missing source fallback counter') END;
                UPDATE market_candle_block_totals SET fallbacks=fallbacks-1,index_bytes=index_bytes-16-length(CAST(OLD.reason AS BLOB)) WHERE id=1;
                UPDATE market_counts SET records=records-1,logical_bytes=logical_bytes-16-length(CAST(OLD.reason AS BLOB)),deleted_bytes=deleted_bytes+16+length(CAST(OLD.reason AS BLOB)) WHERE category='source_block_indexes';
            END;
            INSERT INTO market_meta VALUES('candle_blocks','1');
            COMMIT;
        """)
    except BaseException:
        db.rollback()
        raise


@contextmanager
def atomic(db: sqlite3.Connection) -> Iterator[None]:
    if not db.in_transaction:
        db.execute("BEGIN")
    db.execute("SAVEPOINT cw_source_operation")
    try:
        yield
    except BaseException:
        db.execute("ROLLBACK TO cw_source_operation")
        db.execute("RELEASE cw_source_operation")
        raise
    else:
        db.execute("RELEASE cw_source_operation")


def put(db: sqlite3.Connection, raw: bytes, expected: bytes | None, *, cache: Any = None) -> None:
    if expected is not None and expected.startswith(codec.COLUMN_MAGIC):
        from .source_column_work import readable

        readable(db, expected)
        raw = codec.compact(raw)
    sid, tile, step, n, first = codec.stats(raw, cache=cache)
    with atomic(db):
        if expected is None:
            db.execute(
                "INSERT INTO market_candle_blocks(stream,tile,step,row_count,first_due,payload) VALUES(?,?,?,?,?,?)",
                (sid, tile, step, n, first, raw),
            )
        elif (
            db.execute(
                "UPDATE market_candle_blocks SET row_count=?,first_due=?,payload=? WHERE stream=? AND tile=? AND payload=?",
                (n, first, raw, sid, tile, expected),
            ).rowcount
            != 1
        ):
            raise ValueError("Source tile revision changed")


def stream_step(db: sqlite3.Connection, sid: int) -> int:
    codec.number(sid, positive=True)
    row = db.execute("SELECT name FROM market_streams WHERE id=?", (sid,)).fetchone()
    if row is None:
        raise ValueError("Missing source stream")
    tf = row[0].rsplit("/", 1)[-1]
    if tf not in ("1m", "3m", "5m", "15m"):
        raise ValueError("Unsupported source stream timeframe")
    return int(tf[:-1]) * 60000


def admit_many(
    db: sqlite3.Connection, sid: int, values: list[dict[str, Any]], anchor: int = 0
) -> list[tuple[int, int, int]]:
    """Batch already validated sources; exact old values reuse their revision.

    No standalone helper silently merges an untransferred legacy tile. The
    runtime adapter must transfer it in this same transaction before admission.
    """
    codec.number(anchor)
    step = stream_step(db, sid)
    result = _admit_groups(db, sid, validated_groups(step, values), anchor)
    return [result[index] for index in range(len(values))]


def validated_groups(step: int, values: list[dict[str, Any]]) -> dict[int, list[tuple[int, int, list[Any]]]]:
    """Validate the complete batch once, before either tile or legacy admission."""
    groups: dict[int, list[tuple[int, int, list[Any]]]] = {}
    for index, value in enumerate(values):
        if set(value) != {"start", "step", *codec.TAIL} or value["step"] != step:
            raise ValueError("Source candle fields do not match stream")
        start = codec.number(value["start"])
        if start % step:
            raise ValueError("Source candle is not aligned")
        at = start // (32 * step) * (32 * step)
        slot = (start - at) // step
        tail = [value[k] for k in codec.TAIL]
        codec.public(at, step, slot, tail)
        groups.setdefault(at, []).append((index, slot, tail))
    return groups


def _admit_groups(
    db: sqlite3.Connection,
    sid: int,
    groups: dict[int, list[tuple[int, int, list[Any]]]],
    anchor: int,
    *,
    cache: Any = None,
) -> dict[int, tuple[int, int, int]]:
    """Consume groups just checked by validated_groups in this synchronous call."""
    codec.number(anchor)
    step = stream_step(db, sid)
    result: dict[int, tuple[int, int, int]] = {}
    with atomic(db):
        candle_days, detail_days = policy(db)
        for at, items in groups.items():
            if db.execute(
                "SELECT 1 FROM market_candles WHERE stream=? AND start>=? AND start<? LIMIT 1",
                (sid, at, at + 32 * step),
            ).fetchone():
                raise ValueError("Legacy source tile requires atomic transfer before admission")
            old = tile(db, sid, at, step)
            # Actual frame SHA and typed shape are checked. SQL admission
            # already validated frozen values; every changed frame is fully
            # validated again by pack and the existing SQL guards below.
            slots = (
                codec.unpack(old, validate=False, cache=cache)[3]
                if old is not None
                else [[] for _ in range(32)]
            )
            dirty = False
            for index, slot, tail in items:
                at_start = at + slot * step
                expiry = max(at_start + step + candle_days * 86400000, anchor + detail_days * 86400000)
                existing = next((v for v in reversed(slots[slot]) if encode(v[3]) == encode(tail)), None)
                if existing is not None:
                    revision = existing[0]
                    needed = max(existing[1], anchor)
                    purge = max(existing[2], expiry)
                    if (needed, purge) != (existing[1], existing[2]):
                        existing[1:3] = [needed, purge]
                        dirty = True
                else:
                    revision = (slots[slot][-1][0] if slots[slot] else -1) + 1
                    slots[slot].append([revision, anchor, expiry, tail])
                    dirty = True
                result[index] = (sid, at_start, revision)
            if dirty:
                put(db, codec.pack(sid, at, step, slots), old, cache=cache)
    return result


def import_tile(db: sqlite3.Connection, sid: int, start: int) -> int:
    """Transfer a whole eligible legacy tile atomically, preserving source IDs.

    Unusually large correction histories stay in the legacy layout. A caller
    must record that supported fallback rather than silently dropping rows.
    """
    from .market_storage import public

    step = stream_step(db, sid)
    codec.number(start)
    at = start // (32 * step) * (32 * step)
    with atomic(db):
        legacy = db.execute(
            "SELECT stream,start,revision,payload,needed_until,purge_after FROM market_candles WHERE stream=? AND start>=? AND start<? ORDER BY start,revision LIMIT ?",
            (sid, at, at + 32 * step, codec.MAX_ROWS + 1),
        ).fetchall()
        if not legacy:
            return 0
        if len(legacy) > codec.MAX_ROWS:
            raise codec.Capacity("Source tile row bound")
        old = tile(db, sid, at, step)
        slots = codec.unpack(old)[3] if old is not None else [[] for _ in range(32)]
        for _, when, revision, payload, needed, purge in legacy:
            value = public(bytes(payload))
            if value["start"] != when or value["step"] != step or when % step:
                raise ValueError("Legacy source coordinates do not match tile")
            slot = (when - at) // step
            tail = [value[k] for k in codec.TAIL]
            previous = next((r for r in slots[slot] if r[0] == revision), None)
            if previous is not None:
                if encode(previous[3]) != encode(tail):
                    raise ValueError("Conflicting immutable source revision")
                previous[1] = max(previous[1], needed)
                previous[2] = max(previous[2], purge)
            else:
                slots[slot].append([revision, needed, purge, tail])
                slots[slot].sort(key=lambda r: r[0])
        replacement = codec.pack(sid, at, step, slots)
        retained = {(r[1], r[2]): r for r in codec.rows(replacement)}
        for _, when, revision, payload, needed, purge in legacy:
            actual = retained[(when, revision)]
            if encode(actual[5]) != encode(public(bytes(payload))) or actual[3] < needed or actual[4] < purge:
                raise ValueError("Source transfer changed retained data")
        counter = db.execute(
            "SELECT records,logical_bytes FROM market_counts WHERE category='candles'"
        ).fetchone()
        if counter is None or counter[0] < len(legacy) or counter[1] < sum(len(r[3]) for r in legacy):
            raise ValueError("Missing legacy source counter")
        put(db, replacement, old)
        count = db.executemany(
            "DELETE FROM market_candles WHERE stream=? AND start=? AND revision=? AND payload=? AND needed_until=? AND purge_after=?",
            [tuple(r) for r in legacy],
        ).rowcount
        if count != len(legacy):
            raise ValueError("Legacy source revision changed during transfer")
        return count


def gc(db: sqlite3.Connection, now: int, *, batch: int = 16, seconds: float = 0.05) -> int:
    """Re-evaluate each due revision; holds never apply to a whole tile."""
    codec.number(now)
    if (
        type(batch) is not int
        or not 0 < batch <= 256
        or type(seconds) not in (int, float)
        or not math.isfinite(seconds)
        or seconds <= 0
    ):
        raise ValueError("Invalid source collection budget")
    deadline = time.monotonic() + seconds
    removed = 0
    with atomic(db):
        if db.execute("SELECT 1 FROM market_candle_gc LIMIT 1").fetchone():
            raise ValueError("Unexpected source expiry authority")
        db.execute("INSERT INTO market_candle_gc VALUES(1,?)", (now,))
        try:
            candle_days, detail_days = policy(db)
            candidates = db.execute(
                "SELECT id,payload FROM market_candle_blocks WHERE first_due<=? ORDER BY first_due,id LIMIT ?",
                (now, batch),
            ).fetchall()
            for identity, payload in candidates:
                if time.monotonic() >= deadline:
                    break
                old = bytes(payload)
                sid, at, step, slots = codec.unpack(old)
                dirty = False
                for slot, versions in enumerate(slots):
                    keep = []
                    for entry in versions:
                        revision, needed, purge, tail = entry
                        if purge > now:
                            keep.append(entry)
                            continue
                        actual = candle_deadline(
                            db, sid, at + slot * step, step, needed, now, candle_days, detail_days
                        )
                        if actual > now:
                            entry[2] = actual
                            keep.append(entry)
                        else:
                            removed += 1
                        dirty = True
                    slots[slot] = keep
                if dirty:
                    if any(slots):
                        put(db, codec.pack(sid, at, step, slots), old)
                    elif (
                        db.execute(
                            "DELETE FROM market_candle_blocks WHERE id=? AND payload=?", (identity, old)
                        ).rowcount
                        != 1
                    ):
                        raise ValueError("Source tile revision changed during expiry")
        finally:
            db.execute("DELETE FROM market_candle_gc WHERE id=1")
        collect_fallbacks(db, batch, deadline)
    return removed


def collect_fallbacks(db: sqlite3.Connection, batch: int, deadline: float) -> int:
    """Bounded cyclic keyset sweep of metadata whose legacy tile has expired."""
    marker = db.execute("SELECT value FROM market_meta WHERE key='candle_fallback_cursor'").fetchone()
    point = json.loads(marker[0]) if marker else [0, 0]
    if not isinstance(point, list) or len(point) != 2:
        raise ValueError("Invalid source fallback cursor")
    for n in point:
        codec.number(n)
    rows = db.execute(
        "SELECT stream,tile FROM market_candle_fallbacks WHERE (stream,tile)>(?,?) ORDER BY stream,tile LIMIT ?",
        (*point, batch),
    ).fetchall()
    removed = 0
    scanned = 0
    for sid, at in rows:
        if time.monotonic() >= deadline:
            break
        end = at + codec.SLOTS * stream_step(db, sid)
        if not db.execute(
            "SELECT 1 FROM market_candles WHERE stream=? AND start>=? AND start<? LIMIT 1", (sid, at, end)
        ).fetchone():
            removed += db.execute(
                "DELETE FROM market_candle_fallbacks WHERE stream=? AND tile=?", (sid, at)
            ).rowcount
        point = [sid, at]
        scanned += 1
    if scanned == len(rows) and len(rows) < batch:
        db.execute("DELETE FROM market_meta WHERE key='candle_fallback_cursor'")
    elif scanned:
        db.execute(
            "INSERT INTO market_meta VALUES('candle_fallback_cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (encode(point),),
        )
    return removed


def tile(db: sqlite3.Connection, sid: int, start: int, step: int) -> bytes | None:
    codec.number(start)
    if type(step) is not int or step not in codec.STEPS or start % step:
        return None
    at = start // (codec.SLOTS * step) * (codec.SLOTS * step)
    row = db.execute(
        "SELECT payload FROM market_candle_blocks WHERE stream=? AND tile=?", (sid, at)
    ).fetchone()
    if row is None:
        return None
    raw = bytes(row[0])
    if raw.startswith(codec.COLUMN_MAGIC):
        from .source_column_work import readable

        readable(db, raw)
    if codec.header(raw)[:3] != (sid, at, step):
        raise ValueError("Source tile index binding mismatch")
    return raw


def summary(db: sqlite3.Connection) -> dict[str, int]:
    row = db.execute(
        "SELECT blocks,versions,payload_bytes,fallbacks,index_bytes FROM market_candle_block_totals WHERE id=1"
    ).fetchone()
    counter = db.execute(
        "SELECT records,logical_bytes FROM market_counts WHERE category='source_blocks'"
    ).fetchone()
    if (
        row is None
        or (row[0] and counter is None)
        or counter is not None
        and tuple(counter) != (row[0], row[2])
    ):
        raise ValueError("Source tile counter mismatch")
    if db.execute("SELECT 1 FROM market_candle_gc LIMIT 1").fetchone():
        raise ValueError("Unfinished source expiry authority")
    index = db.execute(
        "SELECT records,logical_bytes FROM market_counts WHERE category='source_block_indexes'"
    ).fetchone()
    if row[3] and index is None or index is not None and tuple(index) != (row[3], row[4]):
        raise ValueError("Source fallback counter mismatch")
    return {
        "blocks": row[0],
        "versions": row[1],
        "payload_bytes": row[2],
        "fallback_tiles": row[3],
        "index_bytes": row[4],
    }
