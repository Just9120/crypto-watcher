"""Exact numeric blocks with mixed readers, leases and finite atomic transfer.

Installation/activation is explicit. Ordinary Store opening only registers
functions for a known marker. Each operation remains inside its caller's
transaction, including operations invoked by runtime/quality SQL triggers.
"""

from __future__ import annotations

import hashlib
import sqlite3
import struct
import time
from typing import Any

from . import metric_price_codec, numeric_refs, numeric_search
from . import numeric_block_codec as codec
from .candle_blocks import atomic
from .numeric_pool import fingerprint


def installed(db: sqlite3.Connection) -> bool:
    row = db.execute("SELECT value FROM market_meta WHERE key='numeric_blocks'").fetchone()
    if row is not None and row[0] not in ("1", "2", "3"):
        raise ValueError("Unknown numeric block layout; database preserved")
    return row is not None


def price_layout(db: sqlite3.Connection) -> bool:
    row = db.execute("SELECT value FROM market_meta WHERE key='numeric_blocks'").fetchone()
    if row is None or row[0] not in ("1", "2", "3"):
        raise ValueError("Unknown numeric block layout; database preserved")
    return row[0] in ("2", "3")


def price_writing(db: sqlite3.Connection) -> bool:
    row = db.execute("SELECT value FROM market_meta WHERE key='numeric_price_runtime'").fetchone()
    if row is not None and (row[0] != "1" or not price_layout(db) or not writing(db)):
        raise ValueError("Unknown numeric price writer; database preserved")
    return row is not None


def upgrade_price(db: sqlite3.Connection) -> None:
    """Explicit format admission only; producer/repack activation is separate."""
    if db.in_transaction or not installed(db):
        raise ValueError("Price layout upgrade requires an installed outer boundary")
    with db:
        db.execute("BEGIN IMMEDIATE")
        summary(db)
        db.execute("UPDATE market_meta SET value='2' WHERE key='numeric_blocks'")


def canonical(raw: bytes) -> bytes:
    # Raw frames are validated on admission and covered by the block checksum.
    return metric_price_codec.frame(raw) if raw and raw[0] in (17, 18, 19) else raw


def signature(raw: bytes) -> bytes:
    return fingerprint(canonical(raw))


def writing(db: sqlite3.Connection) -> bool:
    row = db.execute("SELECT value FROM market_meta WHERE key='numeric_block_runtime'").fetchone()
    if row is not None and (row[0] != "1" or not installed(db)):
        raise ValueError("Unknown numeric writer; database preserved")
    return row is not None


def digest(values: tuple[int, ...]) -> bytes:
    if len(values) != 6:
        raise ValueError("Invalid numeric block totals")
    return hashlib.sha256(b"NT1\x00" + b"".join(struct.pack(">Q", codec.number(n)) for n in values)).digest()


def state(db: sqlite3.Connection) -> tuple[int, ...]:
    row = db.execute(
        "SELECT issued,live,details,quality,detail_bytes,quality_bytes,checksum FROM market_numeric_totals WHERE id=1"
    ).fetchone()
    if row is None or digest(tuple(row[:6])) != row[6] or row[1] != row[2] + row[3] or row[0] < row[1]:
        raise ValueError("Missing or corrupt numeric totals")
    return tuple(row[:6])


def read(db: sqlite3.Connection, block: int) -> tuple[list[bytes], list[int], list[int], bytes] | None:
    search = numeric_search.installed(db)
    extra = ",r.detail_slots,r.search_seal" if search else ",-1,NULL"
    try:
        row = db.execute(
            "SELECT b.payload,r.counts,r.unused,cw_numeric_frames(b.id,b.payload),cw_numeric_counts(b.id,r.counts)"
            + extra
            + " FROM market_numeric_blocks b "
            "LEFT JOIN market_numeric_block_refs r ON r.id=b.id WHERE b.id=?",
            (block,),
        ).fetchone()
    except sqlite3.OperationalError as error:
        if str(error) != "user-defined function raised exception":
            raise
        raise ValueError("Invalid numeric block value/count bytes") from error
    if row is None:
        if db.execute("SELECT 1 FROM market_numeric_block_refs WHERE id=?", (block,)).fetchone():
            raise ValueError("Orphan numeric lease block")
        return None
    # Every read authenticates actual bytes/header. Semantic metric validation
    # happens for materialized slots; immutable guards validate newly added ones.
    raw = bytes(row[0])
    lengths = raw[44:108]
    expanded = bytes(row[3])
    frames = []
    offset = 0
    for length in lengths:
        frames.append(expanded[offset : offset + length])
        offset += length
    if row[1] is None:
        raise ValueError("Missing numeric lease block")
    counts = struct.unpack(">128Q", bytes(row[4]))
    details, quality = list(counts[:64]), list(counts[64:])
    if any((details[i] or quality[i]) and not frame for i, frame in enumerate(frames)) or row[2] != sum(
        bool(frame) and not (details[i] or quality[i]) for i, frame in enumerate(frames)
    ):
        raise ValueError("Numeric slot/lease mismatch")
    numeric_search.checked(block, raw, bytes(row[1]), row[5], row[6], details)
    return frames, details, quality, bytes(row[0])


