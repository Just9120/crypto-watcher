"""Single capped Linux/native whole-path qualification; never accesses a VPS."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CPU_SECONDS = 450
WALL_SECONDS = 510
OUTER_SECONDS = 600
MEMORY = 4 * 1024**3
RESERVE = 1536 * 1024**2
DISK_RESERVE = 6 * 1024**3
MAX_WORK = 1024**3
SEED = "3154c9e1a468e64ee5a0d6d925a581a52710e0f5891d0ea5354a04cc4bf62419"
OFFERED = "5bc73caa8c3f9acc937b649ad6c8b5d610d288944a13019b3f0ac19fe80ec84b"


def docker(*args: str, timeout: float = 10) -> str:
    return subprocess.check_output(
        ["docker", "--host", "unix:///var/run/docker.sock", *args], text=True, timeout=timeout
    )


def observe(identifier: str) -> dict:
    """Retry observation of the same object; a timeout never creates another run."""
    for attempt in range(3):
        try:
            return json.loads(docker("inspect", identifier, timeout=8))[0]
        except (subprocess.SubprocessError, json.JSONDecodeError):
            if attempt == 2:
                raise
            time.sleep(0.5)
    raise RuntimeError("Unreachable observation state")


def interval(seconds: float) -> dict:
    def cpu():
        return list(map(int, Path("/proc/stat").read_text().splitlines()[0].split()[1:]))

    before = cpu()
    time.sleep(seconds)
    change = [b - a for a, b in zip(before, cpu(), strict=True)]
    available = (
        int(
            next(
                v.split()[1]
                for v in Path("/proc/meminfo").read_text().splitlines()
                if v.startswith("MemAvailable:")
            )
        )
        * 1024
    )
    return {
        "idle_pct": change[3] * 100 / sum(change[:8]),
        "iowait_pct": change[4] * 100 / sum(change[:8]),
        "available_bytes": available,
    }


def admit(samples: list[dict], free: int) -> bool:
    return (
        len(samples) >= 6
        and free >= MAX_WORK + DISK_RESERVE
        and all(s["idle_pct"] >= 35 and s["available_bytes"] >= MEMORY + RESERVE for s in samples[-6:])
    )


def verdict(result: dict) -> list[str]:
    # Reuse the exact existing joint assertions, including freshness, maintenance,
    # unique families, independent delivery and the exclusion of future horizons.
    from tests.test_processing_replay import assert_joint_result

    failures = []
    try:
        assert_joint_result(result)
    except (AssertionError, KeyError, TypeError):
        failures.append("JOINT_ASSERTIONS_FAILED_OR_INCOMPLETE")
    if result.get("native_backend") is not True:
        failures.append("NATIVE_REQUIRED")
    if result.get("streams") != 1648 or result.get("offered_closed") != 2472:
        failures.append("BROAD_INPUT_CHANGED")
    if result.get("seed_sha256") != SEED or result.get("offered_sha256") != OFFERED:
        failures.append("FROZEN_INPUT_IDENTITY_CHANGED")
    return failures


def case(report: Path) -> int:
    sys.path.insert(0, str(ROOT))
    from scripts.qualify_framed_backup import require_native_limits
    from tests.processing_replay import replay

    require_native_limits()
    if sys.version_info[:2] != (3, 11) or report.exists() or report.parent != Path("/work"):
        raise ValueError("New Linux3.11 /work report required")

    def expire(*_):
        raise TimeoutError("PROCESSING_WALL_BUDGET")

    signal.signal(signal.SIGALRM, expire)
    signal.alarm(WALL_SECONDS)
    with tempfile.TemporaryDirectory(prefix="paced-", dir="/work") as temporary:
        result = asyncio.run(
            replay(
                Path(temporary) / "processing.sqlite3",
                instruments=206,
                seconds=130,
                progress=lambda value: print(json.dumps(value), file=sys.stderr, flush=True),
            )
        )
    failures = verdict(result)
    payload = {
        "schema": "cryptowatcher.processing-qualification.v1",
        "source": os.environ.get("SOURCE_REVISION"),
        "state": "PASS" if not failures else "FAIL",
        "failures": failures,
        "temporary_database_removed": True,
        "result": result,
        "limitations": ["Isolated mock transports; shared-VPS30min/24h acceptance remains required"],
    }
    report.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    signal.alarm(0)
    return int(bool(failures))


def run(image: str, revision: str, directory: Path) -> int:
    if sys.platform != "linux" or os.geteuid() != 0 or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Linux CI root controller and exact revision required")
    if directory.exists() or directory.is_symlink():
        raise ValueError("New owned report directory required")
    inspected = json.loads(docker("image", "inspect", image))[0]
    if inspected["Config"]["Labels"]["org.opencontainers.image.revision"] != revision:
        raise ValueError("Image source mismatch")
    directory.mkdir(parents=True, mode=0o755)
    work = directory / "work"
    work.mkdir(mode=0o700)
    os.chown(work, 10001, 10001)
    proof = {
        "source": revision,
        "image": inspected["Id"],
        "state": "PREPARED",
        "admission": [],
        "limits": {
            "cpu": 1,
            "memory_bytes": MEMORY,
            "cpu_seconds": CPU_SECONDS,
            "wall_seconds": WALL_SECONDS,
            "outer_seconds": OUTER_SECONDS,
        },
    }
    began = time.monotonic()

    def expire(*_):
        raise TimeoutError("CONTROLLER_OUTER_BUDGET")

    signal.signal(signal.SIGALRM, expire)
    signal.alarm(570)
    for _ in range(12):
        proof["admission"].append(interval(5))
        if admit(proof["admission"], shutil.disk_usage(directory).free):
            break
    if not admit(proof["admission"], shutil.disk_usage(directory).free):
        proof["state"] = "NOT_STARTED_RESOURCE_ADMISSION"
        (directory / "controller.json").write_text(json.dumps(proof, indent=2))
        signal.alarm(0)
        return 2
    name = "cw-ci-processing-" + uuid.uuid4().hex
    created = False
    process = None
    try:
        cid = docker(
            "create",
            "--name",
            name,
            "--label",
            "io.cryptowatcher.qualifier-owner=" + name,
            "--network=none",
            "--cpus=1",
            "--memory=4g",
            "--memory-swap=4g",
            "--ulimit=cpu=450:450",
            "--pids-limit=64",
            "--user=10001:10001",
            "--read-only",
            "--no-healthcheck",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=16m,mode=1777",
            "--mount",
            f"type=bind,src={work.resolve()},dst=/work",
            "--entrypoint=python",
            inspected["Id"],
            "-B",
            "scripts/qualify_processing.py",
            "case",
            "--report",
            "/work/result.json",
        ).strip()
        created = True
        proof["container_id"] = cid
        proof["monitor"] = []
        with (directory / "stdout.log").open("x") as out, (directory / "stderr.log").open("x") as err:
            process = subprocess.Popen(
                ["docker", "--host", "unix:///var/run/docker.sock", "start", "-a", cid],
                stdout=out,
                stderr=err,
            )
            case_started = time.monotonic()
            while process.poll() is None:
                sample = interval(3)
                status = observe(cid)
                if status["State"]["Running"]:
                    pid = status["State"]["Pid"]
                    membership = next(
                        v[3:]
                        for v in Path(f"/proc/{pid}/cgroup").read_text().splitlines()
                        if v.startswith("0::")
                    )
                    cg = Path("/sys/fs/cgroup" + membership)
                    stats = dict(v.split() for v in (cg / "cpu.stat").read_text().splitlines())
                    sample.update(
                        cpu_seconds=int(stats["usage_usec"]) / 1e6,
                        memory_peak_bytes=int((cg / "memory.peak").read_text()),
                        io_stat=(cg / "io.stat").read_text(),
                    )
                size = 0
                for path in work.rglob("*"):
                    try:
                        if path.is_file():
                            size += path.stat().st_size
                    except FileNotFoundError:
                        pass  # The fixture may close/delete a WAL between these reads.
                sample["work_file_bytes"] = size
                proof["monitor"].append(sample)
                reason = None
                if sample["idle_pct"] < 15:
                    reason = "STOP_HOST_IDLE_BELOW15"
                elif sample["available_bytes"] < RESERVE:
                    reason = "STOP_HOST_MEMORY_RESERVE"
                elif shutil.disk_usage(directory).free < DISK_RESERVE or size > MAX_WORK:
                    reason = "STOP_DISK_BUDGET"
                elif sample.get("cpu_seconds", 0) >= 420:
                    reason = "STOP_TOTAL_CPU_BUDGET"
                elif (
                    time.monotonic() - began >= OUTER_SECONDS
                    or time.monotonic() - case_started >= WALL_SECONDS
                ):
                    reason = "STOP_WALL_BUDGET"
                if reason:
                    proof["state"] = reason
                    if status["State"]["Running"]:
                        docker("kill", cid)
                    break
            process.wait(timeout=15)
        proof["exit_code"] = process.returncode
        if (work / "result.json").exists():
            result = json.loads((work / "result.json").read_bytes())
            if result.get("source") != revision:
                raise ValueError("Result source mismatch")
            (directory / "result.json").write_text(json.dumps(result, indent=2))
            proof["result_state"] = result.get("state")
        if proof["state"] == "PREPARED":
            proof["state"] = (
                "PASS" if process.returncode == 0 and proof.get("result_state") == "PASS" else "FAIL"
            )
    except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError) as exc:
        proof.update(state="CONTROLLER_FAILED", error=type(exc).__name__)
    finally:
        signal.alarm(25)
        if created:
            status = observe(name)
            if (
                status["Id"] != proof["container_id"]
                or status["Config"]["Labels"].get("io.cryptowatcher.qualifier-owner") != name
            ):
                raise ValueError("Owned container identity mismatch")
            proof["container_state"] = status["State"]
            proof["container_limits"] = {
                key: status["HostConfig"].get(key)
                for key in ("NanoCpus", "Memory", "MemorySwap", "NetworkMode", "ReadonlyRootfs")
            }
            if status["State"]["Running"]:
                docker("kill", name)
            docker("rm", name)
        proof["elapsed_seconds"] = time.monotonic() - began
        (directory / "controller.json").write_text(json.dumps(proof, indent=2))
        # Only this new synthetic work directory; reports remain outside it.
        shutil.rmtree(work)
        proof["fixture_cleanup"] = "COMPLETE"
        (directory / "controller.json").write_text(json.dumps(proof, indent=2))
        signal.alarm(0)
    print(json.dumps({key: proof.get(key) for key in ("state", "result_state", "elapsed_seconds")}))
    return 0 if proof["state"] == "PASS" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inner = commands.add_parser("case")
    inner.add_argument("--report", type=Path, required=True)
    outer = commands.add_parser("run")
    outer.add_argument("image")
    outer.add_argument("revision")
    outer.add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.command == "case":
        return case(args.report)
    return run(args.image, args.revision, args.directory.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
