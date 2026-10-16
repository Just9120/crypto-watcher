"""Root operator: a managed read-only snapshot, with no production stop or switch."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from cryptowatcher.maintenance import Unit
from cryptowatcher.storage import encode
from scripts import closed_source_maintenance as common
from scripts.closed_page_capture import JOURNAL_READBACK, uncaptured_epoch


def require_live_feed(expected: Unit) -> None:
    value = common.exact_unit(expected, seconds=10)
    state = json.loads(
        common.call(
            [*common.DOCKER, "exec", "-i", expected.container, "python", "-B", "-"],
            seconds=10,
            data=common.RUNTIME_READBACK,
        )
    )
    if value["State"].get("Health", {}).get("Status") != "healthy" or not common.restored_data(
        state, 0, time.time()
    ):
        raise RuntimeError("Fresh radar data required during online capture")


def checked_worker(
    command: list[str],
    expected: Unit,
    ops: common.DockerOperations,
    value: dict,
    *,
    seconds: float,
    stdout,
    interval: float = 15,
) -> None:
    """Release only this helper if live feed health stops being confirmed."""
    done = threading.Event()
    failed = threading.Event()

    def watch() -> None:
        bad = 0
        while not done.wait(interval):
            try:
                require_live_feed(expected)
                bad = 0
            except Exception:
                bad += 1
            if bad >= 2:
                failed.set()
                ops.remove_owned(value["worker"], value["image"])
                return

    require_live_feed(expected)
    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    try:
        subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=sys.stderr.buffer,
            check=True,
            timeout=seconds,
        )
    finally:
        done.set()
        thread.join(timeout=12)
        if failed.is_set():
            raise RuntimeError("Online helper stopped to protect radar feed")
    require_live_feed(expected)


def fingerprint(path: Path, deadline: float) -> tuple[str, int]:
    """Root launcher uses only the host standard library; APSW stays in worker."""
    meta = path.stat()
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for body in iter(lambda: source.read(1024**2), b""):
            if time.monotonic() >= deadline:
                raise RuntimeError("Operator hash deadline exhausted")
            digest.update(body)
            size += len(body)
    current = path.stat()
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode", "st_nlink")
    if (
        tuple(getattr(current, k) for k in fields) != tuple(getattr(meta, k) for k in fields)
        or size != meta.st_size
    ):
        raise RuntimeError("Operator hash identity changed")
    return digest.hexdigest(), size


def configuration(root: Path) -> dict:
    common.private_directory(root, uid=0)
    p = root / "preparation.json"
    meta = p.lstat()
    if p.is_symlink() or meta.st_uid != 0 or meta.st_mode & 0o077:
        raise RuntimeError("Private online plan required")
    value = json.loads(p.read_text())
    for name in ("container", "driver_sha256"):
        if re.fullmatch("[a-f0-9]{64}", value[name]) is None:
            raise RuntimeError("Online identity refused")
    if re.fullmatch("cw-online-snapshot-[a-f0-9]{32}", value["worker"]) is None:
        raise RuntimeError("Online worker identity refused")
    if "initial_snapshot" in value:
        raise RuntimeError("Live journal rearm forbidden")
    if (
        re.fullmatch("sha256:[a-f0-9]{64}", value["image"]) is None
        or re.fullmatch("[a-f0-9]{40}", value["revision"]) is None
    ):
        raise RuntimeError("Online revision refused")
    manifest = json.loads((root / "operator-manifest.json").read_text())
    actual = {
        p.relative_to(root / "operator").as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (root / "operator").rglob("*.py")
    }
    if manifest != actual:
        raise RuntimeError("Online operator code changed")
    return value


def operations(root: Path, value: dict) -> common.DockerOperations:
    return common.DockerOperations(root, root / "code", value["driver_sha256"], value["worker"])


def preflight(root: Path, value: dict) -> dict:
    expected = Unit(value["container"], value["image"], value["revision"])
    baseline = operations(root, value).verify_binding(expected)
    common.private_directory(root / "input", uid=10001)
    if {p.name for p in (root / "input").iterdir()} != {"base.json", "base.hashes", "capture-plan.json"}:
        raise RuntimeError("Online input set changed")
    plan = json.loads((root / "input/capture-plan.json").read_text())
    if plan != {
        "schema": "cryptowatcher.online-capture-plan.v1",
        "baseline_sha256": value["baseline_sha256"],
        "journal_epoch": value["journal_epoch"],
        "maximum": 8 * 1024**3,
    }:
        raise RuntimeError("Online worker plan changed")
    descriptor = root / "input/base.json"
    if hashlib.sha256(descriptor.read_bytes()).hexdigest() != value["baseline_sha256"]:
        raise RuntimeError("Online baseline changed")
    record = json.loads(descriptor.read_text())
    hashes = root / "input/base.hashes"
    if (
        hashes.is_symlink()
        or hashes.stat().st_size != record["raw_bytes"] // 4096 * 32
        or fingerprint(hashes, time.monotonic() + 30)[0] != record["hashes_sha256"]
    ):
        raise RuntimeError("Online baseline hashes changed")
    if shutil.disk_usage(root).free < 1536 * 1024**2 + 128 * 1024**2:
        raise RuntimeError("Online source WAL reserve unavailable")
    memory = dict(x.split(":", 1) for x in Path("/proc/meminfo").read_text().splitlines())
    if int(memory["MemAvailable"].split()[0]) * 1024 < 4 * 1024**3 + 1536 * 1024**2:
        raise RuntimeError("Online RAM reserve unavailable")
    journal = json.loads(
        common.call(
            [*common.DOCKER, "exec", "-i", expected.container, "python", "-B", "-"], data=JOURNAL_READBACK
        )
    )
    if (
        journal.get("epoch") != value["journal_epoch"]
        or journal.get("valid") != 1
        or journal.get("expires", 0) < time.time() + 900
        or journal.get("max_keys") != 500000
        or journal.get("max_bytes") != 128 * 1024**2
    ):
        raise RuntimeError("Current online journal cannot preserve newer writes")
    require_live_feed(expected)
    return baseline


def launch(root: Path, value: dict) -> int:
    preflight(root, value)
    path = root / "dispatch.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(
            encode({"state": "DISPATCH_REQUESTED", "worker": value["worker"], "application_stopped": False})
        )
        out.flush()
        os.fsync(out.fileno())
    argv = ["/usr/bin/python3", "-B", str(root / "operator/scripts/online_capture.py"), "--root", str(root)]
    cleanup = [*argv, "--cleanup"]
    command = [
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
        "--property=ExecStopPost=" + " ".join(cleanup),
        *argv,
        "--run",
    ]
    return subprocess.run(
        command, stdin=subprocess.DEVNULL, stdout=sys.stdout.buffer, stderr=sys.stderr.buffer, check=False
    ).returncode


def worker_command(root: Path, value: dict, seconds: float, *, archive_journal: bool = False) -> list[str]:
    command = [
        *common.DOCKER,
        "run",
        "--name",
        value["worker"],
        "--label",
        "cryptowatcher.maintenance=" + value["worker"],
        "--pull",
        "never",
        "--network",
        "none",
        "--read-only",
        "--no-healthcheck",
        "--log-driver",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "10001:10001",
        "--memory",
        "4g",
        "--memory-swap",
        "4g",
        "--cpus",
        "1",
        "--pids-limit",
        "64",
        "--env",
        "PYTHONPATH=/code:/app",
        "--mount",
        "type=bind,src="
        + str(common.DATABASE.parent)
        + ",dst=/history"
        + ("" if archive_journal else ",readonly"),
        "--mount",
        "type=bind,src=" + str(root / "code") + ",dst=/code,readonly",
        "--mount",
        "type=bind,src=" + str(root / "input") + ",dst=/input,readonly",
        "--mount",
        "type=bind,src=" + str(root / "work") + ",dst=/scratch",
        "--entrypoint",
        "python",
        value["image"],
        "-B",
        "/code/driver.py",
        str(seconds),
    ]
    if archive_journal:
        command.append("--journal-archive")
    return command


def run(root: Path, value: dict) -> int:
    import fcntl

    expected = Unit(value["container"], value["image"], value["revision"])
    cleanup = [
        "/usr/bin/python3",
        "-B",
        str(root / "operator/scripts/online_capture.py"),
        "--root",
        str(root),
        "--cleanup",
    ]
    deadline = common.verify_watchdog(value["worker"], os.environ["INVOCATION_ID"], cleanup)
    ops = operations(root, value)
    with (common.APP / ".deploy.lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        baseline = preflight(root, value)
        report = {
            "state": "ONLINE_CAPTURE_STARTED",
            "application_stopped": False,
            "production_replacement": False,
            "baseline": baseline,
        }
        ops.record(report)
        seconds = common.remaining_seconds(deadline, cap=900)
        command = worker_command(root, value, seconds)
        try:
            uncaptured_epoch(value["journal_epoch"])
            preparation_seconds = min(90, common.remaining_seconds(deadline, cap=900))
            checked_worker(
                worker_command(root, value, preparation_seconds, archive_journal=True),
                expected,
                ops,
                value,
                seconds=preparation_seconds,
                stdout=sys.stderr.buffer,
            )
            prefix = json.loads((root / "work/journal-prefix/acknowledgement.json").read_text())
            if (
                prefix.get("state") != "PREPARED_UNCONSUMED_JOURNAL_PREFIX"
                or prefix.get("epoch") != value["journal_epoch"]
                or prefix.get("application_rows_deleted") != 0
                or prefix.get("requires_fresh_full_source_snapshot") is not True
            ):
                raise RuntimeError("Durable journal-prefix acknowledgement missing")
            report["journal_prefix"] = prefix
            ops.record(report)
            ops.remove_owned(value["worker"], value["image"])
            seconds = common.remaining_seconds(deadline, cap=900)
            command = worker_command(root, value, seconds)
            checked_worker(
                command,
                expected,
                ops,
                value,
                seconds=seconds,
                stdout=sys.stdout.buffer,
            )
            result = json.loads((root / "work/snapshot.json").read_text())
            if (
                result.get("state") != "CAPTURED_SNAPSHOT_NOT_QUALIFIED"
                or result.get("journal_epoch") != value["journal_epoch"]
            ):
                raise RuntimeError("Online primary snapshot evidence missing")
            current = common.exact_unit(expected, seconds=common.remaining_seconds(deadline))
            if (
                not current["State"]["Running"]
                or current["State"].get("Health", {}).get("Status") != "healthy"
            ):
                raise RuntimeError("Live radar health not confirmed")
            report.update(
                state="ONLINE_CAPTURE_FINISHED_PC_READBACK_REQUIRED", snapshot=result, runtime_unchanged=True
            )
            ops.record(report)
            return 0
        except BaseException as error:
            report.update(state="ONLINE_CAPTURE_REFUSED", failure_type=type(error).__name__)
            ops.record(report)
            raise
        finally:
            ops.remove_owned(value["worker"], value["image"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--launch", action="store_true")
    group.add_argument("--run", action="store_true")
    group.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0 or socket.gethostname() != "vmi3224957":
        raise RuntimeError("Wrong online snapshot host/account")
    value = configuration(args.root)
    if args.cleanup:
        operations(args.root, value).remove_owned(value["worker"], value["image"])
        return 0
    if args.launch:
        return launch(args.root, value)
    if args.run:
        return run(args.root, value)
    preflight(args.root, value)
    print('{"state":"PREFLIGHT_PASS","application_stopped":false}', file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
