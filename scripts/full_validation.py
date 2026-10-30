"""Explicit exact-main Linux/native regression profile. Never deploys or publishes."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = (
    ("native", "Pinned native helpers: same build_native command as required CI"),
    ("quality", "Canonical docs, Ruff, formatting and mypy"),
    ("tests", "Whole supported pytest inventory, including recovery/negative cases; JUnit records skips"),
    ("image_build", "Exact-revision pinned Dockerfile; isolated qualification image"),
    ("image_checks", "Real non-root image, migration/private restore/reset/restart/storage/backup fixtures"),
    ("package_round_trip", "Local archive identity/hash/load round trip; no publication/delivery"),
    ("storage_boundary", "Canonical joint native numeric/shared qualification with its unchanged limits"),
)


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def preflight(revision: str, directory: Path) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("An exact40-character main SHA is required")
    if sys.platform != "linux" or sys.version_info[:2] != (3, 11):
        raise ValueError("Full profile requires Linux/Python3.11; no platform fallback")
    if git("rev-parse", "HEAD") != revision or git("rev-parse", "refs/remotes/origin/main") != revision:
        raise ValueError("HEAD and freshly fetched origin/main must match expected revision")
    if git("status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Full profile requires unchanged tracked source")
    cache = ROOT / ".cache-g1"
    if cache.is_symlink():
        raise ValueError("Symlinked report cache is not an owned validation target")
    parent = cache.resolve()
    if directory.exists() or directory.is_symlink() or parent == directory or parent not in directory.parents:
        raise ValueError("New report directory inside project .cache-g1 required")
    if any(shutil.which(tool) is None for tool in ("gcc", "bash", "docker")):
        raise ValueError("Native compiler/bash/local Docker prerequisites missing")
    # Docker context selection overrides DOCKER_HOST; inspect the endpoint
    # that the commands will actually use, not an unused local-looking value.
    context = os.environ.get("DOCKER_CONTEXT")
    inspect = [
        "docker",
        "context",
        "inspect",
        *([context] if context else []),
        "--format",
        "{{.Endpoints.docker.Host}}",
    ]
    host = (
        subprocess.check_output(inspect, text=True).strip()
        if context
        else os.environ.get("DOCKER_HOST") or subprocess.check_output(inspect, text=True).strip()
    )
    if not host.startswith("unix://"):
        raise ValueError("Full profile refuses remote/shared VPS Docker transports")
    server = subprocess.check_output(["docker", "version", "--format", "{{.Server.Os}}"], text=True).strip()
    if server != "linux":
        raise ValueError("Full profile requires a local Linux Docker daemon")
    return {
        "python": sys.version,
        "platform": sys.platform,
        "revision": revision,
        "tree": git("rev-parse", "HEAD^{tree}"),
        "free_filesystem_bytes": shutil.disk_usage(ROOT).free,
        "docker_transport": "LOCAL_UNIX",
        "environment_scope": "isolated fixtures; no VPS or live APIs",
    }


def commands(revision: str, directory: Path, image: str, run_id: str) -> list[tuple[str, list[str]]]:
    py = sys.executable
    return [
        ("native", [py, "scripts/build_native.py"]),
        ("quality", [py, "scripts/checks.py", "quality"]),
        (
            "tests",
            [
                py,
                "scripts/checks.py",
                "test",
                "--basetemp",
                str(directory / "pytest-temp"),
                "--junitxml",
                str(directory / "pytest.xml"),
            ],
        ),
        (
            "image_build",
            [
                "docker",
                "build",
                "--iidfile",
                str(directory / "image-id"),
                "--build-arg",
                "SOURCE_REVISION=" + revision,
                "--build-arg",
                "BUILD_RUN=" + run_id,
                "-t",
                image,
                ".",
            ],
        ),
        ("image_checks", [py, "scripts/image_checks.py", image, revision]),
        (
            "package_round_trip",
            [py, "scripts/package_release.py", revision, run_id, image, str(directory / "package")],
        ),
        ("storage_boundary", [py, "scripts/qualify_numeric_boundary.py"]),
    ]


def junit(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"state": "NOT_RUN"}
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return {"state": "INVALID_XML"}
    cases = list(root.iter("testcase"))
    return {
        "state": "RECORDED",
        "tests": len(cases),
        "failed": sum(c.find("failure") is not None or c.find("error") is not None for c in cases),
        "skipped": [
            {
                "case": c.get("classname", "") + "." + c.get("name", ""),
                "reason": c.find("skipped").get("message", ""),
            }
            for c in cases
            if c.find("skipped") is not None
        ],
    }


def execute(revision: str, directory: Path) -> int:
    environment = preflight(revision, directory)
    run_id = os.environ.get("GITHUB_RUN_ID") or str(time.time_ns())
    if not re.fullmatch(r"[1-9][0-9]*", run_id):
        raise ValueError("Invalid qualification build identity")
    directory.mkdir(parents=True, exist_ok=False)
    image = "cryptowatcher-full:" + uuid.uuid4().hex
    record: dict[str, Any] = {
        "schema": "cryptowatcher.full-validation.v1",
        "kind": "QUALIFICATION_ONLY_NO_CD",
        "environment": environment,
        "inventory": list(INVENTORY),
        "stages": [],
        "state": "RUNNING",
        "not_included": [
            "production/VPS acceptance",
            "live Telegram/agent",
            "visual UI",
            "30min/24h observation",
        ],
        "residuals": [],
    }
    result = 1
    try:
        for name, command in commands(revision, directory, image, run_id):
            started = time.monotonic()
            with (directory / (name + ".log")).open("wb") as output:
                finished = subprocess.run(
                    command, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT, check=False
                )
            if name == "tests" and finished.returncode == 0:
                cases = junit(directory / "pytest.xml")
                if cases.get("state") != "RECORDED" or not cases.get("tests") or cases.get("failed"):
                    finished.returncode = 1
            row = {
                "stage": name,
                "exit_code": finished.returncode,
                "seconds": time.monotonic() - started,
                "state": "PASS" if finished.returncode == 0 else "FAIL",
                "log": name + ".log",
                "command": command,
            }
            record["stages"].append(row)
            print(json.dumps(row), flush=True)
            if finished.returncode:
                result = finished.returncode
                break
        else:
            result = (
                0
                if git("rev-parse", "HEAD") == revision
                and not git("status", "--porcelain", "--untracked-files=no")
                else 1
            )
        record["state"] = "PASS" if result == 0 else "FAIL"
    except Exception as exc:
        record["state"] = "FAIL"
        record["exception_type"] = type(exc).__name__
        result = 1
    finally:
        if record["state"] == "RUNNING":
            record["state"] = "INTERRUPTED"
        record["pytest"] = junit(directory / "pytest.xml")
        # Only exact files created in this fresh qualification directory. Keep
        # small manifests/hashes/logs; never prune Docker caches/base images.
        for name in ("image.tar.gz", "bundle.tar"):
            target = directory / "package" / name
            if target.exists():
                if target.is_file() and not target.is_symlink() and directory in target.resolve().parents:
                    target.unlink()
                else:
                    record["residuals"].append({"file": str(target), "reason": "UNPROVEN_OWNED_PATH"})
        iid = directory / "image-id"
        if iid.exists():
            try:
                actual = json.loads(
                    subprocess.check_output(["docker", "image", "inspect", image], text=True)
                )[0]
                if (
                    actual["Id"] != iid.read_text().strip()
                    or actual["Config"]["Labels"].get("org.opencontainers.image.revision") != revision
                    or actual["Config"]["Labels"].get("io.cryptowatcher.build-run") != run_id
                ):
                    raise ValueError("Image identity changed; preserve it")
                subprocess.run(["docker", "image", "rm", image], check=True, capture_output=True)
            except (ValueError, subprocess.SubprocessError, KeyError) as exc:
                record["residuals"].append({"image": image, "reason": type(exc).__name__})
        record["not_run_stages"] = [
            name for name, _ in INVENTORY if not any(row["stage"] == name for row in record["stages"])
        ]
        record["cleanup_state"] = "PARTIAL" if record["residuals"] else "OWNED_OUTPUTS_CLEANED"
        record["native_builds_and_ordinary_docker_cache"] = "RETAINED; not a global cleanup authorization"
        (directory / "result.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision")
    parser.add_argument("--report-directory", type=Path)
    parser.add_argument("--plan", action="store_true")
    args = parser.parse_args()
    if args.plan:
        print(
            json.dumps(
                {
                    "state": "PLAN_NOT_EXECUTED",
                    "inventory": list(INVENTORY),
                    "prerequisites": "fresh exact origin/main, clean checkout, Linux/Python3.11, locked dev dependencies, gcc/bash/local Docker",
                }
            )
        )
        return 0
    if not args.revision or args.report_directory is None:
        parser.error(
            "--revision and --report-directory are required; execution needs separate owner instruction"
        )
    return execute(args.revision, args.report_directory.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
