"""Counter-only same-host full processing comparison; never deployment."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from scripts import cpu_pair_budget as b
from scripts import qualify_cpu_pair as pair
from scripts import qualify_processing as q

BASELINE = "19e0051b3ed009c90d6badd93a75b14a6d097e6a"


def admission(samples, free, *, cpu_limit):
    if cpu_limit != 1:
        raise ValueError("Unchanged1CPU required")
    return q.admit(samples, free)


def run(images, revision, directory, reverse=False):
    from scripts.qualify_group_pair import comparison

    if (
        sys.platform != "linux"
        or os.geteuid() != 0
        or not re.fullmatch(r"[0-9a-f]{40}", revision)
        or directory.exists()
        or directory.is_symlink()
    ):
        raise ValueError("Exact source and new owned Linux report directory required")
    infos = [json.loads(q.docker("image", "inspect", image))[0] for image in images]
    for info, source, enabled in zip(infos, (BASELINE, revision), ("0", "1"), strict=True):
        labels = info["Config"]["Labels"]
        env = dict(item.split("=", 1) for item in info["Config"]["Env"] if "=" in item)
        if (
            labels.get("org.opencontainers.image.revision") != source
            or labels.get("io.cryptowatcher.measurement-revision") != revision
            or labels.get("io.cryptowatcher.counter-delta") != "temp-sql-ledger-v1"
            or env.get("CW_COUNTER_DELTA") != enabled
        ):
            raise ValueError("Counter source/mode/image identity mismatch")
    directory.mkdir(mode=0o755)
    budget = b.Budget()
    proof = {
        "schema": "cryptowatcher.counter-delta-pair.v1",
        "state": "PREPARED",
        "source": revision,
        "application_baseline": BASELINE,
        "order": "candidate/baseline" if reverse else "baseline/candidate",
        "cases": [],
        "host": {"logical_cpu_count": os.cpu_count(), "kernel": platform.release()},
        "limits": {
            "cpu": 1,
            "memory_bytes": q.MEMORY,
            "cpu_seconds_total": 450,
            "wall_seconds_total": 510,
            "outer_seconds_total": 600,
        },
        "limitations": [
            "Full frozen130s paced input; not steady24h/mature35GB/VPS/live transports",
            "TEMP ledger final flush/restore/reconciliation inside whole case costs",
            "Pair order reversed for repeat; host/cache variation still possible",
        ],
    }

    def expire(*_):
        raise TimeoutError("COUNTER_PAIR_OUTER_BUDGET")

    for sig in (signal.SIGALRM, signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, expire)
    signal.alarm(570)
    try:
        for index in (1, 0) if reverse else (0, 1):
            label = ("baseline", "candidate")[index]
            if budget.reason() or not budget.admit_second():
                proof["state"] = "PAIR_INCOMPLETE_REMAINING_BUDGET"
                break
            case = pair.one(
                infos[index]["Id"],
                revision,
                directory,
                label,
                1,
                budget,
                expected_revision=(BASELINE, revision)[index],
                admission=admission,
                supervisor_module="scripts.qualify_group_pair",
            )
            if case.get("state") == "CASE_COMPLETE":
                counter = case["result"].get("counter_delta", {})
                if counter.get("enabled") is not bool(index) or (
                    index
                    and (
                        counter.get("activations") != 1
                        or counter.get("flushes", 0) <= 0
                        or counter.get("durable_counter_changes", 0) <= 0
                    )
                ):
                    case.update(state="CASE_INCOMPLETE", error="COUNTER_MODE_OR_PATH_NOT_EXERCISED")
                    pair.save(directory, label, case)
            proof["cases"].append(case)
            if case["state"] != "CASE_COMPLETE" or case.get("fixture_cleanup") != "COMPLETE":
                proof["state"] = "PAIR_INCOMPLETE"
                break
        proof["cases"].sort(key=lambda c: c["label"] != "baseline")
        if budget.used() <= 450 and budget.elapsed() <= 510:
            proof.update(comparison(proof["cases"]))
            if len(proof["cases"]) == 2 and all(c.get("result") for c in proof["cases"]):
                snapshots = [c["result"].get("counter_delta", {}).get("snapshots") for c in proof["cases"]]
                proof["same_exact_counter_rows"] = bool(snapshots[0]) and snapshots[0] == snapshots[1]
                if not proof["same_exact_counter_rows"]:
                    proof.update(
                        state="PAIR_COUNTER_MISMATCH",
                        acceptance_qualified=False,
                        candidate_processing_gates=False,
                    )
        else:
            proof["state"] = "PAIR_BUDGET_EXCEEDED"
    except Exception as exc:
        proof.update(state="PAIR_CONTROLLER_FAILED", error=type(exc).__name__, detail=str(exc)[:600])
    finally:
        signal.alarm(0)
        proof.update(
            elapsed_seconds=budget.elapsed(),
            cpu_seconds_upper_including_controller=budget.used(),
            fixture_cleanup="COMPLETE"
            if len(proof["cases"]) == 2
            and all(c.get("fixture_cleanup") == "COMPLETE" for c in proof["cases"])
            else "UNCONFIRMED",
        )
        (directory / "pair.json").write_text(json.dumps(proof, indent=2))
    print(json.dumps({k: v for k, v in proof.items() if k != "cases"}))
    return 0 if proof["state"] == "COMPARISON_COMPLETE" else 1


def case(report, cpu_limit):
    from tests.counter_delta_prototype import installed

    mode = os.environ.get("CW_COUNTER_DELTA")
    if mode not in ("0", "1"):
        raise ValueError("Explicit frozen counter mode required")
    with installed(mode == "1") as stats:
        code = q.case(report, cpu_limit=cpu_limit)
    record = json.loads(report.read_text())
    record["counter_delta"] = {"enabled": mode == "1", **stats}
    record["counter_delta"]["scope"] = (
        "Only market_counts/market_namespaces; finalflush/restore/reconciliation counted in complete container CPU"
    )
    report.write_text(json.dumps(record, indent=2))
    return code


def supervise(cpu_limit, seconds):
    from scripts.qualify_framed_backup import require_native_limits

    if cpu_limit != 1:
        raise ValueError("Unchanged1CPU required")
    require_native_limits(cpu_limit=1)
    work = Path("/work")
    if any((work / name).exists() for name in ("result.json", "supervisor.json", "controller-ack")):
        raise ValueError("New supervisor reports required")
    child = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-m",
            "scripts.qualify_counter_pair",
            "case",
            "--report",
            "/work/result.json",
            "--cpu-limit",
            "1",
        ],
        stdin=subprocess.DEVNULL,
    )
    timed_out = False
    try:
        child.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        child.kill()
        child.wait(timeout=5)
    (work / "supervisor.tmp").write_text(json.dumps({"child_exit": child.returncode, "timed_out": timed_out}))
    (work / "supervisor.tmp").replace(work / "supervisor.json")
    until = time.monotonic() + 20
    while not (work / "controller-ack").exists() and time.monotonic() < until:
        time.sleep(0.05)
    return child.returncode if child.returncode in (0, 1) else 2


def precheck_case(report):
    from scripts.qualify_framed_backup import require_native_limits

    require_native_limits(cpu_limit=1)
    if sys.version_info[:2] != (3, 11) or report.exists() or report.parent != Path("/work"):
        raise ValueError("New bounded native3.11 precheck required")
    import xml.etree.ElementTree as ET

    import pytest

    signal.alarm(510)
    code = pytest.main(
        [
            "tests/test_counter_delta_prototype.py",
            "tests/test_repository_meter.py",
            "tests/test_outcomes.py",
            "tests/test_runtime_repository.py",
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "asyncio_mode=auto",
            "-o",
            "asyncio_default_fixture_loop_scope=function",
            "--basetemp",
            "/work/pytest",
            "--junitxml",
            "/work/precheck.xml",
        ]
    )
    xml = Path("/work/precheck.xml").read_text()
    root = ET.fromstring(xml)
    tests = list(root.iter("testcase"))
    result = {
        "source": os.environ["SOURCE_REVISION"],
        "state": "PASS" if code == 0 else "FAIL",
        "mode": "COUNTER_NATIVE_CORRECTNESS_NOT_PERFORMANCE",
        "result": {
            "exit_code": int(code),
            "tests": len(tests),
            "skipped": sum(t.find("skipped") is not None for t in tests),
            "junit": xml,
        },
        "failures": [] if code == 0 else ["COUNTER_NATIVE_CORRECTNESS_FAILED"],
        "temporary_database_removed": False,
        "fixture_removal_owner": "bounded host controller after native prechecks",
    }
    report.write_text(json.dumps(result, indent=2))
    signal.alarm(0)
    return int(code)


def precheck_host(image, revision, directory):
    from unittest.mock import patch

    original = q.docker

    def docker(*args, **kwargs):
        args = list(args)
        if args and args[0] == "create":
            index = args.index("scripts/qualify_processing.py")
            assert args[index + 1] == "case"
            args[index : index + 2] = ["-m", "scripts.qualify_counter_pair", "precheck-case"]
        return original(*args, **kwargs)

    with patch.object(q, "docker", docker):
        return q.run(image, revision, directory)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    host = sub.add_parser("run")
    host.add_argument("baseline_image")
    host.add_argument("candidate_image")
    host.add_argument("revision")
    host.add_argument("directory", type=Path)
    host.add_argument("--reverse", action="store_true")
    native = sub.add_parser("precheck")
    native.add_argument("image")
    native.add_argument("revision")
    native.add_argument("directory", type=Path)
    inside = sub.add_parser("precheck-case")
    inside.add_argument("--report", type=Path, required=True)
    for mode in ("supervise", "case"):
        p = sub.add_parser(mode)
        p.add_argument("--cpu-limit", type=float, choices=(1,), required=True)
        if mode == "supervise":
            p.add_argument("--seconds", type=float, required=True)
        else:
            p.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "precheck":
        return precheck_host(args.image, args.revision, args.directory)
    if args.command == "precheck-case":
        return precheck_case(args.report)
    if args.command == "supervise":
        return supervise(args.cpu_limit, args.seconds)
    if args.command == "case":
        return case(args.report, args.cpu_limit)
    return run((args.baseline_image, args.candidate_image), args.revision, args.directory, args.reverse)


if __name__ == "__main__":
    raise SystemExit(main())
