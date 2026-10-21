"""Explicit owner-approved fresh accumulation with an optional test-history discard.

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


def discard_identity(path: Path) -> list[int]:
    meta = path.stat()
    return [meta.st_dev, meta.st_ino, meta.st_size, meta.st_mtime_ns]


def test_discard_plan(source: Path, directory: Path) -> list[dict[str, Any]]:
    """Plan only the stopped working files; never inspect or qualify their contents."""
    require(source.name == "cryptowatcher.sqlite3" and source.resolve() == source, "Unsafe discard source")
    require(directory.resolve() == directory and directory.is_dir(), "Unsafe discard directory")
    files = []
    for suffix in SUFFIXES:
        original, staged = Path(str(source) + suffix), directory / (source.name + suffix)
        require(not original.is_symlink() and not staged.is_symlink(), "Symlink discard file")
        require(not staged.exists(), "Discard staging collision")
        if original.exists():
            require(original.is_file() and original.stat().st_nlink == 1, "Unsafe working file")
            files.append(
                {"source": str(original), "path": str(staged), "identity": discard_identity(original)}
            )
    require(any(item["source"] == str(source) for item in files), "Discard source missing")
    return files


def stage_test_discard(files: list[dict[str, Any]], source: Path, directory: Path) -> None:
    require(files == test_discard_plan(source, directory), "Discard plan changed")
    for item in files:
        require_no_handles(Path(item["source"]))
    for item in files:
        Path(item["source"]).rename(item["path"])
    sync_directory(source.parent)
    sync_directory(directory)


def remove_staged_test_history(files: list[dict[str, Any]], source: Path, directory: Path) -> None:
    require(directory.resolve() == directory and directory.is_dir(), "Unsafe discard directory")
    allowed = {str(directory / (source.name + suffix)) for suffix in SUFFIXES}
    require(
        1 <= len(files) <= 4
        and len({item["path"] for item in files}) == len(files)
        and str(directory / source.name) in {item["path"] for item in files},
        "Invalid discard file set",
    )
    for item in files:
        path = Path(item["path"])
        require(
            item["path"] in allowed
            and item["source"] == str(source) + path.name.removeprefix(source.name)
            and not path.is_symlink()
            and path.is_file()
            and path.stat().st_nlink == 1
            and discard_identity(path) == item["identity"],
            "Staged discard file changed",
        )
        require_no_handles(path)
    for item in files:
        path = Path(item["path"])
        require(
            discard_identity(path) == item["identity"] and not path.is_symlink(), "Discard identity changed"
        )
        path.unlink()
    sync_directory(directory)


def offline_command(
    image: str, database: str, args: list[str], *, reset_helper: Path | None = None
) -> list[str]:
    return [
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
        str(APP / ".env"),
        "-e",
        "OWNER_USER_ID=233382575",
        "-e",
        "DATABASE_FILE=" + database,
        "--mount",
        f"type=bind,src={APP}/data,dst=/data",
        "--entrypoint=python",
        *(
            ["--mount", f"type=bind,src={reset_helper},dst=/operator/reset_history.py,readonly"]
            if reset_helper is not None
            else []
        ),
        image,
        "-B",
        *args,
    ]


def failure_details(exc: Exception) -> dict[str, Any]:
    details: dict[str, Any] = {"failure": type(exc).__name__}
    if isinstance(exc, subprocess.CalledProcessError):
        details["returncode"] = exc.returncode
        try:
            diagnostic = json.loads(exc.stderr or "{}")
        except (ValueError, TypeError):
            diagnostic = {}
        if isinstance(diagnostic, dict):
            code = diagnostic.get("failure_code")
            if code in {
                "TIME_BUDGET_EXCEEDED",
                "DATABASE_CORRUPT",
                "INTERRUPTED",
                "IO_ERROR",
                "DATABASE_ERROR",
            }:
                details["failure_code"] = code
    return details


def execute(
    expected_source: str,
    expected_generation: str,
    *,
    approved: bool,
    archive_original: bool = False,
    discard_test_history: bool = False,
    reset_helper: Path | None = None,
    reset_helper_sha256: str | None = None,
) -> dict[str, Any]:
    require(approved, "Explicit --approve-fresh-database is required")
    require(not (discard_test_history and archive_original), "Conflicting archive/discard policies")
    if discard_test_history:
        require(
            reset_helper is not None and reset_helper_sha256 is not None, "Reviewed reset helper required"
        )
        assert reset_helper is not None and reset_helper_sha256 is not None
        require(
            reset_helper.is_absolute()
            and reset_helper.resolve() == reset_helper
            and not any(p.is_symlink() for p in (reset_helper, *reset_helper.parents))
            and reset_helper.is_file()
            and re.fullmatch("[0-9a-f]{64}", reset_helper_sha256)
            and fingerprint(reset_helper) == reset_helper_sha256,
            "Reset helper identity mismatch",
        )
        require(
            reset_helper.stat().st_uid == 0 and reset_helper.stat().st_mode & 0o777 == 0o644,
            "Unsafe reset helper ownership",
        )
    else:
        require(
            reset_helper is None and reset_helper_sha256 is None, "Helper only allowed for explicit discard"
        )
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
            > (0 if archive_original or discard_test_history else 2 * source.stat().st_size) + 1536 * 1024**2,
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
            "archive_method": "OWNER_APPROVED_TEST_HISTORY_DISCARD"
            if discard_test_history
            else "RETAIN_ORIGINAL"
            if archive_original
            else "FULL_COPY",
            "operator_sha256": fingerprint(Path(__file__).resolve()),
            "reset_helper_sha256": reset_helper_sha256,
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
            return run(offline_command(c["Image"], database, args), timeout=timeout)

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
            phase(
                "DISCARD_AUTHORIZED"
                if discard_test_history
                else "FULL_ARCHIVE_INTEGRITY"
                if archive_original
                else "FULL_ARCHIVE_BACKUP"
            )
            budget = min(630, int(deadline - time.monotonic() - 180))
            require(budget >= 60, "Insufficient full archive budget")
            if discard_test_history:
                proof = {
                    "method": "OWNER_APPROVED_TEST_HISTORY_DISCARD",
                    "old_history_integrity": "NOT_CLAIMED",
                }
                archive_input = "/data/sqlite/cryptowatcher.sqlite3"
            elif archive_original:
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
            candidate_args = [
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
            ]
            if discard_test_history:
                loader = "import importlib.util;spec=importlib.util.spec_from_file_location('cryptowatcher.reset_history','/operator/reset_history.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);raise SystemExit(mod.main())"
                require(
                    reset_helper is not None and fingerprint(reset_helper) == reset_helper_sha256,
                    "Reset helper changed",
                )
                candidate_output = run(
                    offline_command(
                        c["Image"],
                        archive_input,
                        ["-c", loader, *candidate_args, "--discard-test-history"],
                        reset_helper=reset_helper,
                    ),
                    timeout=135,
                )
            else:
                candidate_output = offline(
                    archive_input,
                    [
                        "-m",
                        "cryptowatcher.reset_history",
                        *candidate_args,
                        "--preserved-archive-sha256",
                        proof["sha256"],
                    ],
                    135,
                )
            candidate = json.loads(candidate_output)
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
            if discard_test_history:
                fresh_path = folder / "fresh.sqlite3"
                require(
                    fresh_path.is_file()
                    and not fresh_path.is_symlink()
                    and fresh_path.stat().st_size <= 32 * 1024**2,
                    "Invalid fresh candidate",
                )
                require(
                    all(not Path(str(fresh_path) + suffix).exists() for suffix in SUFFIXES[:-1]),
                    "Fresh candidate not closed",
                )
                record["candidate_sha256"] = fingerprint(fresh_path)
                record["original_files"] = test_discard_plan(source, raw_folder)
                require(
                    all(Path(item["source"]).stat().st_uid == 10001 for item in record["original_files"]),
                    "Unexpected working data owner",
                )
                phase("STAGE_TEST_HISTORY")
                stage_test_discard(record["original_files"], source, raw_folder)
                require(fingerprint(fresh_path) == record["candidate_sha256"], "Fresh candidate changed")
            else:
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
            require(
                actual["Id"] == c["Id"] and actual["Image"] == c["Image"] and actual["State"]["Running"],
                "Fresh runtime identity changed",
            )
            after = json.loads(offline("/data/sqlite/cryptowatcher.sqlite3", ["-c", private_code], 30))
            require(after["generation"] == candidate["generation"], "Fresh generation mismatch")
            require(fingerprint(env) == env_hash, "Infrastructure config changed")
            if discard_test_history:
                phase("REMOVE_STAGED_TEST_HISTORY")
                remove_staged_test_history(record["original_files"], source, raw_folder)
                record["old_working_files_removed"] = len(record["original_files"])
            else:
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
                all_original_data_retained=not discard_test_history,
                disk_available_bytes=os.statvfs(source.parent).f_bavail * os.statvfs(source.parent).f_frsize,
            )
            phase("COMPLETE")
            os.chown(folder, 0, 0)
            folder.chmod(0o700)
            return record
        except Exception as exc:
            record.update(state="FAILED", **failure_details(exc))
            failed_phase = record["phase"]
            phase("FAILED_" + failed_phase)
            if (
                failed_phase
                in (
                    "STOP_WRITER",
                    "DISCARD_AUTHORIZED",
                    "FULL_ARCHIVE_BACKUP",
                    "FULL_ARCHIVE_INTEGRITY",
                    "PREPARE_FRESH",
                )
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
    parser.add_argument("--discard-test-history", action="store_true")
    parser.add_argument("--reset-helper", type=Path)
    parser.add_argument("--reset-helper-sha256")
    args = parser.parse_args()
    print(
        json.dumps(
            execute(
                args.expected_source,
                args.expected_generation,
                approved=args.approve_fresh_database,
                archive_original=args.archive_original,
                discard_test_history=args.discard_test_history,
                reset_helper=args.reset_helper,
                reset_helper_sha256=args.reset_helper_sha256,
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
