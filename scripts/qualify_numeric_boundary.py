# ruff: noqa: E402
"""Finite offline joint numeric/shared read geometry, not capacity or live SLO.

Only an owned temporary database is materialized. Repeated actual metric bytes
test coordinate counts and full authentication, not independent market entropy.
The original guarded schema is restored before a new current reader is opened.
The operating-system file cache is not evicted or claimed to be cold.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sqlite3
import struct
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import zstandard  # noqa: E402

from cryptowatcher import (  # noqa: E402
    numeric_block_codec as nc,
)
from cryptowatcher import (
    numeric_blocks,
    numeric_frame_native,
    numeric_frame_work,
    numeric_frames,
    owner_slots,
)
from cryptowatcher import (
    numeric_frame_codec as fc,
)
from cryptowatcher.config import OwnerSettings  # noqa: E402
from cryptowatcher.market_storage import enable  # noqa: E402
from cryptowatcher.numeric_search import signature as search_seal  # noqa: E402
from cryptowatcher.storage import Store  # noqa: E402
from cryptowatcher.storage_repack import work  # noqa: E402

SEED = ROOT / "tests/fixtures/numeric_boundary_seed.json"
SEED_SHA256 = "dde144b2bbfb42d20ae138580b635296b91c55d9ff38ca7d10470fb44502aa4b"
FRAME_COUNT = 129_572
READ_SECONDS = 50


def seed(path: Path = SEED) -> tuple[bytes, list[bytes], list[bytes], int]:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SEED_SHA256:
        raise ValueError("Numeric boundary test seed differs from pinned content")
    value = json.loads(raw)
    payload = base64.b64decode(value["frame"], validate=True)
    entries = fc.raw_entries(payload)
    body = zstandard.ZstdDecompressor().decompress(payload[36:])
    leases = [bytes.fromhex(v) for v in value["quality_leases"]]
    if value["version"] != 1 or body[:4] != b"BP1\0" or len(entries) != 64 or len(leases) != 64:
        raise ValueError("Invalid numeric boundary seed shape")
    metrics = 0
    for entry, lease in zip(entries, leases, strict=True):
        block = struct.unpack(">Q", entry[:8])[0]
        if block == fc.DEAD:
            raise ValueError("Numeric boundary seed must have every owner")
        d, q = nc.leases(nc.seal(b"NR2\0", struct.pack(">Q", block) + lease), block)
        lengths = entry[9:73]
        if any(d) or any(bool(a) != bool(b) for a, b in zip(lengths, q, strict=True)):
            raise ValueError("Numeric boundary seed has inconsistent leases")
        metrics += sum(bool(v) for v in lengths)
    if metrics != value["metrics"]:
        raise ValueError("Numeric boundary seed metric count changed")
    return body, entries, leases, metrics


def schema(db: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in db.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
        )
    ]


def materialize(path: Path, count: int, *, defer_validation: bool = False) -> dict[str, Any]:
    """Build owned rows; the joint builder may defer full CHECK to its final DB.

    Standalone callers retain the original full validation. The canonical joint
    command always performs full integrity/FK after restoring both schemas;
    there is no CLI switch to omit that final superset check.
    """
    if type(defer_validation) is not bool:
        raise ValueError("Invalid numeric fixture validation selection")
    if type(count) is not int or not 0 < count <= FRAME_COUNT:
        raise ValueError("Numeric boundary frame count outside finite fixture")
    if path.exists() or any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("Numeric boundary fixture must be absent")
    body, entries, leases, metrics = seed()
    with closing(Store(path, 233382575)) as store:
        store.initialize(OwnerSettings())
        enable(store.db)
        store.bounded = True
        for _ in range(1000):
            state = work(store, 1_800_000_000_000, seconds=0.04)
            if state["state"] == "COMPLETE" and state.get("initial_reclaim_complete"):
                break
        else:
            raise RuntimeError("Empty numeric boundary layout did not settle")
        before = schema(store.db)
        triggers = store.db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name IN "
            "('market_numeric_frames','market_numeric_blocks','market_numeric_block_refs')"
        ).fetchall()
        for name, _ in triggers:
            store.db.execute('DROP TRIGGER "' + name + '"')
        mask = owner_slots.mask(range(64))
        compressor = zstandard.ZstdCompressor(level=3, write_checksum=True, threads=0)
        payload_bytes = quality_bytes = 0
        try:
            store.db.execute("BEGIN")
            for frame in range(1, count + 1):
                ids = list(range((frame - 1) * 64, frame * 64))
                translated = body[:5] + struct.pack(">64Q", *ids) + body[5 + 64 * 8 :]
                compressed = compressor.compress(translated)
                payload = fc.MAGIC + hashlib.sha256(fc.MAGIC + compressed).digest() + compressed
                if len(payload) > fc.MAX_FRAME:
                    raise ValueError("Numeric boundary fixture exceeds format bound")
                store.db.execute(
                    "INSERT INTO market_numeric_frames(id,payload,slots,checksum,gc_since) VALUES(?,?,?,?,NULL)",
                    (frame, payload, mask, owner_slots.signature(3, frame, mask)),
                )
                blocks, refs = [], []
                for slot, block in enumerate(ids):
                    entry = struct.pack(">Q", block) + entries[slot][8:]
                    pointer = numeric_frames.pointer(frame, slot)
                    nr = nc.seal(b"NR2\0", struct.pack(">Q", block) + leases[slot])
                    blocks.append((block, pointer))
                    refs.append((block, nr, 0, 0, search_seal(block, pointer, nr, binding=entry)))
                    quality_bytes += len(pointer) + len(nr) + 40
                store.db.executemany("INSERT INTO market_numeric_blocks VALUES(?,?)", blocks)
                store.db.executemany("INSERT INTO market_numeric_block_refs VALUES(?,?,?,?,?)", refs)
                payload_bytes += len(payload)
                if frame % 256 == 0:
                    store.db.commit()
                    store.db.execute("BEGIN")
            store.db.commit()
            with store.db:
                totals = (count * 4096, count * metrics, 0, count * metrics, 0, quality_bytes)
                store.db.execute(
                    "UPDATE market_numeric_totals SET issued=?,live=?,details=?,quality=?,detail_bytes=?,"
                    "quality_bytes=?,checksum=? WHERE id=1",
                    (*totals, numeric_blocks.digest(totals)),
                )
                for category, records, size in (
                    ("quality_numeric", totals[3], quality_bytes),
                    ("quality_numeric_frames", count, payload_bytes),
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
        if schema(store.db) != before:
            raise ValueError("Numeric boundary schema was not exactly restored")
        if numeric_blocks.summary(store.db)["live"] != count * metrics:
            raise ValueError("Numeric boundary totals differ from materialized metrics")
        if not defer_validation and store.db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Numeric boundary integrity failed")
        if not defer_validation and store.db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Numeric boundary foreign keys failed")
        store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    return dict(frames=count, owners=count * 64, metrics=count * metrics, database_bytes=path.stat().st_size)


def measure(path: Path) -> dict[str, Any]:
    with closing(Store(path, 233382575, readonly=True)) as reader:
        reader.db.execute("BEGIN")
        deadline = time.monotonic() + READ_SECONDS
        reader.db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        start, cpu = time.perf_counter(), time.process_time()
        try:
            value = numeric_frame_work.measure(reader.db, deadline, reader.cached_bytes)
        finally:
            reader.db.set_progress_handler(None, 0)
            reader.db.rollback()
        return dict(
            measurement=value, seconds=time.perf_counter() - start, cpu_seconds=time.process_time() - cpu
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    if sys.platform != "linux" or not numeric_frame_native.available():
        raise RuntimeError("Full qualification requires the built Linux native artifact")
    import resource

    affinity = os.sched_getaffinity(0)
    os.sched_setaffinity(0, {min(affinity)})
    resource.setrlimit(resource.RLIMIT_AS, (4 * 1024**3, 4 * 1024**3))
    cache = ROOT / ".cache-g1"
    cache.mkdir(exist_ok=True)
    from scripts import qualify_shared_boundary as shared

    try:
        with tempfile.TemporaryDirectory(prefix="numeric-boundary-", dir=cache) as temporary:
            path = Path(temporary) / "fixture.sqlite3"
            built = shared.materialize(path, FRAME_COUNT, shared.GROUP_COUNT)
            result = shared.measure(path)
        report = dict(
            kind="STRUCTURAL_READ_GEOMETRY_ONLY_NOT_CAPACITY_OR_LIVE_SLO",
            verified_cpu_count=len(os.sched_getaffinity(0)),
            memory_address_limit_bytes=resource.getrlimit(resource.RLIMIT_AS)[0],
            temporary_database_removed=not Path(temporary).exists(),
            **built,
            **result,
        )
        print(json.dumps(report), flush=True)
        value = result["measurement"]
        if (
            value["state"] != "MEASURED"
            or value["numeric_frames"]["state"] != "MEASURED"
            or value["numeric_frames"].get("frames") != FRAME_COUNT
            or value["shared_records"]["state"] != "MEASURED"
            or value["shared_records"].get("packets_checked") != built["shared_packets"]
            or result["seconds"] > READ_SECONDS
        ):
            raise RuntimeError("Complete joint boundary exceeded its existing 50-second budget")
    finally:
        os.sched_setaffinity(0, affinity)


if __name__ == "__main__":
    main()
