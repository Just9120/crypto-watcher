"""One frozen-runtime cost diagnosis; no optimization, VPS or acceptance claim."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import functools
import heapq
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from scripts import qualify_processing as q

RUNTIME = "04a894c95ff186c2983387eb74a93219670b17b0"
OPS = (
    "sql_read",
    "sql_write",
    "sql_commit",
    "sql_other",
    "sql_prepare",
    "sql_reset",
    "sql_finalize",
    "sql_exec",
    "sync",
    "pread",
    "pwrite",
    "python_runtime_payload_callback",
    "python_record_json_callback",
    "python_other_cw_callback",
    "python_other_callback",
)
LIB = Path("/diag/processing_cost_probe.so")
BOUND = 2 * 1024**2


def write_json(path, value):
    data = json.dumps(value, separators=(",", ":")).encode()
    if len(data) > BOUND:
        raise ValueError("Bounded diagnostic output exceeded")
    temp = path.with_name(path.name + ".new")
    with temp.open("xb") as f:
        f.write(data)
    os.replace(temp, path)


class Probe:
    def __init__(self):
        self.lib = ctypes.PyDLL(str(LIB))
        self.lib.cw_probe_phase.argtypes = [ctypes.c_int]
        self.lib.cw_probe_role.argtypes = [ctypes.c_int]
        self.lib.cw_probe_callback_enter.restype = ctypes.c_int
        self.lib.cw_probe_callback_enter.argtypes = [ctypes.c_int]
        self.lib.cw_probe_callback_leave.restype = None
        self.lib.cw_probe_snapshot.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        self.lib.cw_probe_calibrate.restype = ctypes.c_uint64
        self.calibration_ns = self.lib.cw_probe_calibrate()
        self.phase = 0
        self.tls = threading.local()
        self.threads = []
        self.stages = []

    def snapshot(self):
        buf = ctypes.create_string_buffer(1024**2)
        length = self.lib.cw_probe_snapshot(buf, len(buf))
        if length < 0:
            raise ValueError("Native diagnostic overflow")
        return json.loads(buf.raw[:length])

    def thread(self):
        if not hasattr(self.tls, "row"):
            self.tls.row = {
                "tid": threading.get_native_id(),
                "transactions": {},
                "slow_transactions": [],
                "groups": [],
                "group_calls": 0,
                "groups_truncated": 0,
            }
            self.threads.append(self.tls.row)
        return self.tls.row

    def stage(self, value):
        name = value["stage"]
        if name == "PACED_START":
            self.phase = 1
            self.lib.cw_probe_phase(1)
        elif name in ("ARRIVAL_WINDOW_COMPLETE", "STOPPING_FIXTURE_WORKERS"):
            self.phase = 2
            self.lib.cw_probe_phase(2)
        record = {
            "stage": name,
            "phase": self.phase,
            "monotonic": time.monotonic(),
            "exchange_ms": int(time.time() * 1000),
            "main_tid": threading.get_native_id(),
            "process_cpu_seconds": time.process_time(),
            "cpu_stat": numbers(Path("/sys/fs/cgroup/cpu.stat")),
            "cpu_pressure": pressure(Path("/sys/fs/cgroup/cpu.pressure")),
            "io_pressure": pressure(Path("/sys/fs/cgroup/io.pressure")),
        }
        self.stages.append(record)
        write_json(Path("/work/cost-phase.json"), record)

    def save(self, destination):
        write_json(
            destination,
            {
                "source": RUNTIME,
                "mode": "COST_DIAGNOSTIC_NOT_ACCEPTANCE",
                "native": self.snapshot(),
                "native_calibration_cpu_ns_per_scope": self.calibration_ns / 10000,
                "operations": OPS,
                "stages": self.stages,
                "threads": self.threads,
                "limitations": [
                    "Instrumentation changes throughput; do not use absolute lag as clean acceptance",
                    "SQLite exclusive CPU excludes separately timed SQL callbacks and nested SQL/IO",
                    "Sync syscall wall includes descheduling; use phase CPU quota/PSI/schedstat jointly, never call wall-minus-CPU pure disk time",
                    "Application source/scenario/queries/settings unchanged; only disposable measurement factories and LD_PRELOAD",
                    "Native clocks/accounting add overhead outside measured calls; process CPU remainder includes it",
                ],
            },
        )


@contextlib.contextmanager
def installed(probe):
    from cryptowatcher.ingestion import BybitRadar

    original = sqlite3.connect
    start_thread = threading.Thread.start
    group = BybitRadar.accept_received_group
    factories = {}

    def connect(*args, **kwargs):
        base = kwargs.get("factory", sqlite3.Connection)
        if base not in factories:
            if not isinstance(base, type) or not issubclass(base, sqlite3.Connection):
                raise ValueError("Unknown SQLite factory")

            class Measured(base):
                def create_function(self, name, narg, func, **options):
                    if func is not None:
                        original_function = func

                        @functools.wraps(original_function)
                        def measured(*arguments):
                            kind = (
                                11
                                if name == "cw_runtime_payload"
                                else 12
                                if name == "cw_record_json"
                                else 13
                                if name.startswith("cw_")
                                else 14
                            )
                            entered = probe.lib.cw_probe_callback_enter(kind)
                            try:
                                return original_function(*arguments)
                            finally:
                                if entered:
                                    probe.lib.cw_probe_callback_leave()

                        func = measured
                    return super().create_function(name, narg, func, **options)

                def __exit__(self, *arguments):
                    transaction = self.in_transaction
                    began = time.monotonic()
                    cpu = time.thread_time()
                    try:
                        return super().__exit__(*arguments)
                    finally:
                        if transaction:
                            wall = time.monotonic() - began
                            used = time.thread_time() - cpu
                            row = probe.thread()
                            role = getattr(probe.tls, "source_role", 0)
                            key = f"{probe.phase}:{role}:" + (
                                "commit" if arguments[0] is None else "rollback"
                            )
                            stat = row["transactions"].setdefault(
                                key, {"calls": 0, "wall": 0.0, "cpu": 0.0, "max_wall": 0.0}
                            )
                            stat["calls"] += 1
                            stat["wall"] += wall
                            stat["cpu"] += used
                            stat["max_wall"] = max(stat["max_wall"], wall)
                            event = (
                                wall,
                                stat["calls"],
                                {
                                    "begin": began,
                                    "end": began + wall,
                                    "cpu": used,
                                    "phase": probe.phase,
                                    "role": role,
                                },
                            )
                            # Each thread owns its bounded heap; no hot shared lock.
                            heapq.heappush(row["slow_transactions"], event)
                            if len(row["slow_transactions"]) > 64:
                                heapq.heappop(row["slow_transactions"])

            factories[base] = Measured
        kwargs["factory"] = factories[base]
        return original(*args, **kwargs)

    def received(self, market, frames, allowed):
        previous = probe.lib.cw_probe_role(1)
        probe.tls.source_role = 1
        began = time.monotonic()
        cpu = time.thread_time()
        try:
            return group(self, market, frames, allowed)
        finally:
            wall = time.monotonic() - began
            used = time.thread_time() - cpu
            row = probe.thread()
            row["group_calls"] += 1
            event = (
                wall,
                row["group_calls"],
                {
                    "begin": began,
                    "end": began + wall,
                    "cpu": used,
                    "phase": probe.phase,
                    "frames": len(frames),
                    "first_receipt_ms": min((at for _, at in frames), default=None),
                },
            )
            if len(row["groups"]) < 4096:
                row["groups"].append(event)
            else:
                row["groups_truncated"] += 1
            probe.lib.cw_probe_role(previous)
            probe.tls.source_role = previous

    def started(thread, *args, **kwargs):
        if thread.name == "finite-mock-wire":
            probe.stage({"stage": "PACED_START"})
        return start_thread(thread, *args, **kwargs)

    with (
        patch.object(threading.Thread, "start", started),
        patch.object(sqlite3, "connect", connect),
        patch.object(BybitRadar, "accept_received_group", received),
    ):
        yield


def case():
    if os.environ.get("SOURCE_REVISION") != RUNTIME:
        raise ValueError("Frozen runtime required")
    probe = Probe()

    class Capture:
        def __init__(self, enabled):
            if enabled:
                raise ValueError("No cProfile mixed into native diagnosis")

        def stage(self, value):
            probe.stage(value)

        def save(self, _):
            probe.save(Path("/work/cost-profile.json"))

    with installed(probe), patch.object(q, "PhaseProfile", Capture):
        status = q.case(Path("/work/result.json"))
    envelope = json.loads(Path("/work/result.json").read_text())
    envelope["diagnostic_mode"] = "COST_DIAGNOSTIC_NOT_ACCEPTANCE"
    envelope["acceptance_qualified"] = False
    write_json(Path("/work/result.json"), envelope)
    return status


def numbers(path):
    return {a.rstrip(":"): int(b) for a, b in (line.split() for line in path.read_text().splitlines())}


def pressure(path):
    return {
        line.split()[0]: {
            k: int(v) for k, v in (word.split("=") for word in line.split()[1:]) if k == "total"
        }
        for line in path.read_text().splitlines()
    }


def sample(pid):
    group = Path("/sys/fs/cgroup")
    value = {
        "monotonic": time.monotonic(),
        "cpu_stat": numbers(group / "cpu.stat"),
        "cpu_pressure": pressure(group / "cpu.pressure"),
        "io_pressure": pressure(group / "io.pressure"),
        "io_stat": (group / "io.stat").read_text(),
        "threads": [],
    }
    task = Path(f"/proc/{pid}/task")
    for p in sorted(task.iterdir()):
        try:
            raw = (p / "stat").read_text()
            fields = raw[raw.rindex(")") + 2 :].split()
            value["threads"].append(
                {
                    "tid": int(p.name),
                    "state": fields[0],
                    "user_ticks": int(fields[11]),
                    "system_ticks": int(fields[12]),
                    "blkio_delay_ticks": int(fields[39]),
                    "schedstat": list(map(int, (p / "schedstat").read_text().split())),
                    "wchan": (p / "wchan").read_text().strip(),
                }
            )
        except FileNotFoundError:
            continue
    try:
        value["process_io"] = numbers(Path(f"/proc/{pid}/io"))
    except FileNotFoundError:
        pass
    return value


def supervise(cpu_limit, seconds):
    from scripts.qualify_framed_backup import require_native_limits

    require_native_limits(cpu_limit=cpu_limit)
    work = Path("/work")
    if any(
        (work / p).exists() for p in ("result.json", "supervisor.json", "controller-ack", "cost-samples.json")
    ):
        raise ValueError("Exclusive new work directory required")
    env = os.environ.copy()
    env["LD_PRELOAD"] = str(LIB) + ":libsqlite3.so.0"
    command = [sys.executable, "-B", "-m", "scripts.processing_cost_probe", "case"]
    start = time.monotonic()
    cpu = time.process_time()
    process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL)
    samples = []
    errors = []
    timed_out = False
    try:
        while process.poll() is None:
            if time.monotonic() - start >= seconds:
                timed_out = True
                process.terminate()
                break
            try:
                samples.append(sample(process.pid))
                if len(samples) > 2600:
                    raise ValueError("Diagnostic sample bound exceeded")
            except FileNotFoundError:
                pass
            except Exception as e:
                errors.append(type(e).__name__)
                raise
            time.sleep(0.2)
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        write_json(
            work / "cost-samples.json",
            {
                "source": RUNTIME,
                "mode": "COST_DIAGNOSTIC_NOT_ACCEPTANCE",
                "sample_period_seconds": 0.2,
                "samples": samples,
                "errors": errors,
                "sampler_cpu_seconds": time.process_time() - cpu,
                "limitations": [
                    "Thread states are sampled, not an exact blocked-time trace",
                    "blkio delay accounting may be disabled by host; zero alone is not absence of IO",
                    "PSI/cgroup throttle counters are aggregate and overlap with syscall wall; do not sum as independent delays",
                ],
            },
        )
        write_json(
            work / "supervisor.json",
            {
                "child_exit": process.returncode,
                "timed_out": timed_out,
                "elapsed_seconds": time.monotonic() - start,
            },
        )
    deadline = time.monotonic() + 15
    while not (work / "controller-ack").exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("Final controller sample acknowledgement missing")
        time.sleep(0.1)
    return 0


def run(image, measurement, directory):
    from scripts import cpu_pair_budget as b
    from scripts import qualify_cpu_pair as pair
    from scripts import qualify_revision_pair as revisions

    if directory.exists():
        raise ValueError("New result directory required")
    directory.mkdir(parents=True)
    info = json.loads(q.docker("image", "inspect", image))[0]
    if info["Config"]["Labels"].get("org.opencontainers.image.revision") != RUNTIME:
        raise ValueError("Frozen image mismatch")
    if info["Config"]["Labels"].get("io.cryptowatcher.measurement") != measurement:
        raise ValueError("Measurement image mismatch")

    def expire(*_):
        raise TimeoutError("COST_DIAGNOSTIC_OUTER_LIMIT")

    for sig in (signal.SIGALRM, signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, expire)
    signal.alarm(570)
    budget = b.Budget()
    try:
        proof = pair.one(
            info["Id"],
            measurement,
            directory,
            "current",
            1,
            budget,
            expected_revision=RUNTIME,
            supervisor_module="scripts.processing_cost_probe",
            admission=revisions.admission,
            measurement_files=("cost-profile.json", "cost-samples.json"),
        )
        result = proof.get("result", {})
        report = {
            "source": RUNTIME,
            "measurement": measurement,
            "mode": "COST_DIAGNOSTIC_NOT_ACCEPTANCE",
            "case": proof,
            "state": "DIAGNOSTIC_COMPLETE" if proof.get("state") == "CASE_COMPLETE" else "INCOMPLETE",
            "cpu_upper_including_controller": budget.used(),
            "elapsed_seconds": budget.elapsed(),
            "acceptance_qualified": False,
        }
        if result and result.get("failures") != q.verdict(result["result"]):
            raise ValueError("Recorded assertions changed")
        if result:
            actual = result["result"]
            pool = actual.get("bounded_pool", {})
            if not (
                actual.get("offered_closed") == actual.get("accepted_closed") == 2472
                and actual.get("seed_sha256") == q.SEED
                and actual.get("offered_sha256") == q.OFFERED
                and pool.get("global_actual") == pool.get("global_expected") == 10300
                and pool.get("global_actual_sha256") == pool.get("global_expected_sha256")
                and pool.get("global_difference", {}).get("missing_count") == 0
                and pool.get("global_difference", {}).get("extra_count") == 0
                and actual.get("producer_errors")
                == actual.get("worker_errors")
                == actual.get("receiver_errors")
                == []
            ):
                report["state"] = "INCOMPLETE_SEMANTICS"
        profile_path = directory / "current-cost-profile.json"
        if profile_path.is_file():
            native = json.loads(profile_path.read_text())
            stages = native.get("stages", [])
            phase_names = {stage["stage"] for stage in stages}
            native_ops = {row[3] for row in native.get("native", {}).get("rows", []) if row[1] == 1}
            if (
                native.get("native", {}).get("lost") != 0
                or not {0, 1, 8, 10}.issubset(native_ops)
                or any(t.get("groups_truncated") for t in native.get("threads", []))
            ):
                report["state"] = "INCOMPLETE_NATIVE_COUNTERS"
            if not {"PACED_START", "ARRIVAL_WINDOW_COMPLETE"}.issubset(phase_names):
                report["state"] = "INCOMPLETE_PHASE_BOUNDARIES"
            for stage in stages:
                if not {"nr_throttled", "throttled_usec", "usage_usec"}.issubset(stage.get("cpu_stat", {})):
                    report["state"] = "INCOMPLETE_CPU_COUNTERS"
        else:
            report["state"] = "INCOMPLETE_NATIVE_REPORT"
        if proof.get("fixture_cleanup") != "COMPLETE":
            report["state"] = "INCOMPLETE"
        write_json(directory / "diagnostic.json", report)
        print(json.dumps({k: v for k, v in report.items() if k != "case"}))
        return int(report["state"] != "DIAGNOSTIC_COMPLETE")
    finally:
        signal.alarm(0)


def preflight():
    probe = Probe()
    probe.lib.cw_probe_phase(1)
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp, installed(probe):
        path = Path(tmp) / "preflight.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE facts(value TEXT)")
            db.create_function("diagnostic_identity", 1, lambda x: x)
            db.execute("INSERT INTO facts VALUES(?)", ("only synthetic preflight",))
        db.close()
        with sqlite3.connect(path) as db:
            db.create_function("diagnostic_identity", 1, lambda x: x)
            assert (
                db.execute("SELECT diagnostic_identity(value) FROM facts").fetchone()[0]
                == "only synthetic preflight"
            )
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        db.close()
    rows = probe.snapshot()
    ops = {r[3] for r in rows["rows"] if r[1] == 1}
    assert rows["lost"] == 0 and all(op in ops for op in (0, 1, 8, 10, 14))
    assert sum(row[4] for row in rows["rows"] if row[1] == 0 and row[3] == 3) == 10000
    assert os.environ.get("EXPECTED_SQLITE") == sqlite3.sqlite_version
    print(
        json.dumps(
            {
                "native_probe": "PASS",
                "lost": rows["lost"],
                "operations": sorted(ops),
                "sqlite_version": sqlite3.sqlite_version,
            }
        )
    )
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("case")
    sub.add_parser("preflight")
    child = sub.add_parser("supervise")
    child.add_argument("--cpu-limit", type=float, choices=(1,), required=True)
    child.add_argument("--seconds", type=float, required=True)
    parent = sub.add_parser("run")
    parent.add_argument("image")
    parent.add_argument("measurement")
    parent.add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.command == "preflight":
        return preflight()
    if args.command == "case":
        return case()
    if args.command == "supervise":
        if not 0 < args.seconds <= 510:
            raise ValueError("Finite supervisor deadline required")
        return supervise(args.cpu_limit, args.seconds)
    return run(args.image, args.measurement, args.directory.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
