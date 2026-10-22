"""Scoped offline adoption on the verified existing unit; no reset or deletion.

Run this reviewed operator after successful exact-image CD. It uses only the
ordinary offline container constraints from fresh_measurement, never its reset
or fresh-database operations. All files, generation and settings are retained.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

if __package__:
    from .fresh_measurement import offline_command
else:
    from fresh_measurement import offline_command

APP = Path("/opt/crypto_watcher")
TARGET = Path("/etc/cryptowatcher/deploy-target.json")
DOCKER = ["docker", "--host", "unix:///var/run/docker.sock"]
OWNER = 233382575
POST_SECONDS = 150
RECOVERY_SECONDS = 60


def require(value: Any, message: str) -> None:
    if not value:
        raise RuntimeError(message)


def root_file(path: Path, mode: int | None = None) -> None:
    require(path.is_file() and path.resolve() == path and not path.is_symlink(), "Unsafe operator file")
    meta = path.stat()
    require(meta.st_uid == 0 and meta.st_mode & 0o022 == 0, "Unsafe operator file permissions")
    if mode is not None:
        require(meta.st_mode & 0o777 == mode, "Wrong private file mode")


def fingerprint(path: Path, deadline: float | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            require(deadline is None or time.monotonic() < deadline, "Fingerprint deadline exceeded")
            block = stream.read(1024 * 1024)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def host_checks() -> None:
    require(sys.platform == "linux" and os.geteuid() == 0, "Reviewed Linux root operator required")
    require(socket.gethostname() == "vmi3224957", "Wrong CryptoWatcher host")


def execute(source: str, generation: str, config: int, private: str, target_sha256: str) -> dict[str, Any]:
    require(
        re.fullmatch("[0-9a-f]{40}", source)
        and re.fullmatch("[0-9a-f]{32}", generation)
        and type(config) is int
        and config > 0
        and re.fullmatch("[0-9a-f]{64}", private)
        and re.fullmatch("[0-9a-f]{64}", target_sha256),
        "Exact source/generation/config/private/target identities required",
    )
    host_checks()
    root_file(TARGET)
    require(fingerprint(TARGET) == target_sha256, "Deployment target changed")
    target = json.loads(TARGET.read_text(encoding="utf-8"))
    require(
        target.get("app_dir") == str(APP)
        and target.get("data_dir") == str(APP / "data")
        and target.get("env_file") == str(APP / ".env")
        and target.get("compose_project") == "crypto_watcher"
        and target.get("service") == "crypto_watcher"
        and target.get("automatic_delivery_enabled") is True,
        "Unknown CryptoWatcher target",
    )
    env = APP / ".env"
    root_file(env, 0o600)
    env_sha = fingerprint(env)
    database = APP / "data/sqlite/cryptowatcher.sqlite3"
    backups = database.parent / "backups"
    migration_path = database.parent / ".g1-migration.json"
    require(
        migration_path.resolve() == migration_path and not migration_path.is_symlink(),
        "Unsafe migration receipt",
    )
    migration = json.loads(migration_path.read_text(encoding="utf-8"))
    require(
        migration.get("schema") == 1
        and migration.get("restore_rehearsal") == "PASS"
        and re.fullmatch("[0-9a-f]{64}", migration.get("legacy_sha256", "")),
        "Missing reviewed migration/recovery receipt",
    )
    require(
        database.is_file()
        and database.resolve() == database
        and database.stat().st_nlink == 1
        and backups.is_dir()
        and backups.resolve() == backups
        and APP.resolve() == APP,
        "Unexpected persistent database or backup directory",
    )
    lock_path = APP / ".deploy.lock"
    root_file(lock_path)
    started = time.monotonic()
    deadline = started + 900

    def run(args: list[str], seconds: float = 20, *, recovery: bool = False) -> str:
        available = deadline - (0 if recovery else RECOVERY_SECONDS) - time.monotonic()
        require(available > 0, "Shared operation900s deadline exceeded")
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=min(seconds, available), check=False
        )
        require(result.returncode == 0, "Operator command failed; inspect retained operation record")
        return result.stdout.strip()

    import fcntl

    with lock_path.open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = json.loads(run([*DOCKER, "inspect", "crypto-watcher"]))[0]
        labels = before["Config"].get("Labels") or {}
        require(
            labels.get("org.opencontainers.image.revision") == source
            and labels.get("com.docker.compose.project") == "crypto_watcher"
            and labels.get("com.docker.compose.service") == "crypto_watcher"
            and before["Name"] == "/crypto-watcher"
            and before["State"]["Running"]
            and not before["State"].get("Restarting"),
            "Running source/unit changed",
        )
        mounts = before.get("Mounts", [])
        require(
            len(mounts) == 1
            and mounts[0].get("Source") == str(APP / "data")
            and mounts[0].get("Destination") == "/data"
            and mounts[0].get("RW") is True,
            "Persistent mount changed",
        )
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
            ),
            "Active capacity observer must be preserved and stopped before adoption",
        )
        records = []
        for path in (APP / "releases").glob(source + "-*/deployment.json"):
            root_file(path)
            record = json.loads(path.read_text(encoding="utf-8"))
            if (
                record.get("state") == "PASS"
                and record.get("source_sha") == source
                and record.get("image_id") == before["Image"]
            ):
                records.append(str(path))
        require(len(records) == 1, "No unique successful exact-image delivery")
        name = "shared-storage-" + uuid.uuid4().hex
        folder = APP / "rehearsals" / name
        folder.mkdir(mode=0o700, parents=False)
        receipt = folder / "operation.json"
        candidate = "cw-verify-" + source[:12] + "-" + uuid.uuid4().hex[:16]
        backup = backups / ("pre-shared-" + source + "-" + uuid.uuid4().hex + ".sqlite3")
        relative_backup = "/data/sqlite/backups/" + backup.name
        image = before["Image"]
        source_identity = (database.stat().st_dev, database.stat().st_ino)
        record = dict(
            state="IN_PROGRESS",
            source_sha=source,
            image_id=image,
            container_id=before["Id"],
            generation=generation,
            configuration_revision=config,
            private_sha256=private,
            target_sha256=target_sha256,
            env_sha256=env_sha,
            delivery_record=records[0],
            backup=str(backup),
            candidate=candidate,
            started_at=time.time(),
            procedure="PRESERVE_CURRENT_GENERATION_SHARED_ADOPTION",
            operator_sha256=fingerprint(Path(__file__).resolve()),
            offline_container_helper_sha256=fingerprint(Path(offline_command.__code__.co_filename).resolve()),
            migration_receipt_sha256=fingerprint(migration_path),
        )

        def phase(value: str) -> None:
            record.update(phase=value, phase_at=time.time())
            temporary = receipt.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as output:
                os.chmod(temporary, 0o600)
                json.dump(record, output, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(receipt)

        def identity(value: dict[str, Any]) -> None:
            require(
                value.get("source_revision") == source
                and (value.get("history_generation") or {}).get("id") == generation
                and value.get("configuration_revision") == config
                and value.get("private_sha256") == private,
                "Source/generation/config/private identity changed",
            )

        def token() -> dict[str, Any]:
            result = json.loads(
                run(
                    [
                        *DOCKER,
                        "exec",
                        before["Id"],
                        "python",
                        "-B",
                        "-m",
                        "cryptowatcher",
                        "shared-storage-status",
                    ]
                )
            )
            identity(result)
            return result

        stopped = False
        candidate_started = False
        try:
            phase("PREFLIGHT")
            preflight = token()
            record["preflight"] = preflight
            if preflight.get("layout") == "3":
                record.update(state="NO_CHANGE", elapsed_seconds=time.monotonic() - started)
                phase("ALREADY_ENABLED")
                return record
            require(preflight.get("layout") in (None, "2"), "Unknown installed shared layout")
            allocated = sum(
                Path(str(database) + suffix).stat().st_size
                for suffix in ("", "-wal")
                if Path(str(database) + suffix).exists()
            )
            disk = os.statvfs(database.parent)
            require(
                disk.f_bavail * disk.f_frsize > allocated + 1536 * 1024**2, "Insufficient full backup reserve"
            )
            require(
                fingerprint(TARGET) == target_sha256 and fingerprint(env) == env_sha, "Target/config changed"
            )
            phase("STOP_WRITER")
            stopped = True  # Stop may succeed even if its client times out.
            run([*DOCKER, "stop", "--time=30", before["Id"]], 45)
            actual = json.loads(run([*DOCKER, "inspect", before["Id"]]))[0]
            require(
                not actual["State"]["Running"] and actual["Image"] == image,
                "Writer did not stop or image changed",
            )
            core = deadline - RECOVERY_SECONDS - POST_SECONDS - time.monotonic()
            require(core > 15, "No adoption budget after post-check/recovery reserve")
            args = offline_command(
                image,
                "/data/sqlite/cryptowatcher.sqlite3",
                [
                    "-m",
                    "cryptowatcher",
                    "enable-shared-storage",
                    "--database",
                    "/data/sqlite/cryptowatcher.sqlite3",
                    "--owner-id",
                    str(OWNER),
                    "--destination",
                    relative_backup,
                    "--expected-source",
                    source,
                    "--expected-generation",
                    generation,
                    "--expected-configuration-revision",
                    str(config),
                    "--expected-private-sha256",
                    private,
                    "--timeout-seconds",
                    str(core - 5),
                ],
            )
            # The shared, unchanged helper contributes only container constraints.
            insert = args.index("--entrypoint=python")
            args[insert:insert] = ["--name", candidate, "-e", "SOURCE_REVISION=" + source]
            phase("OFFLINE_BACKUP_AND_ADOPTION")
            candidate_started = True
            proof = json.loads(run(args, core))
            candidate_started = False
            identity(proof)
            require(
                proof.get("phase") == "COMPLETE" and (proof.get("backup") or {}).get("phase") == "COMPLETE",
                "Adoption incomplete",
            )
            record["adoption"] = proof
            require(
                backup.is_file()
                and fingerprint(backup, deadline - RECOVERY_SECONDS - POST_SECONDS)
                == proof["backup"]["sha256"],
                "Retained backup identity mismatch",
            )
            require(
                fingerprint(TARGET) == target_sha256 and fingerprint(env) == env_sha,
                "Target/config changed during adoption",
            )
            phase("START_SAME_IMAGE")
            run([*DOCKER, "start", before["Id"]], 30)
            stopped = False
            phase("POST_CHECK")
            post_end = min(time.monotonic() + POST_SECONDS, deadline - RECOVERY_SECONDS)
            while True:
                actual = json.loads(run([*DOCKER, "inspect", before["Id"]]))[0]
                require(
                    actual["Image"] == image
                    and actual["Config"]["Labels"].get("org.opencontainers.image.revision") == source,
                    "Post-check image/source changed",
                )
                if actual["State"].get("Health", {}).get("Status") == "healthy":
                    break
                require(time.monotonic() < post_end, "Shared adoption health post-check expired")
                time.sleep(min(3, max(0, post_end - time.monotonic())))
            post = token()
            require(post.get("layout") == "3", "Activated layout not observed after restart")
            require(
                post.get("history_generation") == preflight.get("history_generation"),
                "Generation metadata changed",
            )
            require(
                (database.stat().st_dev, database.stat().st_ino) == source_identity,
                "Working database identity changed",
            )
            require(
                fingerprint(TARGET) == target_sha256 and fingerprint(env) == env_sha,
                "Target/config changed during post-check",
            )
            record["capacity_after"] = json.loads(
                run([*DOCKER, "exec", before["Id"], "python", "-B", "-m", "cryptowatcher", "capacity"])
            )
            record.update(
                state="PASS",
                postflight=post,
                elapsed_seconds=time.monotonic() - started,
                started_at_after=actual["State"]["StartedAt"],
                all_original_data_retained=True,
            )
            phase("COMPLETE")
            return record
        except Exception as exc:
            record.update(state="FAILED", failure_class=type(exc).__name__, failure_phase=record.get("phase"))
            phase("FAILED")
            # Never switch to an old image or restore/delete any database. Stop
            # only this named offline candidate before reusing the writer lease.
            if candidate_started:
                phase("FAILED_CANDIDATE_INSPECTION")
                present = run(
                    [
                        *DOCKER,
                        "ps",
                        "-a",
                        "--no-trunc",
                        "--filter",
                        "name=^/" + candidate + "$",
                        "--format",
                        "{{.ID}}",
                    ],
                    recovery=True,
                )
                if present:
                    require(re.fullmatch("[0-9a-f]{64}", present), "Ambiguous offline recovery identity")
                    try:
                        found = json.loads(run([*DOCKER, "inspect", present], recovery=True))[0]
                    except (RuntimeError, subprocess.TimeoutExpired):
                        # --rm can finish between ps and inspect. Only a new
                        # successful empty inventory proves absence; a daemon
                        # error or still-present candidate remains a blocker.
                        require(
                            not run(
                                [
                                    *DOCKER,
                                    "ps",
                                    "-a",
                                    "--no-trunc",
                                    "--filter",
                                    "name=^/" + candidate + "$",
                                    "--format",
                                    "{{.ID}}",
                                ],
                                recovery=True,
                            ),
                            "Offline candidate inspection outcome unknown",
                        )
                        found = None
                else:
                    found = None
                if found is not None:
                    require(
                        found["Name"] == "/" + candidate
                        and found["Image"] == image
                        and not (found["Config"].get("Labels") or {}).get("com.docker.compose.project"),
                        "Offline recovery identity changed",
                    )
                    run([*DOCKER, "stop", "--time=5", found["Id"]], 10, recovery=True)
                    require(
                        not run(
                            [*DOCKER, "ps", "--filter", "name=^/" + candidate + "$", "--format", "{{.ID}}"],
                            recovery=True,
                        ),
                        "Offline recovery stop not confirmed",
                    )
                record["offline_candidate_stopped_or_absent_confirmed"] = True
                phase("FAILED_CANDIDATE_SETTLED")
            if stopped:
                phase("FAILED_FORWARD_RECOVERY_CHECK")
                actual = json.loads(run([*DOCKER, "inspect", before["Id"]], recovery=True))[0]
                require(
                    actual["Image"] == image and actual["Name"] == before["Name"],
                    "Recovery image/unit changed",
                )
                if not actual["State"]["Running"]:
                    run([*DOCKER, "start", before["Id"]], 20, recovery=True)
                    actual = json.loads(run([*DOCKER, "inspect", before["Id"]], recovery=True))[0]
                    require(
                        actual["State"]["Running"] and actual["Image"] == image,
                        "Forward recovery start not confirmed",
                    )
                    record["same_image_restarted_for_forward_recovery"] = True
                else:
                    record["same_image_observed_running_during_recovery"] = True
                phase("FAILED_FORWARD_RECOVERY")
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-source", required=True)
    parser.add_argument("--expected-generation", required=True)
    parser.add_argument("--expected-configuration-revision", type=int, required=True)
    parser.add_argument("--expected-private-sha256", required=True)
    parser.add_argument("--expected-target-sha256", required=True)
    args = parser.parse_args()
    result = execute(
        args.expected_source,
        args.expected_generation,
        args.expected_configuration_revision,
        args.expected_private_sha256,
        args.expected_target_sha256,
    )
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
