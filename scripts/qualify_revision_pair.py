"""One diagnostic same-host comparison of two immutable normal-runtime versions.

No app/scenario overlays, optimizations, production access or gate exceptions.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import platform
import re
import signal
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from scripts import cpu_pair_budget as b
from scripts import qualify_cpu_pair as pair
from scripts import qualify_processing as q

REVISIONS = ("a5273820ce396ce60efff9856953fdd0a007bb8f", "cf638ec4871a275738c25ae7198f6744ab797e20")
LABELS = ("previous", "current")


@functools.lru_cache(maxsize=256)
def category(sql):
    text = sql.upper()
    found = re.search(r"(?:FROM|INTO|UPDATE|TABLE)\s+[\"`\[]?([A-Z_][A-Z_0-9]*)", text)
    table = found[1].lower() if found else "other"
    verb = text.lstrip().split(None, 1)[0] if text.strip() else "EMPTY"
    if table in ("sqlite_master", "sqlite_schema", "market_meta", "owner_settings") or verb == "PRAGMA":
        group = "metadata"
    elif table.startswith(("direct_", "domain_", "analytics_pool_")):
        group = table
    elif table == "runtime":
        group = "runtime"
    elif any(word in table for word in ("expiry", "lifetime", "reference", "hold", "body_link")):
        group = "retention_references"
    elif any(word in table for word in ("candle", "source_window", "source_usage")):
        group = "sources"
    elif any(word in table for word in ("quality", "cohort")):
        group = "quality"
    else:
        group = "other"
    return group + ":" + ("read" if verb in ("SELECT", "PRAGMA", "WITH") else "write")


class SQLProfile:
    """Exclusive execute/transaction time on the real writer, no SQL changes.

    Deferred cursor fetch and ordinary read-only worker connections are outside
    this counter. Existing whole-phase/transition/consumer counters stay intact.
    """

    def __init__(self):
        self.phase = "seed"
        self.rows = {}
        self.local = threading.local()

    def stage(self, value):
        if value["stage"] == "SEED_COMPLETE":
            self.phase = "paced"
        elif value["stage"] == "STOPPING_FIXTURE_WORKERS":
            self.phase = "cleanup"

    def wrap(self, original, transaction=False):
        def measured(connection, *args, **kwargs):
            phase = self.phase
            if phase == "cleanup":
                return original(connection, *args, **kwargs)
            group = "transaction_boundary" if transaction else category(str(args[0]))
            key = phase + ":" + group
            if key not in self.rows and len(self.rows) >= 128:
                key = phase + ":other:write"
            row = self.rows.setdefault(
                key, {"calls": 0, "failures": 0, "wall_seconds": 0.0, "cpu_seconds": 0.0}
            )
            stack = getattr(self.local, "stack", None)
            if stack is None:
                self.local.stack = stack = []
            entry = [time.perf_counter(), time.thread_time(), 0.0, 0.0]
            stack.append(entry)
            try:
                return original(connection, *args, **kwargs)
            except BaseException:
                row["failures"] += 1
                raise
            finally:
                cpu, wall = time.thread_time() - entry[1], time.perf_counter() - entry[0]
                assert stack.pop() is entry
                row["calls"] += 1
                row["wall_seconds"] += max(0.0, wall - entry[2])
                row["cpu_seconds"] += max(0.0, cpu - entry[3])
                if stack:
                    stack[-1][2] += wall
                    stack[-1][3] += cpu

        return measured

    def install(self, stack):
        from cryptowatcher.writer_memory import Connection, Cursor

        for cls, names in (
            (Connection, ("execute", "executemany", "executescript", "commit", "rollback", "__exit__")),
            (Cursor, ("execute", "executemany")),
        ):
            for name in names:
                stack.enter_context(
                    patch.object(
                        cls, name, self.wrap(getattr(cls, name), name in ("commit", "rollback", "__exit__"))
                    )
                )
        previous_stage = q.PhaseProfile.stage

        def stage(capture, value):
            self.stage(value)
            previous_stage(capture, value)

        stack.enter_context(patch.object(q.PhaseProfile, "stage", stage))

    def save(self, path):
        path.write_text(
            json.dumps(
                {
                    "schema": "cryptowatcher.writer-sql-diagnostic.v1",
                    "source": os.environ.get("SOURCE_REVISION"),
                    "mode": "DIAGNOSTIC_NOT_ACCEPTANCE",
                    "scope": "Exclusive real writer execute and transaction boundaries; SQL, calls and results unchanged",
                    "limitations": [
                        "Wrapper overhead included equally in both cases",
                        "Deferred cursor fetch and readonly worker SQL excluded",
                        "SQL time includes invoked SQLite triggers and UDFs; not a standalone disk-wait measurement",
                    ],
                    "phases": self.rows,
                },
                indent=2,
            ),
            encoding="utf-8",
        )


def case():
    meter = SQLProfile()
    try:
        with ExitStack() as stack:
            meter.install(stack)
            result = q.case(Path("/work/result.json"), cpu_limit=1)
            path = Path("/work/result.json")
            payload = json.loads(path.read_text())
            payload.update(mode="SQL_DIAGNOSTIC_NOT_ACCEPTANCE", acceptance_qualified=False)
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            return result
    finally:
        meter.save(Path("/work/sql-profile.json"))


def supervise(seconds):
    from scripts.qualify_framed_backup import require_native_limits

    require_native_limits(cpu_limit=1)
    work = Path("/work")
    if any(
        (work / name).exists()
        for name in ("result.json", "sql-profile.json", "supervisor.json", "controller-ack")
    ):
        raise ValueError("Exclusive empty supervisor directory required")
    child = subprocess.Popen(
        [sys.executable, "-B", "-m", "scripts.qualify_revision_pair", "case"], stdin=subprocess.DEVNULL
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


def admission(samples, free, *, cpu_limit):
    if cpu_limit != 1:
        raise ValueError("Only the unchanged1CPU limit is authorized")
    return q.admit(samples, free)


def run(images, measurement, directory):
    if sys.platform != "linux" or os.geteuid() != 0 or not re.fullmatch(r"[0-9a-f]{40}", measurement):
        raise ValueError("Linux root and exact measurement revision required")
    if directory.exists() or directory.is_symlink() or directory.parent.resolve() != directory.parent:
        raise ValueError("Exclusive local output directory required")
    infos = [json.loads(q.docker("image", "inspect", image))[0] for image in images]
    if len(infos) != 2:
        raise ValueError("Exactly two versions are authorized")
    for info, revision in zip(infos, REVISIONS, strict=True):
        labels = info["Config"]["Labels"]
        if (
            labels.get("org.opencontainers.image.revision") != revision
            or labels.get("io.cryptowatcher.measurement-revision") != measurement
        ):
            raise ValueError("Runtime/measurement binding mismatch")
    directory.mkdir(mode=0o755)
    budget = b.Budget()
    cpu_model = next(
        (
            line.split(":", 1)[1].strip()
            for line in Path("/proc/cpuinfo").read_text().splitlines()
            if line.startswith("model name")
        ),
        "UNKNOWN",
    )
    proof = {
        "schema": "cryptowatcher.revision-pair.v1",
        "state": "PREPARED",
        "mode": "DIAGNOSTIC_NOT_ACCEPTANCE",
        "measurement": measurement,
        "revisions": REVISIONS,
        "cases": [],
        "host": {
            "cpu_model": cpu_model,
            "logical_cpu_count": os.cpu_count(),
            "kernel": platform.release(),
            "python": sys.version,
        },
        "limits": {
            "cpu_per_case": 1,
            "memory_bytes": q.MEMORY,
            "cpu_seconds_pair_total": 450,
            "wall_seconds_pair_total": 510,
            "outer_seconds_pair_total": 600,
        },
        "limitations": [
            "One fixed previous/current order; OS cache and temporal host noise remain",
            "Same diagnostic wrapper changes absolute throughput; not a replacement for clean qualification",
            "Mock transports; no VPS/live acceptance or delivery",
        ],
    }

    def expire(*_):
        raise TimeoutError("PAIR_OUTER_BUDGET")

    for sig in (signal.SIGALRM, signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, expire)
    signal.alarm(570)
    try:
        for label, revision, image in zip(LABELS, REVISIONS, infos, strict=True):
            if budget.reason() or not budget.admit_second():
                proof["state"] = "PAIR_INCOMPLETE_REMAINING_BUDGET"
                break
            result = pair.one(
                image["Id"],
                measurement,
                directory,
                label,
                1,
                budget,
                expected_revision=revision,
                supervisor_module="scripts.qualify_revision_pair",
                admission=admission,
                measurement_files=("sql-profile.json",),
            )
            proof["cases"].append(result)
            if result["state"] != "CASE_COMPLETE" or result.get("fixture_cleanup") != "COMPLETE":
                proof["state"] = "PAIR_INCOMPLETE"
                break
        complete = len(proof["cases"]) == 2 and all(c["state"] == "CASE_COMPLETE" for c in proof["cases"])
        complete = complete and budget.used() <= 450 and budget.elapsed() <= 510
        proof["comparison_complete"] = complete
        if complete:
            results = [c["result"]["result"] for c in proof["cases"]]
            keys = ("seed_sha256", "offered_sha256", "streams", "offered_closed")
            proof["same_input"] = all(results[0].get(k) == results[1].get(k) for k in keys)
            for envelope in [c["result"] for c in proof["cases"]]:
                if envelope.get("failures") != q.verdict(envelope["result"]):
                    raise ValueError("Recorded gates disagree with independent recomputation")
            proof["state"] = (
                "DIAGNOSTIC_COMPLETE" if proof["same_input"] else "PAIR_INCOMPLETE_INPUT_IDENTITY"
            )
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
    print(
        json.dumps(
            {
                k: proof.get(k)
                for k in (
                    "state",
                    "comparison_complete",
                    "same_input",
                    "elapsed_seconds",
                    "cpu_seconds_upper_including_controller",
                    "fixture_cleanup",
                )
            }
        )
    )
    return int(proof["state"] != "DIAGNOSTIC_COMPLETE")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("case")
    child = sub.add_parser("supervise")
    child.add_argument("--cpu-limit", type=float, choices=(1,), required=True)
    child.add_argument("--seconds", type=float, required=True)
    parent = sub.add_parser("run")
    parent.add_argument("previous_image")
    parent.add_argument("current_image")
    parent.add_argument("measurement")
    parent.add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.command == "case":
        return case()
    if args.command == "supervise":
        if not 0 < args.seconds <= 510:
            raise ValueError("Unchanged finite supervisor deadline required")
        return supervise(args.seconds)
    return run([args.previous_image, args.current_image], args.measurement, args.directory.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
