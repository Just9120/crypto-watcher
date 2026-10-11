"""Deliver through the fixed SSH target and persist a safe success or failure record."""

import argparse
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

PHASES = {
    "RECEIVE_ARTIFACT",
    "VERIFY_ARTIFACT",
    "LOAD_IMAGE",
    "CHECK_CONFIG",
    "READ_DIAGNOSTICS",
    "CHECK_STATE_INTEGRITY",
    "BACKUP_AND_VALIDATE",
    "SWITCH",
    "POST_CHECKS",
    "UNSET",
}
SUCCESS_FIELDS = {
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
}


def parsed_record(value: bytes) -> dict:
    if len(value) > 65536:
        return {}
    try:
        result = json.loads(value)
    except (ValueError, UnicodeError):
        return {}
    return result if isinstance(result, dict) else {}


def delivery_result(result, *, revision: str, digest: str, build_run: int) -> tuple[dict, int]:
    success = parsed_record(result.stdout)
    if (
        result.returncode == 0
        and success.get("state") == "PASS"
        and success.get("source_sha") == revision
        and success.get("bundle_sha256") == digest
        and success.get("build_run") == build_run
        and success.get("post_checks") == "source/image/health/persistence/market PASS"
        and success.get("phase") == "COMPLETE"
        and success.get("image_id")
        in (success.get("image_config_digest"), success.get("image_manifest_digest"))
        and re.fullmatch(r"sha256:[0-9a-f]{64}", str(success.get("image_id")))
    ):
        return {key: value for key, value in success.items() if key in SUCCESS_FIELDS}, 0
    failure = parsed_record(result.stderr)
    if failure.get("state") != "BLOCKED":
        failure = {}
    phase = failure.get("failure_phase", "UNSET")
    phase = phase if isinstance(phase, str) and phase in PHASES else "UNSET"
    remote_exit = re.fullmatch(r"Operation failed: docker exit ([0-9]{1,3})", str(failure.get("reason")))
    code = (
        {
            "3": "TIME_BUDGET_EXCEEDED",
            "4": "DATABASE_CORRUPT",
            "5": "INTERRUPTED",
            "6": "IO_ERROR",
            "7": "DATABASE_ERROR",
        }.get(remote_exit[1], "REMOTE_OPERATION_FAILED")
        if remote_exit
        else "DELIVERY_UNCONFIRMED"
    )
    return {
        "state": "BLOCKED",
        "attempted_source_sha": revision,
        "bundle_sha256": digest,
        "build_run": build_run,
        "failure_phase": phase,
        "failure_code": code,
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
                f"cryptowatcher-deploy {revision} {digest} {bundle.stat().st_size}",
            ]
            with bundle.open("rb") as stream:
                result = subprocess.run(command, stdin=stream, capture_output=True, check=False)
            payload, exit_status = delivery_result(
                result, revision=revision, digest=digest, build_run=build_run
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
