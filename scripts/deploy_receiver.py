#!/usr/bin/env python3
"""Root-owned forced SSH command, restricted to the established CryptoWatcher unit.

Install only by the reviewed first-delivery procedure. No shell/eval, Git reset,
state import, data restore, SSH configuration or unrelated service operations.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

if __package__:
    from . import reset_delivery
else:
    import reset_delivery

APP = Path("/opt/crypto_watcher")
TARGET = Path("/etc/cryptowatcher/deploy-target.json")
NEW_WRITE_PERMIT = Path("/etc/cryptowatcher/new-write-permit.json")
NEW_WRITE_POLICY = "OWNER_APPROVED_NEW_WRITES_ONLY_V1"
MAX_BUNDLE = 1024 * 1024 * 1024
MEMBERS = {"manifest.json", "image.tar.gz", "docker-compose.yml"}
ACTIVE_RECORD: dict[str, Any] | None = None
ACTIVE_PATH: Path | None = None
OPERATION_DEADLINE: float | None = None
HARD_DEADLINE: float | None = None
# Above the pinned disk validator's mmap range, use the existing bounded
# frozen image; its3GiB compressed cap and4GiB process cap fail closed.
SCHEMA2_DISK_MAX_BYTES = 3 * 1024**3


class DeploymentError(RuntimeError):
    pass


def record_phase(phase: str, *, failure: str | None = None) -> None:
    if ACTIVE_RECORD is None or ACTIVE_PATH is None:
        return
    previous_phase = ACTIVE_RECORD.get("phase")
    ACTIVE_RECORD.update(phase=phase, phase_at=time.time())
    if failure is not None:
        ACTIVE_RECORD.update(
            state="POST_CHECK_FAILED" if ACTIVE_RECORD.get("switch_started") else "FAILED_PRE_SWITCH",
            error_class=failure,
            failure_phase=previous_phase,
        )
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=ACTIVE_PATH.parent, delete=False
    ) as output:
        output.write(json.dumps(ACTIVE_RECORD, sort_keys=True))
        output.flush()
        os.fsync(output.fileno())
        temporary = Path(output.name)
    os.replace(temporary, ACTIVE_PATH)


def database_timeout(size: int) -> int:
    # The 4.82 GB production snapshot took 614s for full integrity with validation I/O tuning.
    # Include copy/validation margin while retaining the 900s stage cap and every safety check.
    return min(900, 180 + ((size + (1 << 30) - 1) // (1 << 30)) * 150)


def validation_resources(size: int, available: int) -> dict[str, int]:
    require(type(size) is int and size > 0, "Validated snapshot size unavailable")
    require(type(available) is int and available > 0, "Host memory evidence unavailable")
    mib = 1024 * 1024
    # Compressed pages are capped at 3 GiB; leave 1 GiB for SQLite/interpreter/cache.
    # Incompressible input fails inside validation; no fallback or weaker check is allowed.
    limit = 4096 * mib
    reserve = 1536 * mib
    require(available >= limit + reserve, "Insufficient available RAM for full validation")
    return {
        "snapshot_bytes": size,
        "scratch_bytes": 64 * mib,
        "memory_limit_bytes": limit,
        "host_available_bytes": available,
        "host_reserve_bytes": reserve,
    }


def memory_available() -> int:
    values = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    return int(values["MemAvailable"].split()[0]) * 1024


def validation_args(container_args: list[str], allocation: dict[str, int]) -> list[str]:
    return [
        *container_args[:-1],
        "--memory",
        str(allocation["memory_limit_bytes"]),
        "--memory-swap",
        str(allocation["memory_limit_bytes"]),
        "--cpus",
        "1",
        "--tmpfs",
        "/validation"
        + f":rw,noexec,nosuid,nodev,size={allocation['scratch_bytes']},mode=0700,uid=10001,gid=10001",
        container_args[-1],
    ]


def stop_candidate() -> None:
    if ACTIVE_RECORD is None or not ACTIVE_RECORD.get("candidate_name"):
        return
    name = ACTIVE_RECORD["candidate_name"]
    require(bool(re.fullmatch(r"cw-verify-[0-9a-f]{12}-[0-9a-f]{16}", name)), "Unknown candidate")
    result = subprocess.run(["docker", "inspect", name], capture_output=True, text=True, timeout=15)
    if result.returncode:
        return
    container = json.loads(result.stdout)[0]
    require(container["Image"] == ACTIVE_RECORD.get("image_id"), "Candidate image changed")
    require(container["Name"] == "/" + name, "Candidate identity changed")
    require(
        not (container["Config"].get("Labels") or {}).get("com.docker.compose.project"),
        "Not an offline candidate",
    )
    run(["docker", "stop", "--time", "5", container["Id"]], timeout=15)


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise DeploymentError(reason)


def fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def command_arguments(command: str) -> tuple[str, str, int]:
    match = re.fullmatch(
        r"cryptowatcher-deploy ([0-9a-f]{40}) ([0-9a-f]{64}) ([1-9][0-9]{0,9})(?: ([1-9][0-9]{9}))?", command
    )
    require(match is not None, "Unsupported SSH operation")
    assert match is not None
    size = int(match[3])
    require(size <= MAX_BUNDLE, "Bundle exceeds limit")
    return match[1], match[2], size


def extract_bundle(bundle: Path, destination: Path) -> dict[str, Any]:
    with tarfile.open(bundle, "r:") as archive:
        members = archive.getmembers()
        require(len(members) == 3 and {item.name for item in members} == MEMBERS, "Invalid bundle members")
        for item in members:
            require(item.isfile() and item.size > 0, "Non-regular bundle member")
            limit = MAX_BUNDLE if item.name == "image.tar.gz" else 65536
            require(item.size <= limit, "Bundle member exceeds limit")
            source = archive.extractfile(item)
            require(source is not None, "Missing bundle member")
            assert source is not None
            with source, (destination / item.name).open("xb") as output:
                shutil.copyfileobj(source, output)
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    require(isinstance(manifest, dict), "Invalid manifest")
    require(manifest.get("schema") == "cryptowatcher.release.v1", "Unknown release schema")
    require(
        manifest.get("database_schema") == 1 and manifest.get("migration_class") == "NONE",
        "Stateful operation needs a new explicit gate",
    )
    require(
        bool(re.fullmatch(r"sha256:[0-9a-f]{64}", str(manifest.get("image_id")))), "Invalid image identity"
    )
    for name, field in (("image.tar.gz", "image_archive_sha256"), ("docker-compose.yml", "compose_sha256")):
        require(fingerprint(destination / name) == manifest.get(field), "Artifact hash mismatch")
    return manifest


def run(args: list[str], *, env: dict[str, str] | None = None, timeout: int = 120) -> str:
    if OPERATION_DEADLINE is not None:
        remaining = OPERATION_DEADLINE - time.monotonic()
        require(remaining > 0, "Deployment900s budget exhausted")
        timeout = min(timeout, max(1, int(remaining)))
    command = [args[0], "--host", "unix:///var/run/docker.sock", *args[1:]] if args[0] == "docker" else args
    try:
        result = subprocess.run(
            command, env=env, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        if ACTIVE_RECORD is not None:
            ACTIVE_RECORD["failure_code"] = "TIME_BUDGET_EXCEEDED"
        raise DeploymentError("Operation time budget exceeded") from exc
    # Never echo subprocess stderr or env-file/config contents into Actions logs.
    require(result.returncode == 0, f"Operation failed: {args[0]} exit {result.returncode}")
    return result.stdout.strip()


def archive_image_identity(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Derive config and OCI identities from the verified single-image archive.

    Classic Docker addresses config digests; containerd addresses manifest
    digests. Neither a tag alone nor source labels prove artifact identity.
    """
    revision = manifest.get("source_sha")
    build_run = manifest.get("build_run")
    require(bool(re.fullmatch(r"[0-9a-f]{40}", str(revision))), "Invalid source identity")
    require(type(build_run) is int and build_run > 0, "Invalid build identity")
    image_ref = f"cryptowatcher:{revision}"
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        require(len({m.name for m in members}) == len(members), "Duplicate image archive member")
        by_name = {m.name: m for m in members}

        def read(name: str) -> bytes:
            member = by_name.get(name)
            require(
                member is not None and member.isfile() and 0 < member.size <= 1024 * 1024,
                "Invalid image metadata member",
            )
            stream = archive.extractfile(member)
            require(stream is not None, "Missing image metadata")
            assert stream is not None
            with stream:
                return stream.read()

        entries = json.loads(read("manifest.json"))
        require(isinstance(entries, list) and len(entries) == 1, "Expected one archived image")
        entry = entries[0]
        require(isinstance(entry, dict) and entry.get("RepoTags") == [image_ref], "Archived tag mismatch")
        config_name = entry.get("Config")
        require(
            isinstance(config_name, str)
            and bool(re.fullmatch(r"(?:blobs/sha256/[0-9a-f]{64}|[0-9a-f]{64}\.json)", config_name)),
            "Invalid archived config path",
        )
        config_bytes = read(config_name)
        config_digest = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
        expected_config_path = (
            "blobs/sha256/" + config_digest[7:]
            if config_name.startswith("blobs/")
            else config_digest[7:] + ".json"
        )
        require(config_name == expected_config_path, "Archived config digest mismatch")
        config = json.loads(config_bytes)
        require(isinstance(config, dict), "Invalid archived config")
        require(
            config.get("os") == "linux" and config.get("architecture") == manifest.get("architecture"),
            "Archived platform mismatch",
        )
        settings = config.get("config", {})
        require(settings.get("User") == "10001:10001", "Unexpected archived application user")
        labels = settings.get("Labels", {})
        require(
            labels.get("org.opencontainers.image.revision") == revision
            and labels.get("io.cryptowatcher.build-run") == str(build_run),
            "Archived source/build mismatch",
        )
        layers = entry.get("Layers")
        diff_ids = config.get("rootfs", {}).get("diff_ids")
        require(
            isinstance(layers, list)
            and isinstance(diff_ids, list)
            and len(layers) == len(diff_ids) > 0
            and all(isinstance(d, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", d) for d in diff_ids)
            and all(isinstance(n, str) and n in by_name and by_name[n].isfile() for n in layers),
            "Archived root filesystem mismatch",
        )
        manifest_digest = None
        if "index.json" in by_name:
            index = json.loads(read("index.json"))
            descriptors = index.get("manifests", [])
            require(len(descriptors) == 1, "Expected one OCI manifest")
            descriptor = descriptors[0]
            require(
                descriptor.get("mediaType") == "application/vnd.oci.image.manifest.v1+json"
                and bool(re.fullmatch(r"sha256:[0-9a-f]{64}", str(descriptor.get("digest")))),
                "Unsupported OCI image descriptor",
            )
            manifest_digest = descriptor["digest"]
            oci_bytes = read("blobs/sha256/" + manifest_digest[7:])
            require(
                "sha256:" + hashlib.sha256(oci_bytes).hexdigest() == manifest_digest
                and len(oci_bytes) == descriptor.get("size"),
                "OCI manifest digest mismatch",
            )
            oci = json.loads(oci_bytes)
            require(oci.get("config", {}).get("digest") == config_digest, "OCI config binding mismatch")
            require(oci.get("config", {}).get("size") == len(config_bytes), "OCI config size mismatch")
            require(
                ["blobs/sha256/" + d["digest"][7:] for d in oci.get("layers", [])] == layers,
                "OCI layer binding mismatch",
            )
    identities = {config_digest}
    if manifest_digest:
        identities.add(manifest_digest)
    require(manifest.get("image_id") in identities, "Build image identity differs from archive")
    for field, value in (("image_config_digest", config_digest), ("image_manifest_digest", manifest_digest)):
        require(field not in manifest or manifest[field] == value, "Release image provenance mismatch")
    return {
        "image_ref": image_ref,
        "config_digest": config_digest,
        "manifest_digest": manifest_digest,
        "identities": identities,
        "diff_ids": diff_ids,
    }


def loaded_image_id(identity: dict[str, Any], manifest: dict[str, Any]) -> str:
    """Resolve the tag only to validate it, then return the immutable host ID."""
    info = json.loads(run(["docker", "image", "inspect", identity["image_ref"]]))[0]
    require(info["Id"] in identity["identities"], "Loaded image differs from verified archive")
    require(
        info["Os"] == "linux" and info["Architecture"] == manifest["architecture"],
        "Loaded platform mismatch",
    )
    require(info["RootFS"]["Layers"] == identity["diff_ids"], "Loaded root filesystem mismatch")
    require(info["Config"]["User"] == "10001:10001", "Unexpected application user")
    labels = info["Config"].get("Labels", {})
    require(
        labels.get("org.opencontainers.image.revision") == manifest["source_sha"]
        and labels.get("io.cryptowatcher.build-run") == str(manifest["build_run"]),
        "Loaded source/build mismatch",
    )
    return info["Id"]


def main_revision() -> str:
    request = urllib.request.Request(
        "https://api.github.com/repos/Just9120/crypto-watcher/git/ref/heads/main",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "CryptoWatcher-CD"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.load(response)
    revision = payload["object"]["sha"]
    require(bool(re.fullmatch(r"[0-9a-f]{40}", revision)), "Invalid main record")
    return revision


def new_write_permit(revision: str) -> dict[str, Any] | None:
    if not NEW_WRITE_PERMIT.exists():
        require(not NEW_WRITE_PERMIT.is_symlink(), "Dangling permit")
        return None
    require(NEW_WRITE_PERMIT.resolve() == NEW_WRITE_PERMIT, "Symlink permit")
    meta = NEW_WRITE_PERMIT.stat()
    require(meta.st_uid == 0 and meta.st_mode & 0o077 == 0 and meta.st_size <= 8192, "Unsafe permit")
    permit = json.loads(NEW_WRITE_PERMIT.read_text(encoding="utf-8"))
    require(permit.get("schema") == NEW_WRITE_POLICY, "Unknown permit")
    if permit.get("state") == "CONSUMED":
        return None
    require(permit.get("state") == "ARMED" and permit.get("pr") in (31, 33, 34, 35), "Unapproved permit")
    if permit.get("pr") == 33:
        require(
            permit.get("previous_revision") == "047c66e5bb9ef5b9c0650ef91d2e5b92275072c4",
            "Unreviewed compatibility baseline",
        )
    if permit.get("pr") == 34:
        require(
            permit.get("previous_revision") == "ef5daeb08a253c4062beed375f90f3b6628843a2",
            "Unreviewed compatibility baseline",
        )
    if permit.get("pr") == 35:
        require(
            permit.get("previous_revision") == "23bb023e3872d5791fb270745635da1048dce682",
            "Unreviewed compatibility baseline",
        )
    require(permit.get("target_sha256") == fingerprint(TARGET), "Permit target changed")
    request = urllib.request.Request(
        f"https://api.github.com/repos/Just9120/crypto-watcher/git/commits/{revision}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "CryptoWatcher-CD"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        commit = json.load(response)
    if commit["tree"]["sha"] != permit.get("tree_sha"):
        return None
    live = json.loads(run(["docker", "inspect", "crypto-watcher"]))[0]
    require(
        live["Id"] == permit.get("previous_container")
        and live["Image"] == permit.get("previous_image")
        and live["State"]["Running"]
        and (live["Config"].get("Labels") or {}).get("org.opencontainers.image.revision")
        == permit.get("previous_revision"),
        "Permit runtime changed",
    )
    return permit


def bounded_args(container_args: list[str], recovery: Path | None = None) -> list[str]:
    args = [*container_args[:-1]]
    data = str(APP / "data") + ":/data"
    require(data in args, "Unexpected additive data mount")
    args[args.index(data)] = data + ":ro"
    args += [
        "--memory",
        "512m",
        "--memory-swap",
        "512m",
        "--cpus",
        "1",
        "--tmpfs",
        "/probe:rw,noexec,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=0700",
    ]
    if recovery is not None:
        args += ["-v", str(recovery) + ":/recovery"]
    return [*args, container_args[-1]]


def additive_preflight(container_args: list[str], release: Path, permit: dict[str, Any]) -> dict[str, Any]:
    recovery = release / "config-recovery"
    recovery.mkdir(mode=0o700)
    os.chown(recovery, 10001, 10001)
    # These sensitive bytes remain on the host and are never part of the response.
    for name, source in (
        ("original.env", APP / ".env"),
        ("original-compose.yml", APP / "docker-compose.yml"),
    ):
        require(source.is_file() and source.resolve() == source, "Unexpected config source")
        destination = recovery / name
        with destination.open("xb") as output:
            output.write(source.read_bytes())
            output.flush()
            os.fsync(output.fileno())
        destination.chmod(0o600)
        require(fingerprint(destination) == fingerprint(source), "Config snapshot mismatch")
    live = json.loads(run(["docker", "inspect", "crypto-watcher"]))[0]
    config_files = (live["Config"].get("Labels") or {}).get("com.docker.compose.project.config_files", "")
    actual_compose = Path(config_files)
    require(
        actual_compose.is_file()
        and actual_compose.resolve() == actual_compose
        and actual_compose.is_relative_to(APP / "releases"),
        "Unknown active Compose file",
    )
    actual_copy = recovery / "active-compose.yml"
    with actual_copy.open("xb") as output:
        output.write(actual_compose.read_bytes())
        output.flush()
        os.fsync(output.fileno())
    actual_copy.chmod(0o600)
    require(fingerprint(actual_copy) == fingerprint(actual_compose), "Active Compose snapshot mismatch")
    result = json.loads(
        run(
            [
                *bounded_args(container_args, recovery),
                "python",
                "-m",
                "cryptowatcher.new_write_check",
                "--destination",
                "/recovery/settings.json",
            ],
            timeout=120,
        )
    )
    require(result.get("state") == "BOUNDED_COMPATIBILITY_PASS", "Bounded compatibility failed")
    receipt = json.loads((APP / "data/sqlite/.g1-migration.json").read_text(encoding="utf-8"))
    require(result.get("legacy_sha256") == receipt["legacy_sha256"], "Migration identity mismatch")
    require(fingerprint(recovery / "settings.json") == result["configuration_sha256"], "Config hash mismatch")
    require(NEW_WRITE_PERMIT.read_bytes() == json.dumps(permit, sort_keys=True).encode(), "Permit changed")
    return result


def usable_poll(poll: dict[str, Any], switch_time: float) -> bool:
    return bool(
        poll.get("at", 0) >= switch_time
        and poll.get("state") in ("OK", "DEGRADED")
        and (poll.get("available", 0) > 0 or poll.get("requested") == 0)
    )


def target_config() -> dict[str, Any]:
    stat = TARGET.stat()
    require(stat.st_uid == 0 and stat.st_mode & 0o022 == 0, "Unsafe target owner/permissions")
    target = json.loads(TARGET.read_text(encoding="utf-8"))
    require(target.get("app_dir") == str(APP), "Wrong application target")
    require(target.get("data_dir") == str(APP / "data"), "Wrong persistent target")
    require(target.get("env_file") == str(APP / ".env"), "Wrong config target")
    require(target.get("service") == "crypto_watcher", "Wrong deployment unit")
    require(
        bool(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", str(target.get("compose_project")))),
        "Invalid Compose project",
    )
    require(target.get("architecture") in ("amd64", "arm64"), "Unknown target architecture")
    require(target.get("automatic_delivery_enabled") is True, "First delivery not authorized yet")
    require(APP.resolve() == APP and (APP / "data").resolve() == APP / "data", "Symlink target unsupported")
    env_stat = (APP / ".env").stat()
    require(env_stat.st_uid == 0 and env_stat.st_mode & 0o077 == 0, "Unsafe config permissions")
    receipt = json.loads((APP / "data/sqlite/.g1-migration.json").read_text(encoding="utf-8"))
    require(
        receipt.get("schema") == 1 and receipt.get("restore_rehearsal") == "PASS",
        "Missing reviewed migration/recovery receipt",
    )
    require(bool(re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("legacy_sha256")))), "Missing legacy identity")
    require((APP / "data/sqlite/cryptowatcher.sqlite3").is_file(), "Migrated database missing")
    return target


def deploy(revision: str, digest: str, size: int, deadline_epoch: int | None = None) -> dict[str, Any]:
    import fcntl

    global ACTIVE_RECORD, ACTIVE_PATH, OPERATION_DEADLINE, HARD_DEADLINE

    remaining = min(900, (deadline_epoch or int(time.time() + 900)) - time.time())
    require(remaining >= 180, "Insufficient remaining delivery budget")
    HARD_DEADLINE = time.monotonic() + remaining
    OPERATION_DEADLINE = HARD_DEADLINE - 30
    if hasattr(signal, "setitimer"):

        def expired(signum: int, frame: Any) -> None:
            raise DeploymentError("Deployment wall-clock budget exhausted")

        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, max(1, remaining - 30))
    require(os.geteuid() == 0, "Receiver needs its approved root account")
    target = target_config()
    releases = APP / "releases"
    releases.mkdir(mode=0o700, exist_ok=True)
    require(releases.resolve() == releases, "Unsafe releases path")
    # Lock is held from candidate selection through final post-checks; never cancel this operation.
    with (APP / ".deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        require(main_revision() == revision, "Candidate is no longer current main")
        require(shutil.disk_usage(APP).free > 3 * size + 512 * 1024 * 1024, "Insufficient deployment disk")
        release = releases / f"{revision}-{digest[:16]}"
        require(not release.exists(), "Release already attempted; inspect record before retry")
        release.mkdir(mode=0o700)
        ACTIVE_PATH = release / "deployment.json"
        ACTIVE_RECORD = {"state": "PRE_SWITCH", "source_sha": revision, "bundle_sha256": digest}
        record_phase("RECEIVE_ARTIFACT")
        bundle = release / "bundle.tar"
        with bundle.open("xb") as output:
            remaining = size
            while remaining:
                chunk = sys.stdin.buffer.read(min(1024 * 1024, remaining))
                require(bool(chunk), "Truncated bundle")
                output.write(chunk)
                remaining -= len(chunk)
            require(not sys.stdin.buffer.read(1), "Unexpected trailing bundle data")
        require(fingerprint(bundle) == digest, "Bundle hash mismatch")
        record_phase("VERIFY_ARTIFACT")
        manifest = extract_bundle(bundle, release)
        require(manifest.get("source_sha") == revision, "Source identity mismatch")
        require(manifest.get("architecture") == target["architecture"], "Target architecture mismatch")
        reset_permit = reset_delivery.load_permit(APP, TARGET, revision, run, require)
        permit = None if reset_permit is not None else new_write_permit(revision)
        if reset_permit is not None:
            require(
                2 in manifest.get("supported_database_schemas", []),
                "Artifact does not support bounded market layout",
            )
            ACTIVE_RECORD.update(
                delivery_policy=reset_delivery.POLICY, reset_permit_tree=reset_permit["tree_sha"]
            )
        if permit is not None:
            ACTIVE_RECORD.update(delivery_policy=NEW_WRITE_POLICY, permit_tree=permit["tree_sha"])
        identity = archive_image_identity(release / "image.tar.gz", manifest)
        record_phase("LOAD_IMAGE")
        run(["docker", "load", "-i", str(release / "image.tar.gz")], timeout=180)
        image = loaded_image_id(identity, manifest)
        ACTIVE_RECORD.update(image_id=image, build_run=manifest["build_run"])
        environment = {
            **os.environ,
            "CW_IMAGE": image,
            "CW_ENV_FILE": target["env_file"],
            "CW_DATA_DIR": target["data_dir"],
            "CW_SOURCE_REVISION": revision,
        }
        compose = [
            "docker",
            "compose",
            "--project-name",
            target["compose_project"],
            "--env-file",
            "/dev/null",
            "-f",
            str(release / "docker-compose.yml"),
        ]
        service_config = json.loads(run([*compose, "config", "--format", "json"], env=environment))[
            "services"
        ]
        require(set(service_config) == {"crypto_watcher"}, "Bundle contains unrelated services")
        service = service_config["crypto_watcher"]
        require(
            service["image"] == image and service.get("container_name") == "crypto-watcher",
            "Wrong service image",
        )
        require(service.get("read_only") is True, "Writable application root")
        volumes = service.get("volumes", [])
        require(
            len(volumes) == 1
            and volumes[0].get("source") == target["data_dir"]
            and volumes[0].get("target") == "/data",
            "Wrong data mount",
        )
        container_args = [
            "docker",
            "run",
            "--rm",
            "--name",
            f"cw-verify-{revision[:12]}-{digest[:16]}",
            "--network=none",
            "--read-only",
            "--no-healthcheck",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--env-file",
            target["env_file"],
            "-e",
            "OWNER_USER_ID=233382575",
            "-e",
            "EXPECTED_BOT_ID=7523865123",
            "-e",
            "DELIVERY_CHAT_ID=233382575",
            "-e",
            f"SOURCE_REVISION={revision}",
            "-e",
            "DATABASE_FILE=/data/sqlite/cryptowatcher.sqlite3",
            "-v",
            f"{target['data_dir']}:/data",
            image,
        ]
        ACTIVE_RECORD["candidate_name"] = f"cw-verify-{revision[:12]}-{digest[:16]}"
        record_phase("CHECK_CONFIG")
        run([*container_args, "python", "-m", "cryptowatcher", "check-config"])
        # CLI command overrides image CMD (which starts with python).
        record_phase("READ_DIAGNOSTICS")
        before = json.loads(run([*container_args, "python", "-m", "cryptowatcher", "diagnostics"]))
        database_size = (APP / "data/sqlite/cryptowatcher.sqlite3").stat().st_size
        budget = database_timeout(database_size)
        ACTIVE_RECORD.update(database_bytes=database_size, database_timeout_seconds=budget)
        if reset_permit is not None:
            require(
                OPERATION_DEADLINE - time.monotonic() >= 480,
                "Insufficient remaining reset/cutover budget",
            )
            proof = reset_delivery.prepare(
                APP, image, reset_permit, run, require, record_phase, ACTIVE_RECORD
            )
            backup_name = None
            ACTIVE_RECORD.update(reset_proof=proof, backup="OLD_TEST_HISTORY_DISCARDED_BY_OWNER_DECISION")
        elif permit is not None:
            record_phase("BOUNDED_COMPATIBILITY")
            proof = additive_preflight(container_args, release, permit)
            backup_name = None
            ACTIVE_RECORD.update(bounded_validation=proof, backup=backup_name)
        else:
            backup_name, proof = full_backup_preflight(container_args, before, revision, budget)
        # Recheck immediately before switching. New main arriving during this operation queues next deploy.
        require(main_revision() == revision, "Candidate changed during preparation")
        previous = json.loads(run(["docker", "inspect", "crypto-watcher"]))[0]
        require(
            previous["Config"]["Labels"].get("com.docker.compose.project") == target["compose_project"],
            "Existing project mismatch",
        )
        require(
            previous["Config"]["Labels"].get("com.docker.compose.service") == "crypto_watcher",
            "Existing service mismatch",
        )
        record = {
            "state": "SWITCHING",
            "source_sha": revision,
            "image_id": image,
            "build_image_id": manifest["image_id"],
            "image_config_digest": identity["config_digest"],
            "image_manifest_digest": identity["manifest_digest"],
            "bundle_sha256": digest,
            "build_run": manifest["build_run"],
            "at": time.time(),
            "previous_image": previous["Image"],
            "backup": "NOT_RUN_OWNER_DISCARD_TEST_HISTORY" if reset_permit is not None else backup_name,
        }
        if permit is None and reset_permit is None:
            record.update(
                backup_validation=proof,
                state_validation_resources=ACTIVE_RECORD["state_validation_resources"],
                backup_validation_resources=ACTIVE_RECORD["backup_validation_resources"],
            )
        ACTIVE_RECORD.update(record, switch_started=True)
        record_phase("SWITCH")
        if reset_permit is not None:
            reset_delivery.promote(APP, reset_permit, require, record_phase, ACTIVE_RECORD)
        run(
            [*compose, "up", "-d", "--no-build", "--pull", "never", "--no-deps", "crypto_watcher"],
            env=environment,
            timeout=180,
        )
        write_cutoff_ms = int(time.time() * 1000)
        deadline = time.monotonic() + 150
        diagnostics = {}
        record_phase("POST_CHECKS")
        while time.monotonic() < deadline:
            current = json.loads(run(["docker", "inspect", "crypto-watcher"]))[0]
            if current["Image"] == image and current["State"].get("Health", {}).get("Status") == "healthy":
                diagnostics = json.loads(
                    run(["docker", "exec", "crypto-watcher", "python", "-m", "cryptowatcher", "diagnostics"])
                )
                poll = diagnostics["last_poll"]
                ingestion = diagnostics.get("ingestion", {})
                if usable_poll(poll, record["at"]) and (
                    permit is None or ingestion.get("diagnostic_state") == "FRESH"
                ):
                    break
            time.sleep(5)
        else:
            ACTIVE_RECORD.update(state="POST_CHECK_FAILED")
            raise DeploymentError("Health post-check failed; forward-fix or reviewed recovery required")
        require(diagnostics["heartbeat"].get("revision") == revision, "Running source mismatch")
        require(
            diagnostics["configuration_revision"] >= before["configuration_revision"],
            "Settings revision regressed",
        )
        if permit is not None:
            post = json.loads(
                run(
                    [
                        *bounded_args(container_args),
                        "python",
                        "-m",
                        "cryptowatcher.new_write_check",
                        "--after-ms",
                        str(write_cutoff_ms),
                    ],
                    timeout=120,
                )
            )
            require(post.get("state") == "BOUNDED_COMPATIBILITY_PASS", "Additive readback failed")
            ACTIVE_RECORD.update(
                additive_post_checks=post, telegram_delivery=diagnostics.get("telegram_delivery", {})
            )
            require(
                NEW_WRITE_PERMIT.read_bytes() == json.dumps(permit, sort_keys=True).encode(),
                "Concurrent permit change",
            )
            permit.update(state="CONSUMED", delivered_revision=revision)
            temporary = NEW_WRITE_PERMIT.with_suffix(".consumed.tmp")
            with temporary.open("x", encoding="utf-8") as output:
                os.chmod(temporary, 0o600)
                output.write(json.dumps(permit, sort_keys=True))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, NEW_WRITE_PERMIT)
            require(json.loads(NEW_WRITE_PERMIT.read_text())["state"] == "CONSUMED", "Permit consume failed")
        if reset_permit is not None:
            record_phase("RESET_PRIVATE_POST_CHECK")
            private = json.loads(
                run(
                    [
                        "docker",
                        "exec",
                        "crypto-watcher",
                        "python",
                        "-B",
                        "-c",
                        "import json; from pathlib import Path; from cryptowatcher.reset_history import verify_private; "
                        "print(json.dumps(verify_private(Path('/data/sqlite/cryptowatcher.sqlite3'),Path("
                        + repr(
                            "/data/sqlite/" + Path(ACTIVE_RECORD["reset_directory"]).name + "/private.json"
                        )
                        + "),233382575)))",
                    ],
                    timeout=30,
                )
            )
            require(private.get("state") == "PASS", "Private configuration post-check failed")
            ACTIVE_RECORD["reset_private_post_check"] = private
            reset_delivery.finish(APP, reset_permit, diagnostics, require, record_phase, ACTIVE_RECORD)
        ACTIVE_RECORD.update(
            state="PASS",
            at=time.time(),
            post_checks="source/image/health/persistence/market PASS",
            data_state=diagnostics["last_poll"]["state"],
            available=diagnostics["last_poll"].get("available"),
            requested=diagnostics["last_poll"].get("requested"),
        )
        record_phase("COMPLETE")
        return dict(ACTIVE_RECORD)


def full_backup_preflight(
    container_args: list[str], before: dict[str, Any], revision: str, budget: int
) -> tuple[str, dict[str, Any]]:
    snapshot_bytes = before.get("database", {}).get("snapshot_bytes")
    require(type(snapshot_bytes) is int and snapshot_bytes > 0, "Validated snapshot size unavailable")
    schema = before.get("database", {}).get("schema")
    require(type(schema) is int and schema in (1, 2), "Unknown database schema")
    if schema == 2 and snapshot_bytes <= SCHEMA2_DISK_MAX_BYTES:
        return disk_backup_preflight(container_args, before, revision, budget)
    deadline = min(
        time.monotonic() + min(budget, 900),
        OPERATION_DEADLINE - 210 if OPERATION_DEADLINE is not None else time.monotonic() + 900,
    )

    def remaining() -> int:
        value = int(deadline - time.monotonic())
        require(value >= 20, "Insufficient full backup budget")
        return value

    record_phase("CHECK_STATE_INTEGRITY")
    allocation = validation_resources(before.get("database", {}).get("snapshot_bytes"), memory_available())
    ACTIVE_RECORD["state_validation_resources"] = allocation
    ACTIVE_RECORD["state_validation_method"] = "COMPRESSED_RAM"
    record_phase("CHECK_STATE_INTEGRITY")
    checks = validation_args(container_args, allocation)
    timeout = remaining()
    state = json.loads(
        run(
            [
                *checks,
                "python",
                "-m",
                "cryptowatcher",
                "state-check",
                "--timeout-seconds",
                str(timeout - 15),
                "--validation-directory",
                "/validation",
                "--compressed-validation",
            ],
            timeout=timeout,
        )
    )
    receipt = json.loads((APP / "data/sqlite/.g1-migration.json").read_text(encoding="utf-8"))
    require(state["legacy_sha256"] == receipt["legacy_sha256"], "Migration identity mismatch")
    require(state.get("schema") == before.get("database", {}).get("schema"), "Database schema changed")
    require(state.get("validation_mode") == "COMPRESSED_RAM", "Full compressed state validation missing")
    ACTIVE_RECORD["state_validation"] = state
    remaining()
    backups = APP / "data/sqlite/backups"
    require(backups.is_dir(), "Reviewed backup directory missing")
    backup_name = f"pre-{revision}-{int(time.time())}.sqlite3.zst"
    # Archived output has the same hard3GiB bound as the validation image;
    # incompressible input fails before switch and leaves its incomplete output.
    require(shutil.disk_usage(APP).free > 3 * 1024**3 + 512 * 1024 * 1024, "Insufficient backup disk")
    ACTIVE_RECORD.update(backup=backup_name)
    record_phase("BACKUP_AND_VALIDATE")
    refreshed = json.loads(run([*container_args, "python", "-m", "cryptowatcher", "diagnostics"]))
    require(refreshed.get("database", {}).get("schema") == schema, "Database schema changed")
    allocation = validation_resources(refreshed.get("database", {}).get("snapshot_bytes"), memory_available())
    ACTIVE_RECORD["backup_validation_resources"] = allocation
    record_phase("BACKUP_AND_VALIDATE")
    timeout = remaining()
    backup = json.loads(
        run(
            [
                *validation_args(container_args, allocation),
                "python",
                "-m",
                "cryptowatcher",
                "backup",
                "--timeout-seconds",
                str(timeout - 15),
                "--destination",
                f"/data/sqlite/backups/{backup_name}",
                "--validation-directory",
                "/validation",
                "--compressed-validation",
                "--archive-backup",
            ],
            timeout=timeout,
        )
    )
    proof = backup.get("backup", {})
    require(
        backup.get("status") == "backup_integrity_ok"
        and proof.get("phase") == "COMPLETE"
        and proof.get("validation_mode") == "COMPRESSED_RAM"
        and proof.get("archive_encoding") == "ZSTD"
        and type(proof.get("archive_bytes")) is int
        and proof["archive_bytes"] > 0
        and bool(re.fullmatch(r"[0-9a-f]{64}", str(proof.get("archive_sha256")))),
        "Full persistent archive identity missing",
    )
    require(
        bool(re.fullmatch(r"[0-9a-f]{64}", str(proof.get("sha256")))),
        "Full persistent backup validation missing",
    )
    ACTIVE_RECORD["backup_validation"] = proof
    record_phase("BACKUP_AND_VALIDATE")
    return backup_name, proof


def disk_backup_preflight(
    container_args: list[str], before: dict[str, Any], revision: str, budget: int
) -> tuple[str, dict[str, Any]]:
    deadline = min(
        time.monotonic() + min(budget, 900),
        OPERATION_DEADLINE - 210 if OPERATION_DEADLINE is not None else time.monotonic() + 900,
    )

    def remaining() -> int:
        value = int(deadline - time.monotonic())
        require(value >= 20, "Insufficient disk backup budget")
        return value

    checks = [
        *container_args[:-1],
        "--memory",
        "4g",
        "--memory-swap",
        "4g",
        "--cpus",
        "1",
        container_args[-1],
    ]
    ACTIVE_RECORD["state_validation_resources"] = {
        "memory_limit_bytes": 4 * 1024**3,
        "cpus": 1,
        "validation_mode": "DISK_STREAMING",
    }
    ACTIVE_RECORD["state_validation_method"] = "DISK_STREAMING"
    record_phase("CHECK_STATE_INTEGRITY_DISK")
    timeout = remaining()
    state = json.loads(
        run(
            [*checks, "python", "-m", "cryptowatcher", "state-check", "--timeout-seconds", str(timeout - 5)],
            timeout=timeout,
        )
    )
    receipt = json.loads((APP / "data/sqlite/.g1-migration.json").read_text())
    require(
        state.get("schema") == 2
        and state.get("legacy_sha256") == receipt["legacy_sha256"]
        and state.get("validation_mode") == "DISK",
        "Disk source validation failed",
    )
    ACTIVE_RECORD["state_validation"] = state
    size = before["database"]["snapshot_bytes"]
    require(shutil.disk_usage(APP).free >= size + 1536 * 1024 * 1024, "Insufficient disk backup reserve")
    name = f"pre-{revision}-{int(time.time())}.sqlite3"
    ACTIVE_RECORD.update(
        backup=name,
        state_validation_resources={
            "memory_limit_bytes": 4 * 1024**3,
            "cpus": 1,
            "validation_mode": "DISK_STREAMING",
        },
        backup_validation_resources={
            "memory_limit_bytes": 4 * 1024**3,
            "cpus": 1,
            "validation_mode": "DISK_STREAMING",
        },
    )
    record_phase("BACKUP_AND_VALIDATE_DISK")
    timeout = remaining()
    result = json.loads(
        run(
            [
                *checks,
                "python",
                "-m",
                "cryptowatcher",
                "backup",
                "--timeout-seconds",
                str(timeout - 5),
                "--destination",
                f"/data/sqlite/backups/{name}",
            ],
            timeout=timeout,
        )
    )
    proof = result.get("backup", {})
    require(
        result.get("status") == "backup_integrity_ok"
        and proof.get("phase") == "COMPLETE"
        and re.fullmatch("[0-9a-f]{64}", str(proof.get("sha256", ""))) is not None,
        "Disk backup identity/integrity missing",
    )
    ACTIVE_RECORD["backup_validation"] = proof
    return name, proof


def main() -> int:
    try:
        revision, digest, size = command_arguments(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
        command = os.environ.get("SSH_ORIGINAL_COMMAND", "").split(" ")
        deadline = int(command[4]) if len(command) == 5 else None
        print(json.dumps(deploy(revision, digest, size, deadline), sort_keys=True))
        return 0
    except Exception as exc:
        record_phase("FAILED", failure=type(exc).__name__)
        try:
            if ACTIVE_RECORD is not None:
                # Recovery of an unchanged old source is a safety action, allowed
                # after a pre-switch timeout. Never restart it after promotion.
                global OPERATION_DEADLINE
                OPERATION_DEADLINE = min(HARD_DEADLINE or time.monotonic() + 30, time.monotonic() + 30)
                if hasattr(signal, "setitimer"):
                    signal.setitimer(signal.ITIMER_REAL, max(1, OPERATION_DEADLINE - time.monotonic()))
                reset_delivery.recover_pre_switch(APP, run, require, ACTIVE_RECORD)
            stop_candidate()
        except Exception as cleanup:
            if ACTIVE_RECORD is not None:
                ACTIVE_RECORD["candidate_cleanup"] = "UNSET/" + type(cleanup).__name__
            record_phase("FAILED")
        print(
            json.dumps(
                {
                    "state": "BLOCKED",
                    "reason": str(exc) if isinstance(exc, DeploymentError) else type(exc).__name__,
                    "failure_phase": ACTIVE_RECORD.get("failure_phase", "UNSET")
                    if ACTIVE_RECORD
                    else "UNSET",
                }
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        if hasattr(signal, "setitimer"):
            signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == "__main__":
    raise SystemExit(main())
