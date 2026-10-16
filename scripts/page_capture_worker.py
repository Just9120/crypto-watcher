"""Isolated worker for a fenced source. No production write or migration qualification."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from cryptowatcher.change_journal import rearm_before_initial_snapshot, state
from cryptowatcher.lease import RuntimeLease
from cryptowatcher.page_delta import capture_closed
from cryptowatcher.storage import encode


def resources() -> None:
    if os.geteuid() != 10001:
        raise RuntimeError("Unprivileged page capture worker required")
    quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
    if (
        quota == "max"
        or not 0 < int(quota) <= int(period)
        or Path("/sys/fs/cgroup/memory.max").read_text().strip() != str(4 * 1024**3)
        or Path("/sys/fs/cgroup/memory.swap.max").read_text().strip() != "0"
    ):
        raise RuntimeError("Page capture worker limits not enforced")


def run(seconds: float) -> dict[str, Any]:
    if not 15 < seconds <= 900:
        raise RuntimeError("Invalid page capture worker deadline")
    resources()
    started = time.monotonic()
    deadline = started + seconds - 10
    source = Path("/history/cryptowatcher.sqlite3")
    plan = json.loads(Path("/input/capture-plan.json").read_text())
    if plan.get("schema") not in (
        "cryptowatcher.closed-page-capture.v1",
        "cryptowatcher.closed-page-capture.v2",
    ):
        raise RuntimeError("Page capture worker plan refused")
    # This source is fenced and has no sidecars. Immutable is only for metadata,
    # never for integrity/CHECK qualification.
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        journal = state(db, plan["journal_epoch"], 233382575)
        owner = db.execute("SELECT owner_id,revision FROM owner_settings").fetchall()
        if owner != [(233382575, 3)] or db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise RuntimeError("Closed source owner or schema changed")
    last = started

    def progress(**fields: Any) -> None:
        nonlocal last
        if time.monotonic() - last >= 30:
            print(
                encode(
                    {
                        "state": "CAPTURING_BYTES",
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        **fields,
                    }
                ),
                flush=True,
            )
            last = time.monotonic()

    result = capture_closed(
        source,
        Path("/input/base.json"),
        Path("/scratch/pages"),
        expected_baseline_sha256=plan["baseline_sha256"],
        max_archive_bytes=plan["max_archive_bytes"],
        seconds=max(0, deadline - time.monotonic()),
        progress=progress,
    )
    descriptor = Path("/scratch/pages/delta.json")
    result.update(
        journal_epoch=journal["epoch"],
        journal_watermark=journal["last_seq"],
        descriptor_sha256=hashlib.sha256(descriptor.read_bytes()).hexdigest(),
        elapsed_seconds=round(time.monotonic() - started, 3),
        full_source_integrity="PENDING",
        source_replaced=False,
    )
    with Path("/scratch/capture.json").open("x", encoding="utf-8") as output:
        output.write(encode(result))
        output.flush()
        os.fsync(output.fileno())
    return result


def prepare_initial_journal(seconds: float) -> dict[str, Any]:
    """Separate fenced RW step; actual page capture still has a read-only mount."""
    if not 15 < seconds <= 90:
        raise RuntimeError("Initial journal preparation budget refused")
    resources()
    deadline = time.monotonic() + seconds - 10
    source = Path("/history/cryptowatcher.sqlite3")
    plan = json.loads(Path("/input/capture-plan.json").read_text())
    initial = plan.get("initial_snapshot", {})
    if (
        plan.get("schema") != "cryptowatcher.closed-page-capture.v2"
        or initial.get("no_prior_capture") is not True
        or source.is_symlink()
        or source.resolve() != source
        or any(Path(str(source) + s).exists() for s in ("-wal", "-shm", "-journal"))
    ):
        raise RuntimeError("Fenced initial journal plan refused")
    with RuntimeLease(source), closing(sqlite3.connect(source, timeout=2)) as db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA busy_timeout=2000")
        if (
            db.execute("SELECT owner_id,revision FROM owner_settings").fetchall() != [(233382575, 3)]
            or db.execute("PRAGMA user_version").fetchone()[0] != 1
        ):
            raise RuntimeError("Initial journal owner changed")
        result = rearm_before_initial_snapshot(
            db,
            prior_epoch=initial["prior_epoch"],
            epoch=plan["journal_epoch"],
            owner=233382575,
            archive_directory=Path("/scratch/retired-journal"),
            expires=int(time.time()) + 7200,
            deadline=deadline,
            no_prior_capture=True,
        )
    with Path("/scratch/journal-rearm.json").open("x", encoding="utf-8") as output:
        output.write(encode(result))
        output.flush()
        os.fsync(output.fileno())
    return result


def main() -> int:
    seconds = float(sys.argv[1])
    if not 15 < seconds <= 900:
        raise RuntimeError("Invalid page capture worker deadline")
    done = threading.Event()

    def watchdog() -> None:
        if not done.wait(seconds):
            os._exit(3)

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        if sys.argv[2:] not in ([], ["--rearm-journal"]):
            raise RuntimeError("Unknown capture worker operation")
        result = prepare_initial_journal(seconds) if sys.argv[2:] else run(seconds)
        print(encode(result), flush=True)
        return 0
    finally:
        done.set()


if __name__ == "__main__":
    raise SystemExit(main())