def allocation(
    block: int,
    frames: list[bytes],
    details: list[int],
    quality: list[int],
    payload_size: int,
    *,
    index_bytes: int | None = None,
    counts_bytes: int | None = None,
) -> tuple[int, ...]:
    if len(frames) != 64 or len(details) != 64 or len(quality) != 64:
        raise ValueError("Invalid numeric slot cardinality")
    for slot, frame in enumerate(frames):
        codec.number(details[slot])
        codec.number(quality[slot])
        if not frame and (details[slot] or quality[slot]):
            raise ValueError("Numeric lease has no value")
    n = sum(bool(frame) for frame in frames)
    q = sum(bool(frame) and quality[i] > 0 for i, frame in enumerate(frames))
    if not n:
        return (0, 0, 0, 0, 0)
    total = (
        payload_size
        + (len(codec.counts(block, details, quality)) if counts_bytes is None else counts_bytes)
        + (16 * n if index_bytes is None else index_bytes)
    )
    # Logical encoded bytes, apportioned by exact frame lengths. Physical
    # SQLite pages/indexes/freelist/sidecars are always measured separately.
    qbytes = total * sum(len(f) for i, f in enumerate(frames) if quality[i]) // sum(map(len, frames))
    return n, n - q, q, total - qbytes, qbytes


def account(
    db: sqlite3.Connection, category: str, old_n: int, old_size: int, new_n: int, new_size: int
) -> None:
    row = db.execute(
        "SELECT records,logical_bytes FROM market_counts WHERE category=?", (category,)
    ).fetchone()
    if row is None:
        if old_n or old_size:
            raise ValueError("Missing numeric byte counter")
        db.execute("INSERT INTO market_counts VALUES(?,0,0,0,0)", (category,))
        row = (0, 0)
    if row[0] < old_n or row[1] < old_size:
        raise ValueError("Lost numeric byte counter")
    db.execute(
        "UPDATE market_counts SET records=records+?,logical_bytes=logical_bytes+?,"
        "inserted_bytes=inserted_bytes+?,deleted_bytes=deleted_bytes+? WHERE category=?",
        (
            new_n - old_n,
            new_size - old_size,
            max(0, new_size - old_size),
            max(0, old_size - new_size),
            category,
        ),
    )


