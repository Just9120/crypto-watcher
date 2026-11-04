"""One explicit same-host canonical service comparison, without deployment."""

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
from scripts.qualify_group_pair import comparison

BASELINE = "19e0051b3ed009c90d6badd93a75b14a6d097e6a"


def admission(samples, free, *, cpu_limit):
    if cpu_limit != 1:
        raise ValueError("Only unchanged 1 CPU is authorized")
    return q.admit(samples, free)


def run(images, candidate, directory):
    if (
        sys.platform != "linux"
        or os.geteuid() != 0
        or not re.fullmatch(r"[0-9a-f]{40}", candidate)
        or candidate == BASELINE
        or directory.exists()
        or directory.is_symlink()
        or directory.parent.resolve() != directory.parent
    ):
        raise ValueError("Linux controller, exact candidate and exclusive directory required")
    revisions = (BASELINE, candidate)
    if len(images) != 2:
        raise ValueError("Exactly baseline/candidate images required")
    infos = [json.loads(q.docker("image", "inspect", image))[0] for image in images]
    for info, revision in zip(infos, revisions, strict=True):
        labels = info["Config"]["Labels"]
        mode = dict(item.split("=", 1) for item in info["Config"]["Env"] if "=" in item).get(
            "CW_SUBJECT_SERVICES"
        )
        if (
            labels.get("org.opencontainers.image.revision") != revision
            or labels.get("io.cryptowatcher.measurement-revision") != candidate
            or labels.get("io.cryptowatcher.subject-services") != "canonical-services-v1"
            or mode != ("0" if revision == BASELINE else "1")
        ):
            raise ValueError("Source/measurement image binding mismatch")
    directory.mkdir(mode=0o755)
    budget = b.Budget()
    proof = {
        "schema": "cryptowatcher.subject-services-pair.v1",
        "state": "PREPARED",
        "revisions": revisions,
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
            "Fixed baseline/candidate order; temporal host/cache noise remains",
            "Mock network and consumers; not shared VPS or live delivery acceptance",
            "No mature-volume capacity or 24h stability claim",
        ],
    }

    def expire(*_):
        raise TimeoutError("PAIR_OUTER_BUDGET")

    for sig in (signal.SIGALRM, signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, expire)
    signal.alarm(570)
    try:
        for label, revision, image in zip(("baseline", "candidate"), revisions, infos, strict=True):
            if budget.reason() or not budget.admit_second():
                proof["state"] = "PAIR_INCOMPLETE_REMAINING_BUDGET"
                break
            case = pair.one(
                image["Id"],
                candidate,
                directory,
                label,
                1,
                budget,
                expected_revision=revision,
                admission=admission,
                supervisor_module="scripts.qualify_service_pair",
            )
            if label == "candidate" and case.get("state") == "CASE_COMPLETE":
                writer = case.get("result", {}).get("result", {}).get("runtime_writer", {})
                if (
                    writer.get("publication_mode") != "explicit-subject-v1"
                    or not writer.get("publication_units")
                    or writer["publication_units"] != writer.get("observations")
                ):
                    case.update(state="CASE_INCOMPLETE", error="EXPLICIT_PUBLICATION_NOT_EXERCISED")
                    pair.save(directory, label, case)
            if label == "candidate" and case.get("state") == "CASE_COMPLETE":
                service = case["result"]["result"].get("subject_services", {})
                operations = service.get("operations", {})
                if not (
                    service.get("accounting", {}).get("state") == "MEASURED_FLOW"
                    and service.get("foreign_keys") == []
                    and len(service.get("exact_counter_reconciliation", {})) == 5
                    and all(
                        row.get("match") is True
                        for row in service.get("exact_counter_reconciliation", {}).values()
                    )
                    and all(
                        operations.get(key, 0) > 0
                        for key in (
                            "job_reservations",
                            "delivery_writes",
                            "status_only_updates",
                            "attempt_writes",
                        )
                    )
                ):
                    case.update(state="CASE_INCOMPLETE", error="CANONICAL_SERVICES_NOT_EXERCISED")
                    pair.save(directory, label, case)
            proof["cases"].append(case)
            if case["state"] != "CASE_COMPLETE" or case.get("fixture_cleanup") != "COMPLETE":
                proof["state"] = "PAIR_INCOMPLETE"
                break
        if budget.used() <= 450 and budget.elapsed() <= 510:
            proof.update(comparison(proof["cases"]))
        else:
            proof["state"] = "PAIR_BUDGET_EXCEEDED"
    except Exception as exc:
        proof.update(state="PAIR_CONTROLLER_FAILED", error=type(exc).__name__, detail=str(exc)[:500])
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
        (directory / "pair.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in proof.items() if k not in ("cases",)}))
    return 0 if proof["state"] == "COMPARISON_COMPLETE" else 1


def case(report, cpu_limit):
    from unittest.mock import patch

    from tests import processing_replay

    mode = os.environ.get("CW_SUBJECT_SERVICES")
    if mode not in ("0", "1"):
        raise ValueError("Explicit subject services selection required")
    original = processing_replay.replay

    async def selected(*args, **kwargs):
        return await original(*args, **kwargs, subject_services=mode == "1")

    # Entry argument only: no replacement of producers, consumers, assertions,
    # clocks, SQL, source generation or persisted observation collectors.
    with patch.object(processing_replay, "replay", selected):
        return q.case(report, cpu_limit=cpu_limit)


def supervise(cpu_limit, seconds):
    from scripts.qualify_framed_backup import require_native_limits

    if cpu_limit != 1:
        raise ValueError("Only unchanged 1 CPU")
    require_native_limits(cpu_limit=1)
    work = Path("/work")
    if any((work / name).exists() for name in ("result.json", "supervisor.json", "controller-ack")):
        raise ValueError("Exclusive empty supervisor directory required")
    mode = os.environ.get("CW_SUBJECT_SERVICES")
    if mode not in ("0", "1"):
        raise ValueError("Explicit subject services case selection required")
    child = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-m",
            "scripts.qualify_service_pair",
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


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    outer = commands.add_parser("run")
    outer.add_argument("baseline_image")
    outer.add_argument("candidate_image")
    outer.add_argument("candidate")
    outer.add_argument("directory", type=Path)
    inner = commands.add_parser("supervise")
    inner.add_argument("--cpu-limit", type=float, choices=(1,), required=True)
    inner.add_argument("--seconds", type=float, required=True)
    inner = commands.add_parser("case")
    inner.add_argument("--report", type=Path, required=True)
    inner.add_argument("--cpu-limit", type=float, choices=(1,), required=True)
    args = parser.parse_args()
    if args.command == "case":
        return case(args.report, args.cpu_limit)
    if args.command == "supervise":
        return supervise(args.cpu_limit, args.seconds)
    return run((args.baseline_image, args.candidate_image), args.candidate, args.directory)


if __name__ == "__main__":
    raise SystemExit(main())
