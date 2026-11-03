"""Fixed1/1.5CPU pair or explicit single2CPU case with the same finite budget."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from scripts import cpu_pair_budget as b
from scripts import qualify_processing as q


def supervise(cpu_limit, seconds):
    from scripts.qualify_framed_backup import require_native_limits

    require_native_limits(cpu_limit=cpu_limit)
    work = Path("/work")
    if any((work / name).exists() for name in ("result.json", "supervisor.json", "controller-ack")):
        raise ValueError("New exclusive supervisor directory required")
    command = [
        sys.executable,
        "-B",
        "-m",
        "scripts.qualify_processing",
        "case",
        "--report",
        "/work/result.json",
        "--bounded-pool",
        "--hot-state",
        "--cpu-limit",
        str(cpu_limit),
    ]
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL)
    timed_out = False
    try:
        process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        process.wait(timeout=5)
    (work / "supervisor.tmp").write_text(
        json.dumps(
            {
                "child_exit": process.returncode,
                "timed_out": timed_out,
                "purpose": "Keep cgroup measurable until controller acknowledges final counters",
            }
        )
    )
    (work / "supervisor.tmp").replace(work / "supervisor.json")
    # No market work after the child. Root controller reads final CPU/IO and
    # then acknowledges; total post-sample CPU is conservatively bounded too.
    until = time.monotonic() + 20
    while not (work / "controller-ack").exists() and time.monotonic() < until:
        time.sleep(0.05)
    return process.returncode if process.returncode in (0, 1) else 2


def work_size(work):
    size = 0
    for path in work.rglob("*"):
        try:
            if path.is_file():
                size += path.stat().st_size
        except FileNotFoundError:
            pass  # Closed fixture may remove WAL/DB between samples.
    return size


def save(directory, label, proof):
    (directory / f"{label}-controller.json").write_text(json.dumps(proof, indent=2))


def one(
    image,
    measurement,
    directory,
    label,
    cpu_limit,
    budget,
    *,
    expected_revision=None,
    admission=None,
    supervisor_module="scripts.qualify_cpu_pair",
):
    if supervisor_module not in ("scripts.qualify_cpu_pair", "scripts.qualify_group_pair"):
        raise ValueError("Unsupported supervisor")
    expected_revision = expected_revision or b.RUNTIME
    admission = admission or b.admission
    work = directory / f"{label}-work"
    work.mkdir(mode=0o700)
    os.chown(work, 10001, 10001)
    name = "cw-cpu-pair-" + uuid.uuid4().hex
    proof = {
        "label": label,
        "cpu_limit": cpu_limit,
        "state": "PREPARED",
        "samples": [],
        "runtime": expected_revision,
        "measurement": measurement,
        "image": image,
        "admission": [],
    }
    created, process, cid, last = False, None, None, None
    tail_start = None
    try:
        for _ in range(12):
            if budget.reason():
                proof["state"] = budget.reason()
                return proof
            proof["admission"].append(q.interval(5))
            if admission(proof["admission"], shutil.disk_usage(directory).free, cpu_limit=cpu_limit):
                break
        if not admission(proof["admission"], shutil.disk_usage(directory).free, cpu_limit=cpu_limit):
            proof["state"] = "NOT_STARTED_RESOURCE_ADMISSION"
            return proof
        if not budget.admit_second():
            proof["state"] = "NOT_STARTED_REMAINING_PAIR_BUDGET"
            return proof
        seconds = q.WALL_SECONDS - budget.elapsed() - 10
        if seconds <= 0:
            raise TimeoutError("Pair deadline before create")
        process_limit = max(1, int(q.CPU_SECONDS - budget.used() - 30))
        proof["process_cpu_limit"] = process_limit
        try:
            cid = q.docker(
                "create",
                "--name",
                name,
                "--label",
                "io.cryptowatcher.cpu-pair=" + name,
                "--network=none",
                f"--cpus={cpu_limit}",
                "--memory=4g",
                "--memory-swap=4g",
                f"--ulimit=cpu={process_limit}:{process_limit}",
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
                image,
                "-B",
                "-m",
                supervisor_module,
                "supervise",
                "--cpu-limit",
                str(cpu_limit),
                "--seconds",
                str(seconds),
            ).strip()
        except subprocess.TimeoutExpired:
            # Mutation outcome is unknown: read the SAME name, never recreate.
            try:
                info = q.observe(name)
                cid = info["Id"]
                b.verify_container(info, name, cid, cpu_limit)
                created = True
                proof["container_id"] = cid
            except Exception:
                proof["creation_outcome"] = "UNKNOWN"
                raise
            raise
        created = True
        proof["container_id"] = cid
        initial = q.observe(cid)
        b.verify_container(initial, name, cid, cpu_limit)
        proof["container_limits"] = {
            key: initial["HostConfig"][key]
            for key in ("NanoCpus", "Memory", "MemorySwap", "NetworkMode", "ReadonlyRootfs", "PidsLimit")
        }
        with (
            (directory / f"{label}-stdout.log").open("x") as out,
            (directory / f"{label}-stderr.log").open("x") as err,
        ):
            process = subprocess.Popen(
                ["docker", "--host", "unix:///var/run/docker.sock", "start", "-a", cid],
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
            )
            group = None
            for _ in range(20):
                info = q.observe(cid)
                if info["State"]["Running"]:
                    group = b.group_path(info["State"]["Pid"])
                    break
                if process.poll() is not None:
                    break
                time.sleep(0.1)
            if group is None:
                raise ValueError("Supervisor did not start")
            while process.poll() is None:
                host = q.interval(3)
                if group.exists():
                    last = b.group_sample(group)
                    if last["cpu_period"] != 100000 or last["cpu_quota"] != int(cpu_limit * 100000):
                        raise ValueError("Actual CPU quota mismatch")
                    host.update(last)
                size = work_size(work)
                host["work_file_bytes"] = size
                proof["samples"].append(host)
                reason = budget.reason(last["cpu_seconds"] if last else 0)
                if host["idle_pct"] < 15:
                    reason = "STOP_HOST_IDLE_BELOW15"
                elif host["available_bytes"] < q.RESERVE:
                    reason = "STOP_HOST_MEMORY_RESERVE"
                elif shutil.disk_usage(directory).free < q.DISK_RESERVE or size > q.MAX_WORK:
                    reason = "STOP_DISK_BUDGET"
                if reason:
                    proof["state"] = reason
                    break
                if (work / "supervisor.json").exists():
                    proof["supervisor"] = json.loads((work / "supervisor.json").read_text())
                    if last is None:
                        raise ValueError("Missing final CPU sample")
                    tail_start = last["sampled_at"]
                    (work / "controller-ack").write_text("ACK")
                    process.wait(timeout=10)
                    break
            proof["exit_code"] = process.poll()
        if (work / "result.json").exists():
            raw = (work / "result.json").read_bytes()
            if len(raw) > 2 * 1024**2:
                raise ValueError("Oversized result")
            result = json.loads(raw)
            if result.get("source") != expected_revision:
                raise ValueError("Frozen source mismatch")
            (directory / f"{label}-result.json").write_bytes(raw)
            proof["result"] = result
            if result.get("result", {}).get("domain_writer"):
                evidence = result["result"].get("fact_evidence", {})
                if evidence.get("file") != "fact-evidence.json":
                    raise ValueError("Missing bounded domain fact evidence")
                path = work / "fact-evidence.json"
                if path.is_symlink() or path.stat().st_size > 5 * 1024**2:
                    raise ValueError("Invalid domain fact evidence file")
                raw = path.read_bytes()
                if len(raw) != evidence.get("bytes") or hashlib.sha256(raw).hexdigest() != evidence.get(
                    "sha256"
                ):
                    raise ValueError("Domain fact evidence digest mismatch")
                (directory / f"{label}-facts.json").write_bytes(raw)
                proof["fact_evidence"] = evidence
        if proof["state"] == "PREPARED":
            proof["state"] = (
                "CASE_COMPLETE"
                if (
                    proof.get("supervisor", {}).get("child_exit") in (0, 1)
                    and not proof["supervisor"]["timed_out"]
                    and "result" in proof
                    and tail_start is not None
                )
                else "CASE_INCOMPLETE"
            )
    except Exception as exc:
        proof.update(state="CONTROLLER_FAILED", error=type(exc).__name__, detail=str(exc)[:500])
    finally:
        try:
            if created:
                info = q.observe(cid)
                b.verify_container(info, name, cid, cpu_limit)
                if info["State"]["Running"]:
                    q.docker("kill", cid)
                if process is not None:
                    process.wait(timeout=10)
                finished = time.monotonic()
                if last is not None:
                    upper = b.tail_upper(last["cpu_seconds"], last["sampled_at"], finished, cpu_limit)
                else:
                    # A startup failure without counters must still consume a
                    # conservative ceiling, never zero cost for missing data.
                    upper = cpu_limit * (finished - budget.started + 0.2)
                proof["cpu_seconds_upper"] = upper
                budget.completed_cpu_upper += upper
                proof["container_state"] = q.observe(cid)["State"]
                q.docker("rm", cid)
            if work.parent != directory or work.is_symlink():
                raise ValueError("Owned fixture path changed")
            shutil.rmtree(work)
            proof["fixture_cleanup"] = (
                "UNCONFIRMED" if proof.get("creation_outcome") == "UNKNOWN" else "COMPLETE"
            )
        except Exception as exc:
            proof.update(state="CLEANUP_INCOMPLETE", cleanup_error=type(exc).__name__)
        proof["pair_cpu_upper_after_case"] = budget.used()
        proof["pair_elapsed_after_case"] = budget.elapsed()
        save(directory, label, proof)
    return proof


def run(image, measurement, directory, *, single_cpu=None, candidate_image=None):
    if single_cpu not in (None, 2):
        raise ValueError("Only the authorized single2CPU exception is supported")
    if candidate_image and single_cpu:
        raise ValueError("Domain comparison and single case are mutually exclusive")
    cases = (
        (("baseline2", 2), ("candidate2", 2))
        if candidate_image
        else ((("cpu2", 2),) if single_cpu == 2 else (("cpu1", 1), ("cpu1_5", 1.5)))
    )
    if sys.platform != "linux" or os.geteuid() != 0 or not re.fullmatch(r"[0-9a-f]{40}", measurement):
        raise ValueError("Linux root and exact measurement revision required")
    if directory.exists() or directory.is_symlink() or directory.parent.resolve() != directory.parent:
        raise ValueError("New owned pair directory required")
    image_info = json.loads(q.docker("image", "inspect", image))[0]
    labels = image_info["Config"]["Labels"]
    if (
        labels["org.opencontainers.image.revision"] != b.RUNTIME
        or labels.get("io.cryptowatcher.measurement-revision") != measurement
    ):
        raise ValueError("Runtime/measurement image identity mismatch")
    candidate_info = None
    if candidate_image:
        candidate_info = json.loads(q.docker("image", "inspect", candidate_image))[0]
        candidate_labels = candidate_info["Config"]["Labels"]
        if (
            candidate_labels.get("org.opencontainers.image.revision") != b.RUNTIME
            or candidate_labels.get("io.cryptowatcher.measurement-revision") != measurement
            or candidate_labels.get("io.cryptowatcher.domain-writer") != "typed-domain-v1"
            or labels.get("io.cryptowatcher.domain-writer") is not None
        ):
            raise ValueError("Explicit baseline/candidate image identities required")
    directory.mkdir(mode=0o755)
    budget = b.Budget()
    proof = {
        "schema": "cryptowatcher.cpu-pair.v1",
        "mode": "DOMAIN_COMPARISON" if candidate_image else ("SINGLE_CPU" if single_cpu else "FIXED_PAIR"),
        "runtime": b.RUNTIME,
        "measurement": measurement,
        "image": image_info["Id"],
        "cases": [],
        "state": "PREPARED",
        "limits": {
            "cpu_seconds_pair_total": 450,
            "wall_seconds_pair_total": 510,
            "outer_seconds_pair_total": 600,
            "memory_bytes": q.MEMORY,
            "cpu_order": [cpu for _, cpu in cases],
        },
        "limitations": [
            "Single fixed order; host/caches still affect inference",
            "Finite mock transports, not Contabo/live or30min/24h acceptance",
            "Full-pool/mature-capacity failures stay failures; no merge/delivery",
        ],
    }

    def expire(*_):
        raise TimeoutError("PAIR_OUTER_BUDGET")

    signal.signal(signal.SIGALRM, expire)
    signal.signal(signal.SIGINT, expire)
    signal.signal(signal.SIGTERM, expire)
    signal.alarm(570)
    try:
        for label, cpu in cases:
            if budget.reason() or not budget.admit_second():
                proof["state"] = "PAIR_INCOMPLETE_REMAINING_BUDGET"
                break
            selected = candidate_info if candidate_image and label == "candidate2" else image_info
            case = one(selected["Id"], measurement, directory, label, cpu, budget)
            if (
                candidate_image
                and label == "candidate2"
                and case.get("result", {}).get("result", {}).get("domain_writer", {}).get("mode")
                != "typed-domain-v1"
            ):
                case.update(state="CASE_INCOMPLETE", error="DOMAIN_WRITER_EVIDENCE_MISSING")
                save(directory, label, case)
            publication = labels.get("io.cryptowatcher.publication-unit")
            if publication is not None:
                unit = case.get("result", {}).get("result", {}).get("publication_unit", {})
                if publication != "one-pass-v1" or unit.get("mode") != publication:
                    case.update(state="CASE_INCOMPLETE", error="PUBLICATION_UNIT_EVIDENCE_MISSING")
                    save(directory, label, case)
            proof["cases"].append(case)
            if case["state"] != "CASE_COMPLETE" or case.get("fixture_cleanup") != "COMPLETE":
                proof["state"] = "PAIR_INCOMPLETE"
                break
        complete = len(proof["cases"]) == len(cases) and all(
            c["state"] == "CASE_COMPLETE" for c in proof["cases"]
        )
        complete = complete and budget.used() <= 450 and budget.elapsed() <= 510
        proof["comparison_complete"] = complete
        if candidate_image:
            same_input = complete and all(
                proof["cases"][0]["result"]["result"].get(key)
                == proof["cases"][1]["result"]["result"].get(key)
                for key in ("seed_sha256", "offered_sha256", "streams", "offered_closed")
            )
            proof["same_input"] = same_input
            proof["cpu_hypothesis"] = (
                "INCOMPLETE"
                if not same_input
                else (
                    "DOMAIN_PROCESSING_GATES_MET"
                    if b.processing_ok(proof["cases"][1]["result"])
                    else "DOMAIN_PROCESSING_GATES_FAILED"
                )
            )
            if not same_input:
                proof["state"] = "PAIR_INCOMPLETE_INPUT_IDENTITY"
        elif single_cpu:
            proof["cpu_hypothesis"] = (
                "INCOMPLETE"
                if not complete
                else (
                    "SINGLE_CASE_PROCESSING_GATES_MET"
                    if b.processing_ok(proof["cases"][0]["result"])
                    else "SINGLE_CASE_PROCESSING_GATES_FAILED"
                )
            )
        else:
            proof["cpu_hypothesis"] = b.interpretation(proof["cases"], complete)
        if proof["state"] == "PREPARED":
            proof["state"] = "QUALIFICATION_FAIL" if complete else "PAIR_INCOMPLETE"
            # Preserve all existing case gates. A budget hypothesis can be useful
            # while full-pressure/capacity qualification remains explicitly FAIL.
            if complete and all(c["result"]["state"] == "PASS" for c in proof["cases"]):
                proof["state"] = "PASS"
    except Exception as exc:
        proof.update(state="PAIR_CONTROLLER_FAILED", error=type(exc).__name__, detail=str(exc)[:500])
    finally:
        signal.alarm(0)
        proof["elapsed_seconds"] = budget.elapsed()
        proof["cpu_seconds_upper_including_controller"] = budget.used()
        proof["fixture_cleanup"] = (
            "COMPLETE"
            if all(c.get("fixture_cleanup") == "COMPLETE" for c in proof["cases"])
            else "INCOMPLETE"
        )
        (directory / "pair.json").write_text(json.dumps(proof, indent=2))
    print(
        json.dumps(
            {
                k: proof.get(k)
                for k in (
                    "state",
                    "comparison_complete",
                    "cpu_hypothesis",
                    "elapsed_seconds",
                    "cpu_seconds_upper_including_controller",
                    "fixture_cleanup",
                )
            }
        )
    )
    return int(proof["state"] != "PASS")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    child = sub.add_parser("supervise")
    child.add_argument("--cpu-limit", type=float, choices=(1, 1.5, 2), required=True)
    child.add_argument("--seconds", type=float, required=True)
    parent = sub.add_parser("run")
    parent.add_argument("image")
    parent.add_argument("measurement")
    parent.add_argument("directory", type=Path)
    parent.add_argument("--single-cpu", type=float, choices=(2,))
    parent.add_argument("--candidate-image")
    args = parser.parse_args()
    if args.command == "supervise":
        if not 0 < args.seconds <= 510:
            raise ValueError("Finite supervisor wall limit required")
        return supervise(args.cpu_limit, args.seconds)
    return run(
        args.image,
        args.measurement,
        args.directory.resolve(),
        single_cpu=args.single_cpu,
        candidate_image=args.candidate_image,
    )


if __name__ == "__main__":
    raise SystemExit(main())
