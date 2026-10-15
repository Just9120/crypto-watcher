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

from cryptowatcher.change_journal import state
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
    if plan.get("schema") != "cryptowatcher.closed-page-capture.v1":
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
        result = run(seconds)
        print(encode(result), flush=True)
        return 0
    finally:
        done.set()


if __name__ == "__main__":
    raise SystemExit(main())
