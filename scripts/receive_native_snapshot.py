"""Pinned Windows native snapshot receiver. Modifies only a disposable isolated copy."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from contextlib import closing
from pathlib import Path, PurePosixPath

from cryptowatcher.lease import RuntimeLease
from scripts import native_snapshot_bridge as native
from scripts.online_capture import fingerprint
from scripts.receive_online_pages import connection, windows_limits


def receive(
    configuration: dict, root: str, working: Path, executable: Path, directory: Path, *, baseline_sha256: str
) -> dict:
    remote = PurePosixPath(root)
    if (
        remote.parent != PurePosixPath("/opt/crypto_watcher/rehearsals")
        or re.fullmatch("online-snapshot-[a-z0-9_-]+", remote.name) is None
    ):
        raise RuntimeError("Reviewed native snapshot root required")
    if re.fullmatch("[a-f0-9]{64}", baseline_sha256) is None:
        raise RuntimeError("Qualified baseline hash required")
    for path in (working, directory):
        if path.resolve() != path or any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
            raise RuntimeError("Private isolated Windows paths required")
    if not working.is_file() or directory.exists():
        raise RuntimeError("Existing disposable copy and new receipt directory required")
    if any(Path(str(working) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise RuntimeError("Close replica SQLite writers first")
    native.binary(executable, windows=True)
    windows_limits(processes=3)
    directory.mkdir(mode=0o700)
    started = time.monotonic()
    deadline = started + 900
    stopped = threading.Event()
    guard_done = threading.Event()
    cancelled = threading.Event()
    state = {
        "state": "BASELINE_HASH",
        "production_replacement": False,
        "working": str(working),
        "seconds": 900,
        "ram_bytes": 4 * 1024**3,
        "cpu": 1,
    }

    def save(**fields):
        state.update(fields)
        state["elapsed_seconds"] = round(time.monotonic() - started, 3)
        tmp = directory / "status.tmp"
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, directory / "status.json")
        print(json.dumps(state), flush=True)

    def watchdog():
        if not stopped.wait(900):
            os._exit(3)

    threading.Thread(target=watchdog, daemon=True).start()
    ssh = connection(configuration)
    assert ssh[-2] == "-n"
    ssh.pop(-2)
    remote_command = (
        "sudo -n env -u DOCKER_CONTEXT DOCKER_HOST=unix:///var/run/docker.sock PYTHONPATH="
        + root
        + "/operator /usr/bin/python3 -B "
        + root
        + "/operator/scripts/native_snapshot.py --root "
        + root
        + " --launch"
    )
    try:
        with RuntimeLease(working):
            save()
            if fingerprint(working, deadline)[0] != baseline_sha256:
                raise RuntimeError("Disposable baseline changed")
            if shutil.disk_usage(working.parent).free < 20 * 1024**3:
                raise RuntimeError("Replica admission disk reserve unavailable")
            save(state="NATIVE_TRANSFER", baseline_sha256=baseline_sha256)
            with (
                (directory / "ssh.stderr").open("xb") as ssherr,
                (directory / "replica.stderr").open("xb") as errors,
            ):
                process = subprocess.Popen(
                    ssh + [remote_command],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=ssherr,
                    bufsize=0,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )

                def space_guard():
                    while not guard_done.wait(10):
                        if shutil.disk_usage(working.parent).free < 1536 * 1024**2:
                            cancelled.set()
                            if process.poll() is None:
                                process.kill()
                            return

                guard = threading.Thread(target=space_guard, daemon=True)
                guard.start()
                try:
                    assert process.stdout is not None and process.stdin is not None
                    result = native.bridge(
                        [
                            str(executable),
                            "--protocol",
                            "1",
                            "--replica",
                            "/history/cryptowatcher.sqlite3",
                            str(working),
                        ],
                        process.stdout,
                        process.stdin,
                        role="replica",
                        errors=errors,
                        seconds=max(1, deadline - time.monotonic() - 90),
                        cancelled=cancelled,
                    )
                    process.stdin.close()
                    process.wait(timeout=max(0.001, deadline - time.monotonic()))
                    if process.returncode or cancelled.is_set():
                        raise RuntimeError("Native source operation refused")
                finally:
                    guard_done.set()
                    guard.join(timeout=2)
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                    if process.stdin is not None:
                        process.stdin.close()
                    if process.stdout is not None:
                        process.stdout.close()
            operation = subprocess.run(
                ssh + ["sudo -n cat " + root + "/operation.json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=min(15, max(0.001, deadline - time.monotonic())),
            )
            if operation.returncode:
                raise RuntimeError("Native source receipt unavailable")
            producer = json.loads(operation.stdout)
            if (
                producer.get("state") != "ONLINE_CAPTURE_FINISHED_PC_READBACK_REQUIRED"
                or producer.get("runtime_unchanged") is not True
            ):
                raise RuntimeError("Native source post-checks not confirmed")
            snapshot = producer["snapshot"]
            if (
                snapshot.get("origin") != result["origin"]
                or snapshot.get("encoded_sent") != result["encoded_received"]
                or snapshot.get("encoded_received") != result["encoded_sent"]
            ):
                raise RuntimeError("Native transcript/transport identity mismatch")
            if any(Path(str(working) + s).exists() for s in ("-wal", "-shm", "-journal")):
                raise RuntimeError("Replica not cleanly closed")
            save(state="SNAPSHOT_READBACK")
            sha, size = fingerprint(working, deadline)
            if size != result["origin"]["pages"] * result["origin"]["page_bytes"]:
                raise RuntimeError("Native replica extent mismatch")
            with closing(sqlite3.connect(working.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
                db.execute("PRAGMA query_only=ON")
                if (
                    db.execute("SELECT owner_id,revision FROM owner_settings").fetchall() != [(233382575, 3)]
                    or db.execute("PRAGMA user_version").fetchone()[0] != 1
                ):
                    raise RuntimeError("Snapshot owner/schema changed")
                names = (
                    "epoch",
                    "owner",
                    "last_seq",
                    "key_count",
                    "key_bytes",
                    "max_keys",
                    "max_bytes",
                    "expires",
                    "valid",
                    "reason",
                )
                journal = dict(
                    zip(
                        names,
                        db.execute(
                            "SELECT " + ",".join(names) + " FROM _cw_catchup_state WHERE id=1"
                        ).fetchone(),
                        strict=True,
                    )
                )
                if (
                    journal["epoch"] != snapshot["journal_epoch"]
                    or journal["valid"] != 1
                    or journal["expires"] <= time.time()
                ):
                    raise RuntimeError("Captured journal cannot preserve subsequent writes")
            if time.monotonic() >= deadline:
                raise RuntimeError("Native receiver whole deadline exhausted")
            result.update(
                state="CAPTURED_SNAPSHOT_NOT_QUALIFIED",
                raw_sha256=sha,
                raw_bytes=size,
                journal=journal,
                source_operation=root + "/operation.json",
                full_sql_integrity="PENDING_PC_READBACK",
                production_replacement=False,
            )
            with (directory / "capture.json").open("x", encoding="utf-8") as out:
                json.dump(result, out, sort_keys=True)
                out.flush()
                os.fsync(out.fileno())
            save(state="CAPTURED_SNAPSHOT_NOT_QUALIFIED", raw_sha256=sha, raw_bytes=size, journal=journal)
            return result
    except BaseException as error:
        save(state="REFUSED", failure_type=type(error).__name__)
        raise
    finally:
        stopped.set()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("configuration", type=Path)
    p.add_argument("root")
    p.add_argument("working", type=Path)
    p.add_argument("executable", type=Path)
    p.add_argument("directory", type=Path)
    p.add_argument("--baseline-sha256", required=True)
    a = p.parse_args()
    receive(
        json.loads(a.configuration.read_text(encoding="utf-8")),
        a.root,
        a.working,
        a.executable,
        a.directory,
        baseline_sha256=a.baseline_sha256,
    )


if __name__ == "__main__":
    main()
