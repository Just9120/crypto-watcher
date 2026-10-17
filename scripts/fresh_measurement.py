"""Explicit owner-approved fresh accumulation, retaining every previous database file.

Run the reviewed script on the identified host only after ordinary successful CD.
It does not re-arm the consumed reset permit or install deployment policy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

APP = Path("/opt/crypto_watcher")
DOCKER = ["docker", "--host", "unix:///var/run/docker.sock"]
SUFFIXES = ("-wal", "-shm", "-journal", "")


def require(value: Any, message: str) -> None:
    if not value:
        raise RuntimeError(message)


def fingerprint(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def identity(path: Path) -> list[int]:
    value = path.stat()
    return [value.st_dev, value.st_ino, value.st_size]


def sync_directory(path: Path) -> None:
    handle = os.open(path, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def require_no_handles(source: Path) -> None:
    expected = identity(source)[:2]
    for process in Path("/proc").glob("[0-9]*/fd"):
        try:
            descriptors = list(process.iterdir())
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        for fd in descriptors:
            try:
                value = fd.stat()
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            require([value.st_dev, value.st_ino] != expected, "Working source still has open handles")


def preserve_original(source: Path, directory: Path) -> list[dict[str, Any]]:
    """Preflight every name before moving anything; no original file is deleted."""
    require(source.name == "cryptowatcher.sqlite3", "Unexpected working database name")
    require(source.resolve() == source and directory.resolve() == directory, "Unsafe archive path")
    require(directory.is_dir() and not directory.is_symlink(), "Archive directory missing")
    targets = []
    for suffix in SUFFIXES:
        path = Path(str(source) + suffix)
        destination = directory / path.name
        require(not path.is_symlink() and not destination.is_symlink(), "Symlink data file")
        require(not destination.exists(), "Original archive target already exists")
        if path.exists():
            require(path.is_file(), "Non-file data source")
            targets.append((path, destination, identity(path), fingerprint(path)))
    require(any(path == source for path, *_ in targets), "Working source missing")
    result = []
    for path, destination, expected, digest in targets:
        require(identity(path) == expected and fingerprint(path) == digest, "Stopped source changed")
        path.rename(destination)
        require(identity(destination) == expected and fingerprint(destination) == digest, "Archive mismatch")
        result.append({"path": str(destination), "identity": expected, "sha256": digest})
    sync_directory(source.parent)
    sync_directory(directory)
    return result


def original_archive_proof(source: Path, validation: dict[str, Any]) -> dict[str, Any]:
    """Qualify the stopped original as its own archive, without allocating another copy."""
    require(
        validation.get("status") == "integrity_ok" and validation.get("schema") == 2,
        "Original archive full integrity failed",
    )
    files = []
    for suffix in SUFFIXES:
        path = Path(str(source) + suffix)
        require(not path.is_symlink(), "Symlink original archive")
        if path.exists():
            files.append({"path": str(path), "sha256": fingerprint(path), "bytes": path.stat().st_size})
    main = next((file for file in files if file["path"] == str(source)), None)
    require(main is not None, "Qualified original database missing")
    assert main is not None
    return {
        "phase": "COMPLETE",
        "method": "RETAIN_ORIGINAL",
        "sha256": main["sha256"],
        "bytes": main["bytes"],
        "files": files,
        "full_integrity": validation,
    }


def execute(
    expected_source: str, expected_generation: str, *, approved: bool, archive_original: bool = False
) -> dict[str, Any]:
    require(approved, "Explicit --approve-fresh-database is required")
    require(os.geteuid() == 0 and socket.gethostname() == "vmi3224957", "Wrong host/account")
    import fcntl

    require(re.fullmatch("[0-9a-f]{40}", expected_source), "Invalid expected source")
    require(re.fullmatch("[0-9a-f]{32}", expected_generation), "Invalid expected generation")
    require(APP.resolve() == APP and not any(p.is_symlink() for p in (APP, *APP.parents)), "Unsafe target")
    source = APP / "data/sqlite/cryptowatcher.sqlite3"
    require(source.is_file() and source.resolve() == source, "Working database missing")
    started = time.monotonic()
    deadline = started + 840

    def run(args: list[str], *, timeout: float = 20, recovery: bool = False) -> str:
        remaining = min(timeout, deadline + (30 if recovery else 0) - time.monotonic())
        require(remaining > 0, "Fresh operation deadline exceeded")
        return subprocess.check_output(args, text=True, timeout=remaining, stderr=subprocess.PIPE)

    lock_path = APP / ".deploy.lock"
    require(lock_path.is_file() and lock_path.resolve() == lock_path, "Unsafe deployment lock")
    with lock_path.open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        c = json.loads(run([*DOCKER, "inspect", "crypto-watcher"]))[0]
        labels = c["Config"]["Labels"]
        require(labels.get("org.opencontainers.image.revision") == expected_source, "Runtime source changed")
        require(labels.get("com.docker.compose.project") == "crypto_watcher", "Wrong project")
        mounts = c.get("Mounts", [])
        require(
            len(mounts) == 1
            and mounts[0].get("Source") == str(APP / "data")
            and mounts[0].get("Destination") == "/data",
            "Unexpected application data mount",
        )
        require(c["State"].get("Health", {}).get("Status") == "healthy", "Runtime not healthy")
        require(
            not run(
                [
                    "systemctl",
                    "list-units",
                    "--state=active,activating",
                    "--no-legend",
                    "--plain",
                    "cryptowatcher-capacity-*",
                ]
            ).strip(),
            "Stop and preserve the identified observer first",
        )
        records = []
        for path in (APP / "releases").glob(expected_source + "-*/deployment.json"):
            require(not path.is_symlink(), "Unsafe delivery record")
            value = json.loads(path.read_text())
            if value.get("state") == "PASS" and value.get("image_id") == c["Image"]:
                records.append(str(path))
        require(len(records) == 1, "No unique successful exact-image delivery")
        require(
            os.statvfs(source.parent).f_bavail * os.statvfs(source.parent).f_frsize
            > (0 if archive_original else 2 * source.stat().st_size) + 1536 * 1024**2,
            "Insufficient archive reserve",
        )
        env = APP / ".env"
        require(
            env.resolve() == env and env.stat().st_uid == 0 and env.stat().st_mode & 0o777 == 0o600,
            "Unsafe runtime config",
        )
        env_hash = fingerprint(env)
        name = (
            "fresh-measurement-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
        )
        folder = source.parent / name
        folder.mkdir(mode=0o700)
        os.chown(folder, 10001, 10001)
        raw_folder = folder / "previous-working"
        raw_folder.mkdir(mode=0o700)
        record: dict[str, Any] = {
            "schema": "cryptowatcher.fresh-measurement.v1",
            "state": "RUNNING",
            "source_revision": expected_source,
            "image_id": c["Image"],
            "container_id": c["Id"],
            "previous_generation": expected_generation,
            "directory": str(folder),
            "delivery_record": records[0],
            "env_sha256": env_hash,
            "scheduled_tasks": False,
            "archive_method": "RETAIN_ORIGINAL" if archive_original else "FULL_COPY",
            "operator_sha256": fingerprint(Path(__file__).resolve()),
        }
        receipt = folder / "operation.json"

        def phase(value: str) -> None:
            record.update(phase=value, phase_at=time.time())
            temporary = receipt.with_suffix(".tmp")
            with temporary.open("w") as output:
                os.fchmod(output.fileno(), 0o600)
                json.dump(record, output, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(receipt)
            sync_directory(folder)

        def offline(database: str, args: list[str], timeout: float) -> str:
            return run(
                [
                    *DOCKER,
                    "run",
                    "--rm",
                    "--pull=never",
                    "--network=none",
                    "--read-only",
                    "--no-healthcheck",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--user=10001:10001",
                    "--memory=4g",
                    "--memory-swap=4g",
                    "--cpus=1",
                    "--pids-limit=64",
                    "--env-file",
                    str(env),
                    "-e",
                    "DATABASE_FILE=" + database,
                    "--mount",
                    f"type=bind,src={APP}/data,dst=/data",
                    "--entrypoint=python",
                    c["Image"],
                    "-B",
                    *args,
                ],
                timeout=timeout,
            )

        relative = "/data/sqlite/" + name
        private_code = "import json,os;from pathlib import Path;from cryptowatcher.storage import Store;from cryptowatcher.reset_history import private_state,signature;s=Store(Path(os.environ['DATABASE_FILE']),233382575,readonly=True);print(json.dumps({'private_sha256':signature(private_state(s)),'generation':s.get_runtime('history_generation')}));s.close()"
        phase("PREFLIGHT_PRIVATE")
        before = json.loads(offline("/data/sqlite/cryptowatcher.sqlite3", ["-c", private_code], 30))
        require(before["generation"]["id"] == expected_generation, "Generation changed")
        record["private_before"] = before["private_sha256"]
        source_identity = identity(source)[:2]
        try:
            phase("STOP_WRITER")
            run([*DOCKER, "stop", "--time=30", c["Id"]], timeout=45)
            require(
                not json.loads(run([*DOCKER, "inspect", c["Id"]]))[0]["State"]["Running"],
                "Writer did not stop",
            )
            require(identity(source)[:2] == source_identity, "Working database identity changed")
            memory = {
                line.split(":", 1)[0]: int(line.split()[1]) * 1024
                for line in Path("/proc/meminfo").read_text().splitlines()
            }
            require(
                memory.get("MemAvailable", 0) >= 4 * 1024**3 + 1536 * 1024**2,
                "Insufficient validation memory reserve",
            )
            phase("FULL_ARCHIVE_INTEGRITY" if archive_original else "FULL_ARCHIVE_BACKUP")
            budget = min(630, int(deadline - time.monotonic() - 180))
            require(budget >= 60, "Insufficient full archive budget")
            if archive_original:
                validation = json.loads(
                    offline(
                        "/data/sqlite/cryptowatcher.sqlite3",
                        ["-m", "cryptowatcher", "state-check", "--timeout-seconds", str(budget - 5)],
                        budget,
                    )
                )
                proof = original_archive_proof(source, validation)
                archive_input = "/data/sqlite/cryptowatcher.sqlite3"
                preserved_archive = raw_folder / source.name
            else:
                backup = json.loads(
                    offline(
                        "/data/sqlite/cryptowatcher.sqlite3",
                        [
                            "-m",
                            "cryptowatcher",
                            "backup",
                            "--destination",
                            relative + "/archive.sqlite3",
                            "--timeout-seconds",
                            str(budget - 5),
                        ],
                        budget,
                    )
                )
                proof = backup["backup"]
                require(
                    backup.get("status") == "backup_integrity_ok" and proof.get("phase") == "COMPLETE",
                    "Archive did not pass full integrity",
                )
                archive_input = relative + "/archive.sqlite3"
                preserved_archive = folder / "archive.sqlite3"
            record["archive_backup"] = proof
            phase("PREPARE_FRESH")
            candidate = json.loads(
                offline(
                    archive_input,
                    [
                        "-m",
                        "cryptowatcher.reset_history",
                        "--source",
                        archive_input,
                        "--candidate",
                        relative + "/fresh.sqlite3",
                        "--private-recovery",
                        relative + "/private.json",
                        "--owner-id",
                        "233382575",
                        "--receipt",
                        relative + "/candidate.json",
                        "--expected-generation",
                        expected_generation,
                        "--preserved-archive-sha256",
                        proof["sha256"],
                    ],
                    135,
                )
            )
            require(candidate["private_source_sha256"] == before["private_sha256"], "Owner state changed")
            require(
                candidate["private_restore"] == candidate["integrity"] == "PASS", "Fresh candidate failed"
            )
            record["candidate"] = candidate
            require_no_handles(source)
            if archive_original:
                for file in proof["files"]:
                    if file["path"].endswith("-shm"):
                        continue  # SQLite readers may advance lock metadata, never database/WAL data.
                    require(fingerprint(Path(file["path"])) == file["sha256"], "Qualified original changed")
            phase("PRESERVE_ORIGINAL")
            record["original_files"] = preserve_original(source, raw_folder)
            phase("PROMOTING")
            (folder / "fresh.sqlite3").rename(source)
            sync_directory(source.parent)
            record["promoted"] = True
            phase("START_FRESH")
            run([*DOCKER, "start", c["Id"]], timeout=45)
            verify_code = (
                "import json;from pathlib import Path;from cryptowatcher.reset_history import verify_private;print(json.dumps(verify_private(Path('/data/sqlite/cryptowatcher.sqlite3'),Path('"
                + relative
                + "/private.json'),233382575)))"
            )
            record["private_readback"] = json.loads(
                offline("/data/sqlite/cryptowatcher.sqlite3", ["-c", verify_code], 30)
            )
            require(record["private_readback"].get("state") == "PASS", "Private readback failed")
            for _ in range(30):
                actual = json.loads(run([*DOCKER, "inspect", c["Id"]]))[0]
                if actual["State"].get("Health", {}).get("Status") == "healthy":
                    break
                time.sleep(3)
            require(actual["State"].get("Health", {}).get("Status") == "healthy", "Fresh runtime not healthy")
            after = json.loads(offline("/data/sqlite/cryptowatcher.sqlite3", ["-c", private_code], 30))
            require(after["generation"] == candidate["generation"], "Fresh generation mismatch")
            require(fingerprint(env) == env_hash, "Infrastructure config changed")
            require(fingerprint(preserved_archive) == proof["sha256"], "Preserved archive changed")
            record["archive_path"] = str(preserved_archive)
            for original in record["original_files"]:
                preserved = Path(original["path"])
                require(
                    identity(preserved) == original["identity"]
                    and fingerprint(preserved) == original["sha256"],
                    "Preserved original changed",
                )
            record.update(
                state="PASS",
                elapsed_seconds=round(time.monotonic() - started, 3),
                container_started_at=actual["State"]["StartedAt"],
                generation=after["generation"],
                all_original_data_retained=True,
                disk_available_bytes=os.statvfs(source.parent).f_bavail * os.statvfs(source.parent).f_frsize,
            )
            phase("COMPLETE")
            os.chown(folder, 0, 0)
            folder.chmod(0o700)
            return record
        except Exception as exc:
            record.update(state="FAILED", failure=type(exc).__name__)
            failed_phase = record["phase"]
            phase("FAILED_" + failed_phase)
            if (
                failed_phase
                in ("STOP_WRITER", "FULL_ARCHIVE_BACKUP", "FULL_ARCHIVE_INTEGRITY", "PREPARE_FRESH")
                and source.is_file()
            ):
                run([*DOCKER, "start", c["Id"]], timeout=30, recovery=True)
            raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-source", required=True)
    parser.add_argument("--expected-generation", required=True)
    parser.add_argument("--approve-fresh-database", action="store_true")
    parser.add_argument("--archive-original", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            execute(
                args.expected_source,
                args.expected_generation,
                approved=args.approve_fresh_database,
                archive_original=args.archive_original,
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
