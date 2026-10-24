"""Deliver through the fixed SSH target and persist a safe success or failure record."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

VALIDATION_DETAIL_CODES = frozenset(
    "ARCHIVE_DISK_RESERVE ARCHIVE_READ_INCOMPLETE ARCHIVE_SIZE_LIMIT ARCHIVE_WRITE_INCOMPLETE BYTE_IDENTITY_MISMATCH CLOSED_FILE_REQUIRED COMPRESSED_MEMORY_LIMIT DATABASE_CORRUPT FROZEN_IMAGE_REQUIRED INVALID_ARCHIVE_BOUND INVALID_ARCHIVE_FINALIZATION INVALID_ARCHIVE_HEADER INVALID_ARCHIVE_INDEX INVALID_ARCHIVE_TRUNCATE INVALID_ARCHIVE_WRITE INVALID_PAGE_SIZE INVALID_READ INVALID_TRUNCATE INVALID_WRITE NATIVE_VFS_CONTEXT_CLOSED NATIVE_VFS_HANDLES_OPEN NATIVE_VFS_LOAD_FAILED NATIVE_VFS_REGISTRATION_REFUSED NONSEQUENTIAL_SNAPSHOT_WRITE PINNED_SOURCE_REQUIRED REGULAR_CLOSED_FILE_REQUIRED SOURCE_CHANGED SQLITE_VALIDATION_ERROR TIME_BUDGET_EXCEEDED UNKNOWN_VALIDATED_SCHEMA UNSAFE_VALIDATION_PATH VALIDATED_OWNER_MISMATCH VALIDATION_IO_ERROR".split()
)

VALIDATION_INNER_PHASES = frozenset(
    "PIN_SOURCE SOURCE_COPY SNAPSHOT_SEAL FULL_INTEGRITY SOURCE_METADATA FULL_BYTE_IDENTITY INDEPENDENT_PERSISTED_READBACK ARCHIVE_FINGERPRINT COMPLETE".split()
)


PHASES = {
    "RECEIVE_ARTIFACT",
    "VERIFY_ARTIFACT",
    "LOAD_IMAGE",
    "CHECK_CONFIG",
    "READ_DIAGNOSTICS",
    "CHECK_STATE_INTEGRITY",
    "BACKUP_AND_VALIDATE",
    "BOUNDED_COMPATIBILITY",
    "SWITCH",
    "POST_CHECKS",
    "RESET_STOP_SOURCE",
    "RESET_PRIVATE_AND_FRESH_CANDIDATE",
    "RESET_CANDIDATE_QUALIFIED",
    "RESET_PROMOTE",
    "RESET_PROMOTED",
    "RESET_PRIVATE_POST_CHECK",
    "RESET_DISCARD_OLD_TEST_HISTORY",
    "RESET_COMPLETE",
    "CHECK_STATE_INTEGRITY_DISK",
    "BACKUP_AND_VALIDATE_DISK",
    "UNSET",
}
SUCCESS_FIELDS = {
    "artifact_id",
    "artifact_sha256",
    "artifact_origin",
    "state",
    "source_sha",
    "image_id",
    "build_image_id",
    "image_config_digest",
    "image_manifest_digest",
    "bundle_sha256",
    "build_run",
    "at",
    "previous_image",
    "backup",
    "switch_started",
    "post_checks",
    "data_state",
    "available",
    "requested",
    "phase",
    "phase_at",
    "database_bytes",
    "database_timeout_seconds",
    "candidate_name",
    "delivery_policy",
    "permit_tree",
    "reset_permit_tree",
    "reset_old_working_files_removed",
    "reset_promoted",
    "reset_private_post_check",
}


def parsed_record(value: bytes) -> dict:
    if len(value) > 65536:
        return {}
    try:
        result = json.loads(value)
    except (ValueError, UnicodeError):
        return {}
    return result if isinstance(result, dict) else {}


def delivery_result(
    result, *, revision: str, digest: str, build_run: int, artifact_digest: str | None = None
) -> tuple[dict, int]:
    success = parsed_record(result.stdout)
    proof = success.get("backup_validation")
    proof = proof if isinstance(proof, dict) else {}
    bounded = success.get("bounded_validation", {})
    post = success.get("additive_post_checks", {})
    additive = (
        success.get("delivery_policy") == "OWNER_APPROVED_NEW_WRITES_ONLY_V1"
        and bool(re.fullmatch(r"[0-9a-f]{40}", str(success.get("permit_tree"))))
        and isinstance(bounded, dict)
        and isinstance(post, dict)
        and all(
            p.get("state") == "BOUNDED_COMPATIBILITY_PASS"
            and p.get("fixture_write_restart") == "PASS"
            and p.get("source_readonly") is True
            and p.get("full_history_integrity") == "NOT_RUN_OWNER_EXCEPTION"
            and p.get("full_history_backup") == "FULL_HISTORY_BACKUP_NOT_RUN_OWNER_EXCEPTION"
            and bool(re.fullmatch(r"[0-9a-f]{64}", str(p.get("configuration_sha256"))))
            for p in (bounded, post)
        )
    )
    full_backup = (
        proof.get("phase") == "COMPLETE"
        and proof.get("validation_mode") == "COMPRESSED_RAM"
        and proof.get("archive_encoding") == "ZSTD"
        and type(proof.get("archive_bytes")) is int
        and proof["archive_bytes"] > 0
        and re.fullmatch(r"[0-9a-f]{64}", str(proof.get("archive_sha256")))
        and re.fullmatch(r"[0-9a-f]{64}", str(proof.get("sha256")))
    )
    state_proof = success.get("state_validation", {})
    state_proof = state_proof if isinstance(state_proof, dict) else {}
    disk_backup = (
        proof.get("phase") == "COMPLETE"
        and proof.get("validation_mode") == "DISK_STREAMING"
        and type(proof.get("bytes")) is int
        and proof["bytes"] > 0
        and re.fullmatch("[0-9a-f]{64}", str(proof.get("sha256"))) is not None
        and state_proof.get("status") == "integrity_ok"
        and state_proof.get("schema") == 2
    )
    framed_backup = (
        proof.get("phase") == "COMPLETE"
        and proof.get("validation_mode") == "NATIVE_DISK_FRAMED"
        and proof.get("archive_encoding") == "ZSTD"
        and proof.get("integrity_basis") == "FULL_NATIVE_DISK_CHECK_AND_INDEPENDENT_EQUAL_RAW_SHA256"
        and proof.get("source_validation") == state_proof
        and state_proof.get("status") == "integrity_ok"
        and type(state_proof.get("schema")) is int
        and state_proof["schema"] == 2
        and state_proof.get("validation_mode") == "NATIVE_DISK_FRAMED"
        and type(state_proof.get("owner_id")) is int
        and state_proof["owner_id"] > 0
        and type(state_proof.get("configuration_revision")) is int
        and state_proof["configuration_revision"] > 0
        and proof.get("sha256") == state_proof.get("snapshot_sha256")
        and all(
            re.fullmatch(r"[0-9a-f]{64}", str(value))
            for value in (
                proof.get("sha256"),
                proof.get("archive_sha256"),
                state_proof.get("settings_sha256"),
                state_proof.get("legacy_sha256"),
            )
        )
        and all(
            type(proof.get(key)) is int and proof[key] > 0
            for key in (
                "bytes",
                "archive_bytes",
                "archive_limit_bytes",
                "disk_reserve_bytes",
                "sqlite_cache_kib",
            )
        )
        and proof["archive_bytes"] <= proof["archive_limit_bytes"]
        and proof["disk_reserve_bytes"] >= 6 * 1024**3
        and proof["sqlite_cache_kib"] == 128 * 1024
        and proof.get("raw_snapshot_removed") is True
        and type(proof.get("temporary_raw_bytes")) is int
        and proof["temporary_raw_bytes"] == proof["bytes"]
        and type(proof.get("mmap_limit_bytes")) is int
        and proof["mmap_limit_bytes"] == 3 * 1024**3
        and type(proof.get("concurrent_disk_headroom_bytes")) is int
        and proof["concurrent_disk_headroom_bytes"] >= 1024**3
    )
    reset_proof = success.get("reset_proof", {})
    reset = (
        success.get("delivery_policy") == "OWNER_APPROVED_TEST_HISTORY_RESET_20261016_V1"
        and isinstance(reset_proof, dict)
        and reset_proof.get("state") == "PASS"
        and reset_proof.get("integrity") == "PASS"
        and reset_proof.get("private_restore") == "PASS"
        and reset_proof.get("backups_and_experiments") == "NOT_TOUCHED"
        and reset_proof.get("owner_id") == 233382575
        and isinstance(reset_proof.get("generation"), dict)
        and bool(reset_proof["generation"].get("id"))
        and isinstance(success.get("reset_private_post_check"), dict)
        and success["reset_private_post_check"].get("state") == "PASS"
        and all(
            re.fullmatch("[0-9a-f]{64}", str(reset_proof.get(key)))
            for key in ("private_source_sha256", "private_fresh_sha256", "private_recovery_sha256")
        )
        and success.get("reset_promoted") is True
        and type(success.get("reset_old_working_files_removed")) is int
        and 1 <= success["reset_old_working_files_removed"] <= 4
        and re.fullmatch("[0-9a-f]{40}", str(success.get("reset_permit_tree"))) is not None
    )
    if (
        result.returncode == 0
        and (
            artifact_digest is None
            or (
                success.get("artifact_sha256") == artifact_digest
                and success.get("artifact_origin") == "GITHUB_ACTIONS_RELEASE_BUILD_V1"
                and type(success.get("artifact_id")) is int
                and success["artifact_id"] > 0
            )
        )
        and success.get("state") == "PASS"
        and success.get("source_sha") == revision
        and success.get("bundle_sha256") == digest
        and success.get("build_run") == build_run
        and success.get("post_checks") == "source/image/health/persistence/market PASS"
        and success.get("phase") == "COMPLETE"
        and success.get("image_id")
        in (success.get("image_config_digest"), success.get("image_manifest_digest"))
        and re.fullmatch(r"sha256:[0-9a-f]{64}", str(success.get("image_id")))
        and (full_backup or additive or disk_backup or framed_backup or reset)
    ):
        record = {key: value for key, value in success.items() if key in SUCCESS_FIELDS}
        if reset:
            record["reset_proof"] = {
                key: reset_proof[key]
                for key in (
                    "schema",
                    "state",
                    "private_source_sha256",
                    "private_fresh_sha256",
                    "private_recovery_sha256",
                    "private_restore",
                    "integrity",
                    "owner_id",
                    "configuration_revision",
                    "generation",
                    "backups_and_experiments",
                    "candidate_bytes",
                )
                if key in reset_proof
            }
            return record, 0
        if disk_backup:
            record["state_validation"] = {
                k: v
                for k, v in success["state_validation"].items()
                if k
                in (
                    "status",
                    "schema",
                    "owner_id",
                    "configuration_revision",
                    "settings_sha256",
                    "legacy_sha256",
                    "validation_mode",
                )
            }
            record["backup_validation"] = {
                k: v
                for k, v in proof.items()
                if k in ("phase", "validation_mode", "bytes", "sha256", "elapsed_seconds")
            }
            return record, 0
        if additive:
            fields = {
                "state",
                "full_history_integrity",
                "full_history_backup",
                "configuration_sha256",
                "configuration_bytes",
                "configuration_revision",
                "sampled_public_records",
                "fixture_write_restart",
                "recent_compact_events",
                "new_write_status",
                "source_readonly",
            }
            record["bounded_validation"] = {k: v for k, v in bounded.items() if k in fields}
            record["additive_post_checks"] = {k: v for k, v in post.items() if k in fields}
            return record, 0
        record["backup_validation"] = {
            "phase": "COMPLETE",
            "validation_mode": proof["validation_mode"],
            "sha256": proof["sha256"],
            "archive_encoding": "ZSTD",
            "archive_bytes": proof["archive_bytes"],
            "archive_sha256": proof["archive_sha256"],
        }
        if framed_backup:
            record["state_validation"] = {
                key: value
                for key, value in state_proof.items()
                if key
                in (
                    "status",
                    "schema",
                    "owner_id",
                    "configuration_revision",
                    "settings_sha256",
                    "legacy_sha256",
                    "validation_mode",
                    "snapshot_sha256",
                )
            }
            record["backup_validation"].update(
                {
                    key: proof[key]
                    for key in (
                        "integrity_basis",
                        "archive_limit_bytes",
                        "disk_reserve_bytes",
                        "sqlite_cache_kib",
                        "mmap_limit_bytes",
                        "temporary_raw_bytes",
                        "raw_snapshot_removed",
                        "concurrent_disk_headroom_bytes",
                    )
                }
            )
            timings = proof.get("phase_seconds")
            if isinstance(timings, dict):
                record["backup_validation"]["phase_seconds"] = {
                    key: value
                    for key, value in timings.items()
                    if key in VALIDATION_INNER_PHASES and type(value) in (int, float) and 0 <= value <= 900
                }
        for key in ("bytes", "compressed_bytes"):
            if type(proof.get(key)) is int and proof[key] > 0:
                record["backup_validation"][key] = proof[key]
        for key in ("sqlite_version", "apsw_version"):
            if re.fullmatch(r"[0-9.]{1,32}", str(proof.get(key))):
                record["backup_validation"][key] = proof[key]
        for name in ("state_validation_resources", "backup_validation_resources"):
            resource = success.get(name)
            if isinstance(resource, dict):
                record[name] = {
                    key: value
                    for key, value in resource.items()
                    if key
                    in {
                        "snapshot_bytes",
                        "scratch_bytes",
                        "memory_limit_bytes",
                        "host_available_bytes",
                        "host_reserve_bytes",
                    }
                    and type(value) is int
                    and value > 0
                }
        return record, 0
    failure = parsed_record(result.stderr)
    if failure.get("state") != "BLOCKED":
        failure = {}
    phase = failure.get("failure_phase", "UNSET")
    phase = phase if isinstance(phase, str) and phase in PHASES else "UNSET"
    remote_exit = re.fullmatch(r"Operation failed: docker exit ([0-9]{1,3})", str(failure.get("reason")))
    if failure.get("reason") in (
        "Operation time budget exceeded",
        "Deployment900s budget exhausted",
        "Deployment wall-clock budget exhausted",
        "Insufficient full backup budget",
        "Insufficient disk backup budget",
    ):
        code = "TIME_BUDGET_EXCEEDED"
    elif remote_exit:
        code = {
            "3": "TIME_BUDGET_EXCEEDED",
            "4": "DATABASE_CORRUPT",
            "5": "INTERRUPTED",
            "6": "IO_ERROR",
            "7": "DATABASE_ERROR",
        }.get(remote_exit[1], "REMOTE_OPERATION_FAILED")
    else:
        code = "DELIVERY_UNCONFIRMED"
    detail = failure.get("failure_detail_code")
    safe_detail = (
        {"failure_detail_code": detail}
        if isinstance(detail, str)
        and detail in VALIDATION_DETAIL_CODES
        and remote_exit is not None
        and code == (detail if detail in ("TIME_BUDGET_EXCEEDED", "DATABASE_CORRUPT") else "DATABASE_ERROR")
        else {}
    )
    inner_phase = failure.get("failure_validation_phase")
    safe_stage = (
        {"failure_validation_phase": inner_phase}
        if isinstance(inner_phase, str)
        and inner_phase in VALIDATION_INNER_PHASES
        and code in ("TIME_BUDGET_EXCEEDED", "DATABASE_CORRUPT", "INTERRUPTED", "IO_ERROR", "DATABASE_ERROR")
        else {}
    )
    return {
        "state": "BLOCKED",
        "attempted_source_sha": revision,
        "bundle_sha256": digest,
        "build_run": build_run,
        "failure_phase": phase,
        "failure_code": code,
        **safe_detail,
        **safe_stage,
        "ssh_exit_status": result.returncode,
        "runtime_state": "UNCONFIRMED",
        "at_utc": datetime.now(timezone.utc).isoformat(),
    }, 1


def deliver(release: Path, ssh_directory: Path, revision: str, build_run: int) -> int:
    output = release / "deployment.json"
    identity = {
        "schema": "cryptowatcher.delivery-record.v1",
        "attempted_source_sha": revision if re.fullmatch(r"[0-9a-f]{40}", revision) else "UNSET",
        "build_run": build_run if type(build_run) is int and build_run > 0 else None,
        "target": "production / Contabo 167.86.68.98 / crypto_watcher",
        "at_utc": datetime.now(timezone.utc).isoformat(),
    }
    # Preserve earlier records: a second attempt requires fresh artifact identity and review.
    with output.open("x", encoding="utf-8") as record:
        record.write(json.dumps({**identity, "state": "UNCONFIRMED", "runtime_state": "UNCONFIRMED"}) + "\n")
        record.flush()
        try:
            if not re.fullmatch(r"[0-9a-f]{40}", revision) or type(build_run) is not int or build_run <= 0:
                raise ValueError("Invalid identity")
            bundle = release / "bundle.tar"
            with bundle.open("rb") as content:
                digest = hashlib.file_digest(content, "sha256").hexdigest()
            if (release / "bundle.sha256").read_text().strip() != digest:
                raise ValueError("Invalid bundle")
            manifest = json.loads((release / "manifest.json").read_text())
            if manifest.get("source_sha") != revision or manifest.get("build_run") != build_run:
                raise ValueError("Invalid build identity")
            artifact = release / "artifact.zip"
            with artifact.open("rb") as content:
                artifact_digest = hashlib.file_digest(content, "sha256").hexdigest()
            command = [
                "ssh",
                "-T",
                "-i",
                str(ssh_directory / "key"),
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "UserKnownHostsFile=" + str(ssh_directory / "known_hosts"),
                "-o",
                "ConnectTimeout=15",
                "-o",
                "ServerAliveInterval=15",
                "-o",
                "ServerAliveCountMax=4",
                "root@167.86.68.98",
                f"cryptowatcher-deploy {revision} {artifact_digest} {artifact.stat().st_size}",
            ]
            deadline = os.environ.get("CW_DELIVERY_DEADLINE")
            if deadline is not None:
                if (
                    re.fullmatch("[1-9][0-9]{9}", deadline) is None
                    or not 30 < int(deadline) - time.time() <= 900
                ):
                    raise ValueError("Invalid delivery deadline")
                command[-1] += " " + deadline
            with artifact.open("rb") as stream:
                result = subprocess.run(
                    command,
                    stdin=stream,
                    capture_output=True,
                    check=False,
                    **({"timeout": max(1, int(deadline) - time.time() + 10)} if deadline else {}),
                )
            payload, exit_status = delivery_result(
                result,
                revision=revision,
                digest=digest,
                build_run=build_run,
                artifact_digest=artifact_digest,
            )
        except Exception:
            # Neither local exception text nor raw SSH stderr belongs in an artifact or log.
            payload = {"state": "BLOCKED", "failure_code": "DELIVERY_PREPARATION_UNCONFIRMED"}
            exit_status = 1
        payload = {**identity, **payload}
        record.seek(0)
        record.write(json.dumps(payload, sort_keys=True) + "\n")
        record.truncate()
        record.flush()
        print(json.dumps(payload, sort_keys=True))
    return exit_status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("revision")
    parser.add_argument("build_run", type=int)
    parser.add_argument("--release", type=Path, default=Path("release"))
    parser.add_argument("--ssh-directory", type=Path, required=True)
    args = parser.parse_args()
    return deliver(args.release, args.ssh_directory, args.revision, args.build_run)


if __name__ == "__main__":
    raise SystemExit(main())