def put(
    db: sqlite3.Connection, block: int, frames: list[bytes], details: list[int], quality: list[int]
) -> None:
    if not db.in_transaction:
        raise ValueError("Numeric block mutation requires an outer transaction")
    summary(db)
    old = read(db, block)
    modern = numeric_search.installed(db)
    search = numeric_search.fields(db, block) if old else (-1, None)
    before = (
        allocation(
            block,
            *old[:3],
            len(old[3]),
            index_bytes=numeric_search.index_bytes(sum(bool(f) for f in old[0]), search),
            counts_bytes=db.execute(
                "SELECT length(counts) FROM market_numeric_block_refs WHERE id=?", (block,)
            ).fetchone()[0],
        )
        if old
        else (0, 0, 0, 0, 0)
    )
    sealed = search[1] is not None
    if sealed and (
        any(details) or (old is not None and any(f and not old[0][i] for i, f in enumerate(frames)))
    ):
        assert old is not None
        # Restore the complete search block before publishing a detail renewal.
        # A partial/duplicate index is rejected by its primary key.
        db.executemany(
            "INSERT INTO market_numeric_block_lookup VALUES(?,?)",
            [(signature(f), block * 64 + i + 1) for i, f in enumerate(old[0]) if f],
        )
        sealed = False
    predicted = any(f and f[0] in (17, 18, 19) for f in frames)
    raw = codec.pack(block, frames, recipes=price_layout(db) if predicted else False) if any(frames) else b""
    search_size = (40 if sealed else 8 + 16 * sum(bool(f) for f in frames)) if modern else None
    refs = codec.counts(block, details, quality, compressed=numeric_refs.writing(db))
    after = allocation(
        block, frames, details, quality, len(raw), index_bytes=search_size, counts_bytes=len(refs)
    )
    current = state(db)
    if any(frame and block * 64 + slot + 1 > current[0] for slot, frame in enumerate(frames)):
        raise ValueError("Numeric slot exceeds issued ceiling")
    updated = (current[0], *(current[i + 1] + after[i] - before[i] for i in range(5)))
    digest(updated)
    if after[0]:
        unused = sum(bool(f) and not (details[i] or quality[i]) for i, f in enumerate(frames))
        db.execute(
            "INSERT INTO market_numeric_blocks VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
            (block, raw),
        )
        if modern:
            seal = numeric_search.signature(block, raw, refs) if sealed else None
            db.execute(
                "INSERT INTO market_numeric_block_refs(id,counts,unused,detail_slots,search_seal) VALUES(?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET counts=excluded.counts,unused=excluded.unused,"
                "detail_slots=excluded.detail_slots,search_seal=excluded.search_seal",
                (block, refs, unused, sum(bool(v) for v in details), seal),
            )
        else:
            db.execute(
                "INSERT INTO market_numeric_block_refs(id,counts,unused) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET counts=excluded.counts,unused=excluded.unused",
                (block, refs, unused),
            )
    elif old:
        db.execute("INSERT INTO market_numeric_gc VALUES(?)", (block,))
        db.execute("DELETE FROM market_numeric_block_refs WHERE id=?", (block,))
        db.execute("DELETE FROM market_numeric_blocks WHERE id=?", (block,))
        db.execute("DELETE FROM market_numeric_gc WHERE id=?", (block,))
    account(db, "numeric_details", before[1], before[3], after[1], after[3])
    account(db, "quality_numeric", before[2], before[4], after[2], after[4])
    db.execute(
        "UPDATE market_numeric_totals SET live=?,details=?,quality=?,detail_bytes=?,quality_bytes=?,checksum=? WHERE id=1",
        (*updated[1:], digest(updated)),
    )


