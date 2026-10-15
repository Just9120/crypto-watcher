"""Build a reviewed watchdog invocation; default mode performs preflight only."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
from pathlib import Path, PurePosixPath

from cryptowatcher.maintenance import Unit


def commands(
    runner: PurePosixPath,
    package: PurePosixPath,
    expected: Unit,
    root: PurePosixPath,
    code: PurePosixPath,
    driver_sha256: str,
    worker: str,
    *,
    python: str = "/usr/bin/python3",
) -> tuple[list[str], list[str]]:
    if (
        re.fullmatch(r"[a-f0-9]{64}", expected.container) is None
        or re.fullmatch(r"sha256:[a-f0-9]{64}", expected.image) is None
        or re.fullmatch(r"[a-f0-9]{40}", expected.revision) is None
        or re.fullmatch(r"[a-f0-9]{64}", driver_sha256) is None
        or re.fullmatch(r"cw-closed-qual-[a-f0-9]{32}", worker) is None
    ):
        raise ValueError("Reviewed identities required")
    args = [
        python,
        "-B",
        str(runner),
        "--container",
        expected.container,
        "--image",
        expected.image,
        "--revision",
        expected.revision,
        "--root",
        str(root),
        "--code",
        str(code),
        "--driver-sha256",
        driver_sha256,
        "--worker",
        worker,
    ]
    # These are generated private Linux paths. Restrict the systemd property
    # grammar instead of forwarding shell syntax, specifiers or env assignments.
    for value in (*args, str(package)):
        if re.fullmatch(r"[A-Za-z0-9_./:=+-]+", value) is None:
            raise ValueError("Unsafe watchdog argument")
    for path in (runner, package, root, code):
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("Absolute reviewed paths required")
    if (
        PurePosixPath("/opt/crypto_watcher/rehearsals") not in root.parents
        or root not in package.parents
        or root not in code.parents
        or package not in runner.parents
    ):
        raise ValueError("Reviewed private operation paths required")
    restore = [*args, "--restore-only"]
    launch = [
        "systemd-run",
        "--quiet",
        "--collect",
        "--unit",
        worker,
        "--property=RuntimeMaxSec=900",
        "--property=TimeoutStopSec=10",
        "--property=KillMode=mixed",
        "--property=UMask=0077",
        "--property=Environment=PYTHONPATH=" + str(package),
        "--property=ExecStopPost=" + " ".join(restore),
        "--property=StandardOutput=append:" + str(root / "manager.log"),
        "--property=StandardError=append:" + str(root / "manager.log"),
        *args,
        "--execute",
    ]
    return args, launch


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runner", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--code", type=Path, required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--driver-sha256", required=True)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0 or socket.gethostname() != "vmi3224957":
        raise RuntimeError("Wrong launcher host/account")
    preflight, launch = commands(
        PurePosixPath(str(args.runner)),
        PurePosixPath(str(args.package)),
        Unit(args.container, args.image, args.revision),
        PurePosixPath(str(args.root)),
        PurePosixPath(str(args.code)),
        args.driver_sha256,
        args.worker,
    )
    environment = {**os.environ, "PYTHONPATH": str(args.package)}
    result = subprocess.run(preflight, env=environment, capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise RuntimeError("Maintenance preflight failed; production unchanged")
    proof = json.loads(result.stdout)
    if proof.get("state") != "PREFLIGHT_PASS" or proof.get("production_changed") is not False:
        raise RuntimeError("Maintenance preflight evidence missing")
    if not args.execute:
        print(json.dumps({**proof, "watchdog_unit": args.worker, "started": False}))
        return 0
    # Caller authorization is external to this CLI. An ambiguous dispatch must
    # be inspected through this exact unit/operation record, never relaunched.
    subprocess.run(launch, check=True, capture_output=True, text=True, timeout=15)
    print(json.dumps({"state": "DISPATCHED", "unit": args.worker, "result": "PENDING"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
