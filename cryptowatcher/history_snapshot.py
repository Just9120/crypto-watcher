"""Read-only WAL snapshot into a new compact candidate; never promote or overwrite."""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import re
import shutil
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .history_rebuild import DISK_RESERVE, copy_snapshot, fingerprint, guard
from .storage import IntegrityError, StateError, encode


def snapshot(
    source: Path,
    destination: Path,
    owner: int,
    expected_revision: int,
    deadline: float,
    maximum: int,
) -> dict[str, Any]:
    if not math.isfinite(deadline) or deadline > time.monotonic() + 900:
        raise StateError("Invalid snapshot deadline")
    guard(deadline)
    if type(maximum) is not int or maximum < 32768:
        raise StateError("Invalid candidate limit")
    if type(expected_revision) is not int or expected_revision < 1:
        raise StateError("Expected owner revision required")
    if any(p.is_symlink() for p in (source, *source.parents, destination, *destination.parents)):
        raise StateError("Symlink refused")
    if not source.is_file() or source.resolve() == destination.resolve() or destination.exists():
        raise StateError("Source must exist and candidate must be new")
    if not destination.parent.is_dir():
        raise StateError("Candidate directory unavailable")
    if os.name != "nt":
        parent = destination.parent.stat()
        if parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
            raise StateError("Candidate directory must be private and owned")
    if shutil.disk_usage(destination.parent).free < maximum + DISK_RESERVE:
        raise StateError("Insufficient candidate disk reserve")
    at = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as original:
        original.execute("PRAGMA query_only=ON")
        original.execute("BEGIN")
        # This read pins one WAL generation before every later source read.
        row = original.execute("SELECT owner_id,revision FROM owner_settings").fetchall()
        if row != [(owner, expected_revision)]:
            raise StateError("Owner settings revision changed")
        pages = original.execute("PRAGMA page_count").fetchone()[0]
        size = original.execute("PRAGMA page_size").fetchone()[0]
        destination.touch(mode=0o600, exist_ok=False)
        copied = copy_snapshot(original, destination, owner, maximum, deadline)
    proof = {
        "at_utc": at,
        "owner_id": owner,
        "owner_revision": expected_revision,
        "snapshot_bytes": pages * size,
        **copied,
    }
    revision = os.environ.get("SOURCE_REVISION", "UNSET")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        revision = "UNSET"
    return {
        "status": "ISOLATED_WAL_SNAPSHOT_PASS",
        "tool_source_revision": revision,
        **proof,
        "snapshot_identity": hashlib.sha256(encode(proof).encode()).hexdigest(),
        "candidate_bytes": destination.stat().st_size,
        "candidate_sha256": fingerprint(destination, deadline),
        "candidate_full_integrity": "PASS",
        "full_logical_row_readback": "PASS",
        "source_mode": "READ_ONLY_WAL_TRANSACTION",
        "source_full_physical_integrity": "NOT_CONFIRMED",
        "source_byte_hash": "UNSET_FOR_MUTABLE_INPUT",
        "live_writer_may_advance": True,
        "production_migration": "NOT_RUN",
        "promotion_eligible": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--owner-id", type=int, required=True)
    parser.add_argument("--expected-owner-revision", type=int, required=True)
    parser.add_argument("--max-candidate-bytes", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=900)
    args = parser.parse_args()
    try:
        if not math.isfinite(args.timeout_seconds) or not 0 < args.timeout_seconds <= 900:
            raise StateError("Invalid snapshot budget")
        result = snapshot(
            args.source,
            args.destination,
            args.owner_id,
            args.expected_owner_revision,
            time.monotonic() + args.timeout_seconds,
            args.max_candidate_bytes,
        )
        print(encode(result))
        return 0
    except IntegrityError as exc:
        print(encode({"status": "BLOCKED", "failure_code": exc.code, "promotion_eligible": False}))
        return exc.exit_status
    except (StateError, sqlite3.DatabaseError, ValueError, OSError):
        print('{"status":"BLOCKED","promotion_eligible":false,"production_migration":"NOT_RUN"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
