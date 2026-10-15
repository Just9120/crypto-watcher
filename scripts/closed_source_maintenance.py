"""Operator-only qualification adapter. Never replaces the production database.

Run under a reviewed managed unit with ExecStopPost restoring the same container.
An independent watchdog is mandatory; a Python finally cannot survive SIGKILL.
"""

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
import time
import uuid
from pathlib import Path
from typing import Any

from cryptowatcher.maintenance import ONE_TIME_SECONDS, Unit, qualify_closed
from cryptowatcher.storage import encode

APP = Path("/opt/crypto_watcher")
DATABASE = APP / "data/sqlite/cryptowatcher.sqlite3"
DOCKER = ["docker", "--host", "unix:///var/run/docker.sock"]
PRIVATE_READBACK = """
import hashlib,json,os,sqlite3
from contextlib import closing
from pathlib import Path
p=Path(os.environ['DATABASE_FILE'])
with closing(sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
 db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
 owner=db.execute('SELECT owner_id,revision FROM owner_settings').fetchall()
 rows={name:db.execute('SELECT * FROM '+name+' ORDER BY 1').fetchall() for name in ('owner_settings','delivery_preferences','mutes','legacy_import')}
 body=json.dumps(rows,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
 print(json.dumps({'owner':owner,'private_sha256':hashlib.sha256(body).hexdigest(),'schema':db.execute('PRAGMA user_version').fetchone()[0]}))
"""
CHECKPOINT = """
import sqlite3
from contextlib import closing
from pathlib import Path
p=Path('/data/sqlite/cryptowatcher.sqlite3')
with closing(sqlite3.connect(p,timeout=2)) as db:
 db.execute('PRAGMA busy_timeout=2000')
 assert db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()==(0,0,0)
assert not any(Path(str(p)+s).exists() for s in ('-wal','-shm','-journal'))
print('{"checkpoint":"PASS"}')
"""
RUNTIME_READBACK = """
import json,os,sqlite3
from contextlib import closing
from pathlib import Path
p=Path(os.environ['DATABASE_FILE'])
with closing(sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
 db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
 def read(key):
  row=db.execute('SELECT payload_json FROM runtime WHERE key=?',(key,)).fetchone()
  return json.loads(row[0]) if row else {}
 g=read('ingestion');t=read('telegram_delivery')
 print(json.dumps({'ingestion_at':g.get('at',0),'timeframes':g.get('timeframes',[]),
 'subscriptions':{k:{p:v.get(p) for p in ('state','requested','acknowledged','last_received_ms')} for k,v in g.get('subscriptions',{}).items()},
 'telegram_state':t.get('state'),'telegram_at':t.get('at')}))
"""


def call(args: list[str], *, seconds: float = 15, data: str | None = None) -> str:
    result = subprocess.run(args, input=data, text=True, capture_output=True, timeout=seconds)
    if result.returncode:
        # Arbitrary stderr may contain resolved application configuration.
        raise RuntimeError("Command failed with exit " + str(result.returncode))
    return result.stdout


def inspect(identifier: str, *, seconds: float = 15) -> dict[str, Any]:
    return json.loads(call([*DOCKER, "inspect", identifier], seconds=seconds))[0]


def exact_unit(expected: Unit, *, seconds: float = 15) -> dict[str, Any]:
    value = inspect("crypto-watcher", seconds=seconds)
    labels = value["Config"].get("Labels") or {}
    if (
        value["Id"] != expected.container
        or value["Image"] != expected.image
        or labels.get("org.opencontainers.image.revision") != expected.revision
        or labels.get("com.docker.compose.project") != "crypto_watcher"
        or labels.get("com.docker.compose.service") != "crypto_watcher"
    ):
        raise RuntimeError("Reviewed unit identity changed")
    return value


def private_readback(expected: Unit, *, seconds: float = 15) -> dict[str, Any]:
    return json.loads(
        call(
            [*DOCKER, "exec", "-i", expected.container, "python", "-B", "-"],
            seconds=seconds,
            data=PRIVATE_READBACK,
        )
    )


