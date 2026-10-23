"""Bounded public backfill for referenced outcomes, including inactive instruments."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict
from typing import Any

from .events import History, identity
from .ingestion import BybitRadar
from .market import MarketError
from .outcomes import MINUTE, pending_key
from .sources import Candle, Sources
from .storage import Store, encode, json_payload


class AnalyticsRecovery:
    def __init__(self, store: Store, market: BybitRadar) -> None:
        self.store, self.market, self.history = store, market, History(store)

    def request(self, stream: str, start: int, end: int) -> str | None:
        if start >= end:
            return "INVALID_REFERENCED_WINDOW"
        if self.complete_locally(stream, start, end):
            return None
        expires = None
        if self.store.bounded:
            expires = self.owner_deadline(stream, start, end, self.market.server_now())
            if expires is None:
                return "REFERENCED_OWNER_EXPIRED"
        merged: list[dict[str, Any]] = []
        recent: list[str] = []
        if self.store.bounded:
            start, end, merged, recent = self.coalesce(stream, start, end)
            expires = self.owner_deadline(stream, start, end, self.market.server_now())
        key = identity(f"analytics-gap/{stream}/{start}/{end}")
        job = self.history.get("analytics-gap:" + key)
        terminal = {"COMPLETE", "SUPERSEDED", "EXPIRED_RETENTION_INCOMPLETE"}
        if job is not None and job["state"] not in terminal:
            return job.get("reason", "REFERENCED_RECOVERY_PENDING")
        if job is None or job["state"] in terminal:
            now = self.market.server_now()
            pending = pending_key(now, key).replace("outcome-pending:", "analytics-gap-pending:", 1)
            with self.store.db:
                for previous in merged:
                    if previous["id"] == key:
                        continue
                    self.history.delete(previous["pending_key"])
                    previous.update(state="SUPERSEDED", completed_ms=now)
                    self.history.put("analytics-gap:" + previous["id"], previous)
                self.history.put(
                    "analytics-gap:" + key,
                    {
                        "id": key,
                        "stream": stream,
                        "start": start,
                        "end": end,
                        "cursor_end": end,
                        "state": "PENDING",
                        "next_attempt_ms": now,
                        "pending_key": pending,
                        "attempts": 0,
                        **({"expires_ms": expires} if expires is not None else {}),
                    },
                )
                self.history.put(pending, {"id": key})
                from .queue_health import arrival

                arrival(self.store, "analytics-gap")
                if self.store.bounded:
                    self.history.put("recovery-ranges:" + stream, (recent + [key])[-32:])
        return "REFERENCED_RECOVERY_PENDING"

    def coalesce(self, stream: str, start: int, end: int) -> tuple[int, int, list[dict[str, Any]], list[str]]:
        """Share overlapping outstanding ranges, with a bounded durable lookup.

        Jobs have no consumer payload identity: outcomes refer to their immutable
        event/source revisions. Only the REST work/index is superseded.
        """
        jobs = [
            self.history.get("analytics-gap:" + key)
            for key in self.history.get("recovery-ranges:" + stream, [])[-32:]
        ]
        jobs = [job for job in jobs if job and job["state"] in ("PENDING", "RETRY")]
        merged = []
        changed = True
        while changed:
            changed = False
            for job in jobs:
                if job in merged:
                    continue
                left, right = min(start, job["start"]), max(end, job["cursor_end"])
                if job["start"] <= end and job["cursor_end"] >= start and right - left <= 1000 * MINUTE:
                    start, end = left, right
                    merged.append(job)
                    changed = True
        return start, end, merged, [job["id"] for job in jobs if job not in merged]

    def complete_locally(self, stream: str, start: int, end: int) -> bool:
        """An arrived WS candle retires a queued REST request without re-admission.

        Check at most one REST page. Larger ranges remain paginated by work(); a
        partial window, conflicting source or merely an open candle is never a hit.
        """
        from .outcomes import read_candles

        step = int(stream.rsplit("/", 1)[1][:-1]) * MINUTE
        if step != MINUTE or end > self.market.server_now() or (end - start) % step:
            return False
        if not 0 < (end - start) // step <= 1000:
            return False
        candles = read_candles(self.store, stream, start, end)
        return len(candles) == (end - start) // step and all(
            candle.start == start + i * step for i, candle in enumerate(candles)
        )

    def owner_deadline(self, stream: str, start: int, end: int, now: int) -> int | None:
        from .market_storage import stream_id

        sid = stream_id(self.store.db, stream)
        row = self.store.db.execute(
            "SELECT MAX(expires) FROM market_holds WHERE stream=? AND start<? AND end>? AND expires>?",
            (sid, end, start, now),
        ).fetchone()
        return row[0] if row else None

    async def work(self, now: int, batch: int = 32, seconds: float = 1) -> None:
        """Drain due work independently of expensive outcome/comparator cycles.

        Requests still pass through the radar's shared rate/concurrency limiter.
        Every page commits before yielding; cancellation never consumes a job.
        """
        # Retire already satisfied/obsolete jobs even while live bootstrap owns
        # the REST budget. Their pending index is not useful historical evidence.
        if self.store.bounded:
            await self.retire_satisfied(now)
        # Retirement has its own bounded quantum and can yield to other jobs.
        # Start the REST admission budget only when this phase is reached.
        deadline = time.monotonic() + seconds
        processed = 0
        while processed < batch:
            if time.monotonic() >= deadline:
                break
            # Recheck live priority between pages, not just at batch admission.
            if any(v[0] < 20 for v in self.market._pending.values()) or self.market._working:
                break
            current = max(now, self.market.server_now())
            due = []
            for item in self.history.items("analytics-gap-pending:", min(2, batch - processed)):
                job = self.history.get("analytics-gap:" + item[1]["id"])
                if (
                    job is not None
                    and job["state"] in ("PENDING", "RETRY")
                    and job["next_attempt_ms"] > current
                ):
                    break
                due.append(item)
            if not due:
                break
            # Independent pages share the existing four-slot/rate limiter. Two
            # slots remain available if live recovery arrives during network I/O.
            # TaskGroup cancels/joins siblings before releasing the worker lock.
            async with asyncio.TaskGroup() as group:
                for item in due:
                    group.create_task(self.one(current, selected=item, admission_deadline=deadline))
            processed += len(due)
            await asyncio.sleep(0)

    async def one(
        self,
        now: int,
        *,
        selected: tuple[str, dict[str, Any]] | None = None,
        admission_deadline: float | None = None,
    ) -> None:
        # Live gaps and bootstrap keep priority over historical analytics in the same REST budget.
        if admission_deadline is not None and time.monotonic() >= admission_deadline:
            return
        if any(v[0] < 20 for v in self.market._pending.values()) or self.market._working:
            return
        for pending, binding in (
            [selected] if selected is not None else self.history.items("analytics-gap-pending:", 1)
        ):
            job = self.history.get("analytics-gap:" + binding["id"])
            if (
                job is None
                or job["state"] in ("COMPLETE", "SUPERSEDED", "EXPIRED_RETENTION_INCOMPLETE")
                or job.get("pending_key") != pending
            ):
                with self.store.db:
                    self.history.delete(pending)
                continue
            if (
                self.store.bounded
                and self.owner_deadline(job["stream"], job["start"], job["end"], now) is None
            ):
                self.finish_unowned(job, pending, now)
                return
            if self.complete_locally(job["stream"], job["start"], job["cursor_end"]):
                self.finish_local(job, pending, now)
                return
            if job["next_attempt_ms"] > now:
                return
            market, symbol, tf = job["stream"].split("/")
            step = int(tf[:-1]) * MINUTE
            if admission_deadline is not None and time.monotonic() >= admission_deadline:
                return
            try:
                response = await self.market._get(
                    "/v5/market/kline",
                    {
                        "category": market,
                        "symbol": symbol,
                        "interval": tf[:-1],
                        "limit": 1000,
                        "start": job["start"],
                        "end": job["cursor_end"] - 1,
                    },
                )
                with self.market._timed("referenced_recovery_response"):
                    # A live calculation can supersede/extend this job during I/O.
                    # Never resurrect its pending index or overwrite a newer cursor.
                    if self.history.get("analytics-gap:" + job["id"]) != job:
                        return
                    if (
                        self.store.bounded
                        and self.owner_deadline(
                            job["stream"], job["start"], job["end"], max(now, self.market.server_now())
                        )
                        is None
                    ):
                        self.finish_unowned(job, pending, now)
                        return
                    candles = sorted(
                        (
                            Candle.parse(
                                row, tf, "rest-analytics", int(time.time() * 1000), int(response["time"])
                            )
                            for row in response["result"]["list"]
                        ),
                        key=lambda c: c.start,
                    )
                    if (
                        not candles
                        or candles[-1].end != job["cursor_end"]
                        or candles[0].start < job["start"]
                        or any(b.start - a.start != step for a, b in zip(candles, candles[1:]))
                    ):
                        raise MarketError("incomplete_referenced_page")
                    writes = []
                    if self.store.bounded:
                        from .market_storage import read

                        existing_values = read(
                            self.store.db, job["stream"], candles[0].start, job["cursor_end"]
                        )
                        old = {v["start"]: Candle(**v) for v in existing_values}
                        if any(c.start in old and old[c.start].row() != c.row() for c in candles):
                            raise MarketError("referenced_source_conflict")
                    for candle in [] if self.store.bounded else candles:
                        key = Sources.prefix(job["stream"]) + f"{candle.start:016d}"
                        existing = self.store.db.execute(
                            "SELECT payload_json FROM runtime WHERE key=?", (key,)
                        ).fetchone()
                        if existing is not None:
                            previous = Candle(**json.loads(existing[0]))
                            if previous.row() != candle.row():
                                raise MarketError("referenced_source_conflict")
                        else:
                            writes.append((key, encode(asdict(candle))))
                    with self.store.db:
                        if self.store.bounded:
                            from .market_storage import admit

                            for candle in candles:
                                if candle.start not in old:
                                    admit(self.store.db, job["stream"], asdict(candle))
                        else:
                            self.store.db.executemany("INSERT INTO runtime VALUES (?,?)", writes)
                        job["cursor_end"] = candles[0].start
                        job["state"] = "COMPLETE" if job["cursor_end"] == job["start"] else "PENDING"
                        job.pop("reason", None)
                        self.history.delete(job["pending_key"])
                        if job["state"] != "COMPLETE":
                            job["pending_key"] = pending_key(now, job["id"]).replace(
                                "outcome-pending:", "analytics-gap-pending:", 1
                            )
                            self.history.put(job["pending_key"], binding)
                        self.history.put("analytics-gap:" + job["id"], job)
                        if job["state"] == "COMPLETE":
                            self.record_completion(job, max(now, self.market.server_now()))

            except (MarketError, KeyError, TypeError, ValueError, OverflowError) as exc:
                if self.history.get("analytics-gap:" + job["id"]) != job:
                    return
                job.update(
                    state="RETRY",
                    attempts=job["attempts"] + 1,
                    reason=str(exc) if isinstance(exc, MarketError) else "INVALID_REFERENCED_RESPONSE",
                )
                job["next_attempt_ms"] = now + min(300, 2 ** min(job["attempts"], 8)) * 1000
                with self.store.db:
                    self.history.delete(job["pending_key"])
                    job["pending_key"] = pending_key(job["next_attempt_ms"], job["id"]).replace(
                        "outcome-pending:", "analytics-gap-pending:", 1
                    )
                    self.history.put(job["pending_key"], binding)
                    self.history.put("analytics-gap:" + job["id"], job)

    def finish_local(self, job: dict[str, Any], pending: str, now: int) -> None:
        with self.store.db:
            self.history.delete(pending)
            job.update(state="COMPLETE", completed_ms=now, completion="LOCAL_CLOSED_DATA")
            job.pop("reason", None)
            # No consumer refers to the job bytes. Keep a one-day terminal
            # receipt for duplicate requests; actual source revisions stay intact.
            self.history.put("analytics-gap:" + job["id"], job)
            self.record_completion(job, now)

    def record_completion(self, job: dict[str, Any], now: int) -> None:
        from .queue_health import completed_batch

        completed_batch(self.store, "analytics-gap", now, [max(0, now - job["end"])], 0)

    def finish_unowned(self, job: dict[str, Any], pending: str, now: int) -> None:
        # No retained consumer requires this work. Do not decode old candle
        # windows or spend REST budget deciding the status of an unused request.
        # Actual event/outcome/source history remains untouched.
        with self.store.db:
            self.history.delete(pending)
            job.update(state="EXPIRED_RETENTION_INCOMPLETE", completed_ms=now, completion="NO_RETAINED_OWNER")
            self.history.put("analytics-gap:" + job["id"], job)

    async def retire_satisfied(self, now: int, batch: int = 64) -> None:
        cursor = self.history.get("recovery-retire-cursor", "")
        prefix = "radar:v1:analytics-gap-pending:"
        rows = self.store.db.execute(
            "SELECT key,payload_json FROM runtime WHERE key>? AND key<? ORDER BY key LIMIT ?",
            (cursor or prefix, prefix + "~", batch),
        ).fetchall()
        deadline = time.monotonic() + 0.025
        for row in rows:
            if time.monotonic() >= deadline:
                break
            binding = json_payload(row[1])
            pending = row[0][len("radar:v1:") :]
            job = self.history.get("analytics-gap:" + binding["id"])
            if job is None or job["state"] in ("COMPLETE", "SUPERSEDED") or job.get("pending_key") != pending:
                with self.store.db:
                    self.history.delete(pending)
            elif self.owner_deadline(job["stream"], job["start"], job["end"], now) is None:
                self.finish_unowned(job, pending, now)
            elif self.complete_locally(job["stream"], job["start"], job["cursor_end"]):
                self.finish_local(job, pending, now)
            cursor = row[0]
            await asyncio.sleep(0)
        if len(rows) < batch and (not rows or cursor == rows[-1][0]):
            cursor = ""
        with self.store.db:
            self.history.put("recovery-retire-cursor", cursor)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "pending": self.history.count("analytics-gap-pending:"),
            "recent": [
                self.history.get("analytics-gap:" + v["id"])
                for _, v in self.history.items("analytics-gap-pending:", 3)
            ],
        }
