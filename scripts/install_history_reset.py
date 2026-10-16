"""Install exact reviewed receiver/helper and arm the single authorized market reset.

This operator step never restarts the bot or modifies its database. The deployed
receiver performs the reset only for the matching reviewed tree after merge.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import sqlite3
import stat
import subprocess
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any

if __package__:
    from .update_cd_receiver import gate, read_url
else:
    from update_cd_receiver import gate, read_url

APP = Path("/opt/crypto_watcher")
RECEIVER = Path("/usr/local/lib/cryptowatcher/deploy_receiver.py")
HELPER = RECEIVER.with_name("reset_delivery.py")
TARGET = Path("/etc/cryptowatcher/deploy-target.json")
PERMIT = TARGET.with_name("history-reset-permit.json")
OLD_RECEIVER = "3754a9858525820864576d15f9517ac297004c918647e94cb67944fe7bc297ba"
TARGET_HASH = "4d69f74f2cd4bbd609b6ac15667d8ff82a12f3cc42db1c2aaf17bf1bda773274"
BASELINE = "2efd9093d9639f767f2a0df697bfd060c2ce47e0"
OWNER = 233382575
POLICY = "OWNER_APPROVED_TEST_HISTORY_RESET_20261016_V1"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def live() -> dict[str, Any]:
    r = subprocess.run(
        ["docker", "--host", "unix:///var/run/docker.sock", "inspect", "crypto-watcher"],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    return json.loads(r.stdout)[0]


def private_signature(source: Path) -> str:
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        require(
            db.execute("PRAGMA user_version").fetchone()[0] == 1, "Source is not the authorized legacy layout"
        )
        require(
            db.execute("SELECT 1 FROM runtime WHERE key=?", ("history_generation",)).fetchone() is None,
            "History was already reset",
        )
        rows = {}
        for name in ("owner_settings", "delivery_preferences", "mutes", "legacy_import"):
            values = [list(r) for r in db.execute(f"SELECT * FROM {name} ORDER BY 1 LIMIT 10001")]
            require(len(values) <= 10000, "Private row bound")
            rows[name] = values
        require(
            len(rows["owner_settings"]) == 1 and rows["owner_settings"][0][0] == OWNER, "Unexpected owner"
        )
        preferences = {}
        for key in ("radar:v1:digest-preferences", "radar:v1:agent:preference"):
            row = db.execute("SELECT payload_json FROM runtime WHERE key=?", (key,)).fetchone()
            preferences[key] = json.loads(row[0]) if row else None
        raw = json.dumps(
            {
                "schema": "cryptowatcher.reset-private.v1",
                "owner_id": OWNER,
                "tables": rows,
                "preferences": preferences,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        require(len(raw) <= 8 * 1024 * 1024, "Private bytes bound")
        return sha(raw)


def new_file(path: Path, raw: bytes, mode: int = 0o600) -> None:
    handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    with os.fdopen(handle, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def install(
    base: str,
    head: str,
    pr: int,
    receiver_sha: str,
    helper_sha: str,
    *,
    execute: bool = False,
    approved: bool = False,
) -> dict[str, Any]:
    import fcntl

    require(os.geteuid() == 0 and socket.gethostname() == "vmi3224957", "Wrong host/account")
    require(
        base == BASELINE and re.fullmatch("[0-9a-f]{40}", head) is not None and pr > 0,
        "Unreviewed source revision",
    )
    require(
        all(re.fullmatch("[0-9a-f]{64}", v) for v in (receiver_sha, helper_sha)), "Invalid script identity"
    )
    for path in (APP, RECEIVER, TARGET, HELPER.parent):
        require(path.resolve() == path and not path.is_symlink(), "Unsafe target path")
    require(
        not HELPER.exists() and not HELPER.is_symlink() and not PERMIT.exists() and not PERMIT.is_symlink(),
        "Reset setup already exists; inspect outcome",
    )
    require(
        sha(RECEIVER.read_bytes()) == OLD_RECEIVER and sha(TARGET.read_bytes()) == TARGET_HASH,
        "Installed deployment procedure changed",
    )
    require(RECEIVER.stat().st_uid == 0 and not RECEIVER.stat().st_mode & 0o022, "Unsafe installed receiver")
    gate(base, head, pr)
    scripts = {}
    for name, expected in (("deploy_receiver.py", receiver_sha), ("reset_delivery.py", helper_sha)):
        raw = read_url(f"https://raw.githubusercontent.com/Just9120/crypto-watcher/{head}/scripts/{name}")
        require(sha(raw) == expected, "Reviewed script hash mismatch")
        compile(raw, name, "exec")
        scripts[name] = raw
    previous = live()
    labels = previous["Config"].get("Labels") or {}
    require(
        previous["State"]["Running"]
        and labels.get("org.opencontainers.image.revision") == base
        and labels.get("com.docker.compose.project") == "crypto_watcher"
        and labels.get("com.docker.compose.service") == "crypto_watcher",
        "Runtime predecessor changed",
    )
    source = APP / "data/sqlite/cryptowatcher.sqlite3"
    require(source.is_file() and source.resolve() == source and not source.is_symlink(), "Unexpected source")
    env = APP / ".env"
    require(env.resolve() == env and not env.is_symlink(), "Unsafe infrastructure config path")
    meta = env.stat()
    require(meta.st_uid == 0 and stat.S_IMODE(meta.st_mode) == 0o600, "Unsafe infrastructure config")
    private = private_signature(source)
    tree = json.loads(read_url(f"https://api.github.com/repos/Just9120/crypto-watcher/git/commits/{head}"))[
        "tree"
    ]["sha"]
    permit = {
        "schema": POLICY,
        "state": "ARMED",
        "pr": pr,
        "reviewed_head": head,
        "tree_sha": tree,
        "target_sha256": TARGET_HASH,
        "previous_container": previous["Id"],
        "previous_image": previous["Image"],
        "previous_revision": base,
        "source_inode": source.stat().st_ino,
        "private_sha256": private,
        "env_sha256": sha(env.read_bytes()),
        "receiver_sha256": receiver_sha,
        "helper_sha256": helper_sha,
        "authorization": "Owner request2026-10-16: reset old test market history after fixes/checks; preserve settings/credentials; backups and experiments excluded",
    }
    result = {
        "schema": "cryptowatcher.reset-bootstrap.v1",
        "state": "PREFLIGHT_PASS",
        "base": base,
        "head": head,
        "pr": pr,
        "private_sha256": private,
        "receiver_sha256": receiver_sha,
        "helper_sha256": helper_sha,
        "database_changed": False,
        "runtime_restarted": False,
    }
    if not execute:
        return result
    require(approved, "Explicit approved reset procedure flag required")
    with (APP / ".deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        gate(base, head, pr)
        require(
            live()["Id"] == previous["Id"]
            and sha(RECEIVER.read_bytes()) == OLD_RECEIVER
            and private_signature(source) == private,
            "Preflight state changed",
        )
        directory = Path(tempfile.mkdtemp(prefix="reset-bootstrap-", dir=APP / "rehearsals"))
        new_file(directory / "previous_receiver.py", RECEIVER.read_bytes())
        new_file(directory / "prepared_permit.json", json.dumps(permit, sort_keys=True).encode())
        new_file(HELPER, scripts["reset_delivery.py"], 0o644)
        temporary = RECEIVER.with_name("deploy_receiver.reset-candidate.py")
        new_file(temporary, scripts["deploy_receiver.py"], 0o644)
        os.replace(temporary, RECEIVER)
        require(
            sha(RECEIVER.read_bytes()) == receiver_sha and sha(HELPER.read_bytes()) == helper_sha,
            "Installation readback failed",
        )
        new_file(PERMIT, json.dumps(permit, sort_keys=True).encode())
        require(
            json.loads(PERMIT.read_bytes()) == permit and live()["Id"] == previous["Id"],
            "Arm/runtime readback failed",
        )
        result.update(
            state="PASS",
            record_path=str(directory / "bootstrap.json"),
            files_changed=True,
            permit_state="ARMED",
        )
        new_file(directory / "bootstrap.json", json.dumps(result, sort_keys=True).encode())
    return result


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--pr", required=True, type=int)
    p.add_argument("--receiver-sha256", required=True)
    p.add_argument("--helper-sha256", required=True)
    p.add_argument("--execute", action="store_true")
    p.add_argument("--approve-reset-test-history", action="store_true")
    a = p.parse_args()
    print(
        json.dumps(
            install(
                a.base,
                a.head,
                a.pr,
                a.receiver_sha256,
                a.helper_sha256,
                execute=a.execute,
                approved=a.approve_reset_test_history,
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
