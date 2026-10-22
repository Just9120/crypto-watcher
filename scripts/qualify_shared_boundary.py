"""Finite combined numeric/shared read geometry, with no capacity or live SLO claim.

The pinned shared seed repeats one owned actual SB2 group. Transiently disabled
construction triggers are restored exactly before full integrity and a fresh
current runtime reader. Counts include more groups and descriptors than the
current 30day scenario, but do not model independent market entropy, a complete
public domain graph, peak retention or future population/rate changes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from cryptowatcher import capacity, packet_bundles, packet_store, shared_record_work, shared_records
from cryptowatcher.storage import Store
from scripts import qualify_numeric_boundary as numeric

SEED = numeric.ROOT / "tests/fixtures/shared_boundary_seed.json"
SEED_SHA256 = "dc3ba212cb60b814a6912d6ef59d35aaee62e5ff6b7d5722fb0a370acf569508"
GROUP_COUNT = 50_000


def seed(path: Path = SEED) -> tuple[int, bytes, bytes, list[tuple[int, int]]]:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SEED_SHA256:
        raise ValueError("Shared boundary test seed differs from pinned content")
    value = json.loads(raw)
    body = base64.b64decode(value["body"], validate=True)
    projection = base64.b64decode(value["projection"], validate=True)
    spans = [tuple(pair) for pair in value["spans"]]
    if (
        value["version"] != 1
        or value["initial"] != len(spans)
        or not 0 < len(spans) <= 32
        or any(len(pair) != 2 or any(type(n) is not int for n in pair) for pair in spans)
    ):
        raise ValueError("Invalid shared boundary seed shape")
    offsets, _ = shared_records.shared_body_frame.layout(body)
    previous = 0
    for first, count in spans:
        shared_records.descriptor(1, first, count, b"x" * 32)
        if first != previous:
            raise ValueError("Invalid shared boundary seed coverage")
        previous = first + count
    if previous != len(offsets) - 1:
        raise ValueError("Incomplete shared boundary seed coverage")
    shared_records.signature("detail_packets", value["bucket"], body, projection, len(spans))
    return value["bucket"], body, projection, spans


def materialize(path: Path, frames: int, groups: int) -> dict[str, Any]:
    if type(groups) is not int or not 0 < groups <= GROUP_COUNT:
        raise ValueError("Shared boundary group count outside finite fixture")
    bucket, body, projection, spans = seed()
    # The final full integrity/FK below covers every numeric and shared row.
    # Avoid an identical full numeric scan before appending the owned tables.
    result = numeric.materialize(path, frames, defer_validation=True)
    started = time.monotonic()
    with closing(Store(path, 233382575)) as store:
        packet_store.install(store.db)
        packet_bundles.install(store.db)
        shared_records.install(store.db, store.cached_bytes)
        shared_record_work.install(store.db, 1_800_000_000_000, store.cached_bytes)
        before = numeric.schema(store.db)
        tables = ("market_packet_arenas", "market_packets", "market_shared_records", "market_shared_members")
        triggers = store.db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type=? AND tbl_name IN (?,?,?,?)",
            ("trigger", *tables),
        ).fetchall()
        for name, _ in triggers:
            store.db.execute('DROP TRIGGER "' + name + '"')
        payload = bytearray(packet_store.CAPACITY)
        packets, members = [], []
        arena = 1
        packet = used = 0

        def flush() -> None:
            nonlocal arena, used, payload
            if not packets:
                return
            store.db.execute(
                "INSERT INTO market_packet_arenas VALUES(?,?,?,?,?,?,?)",
                (arena, "detail_packets", bucket, used, len(packets), used, payload),
            )
            store.db.executemany("INSERT INTO market_packets VALUES(?,?,?,?,?,?,?,?)", packets)
            store.db.executemany("INSERT INTO market_shared_members VALUES(?,?,?,?)", members)
            packets.clear()
            members.clear()
            arena += 1
            used = 0
            payload = bytearray(packet_store.CAPACITY)

        digest = shared_records.signature("detail_packets", bucket, body, projection, len(spans))
        try:
            store.db.execute("BEGIN")
            for group in range(1, groups + 1):
                store.db.execute(
                    "INSERT INTO market_shared_records VALUES(?,?,?,?,?,?,?,?)",
                    (group, "detail_packets", bucket, body, projection, digest, len(spans), len(spans)),
                )
                for first, slots in spans:
                    if used + 48 > packet_store.CAPACITY:
                        flush()
                    packet += 1
                    raw = shared_records.descriptor(group, first, slots, digest)
                    payload[used : used + 48] = raw
                    packets.append((packet, arena, used, 48, hashlib.sha256(raw).digest(), 1, 1, None))
                    members.append((packet, group, first, slots))
                    used += 48
                if group % 64 == 0:
                    store.db.commit()
                    store.db.execute("BEGIN")
            flush()
            store.db.commit()
            with store.db:
                for category, records, size in (
                    ("detail_packets", packet, packet * 48),
                    (shared_records.GROUPS, groups, groups * (len(body) + len(projection))),
                    (shared_records.MEMBERS, packet, packet * 24),
                ):
                    store.db.execute(
                        "INSERT INTO market_counts VALUES(?,?,?,?,0) ON CONFLICT(category) DO UPDATE SET "
                        "records=excluded.records,logical_bytes=excluded.logical_bytes,"
                        "inserted_bytes=excluded.inserted_bytes,deleted_bytes=0",
                        (category, records, size, size),
                    )
        finally:
            store.db.rollback()
            for _, statement in triggers:
                store.db.execute(statement)
        if numeric.schema(store.db) != before:
            raise ValueError("Shared boundary schema was not exactly restored")
        shared_record_work.validate(store.db)
        if [tuple(row) for row in store.db.execute("PRAGMA integrity_check")] != [("ok",)]:
            raise ValueError("Shared boundary integrity failed")
        if store.db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Shared boundary foreign keys failed")
        if store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
            raise ValueError("Shared boundary checkpoint blocked")
    return {
        **result,
        "shared_groups": groups,
        "shared_packets": packet,
        "shared_slots_per_group": sum(n for _, n in spans),
        "shared_body_bytes": len(body),
        "shared_projection_bytes": len(projection),
        "database_bytes": path.stat().st_size,
        "shared_build_seconds": time.monotonic() - started,
    }


def measure(path: Path) -> dict[str, Any]:
    with closing(Store(path, 233382575, readonly=True)) as reader:
        reader.db.execute("BEGIN")
        start, cpu = time.perf_counter(), time.process_time()
        try:
            value = capacity.runtime_ledger(reader, seconds=numeric.READ_SECONDS)
            if reader.db.total_changes:
                raise ValueError("Boundary reader unexpectedly wrote data")
        finally:
            reader.db.rollback()
        return dict(
            measurement=value, seconds=time.perf_counter() - start, cpu_seconds=time.process_time() - cpu
        )
