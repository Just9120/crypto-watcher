"""One online snapshot. Stdout is binary; diagnostics go exclusively to stderr."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

from cryptowatcher.journal_archive import archive_and_ack
from cryptowatcher.online_pages import capture
from cryptowatcher.storage import encode


def run(seconds: float, *, archive_journal: bool = False) -> dict:
    if not 15 < seconds <= 900 or os.geteuid() != 10001:
        raise RuntimeError("Bounded unprivileged worker required")
    quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
    if (
        quota == "max"
        or not 0 < int(quota) <= int(period)
        or Path("/sys/fs/cgroup/memory.max").read_text().strip() != str(4 * 1024**3)
        or Path("/sys/fs/cgroup/memory.swap.max").read_text().strip() != "0"
    ):
        raise RuntimeError("Online capture limits unavailable")
    plan = json.loads(Path("/input/capture-plan.json").read_text())
    if plan.get("schema") != "cryptowatcher.online-capture-plan.v1":
        raise RuntimeError("Online capture plan refused")
    if "initial_snapshot" in plan:
        raise RuntimeError("Live journal rearm forbidden; use an existing valid epoch")
    last = time.monotonic()

    def progress(**fields):
        nonlocal last
        if time.monotonic() - last >= 30:
            print(encode(fields), file=sys.stderr, flush=True)
            last = time.monotonic()

    if archive_journal:
        from contextlib import closing

        with closing(sqlite3.connect("/history/cryptowatcher.sqlite3", timeout=2)) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA busy_timeout=2000")
            result = archive_and_ack(
                db,
                Path("/scratch/journal-prefix"),
                epoch=plan["journal_epoch"],
                owner=233382575,
                seconds=seconds - 10,
                key_only_before_first_snapshot=True,
                no_prior_capture=True,
            )
        return result

    import apsw

    # This is a dedicated process with no open APSW connections. Virtual mapping
    # avoids per-page reads; Docker's existing 4GiB/no-swap cap still bounds all
    # resident pages, caches and hash arrays. No RAM allocation is enlarged.
    apsw.shutdown()
    apsw.config(apsw.SQLITE_CONFIG_MMAP_SIZE, 64 * 1024**3, 64 * 1024**3)
    apsw.initialize()
    return capture(
        Path("/history/cryptowatcher.sqlite3"),
        Path("/input/base.json"),
        Path("/scratch"),
        sys.stdout.buffer,
        expected_baseline_sha256=plan["baseline_sha256"],
        epoch=plan["journal_epoch"],
        owner=233382575,
        maximum=plan["maximum"],
        seconds=seconds - 10,
        progress=progress,
    )


def main() -> int:
    seconds = float(sys.argv[1])
    done = threading.Event()
    if not 15 < seconds <= 900:
        raise RuntimeError("Online capture deadline refused")

    def watchdog():
        if not done.wait(seconds):
            os._exit(3)

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        if sys.argv[2:] not in ([], ["--journal-archive"]):
            raise RuntimeError("Unknown online worker operation")
        result = run(seconds, archive_journal=bool(sys.argv[2:]))
        print(
            encode(
                {
                    "state": result["state"],
                    "data_bytes": result.get("data_bytes", result.get("archive_bytes")),
                    "raw_bytes": result.get("raw_bytes", result.get("archive", {}).get("raw_bytes")),
                }
            ),
            file=sys.stderr,
            flush=True,
        )
        return 0
    finally:
        done.set()


if __name__ == "__main__":
    raise SystemExit(main())
