"""Exact-target, operator-only receiver update before a verified hotfix merge."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = "Just9120/crypto-watcher"
APP = Path("/opt/crypto_watcher")
RECEIVER = Path("/usr/local/lib/cryptowatcher/deploy_receiver.py")
TARGET = Path("/etc/cryptowatcher/deploy-target.json")
OLD_HASH = "9569f32c85da70fe4155b6ce60669911593b2fe3cb7b8a28d7adbddd9db57c1f"
TARGET_HASH = "4d69f74f2cd4bbd609b6ac15667d8ff82a12f3cc42db1c2aaf17bf1bda773274"
PERMIT = Path("/etc/cryptowatcher/new-write-permit.json")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def read_url(url: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json", "User-Agent": "CryptoWatcher-CD-update"}
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        content = response.read(1_048_577)
    require(len(content) <= 1_048_576, "Response exceeds limit")
    return content


def gate(base: str, head: str, number: int) -> None:
    prefix = "https://api.github.com/repos/" + REPO
    main = json.loads(read_url(prefix + "/git/ref/heads/main"))
    pr = json.loads(read_url(prefix + "/pulls/" + str(number)))
    require(main["object"]["sha"] == base, "Main changed; refresh gate")
    require(
        pr["state"] == "open" and pr["head"]["sha"] == head and pr["base"]["sha"] == base,
        "PR revision changed",
    )
    require(
        pr["head"]["repo"]["full_name"] == REPO and pr["base"]["repo"]["full_name"] == REPO,
        "Untrusted PR repository",
    )
    checks = json.loads(read_url(prefix + "/commits/" + head + "/check-runs?per_page=100"))
    require(checks["total_count"] < 100, "Checks pagination needed")
    matching = [c for c in checks["check_runs"] if c["name"] == "ci-required" and c["app"]["id"] == 15368]
    require(bool(matching), "Required check missing")
    latest = max(matching, key=lambda c: c["id"])
    require(latest["status"] == "completed" and latest["conclusion"] == "success", "Required CI not passed")


def install(
    base: str,
    head: str,
    number: int,
    expected: str,
    *,
    execute: bool = False,
    approve_new_writes_only: bool = False,
    expected_receiver_sha256: str | None = None,
    expected_runtime_revision: str | None = None,
) -> dict[str, Any]:
    import fcntl

    require(os.geteuid() == 0 and socket.gethostname() == "vmi3224957", "Wrong host/account")
    require(
        bool(re.fullmatch(r"[0-9a-f]{40}", base)) and bool(re.fullmatch(r"[0-9a-f]{40}", head)),
        "Invalid revision",
    )
    require(bool(re.fullmatch(r"[0-9a-f]{64}", expected)) and number > 0, "Invalid update identity")
    require(
        (expected_receiver_sha256 is None) == (expected_runtime_revision is None),
        "Current receiver/runtime identities must be supplied together",
    )
    old_hash = OLD_HASH if expected_receiver_sha256 is None else expected_receiver_sha256
    runtime_revision = (
        "afd10770aa3a330e6c5a28e3ea85a676060e6c29"
        if expected_runtime_revision is None
        else expected_runtime_revision
    )
    require(
        bool(re.fullmatch(r"[0-9a-f]{64}", old_hash))
        and bool(re.fullmatch(r"[0-9a-f]{40}", runtime_revision)),
        "Invalid current receiver/runtime identity",
    )
    require(
        not approve_new_writes_only or expected_receiver_sha256 is None,
        "New-write permit requires the original reviewed legacy profile",
    )
    require(
        APP.resolve() == APP and RECEIVER.resolve() == RECEIVER and TARGET.resolve() == TARGET,
        "Unexpected symlink target",
    )
    require(digest(TARGET.read_bytes()) == TARGET_HASH, "Deployment target changed")
    old = RECEIVER.read_bytes()
    require(digest(old) == old_hash, "Receiver changed; inspect before retry")
    meta = RECEIVER.stat()
    require(meta.st_uid == 0 and meta.st_mode & 0o022 == 0, "Unsafe receiver permissions")
    candidate = read_url(f"https://raw.githubusercontent.com/{REPO}/{head}/scripts/deploy_receiver.py")
    require(digest(candidate) == expected, "Candidate hash mismatch")
    compile(candidate, "deploy_receiver.py", "exec")
    with (APP / ".deploy.lock").open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        gate(base, head, number)
        result = subprocess.run(
            ["docker", "--host", "unix:///var/run/docker.sock", "inspect", "crypto-watcher"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        live = json.loads(result.stdout)[0]
        labels = live["Config"].get("Labels") or {}
        require(
            live["State"]["Running"] and live["State"].get("Health", {}).get("Status") == "healthy",
            "Runtime not healthy",
        )
        require(
            labels.get("com.docker.compose.project") == "crypto_watcher"
            and labels.get("com.docker.compose.service") == "crypto_watcher",
            "Wrong deployment unit",
        )
        require(
            labels.get("org.opencontainers.image.revision") == runtime_revision,
            "Running revision changed; inspect before update",
        )
        report = {
            "schema": "cryptowatcher.receiver-update.v1",
            "state": "PREFLIGHT_PASS",
            "base": base,
            "candidate": head,
            "pr": number,
            "receiver_before": old_hash,
            "receiver_after": expected,
            "running_revision": labels["org.opencontainers.image.revision"],
            "running_container": live["Id"],
            "runtime_restarted": False,
            "database_changed": False,
            "ssh_changed": False,
        }
        if not execute:
            return report
        if approve_new_writes_only:
            require(
                number == 31 and not PERMIT.exists() and not PERMIT.is_symlink(),
                "Permit already exists or wrong PR",
            )
            commit = json.loads(read_url(f"https://api.github.com/repos/{REPO}/git/commits/{head}"))
            tree = commit["tree"]["sha"]
            require(bool(re.fullmatch(r"[0-9a-f]{40}", tree)), "Unknown approved tree")
            permit = {
                "schema": "OWNER_APPROVED_NEW_WRITES_ONLY_V1",
                "state": "ARMED",
                "pr": number,
                "tree_sha": tree,
                "head": head,
                "base": base,
                "target_sha256": TARGET_HASH,
                "previous_container": live["Id"],
                "previous_image": live["Image"],
                "previous_revision": labels["org.opencontainers.image.revision"],
                "approval": "owner/2026-10-14/call_1ece72ffadf74d4cb986afc82314cf27",
            }
            with PERMIT.open("x", encoding="utf-8") as output:
                os.chmod(PERMIT, 0o600)
                output.write(json.dumps(permit, sort_keys=True))
                output.flush()
                os.fsync(output.fileno())
            require(json.loads(PERMIT.read_text()) == permit, "Permit readback failed")
            report.update(new_write_permit="ARMED", permit_tree=tree)
        require(
            RECEIVER.read_bytes() == old and digest(TARGET.read_bytes()) == TARGET_HASH,
            "Concurrent receiver/target change",
        )
        folder = Path(tempfile.mkdtemp(prefix="receiver-update-", dir=APP / "rehearsals"))
        backup = folder / "previous_receiver.py"
        with backup.open("xb") as output:
            output.write(old)
        backup.chmod(0o600)
        require(digest(backup.read_bytes()) == old_hash, "Backup verification failed")
        with tempfile.NamedTemporaryFile(dir=RECEIVER.parent, delete=False) as output:
            output.write(candidate)
            output.flush()
            os.fsync(output.fileno())
            temporary = Path(output.name)
        temporary.chmod(0o644)
        require(RECEIVER.read_bytes() == old, "Concurrent receiver change")
        os.replace(temporary, RECEIVER)
        require(digest(RECEIVER.read_bytes()) == expected, "Receiver readback failed")
        record = folder / "receiver-update.json"
        report.update(
            state="PASS",
            at_utc=datetime.now(timezone.utc).isoformat(),
            backup_path=str(backup),
            record_path=str(record),
        )
        record.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
        record.chmod(0o600)
        return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--receiver-sha256", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approve-new-writes-only", action="store_true")
    parser.add_argument("--expected-receiver-sha256")
    parser.add_argument("--expected-runtime-revision")
    args = parser.parse_args()
    try:
        print(
            json.dumps(
                install(
                    args.base,
                    args.candidate,
                    args.pr,
                    args.receiver_sha256,
                    execute=args.execute,
                    approve_new_writes_only=args.approve_new_writes_only,
                    expected_receiver_sha256=args.expected_receiver_sha256,
                    expected_runtime_revision=args.expected_runtime_revision,
                )
            )
        )
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "state": "BLOCKED",
                    "reason": str(exc) if type(exc) is RuntimeError else type(exc).__name__,
                    "deploy_started": False,
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