def lookup(db: sqlite3.Connection, identity: int, frame: bytes) -> None:
    present = (
        db.execute(
            "SELECT 1 FROM market_numeric_block_lookup WHERE fingerprint=? AND id=?",
            (signature(frame), identity),
        ).fetchone()
        is not None
    )
    sealed = numeric_search.sealed(db, (identity - 1) // 64)
    if sealed and present:
        raise ValueError("Sealed numeric coordinate remains searchable")
    if not sealed and not present:
        raise ValueError("Missing numeric fingerprint coordinate")


def load_many(db: sqlite3.Connection, ids: set[int]) -> dict[int, tuple[bytes, int, int]]:
    current = state(db)
    groups: dict[int, list[tuple[int, int]]] = {}
    for identity in ids:
        block, slot = codec.coordinate(identity)
        if identity > current[0]:
            raise ValueError("Numeric ID exceeds issued ceiling")
        groups.setdefault(block, []).append((identity, slot))
    result = {}
    for block, group in groups.items():
        found = read(db, block)
        if found is None:
            continue
        frames, details, quality, _ = found
        for identity, slot in group:
            if frames[slot]:
                lookup(db, identity, frames[slot])
                result[identity] = canonical(frames[slot]), details[slot], quality[slot]
    return result


def values_many(db: sqlite3.Connection, ids: set[int]) -> dict[int, bytes]:
    """Value reads authenticate lease bytes/presence without expanding128 counts.

    Ownership/transfer/GC use load_many/read and validate all scalar leases.
    Ordinary materialization never interprets a count as a metric value.
    """
    ceiling = state(db)[0]
    search = numeric_search.installed(db)
    extra = ",r.detail_slots,r.search_seal" if search else ",-1,NULL"
    sealed_ids: set[int] = set()
    groups: dict[int, list[tuple[int, int]]] = {}
    for identity in ids:
        block, slot = codec.coordinate(identity)
        if identity > ceiling:
            raise ValueError("Numeric ID exceeds issued ceiling")
        groups.setdefault(block, []).append((identity, slot))
    result = {}
    for block, group in groups.items():
        try:
            row = db.execute(
                "SELECT b.payload,r.counts,cw_numeric_frames(b.id,b.payload)"
                + extra
                + " FROM market_numeric_blocks b "
                "LEFT JOIN market_numeric_block_refs r ON r.id=b.id WHERE b.id=?",
                (block,),
            ).fetchone()
        except sqlite3.OperationalError as error:
            if str(error) != "user-defined function raised exception":
                raise
            raise ValueError("Invalid numeric block value bytes") from error
        if row is None:
            continue
        if row[1] is None:
            raise ValueError("Missing numeric lease block")
        counts = numeric_refs.header(db, bytes(row[1]))
        if struct.unpack(">Q", counts[:8])[0] != block:
            raise ValueError("Numeric lease identity mismatch")
        sealed = numeric_search.checked(block, bytes(row[0]), bytes(row[1]), row[3], row[4])
        lengths, expanded = bytes(row[0])[44:108], bytes(row[2])
        for identity, slot in group:
            size = lengths[slot]
            if size:
                offset = sum(lengths[:slot])
                result[identity] = canonical(expanded[offset : offset + size])
                if sealed:
                    sealed_ids.add(identity)
    ordered = sorted(result)
    for start in range(0, len(ordered), 256):
        batch = ordered[start : start + 256]
        coordinates = ",".join("(?,?,?)" for _ in batch)
        args = [
            v
            for identity in batch
            for v in (fingerprint(result[identity]), identity, int(identity in sealed_ids))
        ]
        missing = db.execute(
            f"WITH wanted(fingerprint,id,sealed) AS (VALUES{coordinates}) SELECT COUNT(*) FROM wanted w "
            "LEFT JOIN market_numeric_block_lookup l ON l.fingerprint=w.fingerprint AND l.id=w.id "
            "WHERE (w.sealed=0 AND l.id IS NULL) OR (w.sealed=1 AND l.id IS NOT NULL)",
            args,
        ).fetchone()[0]
        if missing:
            raise ValueError("Missing numeric fingerprint coordinate")
    return result


def find(db: sqlite3.Connection, raw: bytes) -> tuple[int, int, int] | None:
    for (identity,) in db.execute(
        "SELECT id FROM market_numeric_block_lookup WHERE fingerprint=? ORDER BY id", (fingerprint(raw),)
    ):
        if numeric_search.sealed(db, (identity - 1) // 64):
            raise ValueError("Sealed numeric coordinate remains searchable")
        found = load_many(db, {identity})
        if identity not in found:
            raise ValueError("Orphan numeric fingerprint coordinate")
        frame, details, quality = found[identity]
        if frame == raw:
            return identity, details, quality
    return None


def offer_price(db: sqlite3.Connection, identity: int, candidate: bytes) -> bool:
    """Recode equal metrics only when the complete stored block shrinks."""
    if not price_writing(db):
        return False
    if not isinstance(candidate, bytes) or not candidate or candidate[0] not in (17, 18, 19):
        raise ValueError("Invalid proposed price metric frame")
    original = metric_price_codec.frame(candidate)
    block, slot = codec.coordinate(identity)
    with atomic(db):
        state(db)
        old = read(db, block)
        if old is None or not old[0][slot]:
            raise ValueError("Missing proposed numeric metric")
        frames, details, quality, payload = old
        if canonical(frames[slot]) != original:
            raise ValueError("Price recipe changed retained metric bits/types")
        lookup(db, identity, frames[slot])
        frames[slot] = candidate
        alternative = codec.pack(block, frames, recipes=True)
        if len(alternative) >= len(payload):
            return False
        put(db, block, frames, details, quality)
        return True


def intern(db: sqlite3.Connection, raw: bytes) -> int:
    with atomic(db):
        current = state(db)
        identity = current[0] + 1
        codec.number(identity, positive=True)
        # The legacy AUTOINCREMENT ceiling includes deleted IDs. Keep it in
        # sync so explicitly admitted legacy writes cannot reuse cold IDs.
        seq = db.execute("SELECT seq FROM sqlite_sequence WHERE name='market_numeric'").fetchone()
        if seq is None or seq[0] != current[0]:
            raise ValueError("Numeric issued ceiling diverged")
        updated = (identity, *current[1:])
        db.execute("UPDATE sqlite_sequence SET seq=? WHERE name='market_numeric'", (identity,))
        db.execute(
            "UPDATE market_numeric_totals SET issued=?,checksum=? WHERE id=1", (identity, digest(updated))
        )
        block, slot = codec.coordinate(identity)
        found = read(db, block)
        frames, details, quality = found[:3] if found else ([b""] * 64, [0] * 64, [0] * 64)
        if frames[slot]:
            raise ValueError("Numeric ID was already issued")
        frames[slot] = raw
        db.execute("INSERT INTO market_numeric_block_lookup VALUES(?,?)", (fingerprint(raw), identity))
        put(db, block, frames, details, quality)
        return identity


def adjust(db: sqlite3.Connection, ids: set[int], column: str, delta: int) -> None:
    if column not in ("detail_refs", "quality_refs") or type(delta) is not int or delta not in (-1, 1):
        raise ValueError("Invalid numeric lease operation")
    groups: dict[int, list[tuple[int, int]]] = {}
    for identity in ids:
        block, slot = codec.coordinate(identity)
        groups.setdefault(block, []).append((identity, slot))
    state(db)
    for block, group in groups.items():
        found = read(db, block)
        if found is None:
            raise ValueError("Missing retained numeric block")
        frames, details, quality, _ = found
        counts = details if column == "detail_refs" else quality
        for identity, slot in group:
            if not frames[slot]:
                raise ValueError("Missing retained numeric slot")
            lookup(db, identity, frames[slot])
            counts[slot] = codec.number(counts[slot] + delta)
        put(db, block, frames, details, quality)


def collect(db: sqlite3.Connection, batch: int) -> int:
    codec.number(batch, positive=True)
    count = 0
    with atomic(db):
        state(db)
        rows = db.execute(
            "SELECT id FROM market_numeric_block_refs WHERE unused>0 ORDER BY unused,id LIMIT ?",
            (min(batch, 256),),
        ).fetchall()
        for (block,) in rows:
            found = read(db, block)
            if found is None:
                raise ValueError("Missing numeric GC block")
            frames, details, quality, _ = found
            sealed = numeric_search.sealed(db, block)
            for slot, frame in enumerate(frames):
                if frame and not (details[slot] or quality[slot]) and count < batch:
                    identity = block * 64 + slot + 1
                    lookup(db, identity, frame)
                    deleted = db.execute(
                        "DELETE FROM market_numeric_block_lookup WHERE fingerprint=? AND id=?",
                        (signature(frame), identity),
                    ).rowcount
                    if deleted != (0 if sealed else 1):
                        raise ValueError("Lost numeric GC coordinate")
                    frames[slot] = b""
                    count += 1
            put(db, block, frames, details, quality)
            if count >= batch:
                break
    return count


def transfer(db: sqlite3.Connection, state_value: dict[str, Any], batch: int, deadline: float) -> bool:
    if not db.in_transaction:
        raise ValueError("Numeric transfer requires an outer checkpoint transaction")
    if type(batch) is not int or not 0 < batch <= 256:
        raise ValueError("Invalid numeric transfer batch")
    if state_value.get("numeric_transfer_complete"):
        return True
    if "numeric_upper" not in state_value:
        row = db.execute("SELECT id FROM market_numeric ORDER BY id DESC LIMIT 1").fetchone()
        state_value.update(numeric_upper=row[0] if row else 0, numeric_cursor=0)
    high, low = state_value["numeric_upper"], state_value["numeric_cursor"]
    codec.number(high)
    codec.number(low)
    for _ in range(batch):
        if time.monotonic() >= deadline:
            return False
        first = db.execute(
            "SELECT id FROM market_numeric WHERE id>? AND id<=? ORDER BY id LIMIT 1", (low, high)
        ).fetchone()
        if first is None:
            state_value["numeric_transfer_complete"] = True
            return True
        block, _slot = codec.coordinate(first[0])
        upper = min(high, (block + 1) * 64)
        rows = db.execute(
            "SELECT id,fingerprint,payload,detail_refs,quality_refs FROM market_numeric WHERE id>? AND id<=? ORDER BY id",
            (low, upper),
        ).fetchall()
        with atomic(db):
            from .numeric_pool import legacy_meter

            legacy_meter(db, [(bytes(r[2]), r[3], r[4]) for r in rows])
            current = state(db)
            if rows[-1][0] > current[0]:
                raise ValueError("Legacy numeric ID exceeds issued ceiling")
            found = read(db, block)
            frames, details, quality = found[:3] if found else ([b""] * 64, [0] * 64, [0] * 64)
            for identity, signature, raw, d, q in rows:
                raw = bytes(raw)
                if fingerprint(raw) != bytes(signature):
                    raise ValueError("Invalid legacy numeric checksum")
                slot = codec.coordinate(identity)[1]
                if frames[slot]:
                    raise ValueError("Conflicting numeric transfer coordinate")
                frames[slot], details[slot], quality[slot] = raw, codec.number(d), codec.number(q)
                db.execute("INSERT INTO market_numeric_block_lookup VALUES(?,?)", (signature, identity))
            put(db, block, frames, details, quality)
            # Read actual persisted bytes/counts before allowing retained legacy
            # rows to leave. The permit exists only inside this savepoint.
            restored = load_many(db, {r[0] for r in rows})
            for identity, _signature, raw, d, q in rows:
                if restored.get(identity) != (bytes(raw), d, q):
                    raise ValueError("Numeric transfer changed values/leases")
                db.execute("INSERT INTO market_numeric_transfer VALUES(?)", (identity,))
                if (
                    db.execute(
                        "DELETE FROM market_numeric WHERE id=? AND payload=? AND detail_refs=? AND quality_refs=?",
                        (identity, raw, d, q),
                    ).rowcount
                    != 1
                ):
                    raise ValueError("Numeric transfer CAS failed")
                db.execute("DELETE FROM market_numeric_transfer WHERE id=?", (identity,))
            state_value["numeric_cursor"] = low = upper
            state_value["numeric_rows_moved"] = state_value.get("numeric_rows_moved", 0) + len(rows)
    return False


def summary(db: sqlite3.Connection) -> dict[str, Any]:
    issued, live, details, quality, dbytes, qbytes = state(db)
    price_writing(db)
    legacy = db.execute("SELECT 1 FROM market_numeric LIMIT 1").fetchone() is not None
    if (
        db.execute("SELECT 1 FROM market_numeric_transfer LIMIT 1").fetchone()
        or db.execute("SELECT 1 FROM market_numeric_gc LIMIT 1").fetchone()
    ):
        raise ValueError("Unfinished numeric transfer authority")
    for category, count, size in (("numeric_details", details, dbytes), ("quality_numeric", quality, qbytes)):
        row = db.execute(
            "SELECT records,logical_bytes FROM market_counts WHERE category=?", (category,)
        ).fetchone()
        if (row is None and (count or size)) or (
            row is not None
            and (any(type(v) is not int or v < 0 for v in row) or row[0] < count or row[1] < size)
        ):
            raise ValueError("Missing numeric encoded-byte counter")
        if not legacy and row is not None and (row[0] != count or row[1] != size):
            raise ValueError("Numeric encoded-byte counter differs from complete blocks")
    return dict(
        issued=issued, live=live, details=details, quality=quality, detail_bytes=dbytes, quality_bytes=qbytes
    )


def install_functions(db: sqlite3.Connection, cache: Any = None) -> None:
    def expanded(block: int, raw: bytes | None, counts: bool = False) -> bytes | None:
        if raw is None:
            return None
        if not counts and raw[:4] == b"NB2\x00" and not price_layout(db):
            raise ValueError("NB2 requires an explicit price layout upgrade")
        body = numeric_refs.header(db, raw) if counts else codec.header(raw)
        if struct.unpack(">Q", body[:8])[0] != codec.block_number(block):
            raise ValueError("Numeric cached block coordinate mismatch")

        def transform() -> bytes:
            if counts:
                d, q = codec.leases(raw, block)
                return struct.pack(">128Q", *(d + q))
            return b"".join(codec.unpack(raw, block, validate=False))

        return (
            transform()
            if cache is None
            else bytes(cache("numeric-counts" if counts else "numeric-frames", str(block), raw, transform))
        )

    db.create_function("cw_numeric_frames", 2, expanded)
    db.create_function("cw_numeric_counts", 2, lambda block, raw: expanded(block, raw, True))

    def admitted(block: int, raw: bytes) -> int:
        if raw[:4] == b"NB2\x00" and not price_writing(db):
            raise ValueError("NB2 admission requires explicit writer activation")
        frames = codec.unpack(raw, block)
        ceiling = state(db)[0]
        if any(f and block * 64 + slot + 1 > ceiling for slot, f in enumerate(frames)):
            raise ValueError("Numeric block admission exceeds issued ceiling")
        return 1

    def change(block: int, old: bytes, new: bytes | None) -> int:
        before = codec.unpack(old, block, validate=False)
        after = codec.unpack(new, block, validate=False) if new is not None else [b""] * 64
        counts_row = db.execute(
            "SELECT counts FROM market_numeric_block_refs WHERE id=?", (block,)
        ).fetchone()
        if counts_row is None:
            # Empty-block GC removes the zero lease row immediately before the
            # payload. A permit is still required; arbitrary DELETE fails.
            permit = db.execute("SELECT 1 FROM market_numeric_gc WHERE id=?", (block,)).fetchone()
            if new is not None or permit is None:
                raise ValueError("Missing numeric immutable guard counts")
            return 1
        ds, qs = codec.leases(bytes(counts_row[0]), block)
        for slot, frame in enumerate(before):
            if frame and after[slot] != frame:
                if after[slot]:
                    if canonical(frame) != metric_price_codec.frame(after[slot]):
                        raise ValueError("Immutable numeric metric value")
                    if after[slot][0] in (17, 18, 19) and not price_writing(db):
                        raise ValueError("Price recoding requires explicit writer activation")
                elif ds[slot] or qs[slot]:
                    raise ValueError("Retained numeric slot")
            elif not frame and after[slot]:
                metric_price_codec.frame(after[slot])
        return 1

    def retained(identity: int, raw: bytes, d: int, q: int) -> int:
        permit = db.execute("SELECT 1 FROM market_numeric_transfer WHERE id=?", (identity,)).fetchone()
        if permit and load_many(db, {identity}).get(identity) == (bytes(raw), d, q):
            return 1
        return int(d == 0 and q == 0)

    db.create_function("cw_numeric_block_change", 3, change)
    db.create_function("cw_numeric_block_admit", 2, admitted)
    db.create_function("cw_numeric_transfer_allowed", 4, retained)
    db.create_function("cw_numeric_totals_digest", 6, lambda *values: digest(values))


def install(db: sqlite3.Connection, cache: Any = None) -> None:
    if installed(db):
        install_functions(db, cache)
        summary(db)
        return
    if db.in_transaction:
        raise ValueError("Numeric block installation requires an outer boundary")
    from .numeric_pool import install_functions as numeric_functions

    numeric_functions(db)
    install_functions(db, cache)
    try:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT sql FROM sqlite_master WHERE name='market_numeric_retained'").fetchone()
        if old is None or "WHEN OLD.detail_refs>0 OR OLD.quality_refs>0" not in old[0]:
            raise ValueError("Unknown legacy numeric guard")
        db.execute("DROP TRIGGER market_numeric_retained")
        db.execute("""CREATE TRIGGER market_numeric_retained BEFORE DELETE ON market_numeric
            WHEN cw_numeric_transfer_allowed(OLD.id,OLD.payload,OLD.detail_refs,OLD.quality_refs)=0
            BEGIN SELECT RAISE(ABORT,'Retained numeric metric'); END""")
        # DDL is statement-by-statement: executescript would commit the caller.
        for statement in (
            "CREATE TABLE market_numeric_blocks(id INTEGER PRIMARY KEY CHECK(id>=0 AND id<=144115188075855871),payload BLOB NOT NULL CHECK(typeof(payload)='blob' AND length(payload) BETWEEN 109 AND 4096))",
            f"CREATE TABLE market_numeric_block_refs(id INTEGER PRIMARY KEY REFERENCES market_numeric_blocks(id),counts BLOB NOT NULL CHECK({numeric_refs.COUNTS_CHECK}),unused INTEGER NOT NULL CHECK(typeof(unused)='integer' AND unused BETWEEN 0 AND 64))",
            "CREATE INDEX market_numeric_block_unused ON market_numeric_block_refs(unused,id) WHERE unused>0",
            "CREATE TABLE market_numeric_block_lookup(fingerprint BLOB NOT NULL CHECK(length(fingerprint)=8),id INTEGER NOT NULL CHECK(id>0),PRIMARY KEY(fingerprint,id)) WITHOUT ROWID",
            "CREATE TABLE market_numeric_totals(id INTEGER PRIMARY KEY CHECK(id=1),issued INTEGER NOT NULL CHECK(typeof(issued)='integer' AND issued>=0),live INTEGER NOT NULL CHECK(typeof(live)='integer' AND live>=0 AND live<=issued),details INTEGER NOT NULL CHECK(typeof(details)='integer' AND details>=0),quality INTEGER NOT NULL CHECK(typeof(quality)='integer' AND quality>=0 AND live=details+quality),detail_bytes INTEGER NOT NULL CHECK(typeof(detail_bytes)='integer' AND detail_bytes>=0),quality_bytes INTEGER NOT NULL CHECK(typeof(quality_bytes)='integer' AND quality_bytes>=0),checksum BLOB NOT NULL CHECK(typeof(checksum)='blob' AND length(checksum)=32))",
            "CREATE TABLE market_numeric_transfer(id INTEGER PRIMARY KEY)",
            "CREATE TABLE market_numeric_gc(id INTEGER PRIMARY KEY)",
            "CREATE TRIGGER market_numeric_block_admit BEFORE INSERT ON market_numeric_blocks BEGIN SELECT cw_numeric_block_admit(NEW.id,NEW.payload); END",
            "CREATE TRIGGER market_numeric_block_binding BEFORE UPDATE OF id ON market_numeric_blocks WHEN OLD.id<>NEW.id BEGIN SELECT RAISE(ABORT,'Immutable numeric block identity'); END",
            "CREATE TRIGGER market_numeric_refs_binding BEFORE UPDATE OF id ON market_numeric_block_refs WHEN OLD.id<>NEW.id BEGIN SELECT RAISE(ABORT,'Immutable numeric lease identity'); END",
            "CREATE TRIGGER market_numeric_block_guard BEFORE UPDATE OF payload ON market_numeric_blocks BEGIN SELECT cw_numeric_block_change(OLD.id,OLD.payload,NEW.payload); END",
            "CREATE TRIGGER market_numeric_block_delete BEFORE DELETE ON market_numeric_blocks BEGIN SELECT cw_numeric_block_change(OLD.id,OLD.payload,NULL); END",
            "CREATE TRIGGER market_numeric_total_guard BEFORE UPDATE ON market_numeric_totals WHEN NEW.issued<OLD.issued OR NEW.checksum<>cw_numeric_totals_digest(NEW.issued,NEW.live,NEW.details,NEW.quality,NEW.detail_bytes,NEW.quality_bytes) BEGIN SELECT RAISE(ABORT,'Invalid numeric totals'); END",
            "CREATE TRIGGER market_numeric_legacy_insert BEFORE INSERT ON market_numeric WHEN EXISTS(SELECT 1 FROM market_meta WHERE key='numeric_block_runtime') BEGIN SELECT RAISE(ABORT,'Numeric writer requires stable block IDs'); END",
            "CREATE TRIGGER market_numeric_legacy_ceiling AFTER INSERT ON market_numeric BEGIN SELECT CASE WHEN NEW.id<=(SELECT issued FROM market_numeric_totals WHERE id=1) THEN RAISE(ABORT,'Numeric ID was already issued') END; UPDATE market_numeric_totals SET issued=NEW.id,checksum=cw_numeric_totals_digest(NEW.id,live,details,quality,detail_bytes,quality_bytes) WHERE id=1; END",
        ):
            db.execute(statement)
        seq = db.execute("SELECT seq FROM sqlite_sequence WHERE name='market_numeric'").fetchone()
        first = db.execute("SELECT id FROM market_numeric ORDER BY id LIMIT 1").fetchone()
        if first:
            codec.number(first[0], positive=True)
        last = db.execute("SELECT id FROM market_numeric ORDER BY id DESC LIMIT 1").fetchone()
        issued = max(codec.number(seq[0] if seq else 0), codec.number(last[0] if last else 0))
        if seq is None:
            db.execute("INSERT INTO sqlite_sequence VALUES('market_numeric',?)", (issued,))
        else:
            db.execute("UPDATE sqlite_sequence SET seq=? WHERE name='market_numeric'", (issued,))
        values = (issued, 0, 0, 0, 0, 0)
        db.execute("INSERT INTO market_numeric_totals VALUES(1,?,?,?,?,?,?,?)", (*values, digest(values)))
        from .numeric_pool import ownership_triggers

        ownership_triggers(db, replace=True)
        from .quality_packets import numeric_trigger_upgrade

        numeric_trigger_upgrade(db)
        from .record_ownership import numeric_trigger_upgrade as catalogue_upgrade

        catalogue_upgrade(db)
        db.execute("INSERT INTO market_meta VALUES('numeric_blocks','1')")
        db.commit()
    except BaseException:
        db.rollback()
        raise