def private_directory(path: Path, *, uid: int) -> None:
    if path.resolve() != path or any(p.is_symlink() for p in (path, *path.parents)):
        raise RuntimeError("Noncanonical operation directory")
    root = APP / "rehearsals"
    if root not in path.parents:
        raise RuntimeError("Operation directory outside rehearsals")
    meta = path.stat()
    if not path.is_dir() or meta.st_uid != uid or meta.st_mode & 0o077:
        raise RuntimeError("Private operation directory required")


class DockerOperations:
    def __init__(
        self, root: Path, code: Path, code_sha256: str, worker: str, *, one_time_maintenance: bool = False
    ) -> None:
        self.root, self.code, self.worker = root, code, worker
        self.work = root / "work"
        self.code_sha256 = code_sha256
        self.one_time_maintenance = one_time_maintenance
        self.restored_at = 0.0

    def record(self, value: dict[str, Any]) -> None:
        destination = self.root / "operation.json"
        temporary = self.root / "operation.json.tmp"
        with temporary.open("w", encoding="utf-8") as output:
            os.chmod(temporary, 0o600)
            output.write(encode(value))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)

    def verify(self, expected: Unit) -> dict[str, Any]:
        baseline = self.verify_binding(expected)
        if shutil.disk_usage(self.work).free < int(12.5 * 1024**3):
            raise RuntimeError("Insufficient disk admission before stop")
        memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        if int(memory["MemAvailable"].split()[0]) * 1024 < 5 * 1024**3:
            raise RuntimeError("Insufficient RAM reserve before stop")
        return baseline

    def verify_binding(self, expected: Unit) -> dict[str, Any]:
        """Shared target/code/private-state identity; operation resource gates stay separate."""
        if os.geteuid() != 0 or socket.gethostname() != "vmi3224957":
            raise RuntimeError("Wrong host/account")
        value = exact_unit(expected)
        if not value["State"]["Running"]:
            raise RuntimeError("Production must be running before maintenance")
        if value.get("ExecIDs"):
            raise RuntimeError("Inspect concurrent production execs first")
        private_directory(self.root, uid=0)
        private_directory(self.code, uid=10001)
        private_directory(self.work, uid=10001)
        driver = self.code / "driver.py"
        if driver.is_symlink() or hashlib.sha256(driver.read_bytes()).hexdigest() != self.code_sha256:
            raise RuntimeError("Reviewed worker code changed")
        verify_code_bundle(self.root, self.code, self.code_sha256)
        if any(self.work.iterdir()):
            raise RuntimeError("Fresh work directory required")
        meta = DATABASE.stat()
        if DATABASE.resolve() != DATABASE or meta.st_ino != 1930781 or meta.st_uid != 10001:
            raise RuntimeError("Reviewed database identity changed")
        baseline = private_readback(expected)
        if baseline.get("owner") != [[233382575, 3]] or baseline.get("schema") != 1:
            raise RuntimeError("Reviewed owner/schema changed")
        return baseline

    def stop(self, expected: Unit, seconds: float) -> None:
        exact_unit(expected)
        call([*DOCKER, "stop", "--time", "30", expected.container], seconds=seconds)
        if exact_unit(expected)["State"]["Running"]:
            raise RuntimeError("Production writer still running")

    def checkpoint(self, expected: Unit, seconds: float) -> None:
        if exact_unit(expected)["State"]["Running"]:
            raise RuntimeError("Live checkpoint operation refused")
        name = self.worker + "-checkpoint"
        try:
            call(
                [
                    *DOCKER,
                    "run",
                    "--name",
                    name,
                    "--label",
                    "cryptowatcher.maintenance=" + self.worker,
                    "--pull",
                    "never",
                    "--network",
                    "none",
                    "--read-only",
                    "--no-healthcheck",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--user",
                    "10001:10001",
                    "--memory",
                    "128m",
                    "--memory-swap",
                    "128m",
                    "--cpus",
                    "1",
                    "--pids-limit",
                    "32",
                    "--mount",
                    "type=bind,src=" + str(DATABASE.parent) + ",dst=/data/sqlite",
                    "--entrypoint",
                    "python",
                    "-i",
                    expected.image,
                    "-B",
                    "-",
                ],
                seconds=seconds,
                data=CHECKPOINT,
            )
        finally:
            self.remove_owned(name, expected.image)
        if any(Path(str(DATABASE) + s).exists() for s in ("-wal", "-shm", "-journal")):
            raise RuntimeError("Closed source sidecars remain")

    def qualify(self, expected: Unit, seconds: float) -> dict[str, Any]:
        return self.run_worker(expected, seconds)

    def run_worker(
        self,
        expected: Unit,
        seconds: float,
        *,
        result_name: str = "qualification.json",
        mount_input: bool = False,
    ) -> dict[str, Any]:
        if result_name not in ("qualification.json", "capture.json"):
            raise RuntimeError("Unknown worker result type")
        if mount_input:
            private_directory(self.root / "input", uid=10001)
        if exact_unit(expected)["State"]["Running"]:
            raise RuntimeError("Closed-source writer reappeared")
        command = [
            *DOCKER,
            "run",
            "--name",
            self.worker,
            "--label",
            "cryptowatcher.maintenance=" + self.worker,
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--no-healthcheck",
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
            "type=bind,src=" + str(DATABASE.parent) + ",dst=/history,readonly",
            "--mount",
            "type=bind,src=" + str(self.code) + ",dst=/code,readonly",
            "--mount",
            "type=bind,src=" + str(self.work) + ",dst=/scratch",
            "--entrypoint",
            "python",
            expected.image,
            "-B",
            "/code/driver.py",
            str(seconds),
            *(["--one-time-maintenance"] if self.one_time_maintenance else []),
        ]
        if mount_input:
            # This is the only extra mount: a private, read-only capture input.
            index = command.index("--entrypoint")
            command[index:index] = [
                "--mount",
                "type=bind,src=" + str(self.root / "input") + ",dst=/input,readonly",
            ]
        # Persist bounded worker diagnostics before cleanup removes its container.
        # This private worker has no network, env file or production secrets.
        with (self.root / "worker.log").open("x", encoding="utf-8") as output:
            os.chmod(self.root / "worker.log", 0o600)
            subprocess.run(
                command, text=True, stdout=output, stderr=subprocess.STDOUT, timeout=seconds, check=True
            )
        receipt = self.work / result_name
        if receipt.is_symlink() or not receipt.is_file():
            raise RuntimeError(
                "Primary qualification receipt missing"
                if result_name == "qualification.json"
                else "Primary capture receipt missing"
            )
        return json.loads(receipt.read_text(encoding="utf-8"))

    def remove_owned(self, name: str, image: str | None = None) -> None:
        result = subprocess.run([*DOCKER, "inspect", name], capture_output=True, text=True, timeout=5)
        if result.returncode:
            return
        value = json.loads(result.stdout)[0]
        if (value["Config"].get("Labels") or {}).get("cryptowatcher.maintenance") != self.worker:
            raise RuntimeError("Worker ownership changed")
        if image is not None and value["Image"] != image:
            raise RuntimeError("Worker image changed")
        call([*DOCKER, "rm", "-f", value["Id"]], seconds=10)

    def terminate_worker(self) -> None:
        self.remove_owned(self.worker)
        self.remove_owned(self.worker + "-checkpoint")

    def restore(self, expected: Unit, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        exact_unit(expected, seconds=remaining_seconds(deadline))
        call([*DOCKER, "start", expected.container], seconds=remaining_seconds(deadline, cap=60))
        self.restored_at = time.time()

    def postcheck(self, expected: Unit, baseline: dict[str, Any], seconds: float) -> dict[str, Any]:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = exact_unit(expected, seconds=remaining_seconds(deadline))
            if value["State"]["Running"] and value["State"].get("Health", {}).get("Status") == "healthy":
                current = private_readback(expected, seconds=remaining_seconds(deadline))
                if current != baseline:
                    raise RuntimeError("Private state changed during maintenance")
                state = json.loads(
                    call(
                        [*DOCKER, "exec", "-i", expected.container, "python", "-B", "-"],
                        seconds=remaining_seconds(deadline, cap=10),
                        data=RUNTIME_READBACK,
                    )
                )
                if restored_data(state, self.restored_at, time.time()):
                    return {
                        "state": "PASS",
                        "health": "PASS",
                        "private_state": "PASS",
                        "ingestion": "FRESH",
                        "subscriptions": state["subscriptions"],
                        "delivery_runtime": "UNCHANGED",
                        "new_message_send": "NOT_FORCED",
                    }
            time.sleep(min(1, remaining_seconds(deadline)))
        raise RuntimeError("Restoration health timeout")


def remaining_seconds(deadline: float, *, cap: float = 15) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("Restoration time budget exhausted")
    return min(cap, remaining)


def watchdog_restore(operations: DockerOperations, expected: Unit) -> None:
    """Record recovery acknowledgement separately; never upgrade qualification."""
    private_directory(operations.root, uid=0)
    report: dict[str, Any] = {
        "state": "RECOVERY_ATTEMPT",
        "container": expected.container,
        "image": expected.image,
        "source": expected.revision,
        "service_result": os.environ.get("SERVICE_RESULT", "UNSET"),
        "qualification": "NOT_CONFIRMED_BY_RECOVERY",
        "health": "PENDING",
    }
    try:
        try:
            operations.terminate_worker()
            report["worker_cleanup"] = "PASS"
        finally:
            operations.restore(expected, 30)
            report["start_acknowledged"] = True
        report["state"] = "RECOVERY_START_ACKNOWLEDGED"
    except Exception as error:
        report["failure_type"] = type(error).__name__
        raise
    finally:
        path = operations.root / ("watchdog-recovery-" + uuid.uuid4().hex + ".json")
        with path.open("x", encoding="utf-8") as output:
            os.chmod(path, 0o600)
            output.write(encode(report))
            output.flush()
            os.fsync(output.fileno())


def restored_data(value: dict[str, Any], restored_at: float, now: float) -> bool:
    at = value.get("ingestion_at", 0)
    subscriptions = value.get("subscriptions", {})
    return (
        isinstance(at, (int, float))
        and restored_at <= at <= now
        and now - at <= 30
        and set(value.get("timeframes", [])) == {"1m", "3m", "5m", "15m"}
        and isinstance(subscriptions, dict)
        and {name.split(":", 1)[0] for name in subscriptions} == {"spot", "linear"}
        and all(
            isinstance(s, dict)
            and s.get("state") == "CONNECTED"
            and type(s.get("requested")) is int
            and s["requested"] > 0
            and s.get("acknowledged") == s["requested"]
            and isinstance(s.get("last_received_ms"), (int, float))
            and s["last_received_ms"] >= restored_at * 1000
            for s in subscriptions.values()
        )
        and value.get("telegram_state") not in {"FAILED", "ERROR", "BLOCKED"}
    )


def verify_code_bundle(root: Path, code: Path, driver_sha256: str) -> None:
    manifest = root / "code-manifest.json"
    meta = manifest.lstat()
    if manifest.is_symlink() or meta.st_uid != 0 or meta.st_mode & 0o077:
        raise RuntimeError("Root-owned private code manifest required")
    expected = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(expected, dict) or expected.get("driver.py") != driver_sha256:
        raise RuntimeError("Reviewed driver binding missing")
    actual = set()
    for path in code.rglob("*"):
        if path.is_symlink():
            raise RuntimeError("Worker bundle symlink refused")
        if path.is_file():
            name = path.relative_to(code).as_posix()
            actual.add(name)
            if path.suffix != ".py" or expected.get(name) != hashlib.sha256(path.read_bytes()).hexdigest():
                raise RuntimeError("Worker module binding mismatch")
    if actual != set(expected):
        raise RuntimeError("Worker module set mismatch")


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
    parser.add_argument("--one-time-maintenance", action="store_true")
    args = parser.parse_args()
    for value in (args.container, args.driver_sha256):
        if re.fullmatch(r"[a-f0-9]{64}", value) is None:
            raise RuntimeError("Reviewed identity required")
    if re.fullmatch(r"cw-closed-qual-[a-f0-9]{32}", args.worker) is None:
        raise RuntimeError("Owned worker identity required")
    if (
        re.fullmatch(r"sha256:[a-f0-9]{64}", args.image) is None
        or re.fullmatch(r"[a-f0-9]{40}", args.revision) is None
    ):
        raise RuntimeError("Reviewed source/image identity required")
    expected = Unit(args.container, args.image, args.revision)
    operations = DockerOperations(
        args.root, args.code, args.driver_sha256, args.worker, one_time_maintenance=args.one_time_maintenance
    )
    if args.restore_only:
        if os.geteuid() != 0 or socket.gethostname() != "vmi3224957":
            raise RuntimeError("Wrong recovery host/account")
        watchdog_restore(operations, expected)
        return 0
    if not args.execute:
        operations.verify(expected)
        print('{"state":"PREFLIGHT_PASS","production_changed":false}')
        return 0
    if not os.environ.get("INVOCATION_ID"):
        raise RuntimeError("Managed watchdog invocation required")
    restore_argv = [
        sys.executable,
        "-B",
        str(Path(__file__).resolve()),
        *(value for value in sys.argv[1:] if value != "--execute"),
        "--restore-only",
    ]
    managed_deadline = verify_watchdog(
        args.worker, os.environ["INVOCATION_ID"], restore_argv, one_time_maintenance=args.one_time_maintenance
    )
    import fcntl

    with (APP / ".deploy.lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = qualify_closed(
            operations,
            expected,
            seconds=ONE_TIME_SECONDS if args.one_time_maintenance else 900,
            one_time_maintenance=args.one_time_maintenance,
            absolute_deadline=managed_deadline,
        )
    print(encode(report))
    return 0 if report["state"] == "QUALIFIED_RUNTIME_RESTORED" else 3


def verify_watchdog(
    unit: str, invocation: str, restore_argv: list[str], *, one_time_maintenance: bool = False
) -> float:
    service = "org.freedesktop.systemd1"
    result = json.loads(
        call(
            [
                "busctl",
                "--json=short",
                "call",
                service,
                "/org/freedesktop/systemd1",
                service + ".Manager",
                "GetUnit",
                "s",
                unit + ".service",
            ]
        )
    )
    if result.get("type") != "o" or len(result.get("data", [])) != 1:
        raise RuntimeError("Managed unit path missing")
    path = result["data"][0]

    def prop(interface: str, name: str, kind: str) -> Any:
        value = json.loads(
            call(["busctl", "--json=short", "get-property", service, path, service + "." + interface, name])
        )
        if value.get("type") != kind:
            raise RuntimeError("Watchdog property type mismatch")
        return value.get("data")

    identifier = prop("Unit", "InvocationID", "ay")
    duration = prop("Service", "RuntimeMaxUSec", "t")
    entered = prop("Unit", "ActiveEnterTimestampMonotonic", "t")
    commands = prop("Service", "ExecStopPost", "a(sasbttttuii)")
    if (
        bytes(identifier).hex() != invocation
        or duration != (ONE_TIME_SECONDS if one_time_maintenance else 900) * 1_000_000
        or type(entered) is not int
        or not 0 < entered <= time.monotonic() * 1_000_000
        or len(commands) != 1
        or commands[0][0] != restore_argv[0]
        or commands[0][1] != restore_argv
        or commands[0][2] is not False
    ):
        raise RuntimeError("Reviewed watchdog/recovery binding missing")
    # systemd starts its clock before this interpreter/preflight. Leave ten
    # seconds for final durable reporting rather than racing RuntimeMaxSec.
    return (entered + duration) / 1_000_000 - 10


if __name__ == "__main__":
    sys.exit(main())
