"""One clean same-host old/new source-group comparison, without deployment."""

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
        raise ValueError("Only unchanged 1 CPU is authorized")
    return q.admit(samples, free)


def comparison(cases):
    if len(cases) != 2 or any(
        c.get("state") != "CASE_COMPLETE" or c.get("fixture_cleanup") != "COMPLETE" for c in cases
    ):
        return {"state": "PAIR_INCOMPLETE", "same_input": False}
    results = [c["result"]["result"] for c in cases]
    keys = ("seed_sha256", "offered_sha256", "streams", "offered_closed")
    same = all(results[0].get(k) is not None and results[0].get(k) == results[1].get(k) for k in keys)
    observation_ok = all(
        r["bounded_pool"].get("observation", {}).get("mode") == "PERSISTED_REVISIONS_AFTER_PHASE"
        and r["bounded_pool"]["observation"].get("consumer_wiring_unchanged") is True
        and r["bounded_pool"]["observation"].get("events_read") == r["bounded_pool"]["global_actual"]
        and r["bounded_pool"]["observation"].get("producer_facts") == r["bounded_pool"]["global_actual"]
        for r in results
    )
    if not observation_ok:
        return {"state": "PAIR_INCOMPLETE_OBSERVATION", "same_input": same}
    for case in cases:
        record = case["result"]
        if record.get("failures") != q.verdict(record["result"]):
            raise ValueError("Original assertions differ from independent recomputation")
    global_ok = all(
        r["bounded_pool"].get("global_actual_sha256")
        and r["bounded_pool"].get("global_actual_sha256") == r["bounded_pool"].get("global_expected_sha256")
        for r in results
    )
    global_ok = global_ok and (
        results[0]["bounded_pool"]["global_actual_sha256"]
        == results[1]["bounded_pool"]["global_actual_sha256"]
    )
    return {
        "state": "COMPARISON_COMPLETE" if same and global_ok else "PAIR_INCOMPLETE_IDENTITY",
        "same_input": same,
        "same_global_facts": global_ok,
        "same_observation": observation_ok,
        "baseline_failures": cases[0]["result"]["failures"],
        "candidate_failures": cases[1]["result"]["failures"],
        "candidate_processing_gates": (
            not cases[1]["result"]["failures"]
            and cases[1]["result"].get("temporary_database_removed") is True
            and results[1]["bounded_pool"]["agent_receipts_by_kind"].get("LIVE_EVENT", 0) > 0
            and results[1]["bounded_pool"]["agent_receipts_by_kind"].get("HISTORICAL_OUTCOME_UPDATE", 0) > 0
            and not any(x.get("pending_total", 0) for x in results[1]["delivery_queues_at_endpoint"].values())
        ),
        "acceptance_qualified": all(c["result"]["state"] == "PASS" for c in cases),
    }


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
        if (
            labels.get("org.opencontainers.image.revision") != revision
            or labels.get("io.cryptowatcher.measurement-revision") != candidate
            or labels.get("io.cryptowatcher.group-publication") != "clean-pair-v1"
        ):
            raise ValueError("Source/measurement image binding mismatch")
    directory.mkdir(mode=0o755)
    budget = b.Budget()
    proof = {
        "schema": "cryptowatcher.group-publication-pair.v1",
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
                supervisor_module="scripts.qualify_group_pair",
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


def supervise(cpu_limit, seconds):
    from scripts.qualify_framed_backup import require_native_limits

    if cpu_limit != 1:
        raise ValueError("Only unchanged 1 CPU")
    require_native_limits(cpu_limit=1)
    work = Path("/work")
    if any((work / name).exists() for name in ("result.json", "supervisor.json", "controller-ack")):
        raise ValueError("Exclusive empty supervisor directory required")
    child = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-m",
            "scripts.qualify_processing",
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
    args = parser.parse_args()
    if args.command == "supervise":
        return supervise(args.cpu_limit, args.seconds)
    return run((args.baseline_image, args.candidate_image), args.candidate, args.directory)


if __name__ == "__main__":
    raise SystemExit(main())
