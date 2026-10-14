#!/usr/bin/env python3
"""Reviewed root bootstrap: receiver, exact target and one restricted public key.

No SSH daemon configuration/restart, bot restart, secret upload, DB changes,
GitHub variable change, removal or replacement of existing SSH keys.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import socket
import stat
import struct
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

SAMPLE_SOURCE = "54b2ce0c47194ee8e05fdc06910de2ad31b7d1d1"
BUILD_RUN = 38073301959
BUNDLE_HASH = "c7871651e916d44746d854b680ca04843f9e220f9926bcd09248de3e1bae7d40"
CONFIG_DIGEST = "sha256:5fba1047b65b5285f1caf4754b97e9f7e75f712a16da519a8459894ac9a60c19"
OCI_DIGEST = "sha256:89e8dfbed6e6f435a494e3a116b108e6215e6b11837bd601f6e612d9e8a81a09"
RECEIVER_HASH = "3754a9858525820864576d15f9517ac297004c918647e94cb67944fe7bc297ba"
HOST_FP = "SHA256:Yn3oI3abub6NtEBErRsDzduS7QNjYcDze4eqv8QXLaY"
FIRST_SOURCE = "065e49d112637d5eff4fd309c13aaab30fb53756"
FIRST_RUN = 38046940245
FIRST_BUNDLE = "8ed26d249000e363958b291ca77631b17c0359a61a56be6568b4f5472b0a171e"
FIRST_CONFIG = "sha256:14a9a6ebfc4664296d84d46c6c8438906aa061d5f1eaba52e3097cc74c9ac0e2"
FIRST_OCI = "sha256:a2d59bc276d6e8bd4034593bf5aa48706f746ba18a52d4cc1178bf67a56398fe"
APP = Path("/opt/crypto_watcher")
RECEIVER = Path("/usr/local/lib/cryptowatcher/deploy_receiver.py")
TARGET = Path("/etc/cryptowatcher/deploy-target.json")
KEYS = Path("/root/.ssh/authorized_keys")
HOST_PUBLIC = Path("/etc/ssh/ssh_host_ed25519_key.pub")
EXPECTED_TARGET = {
    "app_dir": "/opt/crypto_watcher",
    "data_dir": "/opt/crypto_watcher/data",
    "env_file": "/opt/crypto_watcher/.env",
    "service": "crypto_watcher",
    "compose_project": "crypto_watcher",
    "architecture": "amd64",
    "automatic_delivery_enabled": True,
}


class GuardError(RuntimeError):
    pass


def require(condition, reason):
    if not condition:
        raise GuardError(reason)


def sha(content):
    return hashlib.sha256(content).hexdigest()


def trusted(path, kind="file", private=False):
    require(path.is_absolute() and path.resolve() == path, "Unsafe path/symlink")
    s = path.lstat()
    require(s.st_uid == 0 and s.st_gid == 0 and not s.st_mode & 0o022, "Unsafe root ownership/permissions")
    require(stat.S_ISDIR(s.st_mode) if kind == "dir" else stat.S_ISREG(s.st_mode), "Unexpected file type")
    if private:
        require(not s.st_mode & 0o077, "Private permissions required")


def public_identity(content):
    lines = content.decode("ascii").splitlines()
    require(len(lines) == 1, "Expected one public key")
    fields = lines[0].split()
    require(len(fields) >= 2 and fields[0] == "ssh-ed25519", "Unexpected public key type")
    raw = base64.b64decode(fields[1], validate=True)
    require(
        len(raw) == 51
        and raw[:4] == struct.pack(">I", 11)
        and raw[4:15] == b"ssh-ed25519"
        and raw[15:19] == struct.pack(">I", 32),
        "Invalid Ed25519 public key",
    )
    fp = "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
    return fp, fields[1]


def run(args):
    r = subprocess.run(args, capture_output=True, text=True, timeout=45)
    require(r.returncode == 0, "Read-only precondition command failed")
    return r.stdout


def main_revision():
    request = urllib.request.Request(
        "https://api.github.com/repos/Just9120/crypto-watcher/git/ref/heads/main",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "CryptoWatcher-CD-bootstrap"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)["object"]["sha"]


def ssh_configuration():
    connection = os.environ.get("SSH_CONNECTION", "").split()
    require(len(connection) == 4 and connection[3] == "22", "Use the existing root SSH session")
    context = (
        "user=root,addr=" + connection[0] + ",host=" + connection[0] + ",laddr=" + connection[2] + ",lport=22"
    )
    expected = {
        "authorizedkeysfile": ".ssh/authorized_keys .ssh/authorized_keys2",
        "permitrootlogin": "yes",
        "strictmodes": "yes",
        "pubkeyauthentication": "yes",
        "authenticationmethods": "any",
        "usedns": "no",
        "forcecommand": "none",
        "authorizedkeyscommand": "none",
    }
    result = {}
    for label, extra in (("default", []), ("current_connection", ["-C", context])):
        values = dict(
            line.split(" ", 1) for line in run(["/usr/sbin/sshd", "-T", *extra]).splitlines() if " " in line
        )
        require(
            all(values.get(k) == v for k, v in expected.items()), "SSH configuration changed; review required"
        )
        require(
            "ssh-ed25519" in values.get("pubkeyacceptedalgorithms", "").split(","),
            "Ed25519 authentication unavailable",
        )
        result[label] = {**expected, "ed25519_accepted": True}
    return result


def active_runtime():
    first_path = APP / "releases/first-38046940245-20261010T172100Z-775978f9/deployment.json"
    trusted(first_path)
    first = json.loads(first_path.read_text())
    require(
        first.get("production_migration") == "PASS"
        and first.get("source_sha") == FIRST_SOURCE
        and first.get("build_run") == FIRST_RUN
        and first.get("bundle_sha256") == FIRST_BUNDLE,
        "Reviewed first migration not confirmed",
    )
    checks = first.get("post_checks", {})
    require(
        all(checks.get(key) == "PASS" for key in ("health", "persistence", "source_image"))
        and first.get("migration_readback", {}).get("all_tables_restore_readback") == "PASS",
        "First migration/runtime checks incomplete",
    )
    current = json.loads(run(["docker", "inspect", "crypto-watcher"]))[0]
    labels = current["Config"].get("Labels", {})
    source = labels.get("org.opencontainers.image.revision")
    require(source in (FIRST_SOURCE, SAMPLE_SOURCE), "Unreviewed running revision; inspect actual delivery")
    is_first = source == FIRST_SOURCE
    build_run = FIRST_RUN if is_first else BUILD_RUN
    bundle_hash = FIRST_BUNDLE if is_first else BUNDLE_HASH
    config_digest = FIRST_CONFIG if is_first else CONFIG_DIGEST
    oci_digest = FIRST_OCI if is_first else OCI_DIGEST
    record_path = (
        first_path
        if is_first
        else APP / "releases" / f"{SAMPLE_SOURCE}-{BUNDLE_HASH[:16]}" / "deployment.json"
    )
    trusted(record_path)
    record = json.loads(record_path.read_text())
    require(
        record.get("state") == ("RUNTIME_PASS_OWNER_SMOKE_PENDING" if is_first else "PASS")
        and record.get("source_sha") == source
        and record.get("build_run") == build_run
        and record.get("bundle_sha256") == bundle_hash,
        "Reviewed production delivery not confirmed",
    )
    require(
        record.get("image_config_digest") == config_digest
        and record.get("image_manifest_digest") == oci_digest,
        "Runtime artifact identity changed",
    )
    require(
        current["Image"] == record.get("image_id") and current["Image"] in (config_digest, oci_digest),
        "Running image differs from reviewed artifact",
    )
    require(
        current["State"].get("Running") and current["State"].get("Health", {}).get("Status") == "healthy",
        "Runtime health precondition failed",
    )
    require(
        labels.get("io.cryptowatcher.build-run") == str(build_run),
        "Running source/build changed",
    )
    require(
        labels.get("com.docker.compose.project") == "crypto_watcher"
        and labels.get("com.docker.compose.service") == "crypto_watcher",
        "Running deployment unit changed",
    )
    require(current["Config"].get("User") == "10001:10001", "Running application identity changed")
    mounts = current.get("Mounts", [])
    require(
        len(mounts) == 1
        and mounts[0].get("Source") == str(APP / "data")
        and mounts[0].get("Destination") == "/data",
        "Persistent mount changed",
    )
    receipt_path = APP / "data/sqlite/.g1-migration.json"
    require(receipt_path.resolve() == receipt_path, "Unsafe migration receipt path")
    receipt = json.loads(receipt_path.read_text())
    require(
        receipt.get("schema") == 1 and receipt.get("restore_rehearsal") == "PASS",
        "Migration/restore receipt missing",
    )
    database = APP / "data/sqlite/cryptowatcher.sqlite3"
    require(database.is_file() and database.resolve() == database, "Migrated SQLite missing/unsafe")
    state = json.loads(
        run(["docker", "exec", "crypto-watcher", "python", "-m", "cryptowatcher", "state-check"])
    )
    require(
        state.get("status") == "integrity_ok"
        and state.get("schema") == 1
        and state.get("owner_id") == 233382575,
        "Persistent integrity/schema/owner precondition failed",
    )
    legacy = receipt.get("legacy_sha256")
    require(
        isinstance(legacy, str) and len(legacy) == 64 and all(c in "0123456789abcdef" for c in legacy),
        "Migration identity missing",
    )
    require(
        state.get("legacy_sha256") == receipt.get("legacy_sha256") == first.get("legacy_sha256"),
        "Persistent migration identity changed",
    )
    return {
        "source_sha": source,
        "build_run": build_run,
        "image_id": current["Image"],
        "runtime_record": str(record_path),
    }


def preflight(directory, expected_main, public_file, approved_key=None):
    require(os.geteuid() == 0 and socket.gethostname() == "vmi3224957", "Wrong host/account")
    require(bool(re.fullmatch(r"[0-9a-f]{40}", expected_main)), "Invalid expected main revision")
    trusted(directory, "dir")
    path = directory / "deploy_receiver.py"
    trusted(path)
    content = {"deploy_receiver.py": path.read_bytes()}
    require(sha(content["deploy_receiver.py"]) == RECEIVER_HASH, "Bootstrap artifact changed")
    content["deploy-target.json"] = (json.dumps(EXPECTED_TARGET, sort_keys=True, indent=2) + "\n").encode()
    trusted(public_file, private=True)
    fp, encoded = public_identity(public_file.read_bytes())
    require(approved_key is None or approved_key == fp, "Explicit public key authorization mismatch")
    trusted(HOST_PUBLIC)
    require(public_identity(HOST_PUBLIC.read_bytes())[0] == HOST_FP, "Trusted SSH host identity changed")
    for path in (Path("/root"), KEYS.parent):
        trusted(path, "dir", private=True)
    trusted(KEYS, private=True)
    original = KEYS.read_bytes()
    require(encoded.encode() not in original, "This key is already installed; inspect prior bootstrap record")
    for destination in (RECEIVER, TARGET):
        trusted(destination.parent.parent, "dir")
        require(
            not destination.parent.exists() and not destination.parent.is_symlink(),
            "Bootstrap destination already exists; preserve and inspect",
        )
    trusted(APP, "dir")
    trusted(APP / "rehearsals", "dir")
    trusted(APP / ".env", private=True)
    require(main_revision() == expected_main, "Candidate superseded by current main")
    ssh = ssh_configuration()
    runtime = active_runtime()
    line = (
        'restrict,command="/usr/bin/python3 /usr/local/lib/cryptowatcher/deploy_receiver.py" ssh-ed25519 '
        + encoded
        + " cryptowatcher-production-CD\n"
    ).encode("ascii")
    runtime["expected_main"] = expected_main
    return content, original, line, ssh, runtime, fp


def publish_new(path, content, mode):
    path.parent.mkdir(mode=0o755)
    with path.open("xb") as out:
        out.write(content)
        out.flush()
        os.fsync(out.fileno())
    path.chmod(mode)
    trusted(path, private=mode == 0o600)
    require(path.read_bytes() == content, "Installed file readback failed")


def execute(content, original, line, ssh, runtime, public_fp):
    record_dir = (
        APP
        / "rehearsals"
        / ("cd-bootstrap-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8])
    )
    record_dir.mkdir(mode=0o700)
    record_path = record_dir / "bootstrap.json"
    record = {
        "schema": "cryptowatcher.cd-bootstrap.v1",
        "state": "PREPARED",
        "at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "public_key_fingerprint": public_fp,
        "host_key_fingerprint": HOST_FP,
        "runtime": runtime,
        "authorized_keys_before_sha256": sha(original),
        "receiver_sha256": RECEIVER_HASH,
        "target_sha256": sha(content["deploy-target.json"]),
        "github_changed": False,
        "automatic_cd": "NOT_ENABLED",
        "runtime_restarted": False,
    }

    def save():
        record_path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        record_path.chmod(0o600)

    save()
    try:
        backup = record_dir / "authorized_keys.before"
        backup.write_bytes(original)
        backup.chmod(0o600)
        require(backup.read_bytes() == original, "SSH access backup readback failed")
        publish_new(RECEIVER, content["deploy_receiver.py"], 0o644)
        record["state"] = "RECEIVER_INSTALLED"
        save()
        publish_new(TARGET, content["deploy-target.json"], 0o600)
        record["state"] = "TARGET_INSTALLED"
        save()
        trusted(KEYS, private=True)
        require(KEYS.read_bytes() == original, "SSH keys changed concurrently; append aborted")
        candidate = original + (b"" if not original or original.endswith(b"\n") else b"\n") + line
        staged = KEYS.parent / (".cryptowatcher-key-" + uuid.uuid4().hex)
        with staged.open("xb") as out:
            out.write(candidate)
            out.flush()
            os.fsync(out.fileno())
        staged.chmod(0o600)
        trusted(staged, private=True)
        require(KEYS.read_bytes() == original, "SSH keys changed concurrently; commit aborted")
        os.replace(staged, KEYS)
        trusted(KEYS, private=True)
        require(
            KEYS.read_bytes() == candidate and candidate.startswith(original),
            "Existing SSH access preservation failed",
        )
        record["state"] = "KEY_INSTALLED"
        save()
        require(ssh_configuration() == ssh, "Effective SSH configuration changed; inspect installation")
        require(
            sha(RECEIVER.read_bytes()) == RECEIVER_HASH
            and TARGET.read_bytes() == content["deploy-target.json"],
            "Installed configuration changed",
        )
        record.update(
            state="PASS",
            authorized_keys_after_sha256=sha(candidate),
            existing_keys_preserved=True,
            record_path=str(record_path),
            dedicated_ssh_validation="PENDING",
            github_secret_setup="NOT_RUN",
        )
        save()
        return record
    except Exception as error:
        record.update(
            state="PARTIAL_INSTALLATION_STOPPED",
            reason=str(error) if isinstance(error, GuardError) else type(error).__name__,
            record_path=str(record_path),
            next_step="Inspect installed files and existing keys; no blind retry or SSH restart",
        )
        save()
        raise GuardError("Bootstrap partial; inspect record " + str(record_path)) from None


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-main", required=True)
    parser.add_argument("--public-key-file", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approve-key")
    args = parser.parse_args(argv)
    try:
        require(not args.execute or bool(args.approve_key), "Explicit bootstrap authorization required")
        public_file = args.public_key_file.absolute()
        if not args.execute:
            prepared = preflight(Path(__file__).absolute().parent, args.expected_main, public_file)
            print(
                json.dumps(
                    {
                        "state": "PREFLIGHT_PASS",
                        "expected_main": args.expected_main,
                        "runtime": prepared[4],
                        "files_changed": False,
                        "automatic_cd": "NOT_ENABLED",
                        "public_key_fingerprint": prepared[5],
                    }
                )
            )
            return 0
        import fcntl

        trusted(APP, "dir")
        trusted(APP / ".deploy.lock")
        with (APP / ".deploy.lock").open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            prepared = preflight(
                Path(__file__).absolute().parent, args.expected_main, public_file, args.approve_key
            )
            print(json.dumps(execute(*prepared), sort_keys=True))
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "state": "BLOCKED",
                    "reason": str(error) if isinstance(error, GuardError) else type(error).__name__,
                    "automatic_cd": "NOT_ENABLED",
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
