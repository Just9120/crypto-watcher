"""One owner-authorized history reset through the existing deployment lock.

Root-installed next to deploy_receiver. No action happens at import time.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

POLICY = "OWNER_APPROVED_TEST_HISTORY_RESET_20261016_V1"
PERMIT = Path("/etc/cryptowatcher/history-reset-permit.json")
OWNER = 233382575


def load_permit(
    app: Path, target: Path, revision: str, run: Callable[..., str], require: Callable[..., None]
) -> dict[str, Any] | None:
    if not PERMIT.exists():
        require(not PERMIT.is_symlink(), "Dangling reset permit")
        return None
    require(PERMIT.resolve() == PERMIT and not PERMIT.is_symlink(), "Unsafe reset permit path")
    meta = PERMIT.stat()
    require(
        meta.st_uid == 0 and stat.S_IMODE(meta.st_mode) == 0o600 and meta.st_size <= 8192,
        "Unsafe reset permit",
    )
    value = json.loads(PERMIT.read_text())
    require(value.get("schema") == POLICY, "Unknown reset policy")
    if value.get("state") in ("SWITCHED", "CONSUMED"):
        return None
    require(value.get("state") == "ARMED", "Reset permit not armed")
    require(
        value.get("target_sha256") == hashlib.sha256(target.read_bytes()).hexdigest(), "Reset target changed"
    )
    request = urllib.request.Request(
        f"https://api.github.com/repos/Just9120/crypto-watcher/git/commits/{revision}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "CryptoWatcher-CD"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        commit = json.load(response)
    if commit["tree"]["sha"] != value.get("tree_sha"):
        return None
    old = json.loads(run(["docker", "inspect", "crypto-watcher"]))[0]
    labels = old["Config"].get("Labels") or {}
    require(
        old["Id"] == value.get("previous_container")
        and old["Image"] == value.get("previous_image")
        and old["State"]["Running"]
        and labels.get("org.opencontainers.image.revision") == value.get("previous_revision"),
        "Reset predecessor changed",
    )
    source = app / "data/sqlite/cryptowatcher.sqlite3"
    require(
        source.resolve() == source and source.is_file() and source.stat().st_ino == value.get("source_inode"),
        "Reset source changed",
    )
    require(
        hashlib.sha256((app / ".env").read_bytes()).hexdigest() == value.get("env_sha256"),
        "Runtime config changed",
    )
    return value


def store_permit(value: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=PERMIT.parent, delete=False) as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(json.dumps(value, sort_keys=True))
        output.flush()
        os.fsync(output.fileno())
        temporary = Path(output.name)
    os.replace(temporary, PERMIT)
    with _directory_sync(PERMIT.parent):
        pass


class _directory_sync:
    def __init__(self, path: Path):
        self.path = path

    def __enter__(self) -> None:
        fd = os.open(self.path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def __exit__(self, *args: Any) -> None:
        pass


def offline(app: Path, image: str) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--pull",
        "never",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "10001:10001",
        "--memory",
        "512m",
        "--memory-swap",
        "512m",
        "--cpus",
        "1",
        "--pids-limit",
        "64",
        "--ulimit",
        "core=0",
        "--mount",
        f"type=bind,src={app}/data,dst=/data",
        "--entrypoint",
        "python",
        image,
    ]


def prepare(
    app: Path,
    image: str,
    permit: dict[str, Any],
    run: Callable[..., str],
    require: Callable[..., None],
    phase: Callable[..., None],
    record: dict[str, Any],
) -> dict[str, Any]:
    source = app / "data/sqlite/cryptowatcher.sqlite3"
    require(source.stat().st_ino == permit["source_inode"], "Source inode changed")
    # Filesystem staging is tiny; no old-history copy or validation is requested.
    directory = Path(tempfile.mkdtemp(prefix="history-reset-", dir=source.parent))
    os.chown(directory, 10001, 10001)
    directory.chmod(0o700)
    record.update(
        reset_directory=str(directory),
        reset_directory_inode=[directory.stat().st_dev, directory.stat().st_ino],
        reset_stopped_container=permit["previous_container"],
    )
    phase("RESET_STOP_SOURCE")
    run(["docker", "stop", "--time", "30", permit["previous_container"]], timeout=45)
    old = json.loads(run(["docker", "inspect", permit["previous_container"]]))[0]
    require(
        not old["State"]["Running"] and old["Image"] == permit["previous_image"], "Old writer did not stop"
    )
    phase("RESET_PRIVATE_AND_FRESH_CANDIDATE")
    relative = "/data/sqlite/" + directory.name
    options = offline(app, image)
    options[-1:-1] = ["--name", record["candidate_name"], "--no-healthcheck"]
    proof = json.loads(
        run(
            [
                *options,
                "-B",
                "-m",
                "cryptowatcher.reset_history",
                "--source",
                "/data/sqlite/cryptowatcher.sqlite3",
                "--candidate",
                relative + "/fresh.sqlite3",
                "--private-recovery",
                relative + "/private.json",
                "--owner-id",
                str(OWNER),
                "--receipt",
                relative + "/candidate.json",
            ],
            timeout=135,
        )
    )
    require(
        proof.get("state") == "PASS"
        and proof.get("integrity") == "PASS"
        and proof.get("private_restore") == "PASS",
        "Reset candidate proof failed",
    )
    require(
        proof.get("private_source_sha256") == permit["private_sha256"], "Owner settings changed before reset"
    )
    candidate = directory / "fresh.sqlite3"
    require(
        candidate.is_file()
        and not candidate.is_symlink()
        and candidate.stat().st_uid == 10001
        and candidate.stat().st_size <= 32 * 1024 * 1024,
        "Unexpected fresh candidate",
    )
    require(
        all(not Path(str(candidate) + s).exists() for s in ("-wal", "-shm", "-journal")),
        "Candidate is not closed",
    )
    record.update(
        reset_proof=proof, reset_candidate_sha256=hashlib.sha256(candidate.read_bytes()).hexdigest()
    )
    phase("RESET_CANDIDATE_QUALIFIED")
    return proof


def identity(path: Path) -> list[int]:
    value = path.stat()
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns]


def source_handles(path: Path) -> list[int]:
    wanted = path.stat()
    found = []
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal():
            continue
        try:
            for fd in (process / "fd").iterdir():
                try:
                    value = fd.stat()
                except (FileNotFoundError, PermissionError):
                    continue
                if (value.st_dev, value.st_ino) == (wanted.st_dev, wanted.st_ino):
                    found.append(int(process.name))
                    break
        except (FileNotFoundError, PermissionError):
            continue
    return sorted(found)


def safe_directory(app: Path, record: dict[str, Any], require: Callable[..., None]) -> Path:
    directory = Path(record["reset_directory"])
    require(
        directory.is_absolute()
        and directory.resolve() == directory
        and directory.parent == app / "data/sqlite"
        and not directory.is_symlink(),
        "Unexpected reset directory",
    )
    value = directory.stat()
    require(
        [value.st_dev, value.st_ino] == record["reset_directory_inode"]
        and value.st_uid == 10001
        and stat.S_IMODE(value.st_mode) == 0o700,
        "Reset directory identity changed",
    )
    return directory


def promote(
    app: Path,
    permit: dict[str, Any],
    require: Callable[..., None],
    phase: Callable[..., None],
    record: dict[str, Any],
) -> None:
    source = app / "data/sqlite/cryptowatcher.sqlite3"
    directory = safe_directory(app, record, require)
    candidate = directory / "fresh.sqlite3"
    require(
        source.stat().st_ino == permit["source_inode"] and not source_handles(source),
        "Unexpected source handles/identity",
    )
    require(
        hashlib.sha256(candidate.read_bytes()).hexdigest() == record["reset_candidate_sha256"],
        "Candidate changed",
    )
    staged = []
    for suffix in ("-wal", "-shm", "-journal", ""):
        original = Path(str(source) + suffix)
        if not original.exists():
            require(not original.is_symlink(), "Dangling source sidecar")
            continue
        require(
            original.is_file() and not original.is_symlink() and original.stat().st_uid == 10001,
            "Unsafe source data file",
        )
        destination = directory / ("discarded-market.sqlite3" + suffix)
        require(not destination.exists() and not destination.is_symlink(), "Staging collision")
        staged.append({"source": str(original), "path": str(destination), "identity": identity(original)})
    record["reset_discard_scope"] = staged
    record["reset_promotion_started"] = True
    phase("RESET_PROMOTE")
    for value in staged:
        os.rename(value["source"], value["path"])
    os.rename(candidate, source)
    with _directory_sync(source.parent):
        pass
    record["reset_promoted"] = True
    record["reset_new_inode"] = source.stat().st_ino
    permit.update(
        state="SWITCHED", generation=record["reset_proof"]["generation"]["id"], reset_directory=str(directory)
    )
    store_permit(permit)
    phase("RESET_PROMOTED")


def recover_pre_switch(
    app: Path, run: Callable[..., str], require: Callable[..., None], record: dict[str, Any]
) -> None:
    if not record.get("reset_stopped_container") or record.get("reset_promoted"):
        return
    # If promotion started but the main path has gone, never guess which version
    # can run. Leave the exact paths/record for concrete forward recovery.
    source = app / "data/sqlite/cryptowatcher.sqlite3"
    if record.get("reset_promotion_started") or not source.is_file():
        record["reset_recovery"] = "INSPECT_PROMOTION_RECORD"
        return
    old = json.loads(run(["docker", "inspect", record["reset_stopped_container"]]))[0]
    if not old["State"]["Running"]:
        run(["docker", "start", old["Id"]], timeout=30)
    record["reset_recovery"] = "ORIGINAL_CONTAINER_RESTARTED_UNCHANGED_DATABASE"


def finish(
    app: Path,
    permit: dict[str, Any],
    diagnostics: dict[str, Any],
    require: Callable[..., None],
    phase: Callable[..., None],
    record: dict[str, Any],
) -> None:
    proof = record["reset_proof"]
    safe_directory(app, record, require)
    require(diagnostics.get("database", {}).get("schema") == 2, "Reset schema post-check failed")
    require(
        diagnostics.get("history_generation", {}).get("id") == proof["generation"]["id"],
        "Reset generation mismatch",
    )
    require(
        hashlib.sha256((app / ".env").read_bytes()).hexdigest() == permit["env_sha256"],
        "Runtime config modified",
    )
    source = app / "data/sqlite/cryptowatcher.sqlite3"
    require(source.stat().st_ino == record["reset_new_inode"], "New database changed unexpectedly")
    phase("RESET_DISCARD_OLD_TEST_HISTORY")
    for value in record["reset_discard_scope"]:
        path = Path(value["path"])
        require(
            path.parent == Path(record["reset_directory"])
            and re.fullmatch(r"discarded-market\.sqlite3(?:-wal|-shm|-journal)?", path.name) is not None,
            "Unexpected discard target",
        )
        require(
            path.is_file()
            and not path.is_symlink()
            and identity(path) == value["identity"]
            and not source_handles(path),
            "Old test file identity/handles changed",
        )
    # These are only the former working DB and its sidecars, temporarily renamed
    # in this reset. No existing backup, rehearsal or experimental file is touched.
    for value in record["reset_discard_scope"]:
        path = Path(value["path"])
        require(not path.is_symlink() and identity(path) == value["identity"], "Discard identity changed")
        path.unlink()
    with _directory_sync(Path(record["reset_directory"])):
        pass
    record["reset_old_working_files_removed"] = len(record["reset_discard_scope"])
    permit.update(state="CONSUMED", completed_at=time.time())
    store_permit(permit)
    phase("RESET_COMPLETE")
