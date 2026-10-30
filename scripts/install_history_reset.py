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


def private_signature(source: Path, expected_generation: str | None = None) -> str:
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        layout = db.execute("PRAGMA user_version").fetchone()[0]
        generation_row = db.execute(
            "SELECT payload_json FROM runtime WHERE key='history_generation'"
        ).fetchone()
        if expected_generation is None:
            require(layout == 1, "Source is not the authorized legacy layout")
            require(generation_row is None, "History was already reset")
        else:
            require(
                re.fullmatch("[0-9a-f]{32}", expected_generation) is not None, "Invalid expected generation"
            )
            require(layout == 2 and generation_row is not None, "Expected bounded test history")
            generation = json.loads(generation_row[0])
            require(generation.get("id") == expected_generation, "Source generation changed")
            require(
                generation.get("model") != "EPISODE_ORIGIN_V2_72H", "Valid light history must not be reset"
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
    light_radar_transition: bool = False,
    expected_generation: str | None = None,
    expected_receiver_sha256: str | None = None,
    expected_helper_sha256: str | None = None,
    expected_runtime_revision: str | None = None,
) -> dict[str, Any]:
    import fcntl

    require(os.geteuid() == 0 and socket.gethostname() == "vmi3224957", "Wrong host/account")
    if light_radar_transition:
        require(
            re.fullmatch("[0-9a-f]{40}", base) is not None
            and re.fullmatch("[0-9a-f]{40}", expected_runtime_revision or "") is not None
            and re.fullmatch("[0-9a-f]{64}", expected_receiver_sha256 or "") is not None
            and re.fullmatch("[0-9a-f]{32}", expected_generation or "") is not None,
            "Light transition requires exact reviewed base/runtime/receiver/generation identities",
        )
    else:
        require(
            expected_generation is None
            and expected_receiver_sha256 is None
            and expected_helper_sha256 is None
            and expected_runtime_revision is None,
            "Light transition identities require the explicit transition mode",
        )
    installed_receiver = expected_receiver_sha256 if light_radar_transition else OLD_RECEIVER
    runtime_revision = expected_runtime_revision if light_radar_transition else base
    require(
        (light_radar_transition or base == BASELINE)
        and re.fullmatch("[0-9a-f]{40}", head) is not None
        and pr > 0,
        "Unreviewed source revision",
    )
    require(
        all(re.fullmatch("[0-9a-f]{64}", v) for v in (receiver_sha, helper_sha)), "Invalid script identity"
    )
    for path in (APP, RECEIVER, TARGET, HELPER.parent, APP / "rehearsals"):
        require(path.resolve() == path and not path.is_symlink(), "Unsafe target path")
    helper_before = None
    permit_before = None
    require(not HELPER.is_symlink() and not PERMIT.is_symlink(), "Unsafe reset setup path")
    if light_radar_transition:
        if HELPER.exists():
            helper_before = sha(HELPER.read_bytes())
            require(
                expected_helper_sha256 == helper_before
                and HELPER.stat().st_uid == 0
                and not HELPER.stat().st_mode & 0o022,
                "Installed helper identity/permissions changed",
            )
        else:
            require(expected_helper_sha256 is None, "Expected installed helper is missing")
        if PERMIT.exists():
            meta = PERMIT.stat()
            require(
                meta.st_uid == 0 and stat.S_IMODE(meta.st_mode) == 0o600 and meta.st_size <= 8192,
                "Unsafe preceding reset permit",
            )
            prior_permit = json.loads(PERMIT.read_bytes())
            require(
                prior_permit.get("schema") == POLICY and prior_permit.get("state") == "CONSUMED",
                "Existing reset permit is not a completed known operation",
            )
            permit_before = sha(PERMIT.read_bytes())
    else:
        require(not HELPER.exists() and not PERMIT.exists(), "Reset setup already exists; inspect outcome")
    require(
        sha(RECEIVER.read_bytes()) == installed_receiver and sha(TARGET.read_bytes()) == TARGET_HASH,
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
        and labels.get("org.opencontainers.image.revision") == runtime_revision
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
    private = private_signature(source, expected_generation)
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
        "previous_revision": runtime_revision,
        "source_inode": source.stat().st_ino,
        "private_sha256": private,
        "env_sha256": sha(env.read_bytes()),
        "receiver_sha256": receiver_sha,
        "helper_sha256": helper_sha,
        "authorization": "Owner request2026-10-16: reset old test market history after fixes/checks; preserve settings/credentials; backups and experiments excluded",
    }
    if light_radar_transition:
        permit.update(
            light_radar_transition=True,
            expected_generation=expected_generation,
            authorization="Owner-approved LIGHT-RADAR Goal: discard only old test market history; preserve private state with explicit 72h/activation migration; TA/Paper/backups excluded",
        )
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
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        gate(base, head, pr)
        require(
            live()["Id"] == previous["Id"]
            and source.stat().st_ino == permit["source_inode"]
            and sha(env.read_bytes()) == permit["env_sha256"]
            and sha(TARGET.read_bytes()) == TARGET_HASH
            and sha(RECEIVER.read_bytes()) == installed_receiver
            and private_signature(source, expected_generation) == private
            and (sha(HELPER.read_bytes()) if HELPER.exists() else None) == helper_before
            and (sha(PERMIT.read_bytes()) if PERMIT.exists() else None) == permit_before,
            "Preflight state changed",
        )
        directory = Path(tempfile.mkdtemp(prefix="reset-bootstrap-", dir=APP / "rehearsals"))
        new_file(directory / "previous_receiver.py", RECEIVER.read_bytes())
        new_file(directory / "prepared_permit.json", json.dumps(permit, sort_keys=True).encode())
        if helper_before is not None:
            new_file(directory / "previous_helper.py", HELPER.read_bytes())
        if permit_before is not None:
            new_file(directory / "previous_permit.json", PERMIT.read_bytes())
        helper_candidate = HELPER.with_name("reset_delivery.reset-candidate.py")
        temporary = RECEIVER.with_name("deploy_receiver.reset-candidate.py")
        permit_candidate = PERMIT.with_name("history-reset-permit.candidate.json")
        new_file(helper_candidate, scripts["reset_delivery.py"], 0o644)
        new_file(temporary, scripts["deploy_receiver.py"], 0o644)
        new_file(permit_candidate, json.dumps(permit, sort_keys=True).encode())
        new_file(directory / "installation-prepared.json", json.dumps(result, sort_keys=True).encode())
        os.replace(helper_candidate, HELPER)
        os.replace(temporary, RECEIVER)
        require(
            sha(RECEIVER.read_bytes()) == receiver_sha and sha(HELPER.read_bytes()) == helper_sha,
            "Installation readback failed",
        )
        os.replace(permit_candidate, PERMIT)
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
    p.add_argument("--light-radar-transition", action="store_true")
    p.add_argument("--expected-generation")
    p.add_argument("--expected-receiver-sha256")
    p.add_argument("--expected-helper-sha256")
    p.add_argument("--expected-runtime-revision")
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
                light_radar_transition=a.light_radar_transition,
                expected_generation=a.expected_generation,
                expected_receiver_sha256=a.expected_receiver_sha256,
                expected_helper_sha256=a.expected_helper_sha256,
                expected_runtime_revision=a.expected_runtime_revision,
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
