"""One bounded native narrow experiment; never full processing acceptance.

The existing read-only controller owns admission, cgroup limits, host stop and
cleanup. Only the in-container case entrypoint changes. Application sources and
its full-flow assertions remain frozen at the declared baseline.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
import re
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

BASELINE = "19e0051b3ed009c90d6badd93a75b14a6d097e6a"
MODE = "SUBJECT_SAVE_NATIVE_NARROW_NOT_PROCESSING_ACCEPTANCE"


def rewrite_create(arguments):
    result = list(arguments)
    if not result or result[0] != "create":
        return result
    if result.count("scripts/qualify_processing.py") != 1 or "--entrypoint=python" not in result:
        raise ValueError("Unexpected native narrow command")
    position = result.index("scripts/qualify_processing.py")
    if result[position + 1 : position + 4] != ["case", "--report", "/work/result.json"]:
        raise ValueError("Unexpected native narrow report/arguments")
    result[position : position + 1] = ["-m", "scripts.qualify_subject_save"]
    result.insert(result.index("--entrypoint=python"), "--env=TMPDIR=/work")
    return result


def identity(info, name, expected_image):
    h = info["HostConfig"]
    if (
        info["Config"]["Labels"].get("io.cryptowatcher.qualifier-owner") != name
        or info["Image"] != expected_image
        or h["NanoCpus"] != 1000000000
        or h["Memory"] != 4294967296
        or h["MemorySwap"] != 4294967296
        or h["NetworkMode"] != "none"
        or h["ReadonlyRootfs"] is not True
        or h["PidsLimit"] != 64
    ):
        raise ValueError("Native narrow container identity/limits changed")
    return info["Id"]


def assess(rows):
    if len(rows) != 6:
        raise ValueError("Three complete narrow pairs required")
    for row in rows:
        if (
            not row["oracle_equal"]
            or row["facts"] != 800
            or row["offered_closed"] != 192
            or row["due_pending"] != 0
            or row.get("completed_outcomes") != 8
            or row.get("future_jobs", 0) <= 0
            or any(row["queues"].values())
            or row["source_blocks"]
            or row["telegram"] != 32
            or row["agent"] != 168
            or row["agent_kinds"] != {"LIVE_EVENT": 96, "HISTORICAL_OUTCOME_UPDATE": 72}
        ):
            raise ValueError("Native narrow flow correctness failed")
    per_pair = []
    for repeat in range(3):
        pair = {row["enabled"]: row for row in rows if row["repeat"] == repeat}
        if set(pair) != {False, True}:
            raise ValueError("Narrow pair identities changed")
        for key in ("facts_sha", "oracle_sha", "outcomes_sha", "agent_payloads_sha", "telegram_texts_sha"):
            if pair[False][key] != pair[True][key]:
                raise ValueError("Independent/public numeric or delivery mismatch: " + key)
        if pair[True]["subject"]["planned"] <= 0:
            raise ValueError("Prepared subject path was not exercised")
        per_pair.append(
            {
                key: 100
                * (
                    1
                    - (
                        pair[True]["cpu_seconds"]
                        if key == "total"
                        else pair[True]["stages"]["save_operation_cpu"]
                    )
                    / (
                        pair[False]["cpu_seconds"]
                        if key == "total"
                        else pair[False]["stages"]["save_operation_cpu"]
                    )
                )
                for key in ("total", "save")
            }
        )
    medians = {
        name: {
            str(flag): statistics.median(
                row["cpu_seconds"] if name == "total" else row["stages"]["save_operation_cpu"]
                for row in rows
                if row["enabled"] == flag
            )
            for flag in (False, True)
        }
        for name in ("total", "save")
    }
    gains = {name: 100 * (1 - value["True"] / value["False"]) for name, value in medians.items()}
    return {
        "medians": medians,
        "reduction_pct": gains,
        "per_pair_reduction_pct": per_pair,
        "narrow_gate": gains["save"] >= 25 and all(p["save"] > 0 and p["total"] > 0 for p in per_pair),
        "full_processing_acceptance": False,
    }


def cgroup():
    root = Path("/sys/fs/cgroup")
    if not (root / "cpu.stat").exists():
        return {"state": "UNAVAILABLE"}
    return {
        "cpu_stat": (root / "cpu.stat").read_text(),
        "io_stat": (root / "io.stat").read_text(),
        "memory_peak_bytes": int((root / "memory.peak").read_text()),
        "memory_current_bytes": int((root / "memory.current").read_text()),
    }


def case(report):
    import pytest

    from cryptowatcher import frozen_codec_native
    from tests.subject_save_workload import run

    if sys.platform != "linux" or not frozen_codec_native.available():
        raise ValueError("Native Linux implementation required")
    source = os.environ.get("SOURCE_REVISION", "")
    if not re.fullmatch("[0-9a-f]{40}", source) or report.exists():
        raise ValueError("Exact source and new owned report required")
    envelope = {
        "schema": "cryptowatcher.subject-save-narrow.v1",
        "source": source,
        "application_baseline": BASELINE,
        "mode": MODE,
        "state": "FAIL",
        "failures": [],
        "comparison_complete": False,
        "result": {},
        "rows": [],
    }
    signal.alarm(510)
    start = time.monotonic()
    try:
        code = pytest.main(
            [
                "tests/test_subject_save_prototype.py",
                "tests/test_outcomes.py",
                "tests/test_processing_observation.py",
                "tests/test_bounded_pool.py",
                "tests/test_domain_writer.py",
                "tests/test_hot_agent.py",
                "tests/test_publication_unit.py",
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
                "/work/native-tests.xml",
            ]
        )
        envelope["native_tests_exit"] = int(code)
        if code:
            raise ValueError("Native prechecks failed")
        with tempfile.TemporaryDirectory(prefix="subject-save-", dir=report.parent) as temporary:
            for repeat in range(3):
                for enabled in (False, True) if repeat % 2 == 0 else (True, False):
                    before = cgroup()
                    row = asyncio.run(
                        run(Path(temporary) / f"{repeat}-{enabled}.sqlite3", enabled, instruments=16)
                    )
                    row.update(repeat=repeat, cgroup_before=before, cgroup_after=cgroup())
                    envelope["rows"].append(row)
                    report.write_text(json.dumps(envelope, indent=2))
                    print(
                        json.dumps(
                            {
                                "repeat": repeat,
                                "enabled": enabled,
                                "cpu": row["cpu_seconds"],
                                "save_cpu": row["stages"]["save_operation_cpu"],
                            }
                        ),
                        flush=True,
                    )
        envelope["result"] = assess(envelope["rows"])
        envelope["comparison_complete"] = True
        envelope["state"] = "PASS" if envelope["result"]["narrow_gate"] else "FAIL"
        if envelope["state"] == "FAIL":
            envelope["failures"] = ["NARROW_GAIN_NOT_REPRODUCIBLY_AT_LEAST25_PERCENT"]
    except Exception as exc:
        envelope["failures"].append(type(exc).__name__ + ":" + str(exc)[:600])
    finally:
        envelope["elapsed_seconds"] = time.monotonic() - start
        envelope["temporary_database_removed"] = not any(report.parent.glob("subject-save-*/*.sqlite3*"))
        report.write_text(json.dumps(envelope, indent=2))
        signal.alarm(0)
    return 0 if envelope["state"] == "PASS" else 1


def run_controller(image, revision, directory):
    from scripts import qualify_processing as q

    original = q.docker
    ownership = {}

    def docker(*arguments, **options):
        arguments = rewrite_create(arguments)
        if arguments and arguments[0] == "create":
            name = arguments[arguments.index("--name") + 1]
            expected_image = arguments[arguments.index("--entrypoint=python") + 1]
            ownership.update(name=name, image=expected_image)
            try:
                return original(*arguments, **options)
            except subprocess.TimeoutExpired:
                info = q.observe(name)
                return identity(info, name, expected_image)
        if arguments and arguments[0] == "inspect":
            for attempt in range(3):
                try:
                    return original(*arguments, **options)
                except subprocess.TimeoutExpired:
                    if attempt == 2:
                        raise
        return original(*arguments, **options)

    output = io.StringIO()
    try:
        with patch.object(q, "docker", docker), contextlib.redirect_stdout(output):
            q.run(image, revision, directory)
    finally:
        # A timed-out create may appear after its first readback. Inspect the
        # same unique owned name; never create another container or prune.
        if ownership:
            try:
                info = json.loads(original("inspect", ownership["name"]))[0]
            except subprocess.CalledProcessError:
                info = None
            if info is not None:
                cid = identity(info, ownership["name"], ownership["image"])
                if info["State"]["Running"]:
                    original("kill", cid)
                original("rm", cid)
    proof_path = directory / "controller.json"
    proof = json.loads(proof_path.read_text())
    proof["mode"] = MODE
    proof_path.write_text(json.dumps(proof, indent=2))
    result_path = directory / "result.json"
    report = json.loads(result_path.read_text()) if result_path.exists() else {}
    complete = report.get("comparison_complete") is True and proof.get("fixture_cleanup") == "COMPLETE"
    (directory / "controller-summary.json").write_text(
        json.dumps(
            {
                "mode": MODE,
                "comparison_complete": complete,
                "result": report.get("result"),
                "controller_state": proof.get("state"),
                "original_summary": output.getvalue(),
            },
            indent=2,
        )
    )
    print(
        json.dumps(
            {
                "mode": MODE,
                "comparison_complete": complete,
                "result": report.get("result"),
                "failures": report.get("failures"),
            }
        )
    )
    return 0 if complete else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    worker = sub.add_parser("case")
    worker.add_argument("--report", type=Path, required=True)
    host = sub.add_parser("run")
    host.add_argument("image")
    host.add_argument("revision")
    host.add_argument("directory", type=Path)
    args = parser.parse_args()
    return (
        case(args.report)
        if args.action == "case"
        else run_controller(args.image, args.revision, args.directory)
    )


if __name__ == "__main__":
    raise SystemExit(main())
