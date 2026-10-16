"""Isolated unprivileged native snapshot worker; no host operator dependencies."""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

from scripts import native_snapshot_bridge as native


def worker(seconds: float) -> None:
    if not 15 < seconds <= 900 or os.geteuid() != 10001:
        raise RuntimeError("Bounded unprivileged native worker required")
    quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
    if (
        quota == "max"
        or not 0 < int(quota) <= int(period)
        or Path("/sys/fs/cgroup/memory.max").read_text().strip() != str(4 * 1024**3)
        or Path("/sys/fs/cgroup/memory.swap.max").read_text().strip() != "0"
    ):
        raise RuntimeError("Native worker resource limits unavailable")
    plan = json.loads(Path("/input/capture-plan.json").read_text())
    executable = Path("/tool/sqlite3_rsync")
    native.binary(executable, windows=False)
    done = threading.Event()

    def watchdog() -> None:
        if not done.wait(seconds):
            os._exit(3)

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        with Path("/scratch/native.stderr").open("xb") as errors:
            result = native.bridge(
                [str(executable), "--origin", "/history/cryptowatcher.sqlite3", "source.sqlite3"],
                sys.stdin.buffer,
                sys.stdout.buffer,
                role="origin",
                errors=errors,
                seconds=seconds - 5,
            )
        result.update(
            state="CAPTURED_SNAPSHOT_NOT_QUALIFIED",
            journal_epoch=plan["journal_epoch"],
            full_sql_integrity="PENDING_PC_READBACK",
            production_replacement=False,
        )
        p = Path("/scratch/snapshot.json")
        with p.open("x") as out:
            json.dump(result, out, sort_keys=True)
            out.flush()
            os.fsync(out.fileno())
    finally:
        done.set()
