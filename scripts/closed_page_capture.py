"""Operator-only closed page capture with the existing independent recovery watchdog."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import sys
import time
from pathlib import Path
from typing import Any

from cryptowatcher.maintenance import Unit, capture_closed
from cryptowatcher.storage import encode
from scripts import closed_source_maintenance as common

MAX_ARCHIVE = 3 * 1024**3
RESERVE = 1536 * 1024**2

JOURNAL_READBACK = """
import json,os,sqlite3
from contextlib import closing
from pathlib import Path
p=Path(os.environ['DATABASE_FILE'])
with closing(sqlite3.connect(p.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
 db.execute('PRAGMA query_only=ON')
 row=db.execute('SELECT epoch,last_seq,key_count,key_bytes,valid,expires FROM _cw_catchup_state WHERE id=1').fetchone()
 assert row is not None
 print(json.dumps(dict(zip(('epoch','last_seq','keys','key_bytes','valid','expires'),row))))
"""


def plan(root: Path) -> dict[str, Any]:
    path = root / "capture-plan.json"
    meta = path.lstat()
    if path.resolve() != path or path.is_symlink() or meta.st_uid != 0 or meta.st_mode & 0o077:
        raise RuntimeError("Root-owned private capture plan required")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        set(value) != {"schema", "baseline_sha256", "journal_epoch", "max_archive_bytes"}
        or value["schema"] != "cryptowatcher.closed-page-capture.v1"
        or re.fullmatch(r"[a-f0-9]{64}", value["baseline_sha256"]) is None
        or re.fullmatch(r"[a-f0-9]{32}", value["journal_epoch"]) is None
        or type(value["max_archive_bytes"]) is not int
        or not 0 < value["max_archive_bytes"] <= MAX_ARCHIVE
    ):
        raise RuntimeError("Capture plan bounds refused")
    inputs = root / "input"
    common.private_directory(inputs, uid=10001)
    if {p.name for p in inputs.iterdir()} != {"base.json", "base.index", "capture-plan.json"}:
        raise RuntimeError("Capture input set changed")
    for source in inputs.iterdir():
        current = source.lstat()
        if source.is_symlink() or not source.is_file() or current.st_uid != 10001 or current.st_mode & 0o077:
            raise RuntimeError("Private capture input required")
    if (inputs / "capture-plan.json").read_bytes() != path.read_bytes():
        raise RuntimeError("Worker capture plan differs")
    baseline = inputs / "base.json"
    if hashlib.sha256(baseline.read_bytes()).hexdigest() != value["baseline_sha256"]:
        raise RuntimeError("Capture baseline descriptor changed")
    record = json.loads(baseline.read_text())
    index = inputs / "base.index"
    if (
        record.get("schema") != "cryptowatcher.page-base.v1"
        or record.get("index") != "base.index"
        or type(record.get("blocks")) is not int
        or not 0 <= record["blocks"] <= 64 * 1024**2 // 32
        or index.stat().st_size != record["blocks"] * 32
        or hashlib.sha256(index.read_bytes()).hexdigest() != record.get("index_sha256")
    ):
        raise RuntimeError("Capture baseline index changed")
    return value


class PageOperations(common.DockerOperations):
    def verify(self, expected: Unit) -> dict[str, Any]:
        baseline = self.verify_binding(expected)
        value = plan(self.root)
        # Capture creates only a bounded changed-page archive, no full candidate.
        # The original qualification's 12.5GiB admission remains unchanged.
        if shutil.disk_usage(self.work).free < value["max_archive_bytes"] + RESERVE + 64 * 1024**2:
            raise RuntimeError("Insufficient page capture disk admission")
        memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        if int(memory["MemAvailable"].split()[0]) * 1024 < 4 * 1024**3 + RESERVE:
            raise RuntimeError("Insufficient page capture RAM reserve")
        journal = json.loads(
            common.call(
                [*common.DOCKER, "exec", "-i", expected.container, "python", "-B", "-"],
                data=JOURNAL_READBACK,
            )
        )
        if (
            journal.get("epoch") != value["journal_epoch"]
            or journal.get("valid") != 1
            or not isinstance(journal.get("expires"), (int, float))
            or journal["expires"] < time.time() + 900
        ):
            raise RuntimeError("Current journal cannot preserve post-capture writes")
        return baseline

    def capture(self, expected: Unit, seconds: float) -> dict[str, Any]:
        result = self.run_worker(expected, seconds, result_name="capture.json", mount_input=True)
        value = plan(self.root)
        descriptor = self.work / "pages/delta.json"
        if (
            result.get("state") != "CAPTURED_BYTES_NOT_QUALIFIED"
            or result.get("full_source_integrity") != "PENDING"
            or result.get("journal_epoch") != value["journal_epoch"]
            or type(result.get("journal_watermark")) is not int
            or descriptor.is_symlink()
            or not descriptor.is_file()
            or hashlib.sha256(descriptor.read_bytes()).hexdigest() != result.get("descriptor_sha256")
        ):
            raise RuntimeError("Primary page capture evidence missing")
        return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--code", type=Path, required=True)
    parser.add_argument("--driver-sha256", required=True)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--restore-only", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0 or socket.gethostname() != "vmi3224957":
        raise RuntimeError("Wrong page capture host/account")
    for value in (args.container, args.driver_sha256):
        if re.fullmatch(r"[a-f0-9]{64}", value) is None:
            raise RuntimeError("Reviewed page capture identity required")
    if (
        re.fullmatch(r"sha256:[a-f0-9]{64}", args.image) is None
        or re.fullmatch(r"[a-f0-9]{40}", args.revision) is None
        or re.fullmatch(r"cw-closed-qual-[a-f0-9]{32}", args.worker) is None
    ):
        raise RuntimeError("Reviewed page capture target required")
    expected = Unit(args.container, args.image, args.revision)
    operations = PageOperations(args.root, args.code, args.driver_sha256, args.worker)
    if args.restore_only:
        common.watchdog_restore(operations, expected)
        return 0
    if not args.execute:
        operations.verify(expected)
        print('{"state":"PREFLIGHT_PASS","production_changed":false}')
        return 0
    if not os.environ.get("INVOCATION_ID"):
        raise RuntimeError("Managed page capture watchdog required")
    restore_argv = [
        sys.executable,
        "-B",
        str(Path(__file__).resolve()),
        *(v for v in sys.argv[1:] if v != "--execute"),
        "--restore-only",
    ]
    deadline = common.verify_watchdog(args.worker, os.environ["INVOCATION_ID"], restore_argv)
    import fcntl

    with (common.APP / ".deploy.lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = capture_closed(operations, expected, absolute_deadline=deadline)
    print(encode(result))
    return 0 if result["state"] == "CAPTURED_RUNTIME_RESTORED" else 3


if __name__ == "__main__":
    raise SystemExit(main())
