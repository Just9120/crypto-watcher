"""Accounting and interpretation for one owner-authorized CPU pair."""

from __future__ import annotations

import math
import os
import time
from pathlib import Path

from scripts import qualify_processing as q

RUNTIME = "52d31a11a993b3bd765722c8ebf2c6a0e3dc4205"
COVERAGE = {"FULL_POOL_PRESSURE_UNPROVEN", "MATURE_CAPACITY_UNPROVEN"}


def process_cpu():
    import resource

    values = [resource.getrusage(kind) for kind in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN)]
    return sum(v.ru_utime + v.ru_stime for v in values)


class Budget:
    def __init__(self, clock=time.monotonic, cpu=process_cpu):
        self.clock, self.cpu = clock, cpu
        self.started, self.initial_cpu = clock(), cpu()
        self.completed_cpu_upper = 0.0

    def elapsed(self):
        return self.clock() - self.started

    def used(self, active=0.0):
        return self.completed_cpu_upper + max(0, self.cpu() - self.initial_cpu) + active

    def reason(self, active=0.0):
        if self.elapsed() >= q.WALL_SECONDS:
            return "STOP_PAIR_WALL_BUDGET"
        if self.used(active) >= q.CPU_SECONDS - 30:
            return "STOP_PAIR_CPU_RESERVE"
        return None

    def admit_second(self):
        # The130s arrival phase is unchanged. This is a conservative admission
        # floor, never permission to exceed the shared deadline if seed is slow.
        return q.WALL_SECONDS - self.elapsed() >= 165 and q.CPU_SECONDS - self.used() >= 150


def tail_upper(sample_cpu, sampled_at, finished_at, cpu_limit):
    if (
        cpu_limit not in (1, 1.5)
        or not all(math.isfinite(v) for v in (sample_cpu, sampled_at, finished_at))
        or sample_cpu < 0
        or finished_at < sampled_at
    ):
        raise ValueError("Invalid final CPU accounting")
    # Default CFS period is verified100ms. Cover both partial end periods and
    # ALL final supervisor/teardown CPU even when the cgroup vanishes on exit.
    return sample_cpu + cpu_limit * (finished_at - sampled_at + 0.2)


def admission(samples, free, cores=None):
    cores = os.cpu_count() if cores is None else cores
    if not cores or cores < 1:
        return False
    required_idle = max(35, 15 + 150 / cores)
    return q.admit(samples, free) and all(s["idle_pct"] >= required_idle for s in samples[-6:])


def verify_container(info, name, cid, cpu_limit):
    h = info["HostConfig"]
    if (
        info["Id"] != cid
        or info["Config"]["Labels"].get("io.cryptowatcher.cpu-pair") != name
        or h["NanoCpus"] != int(cpu_limit * 1e9)
        or h["Memory"] != q.MEMORY
        or h["MemorySwap"] != q.MEMORY
        or h["NetworkMode"] != "none"
        or h["ReadonlyRootfs"] is not True
        or h["PidsLimit"] != 64
    ):
        raise ValueError("CPU pair container identity/limits mismatch")


def group_sample(group):
    sampled_at = time.monotonic()
    stat = dict(line.split() for line in (group / "cpu.stat").read_text().splitlines())
    quota, period = (group / "cpu.max").read_text().split()
    return {
        "sampled_at": sampled_at,
        "cpu_seconds": int(stat["usage_usec"]) / 1e6,
        "nr_periods": int(stat.get("nr_periods", 0)),
        "nr_throttled": int(stat.get("nr_throttled", 0)),
        "throttled_seconds": int(stat.get("throttled_usec", 0)) / 1e6,
        "cpu_quota": int(quota),
        "cpu_period": int(period),
        "memory_peak_bytes": int((group / "memory.peak").read_text()),
        "io_stat": (group / "io.stat").read_text(),
    }


def group_path(pid):
    entry = next(v[3:] for v in Path(f"/proc/{pid}/cgroup").read_text().splitlines() if v.startswith("0::"))
    root = Path("/sys/fs/cgroup")
    path = root / entry.lstrip("/")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Invalid qualification cgroup")
    return path


def processing_ok(envelope):
    try:
        failures = q.verdict(envelope["result"], direct_sqlite=True, bounded_pool=True)
        kinds = envelope["result"]["bounded_pool"]["agent_receipts_by_kind"]
        return (
            envelope.get("failures") == failures
            and set(failures) <= COVERAGE
            and kinds.get("LIVE_EVENT", 0) > 0
            and kinds.get("HISTORICAL_OUTCOME_UPDATE", 0) > 0
            and envelope.get("temporary_database_removed") is True
            and envelope.get("source") == RUNTIME
        )
    except (KeyError, TypeError, ValueError):
        return False


def interpretation(cases, complete):
    if not complete or len(cases) != 2:
        return "INCOMPLETE"
    first, second = cases
    if not processing_ok(second["result"]):
        return "CPU_BUDGET_NOT_SUFFICIENT"
    result = second["result"]["result"]
    if result["phase_cpu_seconds"] / result["phase_seconds"] > 1:
        return "STEADY_CPU_TARGET_EXCEEDED"
    samples = second["samples"]
    concurrent = any(
        b["cpu_seconds"] - a["cpu_seconds"] > 1.05 * (b["sampled_at"] - a["sampled_at"])
        for a, b in zip(samples, samples[1:])
    )
    if not processing_ok(first["result"]) and first["samples"][-1]["nr_throttled"] > 0 and concurrent:
        return "CPU_HYPOTHESIS_SUPPORTED_IN_FINITE_CASE"
    return "PROCESSING_GATES_MET_CPU_CAUSE_UNPROVEN"
