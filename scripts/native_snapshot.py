"""Managed native SQLite snapshot transport; never stops or replaces the application."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

from cryptowatcher.maintenance import Unit
from scripts import closed_source_maintenance as common
from scripts import native_snapshot_bridge as native
from scripts import online_capture as online


def command(root: Path, value: dict, seconds: float) -> list[str]:
    argv = online.worker_command(root, value, seconds)
    argv.insert(argv.index("--name"), "-i")
    index = argv.index("--entrypoint")
    argv[index:index] = [
        "--workdir",
        "/code",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=512m,uid=10001,gid=10001",
        "--mount",
        "type=bind,src=" + str(root / "tools/sqlite3_rsync") + ",dst=/tool/sqlite3_rsync,readonly",
        "--mount",
        "type=bind,src=" + str(common.DATABASE) + "-shm,dst=/history/cryptowatcher.sqlite3-shm",
    ]
    return argv


def preflight(root: Path, value: dict) -> dict:
    baseline = online.preflight(root, value)
    tool = root / "tools/sqlite3_rsync"
    native.binary(tool, windows=False)
    if tool.stat().st_uid != 0 or tool.stat().st_mode & 0o022:
        raise RuntimeError("Root-owned native tool required")
    for suffix in ("-wal", "-shm"):
        p = Path(str(common.DATABASE) + suffix)
        if not p.is_file() or p.is_symlink() or p.stat().st_uid != 10001:
            raise RuntimeError("Existing live WAL coordination files required")
    return baseline


def cleanup_command(root: Path) -> list[str]:
    return [
        "/usr/bin/python3",
        "-B",
        str(root / "operator/scripts/native_snapshot.py"),
        "--root",
        str(root),
        "--cleanup",
    ]


def run(root: Path, value: dict) -> int:
    import fcntl

    expected = Unit(value["container"], value["image"], value["revision"])
    deadline = common.verify_watchdog(value["worker"], os.environ["INVOCATION_ID"], cleanup_command(root))
    ops = online.operations(root, value)
    with (common.APP / ".deploy.lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        baseline = preflight(root, value)
        report = {
            "state": "NATIVE_CAPTURE_STARTED",
            "application_stopped": False,
            "production_replacement": False,
            "baseline": baseline,
        }
        ops.record(report)
        done = threading.Event()
        failed = threading.Event()

        def guard() -> None:
            bad = 0
            while not done.wait(15):
                try:
                    online.require_live_feed(expected)
                    if shutil.disk_usage(root).free < 1536 * 1024**2:
                        raise RuntimeError("Source WAL disk reserve exhausted")
                    bad = 0
                except Exception:
                    bad += 1
                if bad >= 2:
                    failed.set()
                    ops.remove_owned(value["worker"], value["image"])
                    return

        watch = threading.Thread(target=guard, daemon=True)
        watch.start()
        try:
            seconds = common.remaining_seconds(deadline, cap=900)
            with (root / "producer.stderr").open("xb") as errors:
                subprocess.run(
                    command(root, value, seconds - 15),
                    stdin=sys.stdin.buffer,
                    stdout=sys.stdout.buffer,
                    stderr=errors,
                    timeout=seconds,
                    check=True,
                )
            if failed.is_set():
                raise RuntimeError("Native helper stopped to protect live feed")
            online.require_live_feed(expected)
            result = json.loads((root / "work/snapshot.json").read_text())
            if (
                result.get("state") != "CAPTURED_SNAPSHOT_NOT_QUALIFIED"
                or result.get("journal_epoch") != value["journal_epoch"]
            ):
                raise RuntimeError("Native source snapshot receipt missing")
            report.update(
                state="ONLINE_CAPTURE_FINISHED_PC_READBACK_REQUIRED", snapshot=result, runtime_unchanged=True
            )
            ops.record(report)
            return 0
        except BaseException as error:
            report.update(state="NATIVE_CAPTURE_REFUSED", failure_type=type(error).__name__)
            ops.record(report)
            raise
        finally:
            done.set()
            watch.join(timeout=12)
            ops.remove_owned(value["worker"], value["image"])


def launch(root: Path, value: dict) -> int:
    preflight(root, value)
    with (root / "dispatch.json").open("x") as out:
        json.dump({"state": "NATIVE_DISPATCH_REQUESTED", "worker": value["worker"]}, out)
        out.flush()
        os.fsync(out.fileno())
    argv = [
        "/usr/bin/python3",
        "-B",
        str(root / "operator/scripts/native_snapshot.py"),
        "--root",
        str(root),
        "--run",
    ]
    return subprocess.run(
        [
            "systemd-run",
            "--quiet",
            "--collect",
            "--wait",
            "--pipe",
            "--unit",
            value["worker"],
            "--property=RuntimeMaxSec=900",
            "--property=TimeoutStopSec=10",
            "--property=KillMode=mixed",
            "--property=UMask=0077",
            "--property=Environment=PYTHONPATH=" + str(root / "operator"),
            "--property=ExecStopPost=" + " ".join(cleanup_command(root)),
            *argv,
        ],
        stdin=sys.stdin.buffer,
        stdout=sys.stdout.buffer,
        stderr=sys.stderr.buffer,
        check=False,
    ).returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--launch", action="store_true")
    group.add_argument("--run", action="store_true")
    group.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0 or socket.gethostname() != "vmi3224957":
        raise RuntimeError("Wrong native capture host/account")
    value = online.configuration(args.root)
    if args.cleanup:
        online.operations(args.root, value).remove_owned(value["worker"], value["image"])
        return 0
    if args.launch:
        return launch(args.root, value)
    if args.run:
        return run(args.root, value)
    preflight(args.root, value)
    print('{"state":"PREFLIGHT_PASS"}', file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
