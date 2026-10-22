"""One bounded readonly preparation plus short main-thread publication steps.

Uses the existing maintenance callback. No install, additional scheduled job,
writer thread or transaction spanning an await. A live preparation is retained
and joined on shutdown; cancellation never publishes its result later.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

from . import indexed_frame, packet_store, record_blocks
from . import shared_record_work as work
from . import shared_records as shared
from .storage import Store

PREPARE_SECONDS = 5


def _budget(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("Shared read preparation deadline exceeded")


def read_batch(path: Path, owner: int, identities: list[int], now: int) -> shared.Prepared | None:
    """One snapshot and maximal original prefix within both expanded bounds."""
    if not 0 < len(identities) <= 32:
        raise ValueError("Invalid shared read preparation bound")
    deadline = time.monotonic() + PREPARE_SECONDS
    with closing(Store(path, owner, readonly=True)) as store:
        store.clock_ms = lambda: now
        store.db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        store.db.execute("BEGIN")
        selected: list[int] = []
        projection_size = body_size = 0
        for identity in identities:
            _budget(deadline)
            row = store.db.execute(
                "SELECT refs,bundle_checked FROM market_packets WHERE id=?", (identity,)
            ).fetchone()
            if (
                row is None
                or row[0] <= 0
                or row[1] != 1
                or store.db.execute(
                    "SELECT 1 FROM market_shared_members WHERE packet=?", (identity,)
                ).fetchone()
            ):
                return None
            raw = packet_store.read(store.db, identity)
            if not raw.startswith(record_blocks.MAGIC):
                raise ValueError("Unsupported shared source encoding; old data preserved")
            projection, body = record_blocks.frames(raw)
            p = indexed_frame.layout(projection)[0][-1]
            b = indexed_frame.layout(body)[0][-1]
            if selected and max(projection_size + p, body_size + b) > indexed_frame.MAX_BYTES:
                break
            projection_size += p
            body_size += b
            selected.append(identity)
        while selected:
            _budget(deadline)
            try:
                prepared = shared.prepare(store.db, selected)
            except indexed_frame.FrameSizeError:
                if len(selected) == 1:
                    raise
                selected = selected[: len(selected) // 2]
                continue
            _budget(deadline)
            return prepared
    return None


class Worker:
    def __init__(self, store: Store):
        self.store = store
        self.preparing: asyncio.Task[shared.Prepared | None] | None = None

    def _record(self, state: str, now: int, header: dict[str, Any]) -> dict[str, Any]:
        value = {**header, "state": state, "at_ms": now}
        self.store.put_runtime("shared_record_worker", value)
        return value

    def _maintenance(self, now: int) -> dict[str, Any]:
        db = self.store.db
        work.scan(db, now)
        packet_store.collect(db, 32)
        shared.collect(db, 32)
        if db.execute("PRAGMA freelist_count").fetchone()[0] > 256:
            db.execute("PRAGMA incremental_vacuum(32)").fetchall()
        value = work.header(db)
        if (
            value["initial_complete"]
            and not value["initial_reclaim_complete"]
            and not value["sparse_pending"]
            and not value["garbage_pending"]
            and db.execute("SELECT 1 FROM market_packet_arenas WHERE live_packets=0 LIMIT 1").fetchone()
            is None
            and db.execute("PRAGMA freelist_count").fetchone()[0] <= 256
        ):
            db.execute("UPDATE market_shared_scan SET reclaimed=1 WHERE id=1")
        db.execute("UPDATE market_shared_scan SET at_ms=? WHERE id=1", (now,))
        return work.header(db)

    async def step(self, clock: Callable[[], int]) -> dict[str, Any]:
        if not self.store.bounded or not work.enabled(self.store.db):
            return {"state": "NOT_INSTALLED"}
        now = clock()
        if type(now) is not int or not 0 < now < 2**63:
            return {"state": "CLOCK_UNAVAILABLE"}
        if self.store.db.in_transaction:
            raise ValueError("Shared worker cannot commit another transaction")
        try:
            with self.store.db:
                self.store.db.execute("BEGIN IMMEDIATE")
                value = self._maintenance(now)
                if not value["writer_enabled"]:
                    return self._record("WRITER_DISABLED", now, value)
                if value["pending"]:
                    work.advance(self.store.db, self.store.cached_bytes)
                    return self._record("ADVANCED", now, work.header(self.store.db))
                sparse = work.sparse(self.store.db)
                if sparse is not None:
                    from .catalogue_compaction import compact

                    _, fallbacks = compact(self.store, sparse, reclaim_shared=True)
                    if fallbacks:
                        raise ValueError("Shared sparse reclaim exceeded the existing record bound")
                    packet_store.collect(self.store.db, 32)
                    shared.collect(self.store.db, 32)
                    return self._record("RECLAIMED", now, work.header(self.store.db))
            if self.preparing is not None:
                if not self.preparing.done():
                    return self._record("PREPARING", now, value)
                task, self.preparing = self.preparing, None
                prepared = task.result()
                with self.store.db:
                    self.store.db.execute("BEGIN IMMEDIATE")
                    group = work.stage(self.store.db, prepared) if prepared is not None else None
                    return self._record(
                        "STAGED" if group is not None else "STALE", now, work.header(self.store.db)
                    )
            with self.store.db:
                self.store.db.execute("BEGIN")
                selected = work.select(self.store.db, now)
            if selected:
                self.preparing = asyncio.create_task(
                    asyncio.to_thread(read_batch, self.store.path, self.store.owner_id, selected, now),
                    name="shared-record-prepare",
                )
                await asyncio.sleep(0)
                return self._record("PREPARING", now, value)
            return self._record("WAITING" if value["queued"] else "CURRENT", now, value)
        except Exception as exc:
            self.store.put_runtime(
                "shared_record_worker", {"state": "FAILED", "at_ms": now, "error_class": type(exc).__name__}
            )
            raise

    async def close(self) -> None:
        """Join the actual readonly task; never publish on shutdown."""
        task = self.preparing
        if task is None:
            return
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        if task.done():
            try:
                task.result()
            except (Exception, asyncio.CancelledError):
                pass
            self.preparing = None
        if cancelled:
            raise asyncio.CancelledError
