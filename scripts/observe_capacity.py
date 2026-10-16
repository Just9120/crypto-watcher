"""Finite host-side readonly observation; no workflow waits and no production writes.

Run only against the identified CryptoWatcher unit. Metadata files are private.
Deep ledger/index/cohort scans happen once at each24h boundary, never every poll.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

DOCKER = ["docker", "--host", "unix:///var/run/docker.sock"]
SCHEMA = "cryptowatcher.capacity-observation.v1"


def atomic(path: Path, value: dict[str, Any]) -> None:
    candidate = path.with_suffix(".tmp")
    if path.is_symlink() or candidate.is_symlink():
        raise ValueError("Symlink observation file")
    with candidate.open("w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    candidate.chmod(0o600)
    os.replace(candidate, path)


def probe(expected: str, *, deep: bool = False) -> dict[str, Any]:
    command = """import os,resource,sys
os.nice(15)
resource.setrlimit(resource.RLIMIT_AS,(256*1024**2,256*1024**2))
resource.setrlimit(resource.RLIMIT_CPU,(90,90))
from cryptowatcher.__main__ import main
sys.argv=['cryptowatcher','capacity'] + (['--capacity-ledger','--capacity-cohorts'] if sys.argv[1]=='deep' else [])
raise SystemExit(main())
"""
    result = subprocess.run(
        DOCKER + ["exec", "crypto-watcher", "python", "-B", "-c", command, "deep" if deep else "quick"],
        capture_output=True,
        text=True,
        check=True,
        timeout=145 if deep else 15,
    )
    value = json.loads(result.stdout)
    if value.get("source_revision") != expected:
        raise ValueError("Runtime revision changed; observation invalidated")
    return value


def advance(state: dict[str, Any], value: dict[str, Any], now: float) -> dict[str, Any]:
    result = dict(state)
    blockers = value["stability_blockers"]
    result.update(last_sample_ms=value["at_ms"], last_blockers=blockers)
    if "BOOTSTRAP_PENDING_STREAMS" not in blockers and "INGESTION_STALE" not in blockers:
        result.setdefault("bootstrap_completion_observed_ms", value["at_ms"])
    if "REPACK" not in blockers:
        result.setdefault("repack_completion_observed_ms", value["at_ms"])
    if result["state"] == "WAITING_STABLE":
        result["stable_samples"] = 0 if blockers else result.get("stable_samples", 0) + 1
        if now - result["created_at"] > 12 * 3600:
            result["state"] = "WAIT_LIMIT_REACHED"
        elif result["stable_samples"] >= 2:
            result["state"] = "READY_FOR_BASELINE"
    elif result["state"] == "OBSERVING":
        if blockers:
            result["interrupted_samples"] = result.get("interrupted_samples", 0) + 1
        if now >= result["baseline_ms"] / 1000 + 86400:
            result["state"] = "READY_FOR_FINAL"
    return result


def comparison(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    command = (
        "import json,sys;from cryptowatcher.capacity import interval;"
        "v=json.load(sys.stdin);print(json.dumps(interval(v['before'],v['after'])))"
    )
    result = subprocess.run(
        DOCKER + ["exec", "-i", "crypto-watcher", "python", "-B", "-c", command],
        input=json.dumps({"before": before, "after": after}),
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--expected-source", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or socket.gethostname() != "vmi3224957":
        raise ValueError("Wrong host/account")
    expected = args.expected_source
    if len(expected) != 40 or any(c not in "0123456789abcdef" for c in expected):
        raise ValueError("Invalid expected source")
    root = Path("/opt/crypto_watcher/observations")
    folder = args.directory
    if folder.parent != root or not folder.name.startswith("capacity-"):
        raise ValueError("Wrong observation scope")
    if any(p.is_symlink() for p in (folder, *folder.parents)):
        raise ValueError("Symlink observation target")
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.chmod(0o700)
    record = folder / "status.json"
    if record.exists():
        state = json.loads(record.read_text(encoding="utf-8"))
        if state.get("schema") != SCHEMA or state.get("expected_source") != expected:
            raise ValueError("Conflicting existing observation")
    else:
        state = {
            "schema": SCHEMA,
            "expected_source": expected,
            "state": "WAITING_STABLE",
            "created_at": time.time(),
            "samples": 0,
        }
        atomic(record, state)
    while state["state"] in ("WAITING_STABLE", "OBSERVING"):
        try:
            sample = probe(expected)
            state = advance(state, sample, time.time())
            state["samples"] += 1
            # Each sample keeps counters/files and lifecycle evidence. No raw events or credentials.
            atomic(folder / f"sample-{state['samples']:04d}.json", sample)
            if state["state"] == "READY_FOR_BASELINE":
                baseline = probe(expected, deep=True)
                if baseline["stability_blockers"]:
                    state.update(state="WAITING_STABLE", stable_samples=0)
                else:
                    atomic(folder / "baseline.json", baseline)
                    state.update(state="OBSERVING", baseline_ms=baseline["at_ms"], interrupted_samples=0)
            elif state["state"] == "READY_FOR_FINAL":
                after = probe(expected, deep=True)
                before = json.loads((folder / "baseline.json").read_text(encoding="utf-8"))
                atomic(folder / "final.json", after)
                summary = comparison(before, after)
                summary["interrupted_samples"] = state.get("interrupted_samples", 0)
                if summary["interrupted_samples"]:
                    summary["state"] = "OBSERVED_24H_WITH_INTERRUPTION"
                    summary["daily_extrapolation_bytes"] = None
                summary["capacity_verdict"] = "UNPROVEN; evaluate population/retention/scenarios separately"
                atomic(folder / "result.json", summary)
                state.update(state="COMPLETE", result_state=summary["state"], finished_at=time.time())
            state["consecutive_probe_failures"] = 0
        except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            if state["state"] == "READY_FOR_BASELINE":
                state["state"] = "WAITING_STABLE"
            elif state["state"] == "READY_FOR_FINAL":
                state["state"] = "OBSERVING"
            state["consecutive_probe_failures"] = state.get("consecutive_probe_failures", 0) + 1
            if state["state"] == "WAITING_STABLE":
                state["stable_samples"] = 0
            elif state["state"] == "OBSERVING":
                state["interrupted_samples"] = state.get("interrupted_samples", 0) + 1
            state["last_error_class"] = type(exc).__name__
            if isinstance(exc, ValueError) or state["consecutive_probe_failures"] >= 3:
                state.update(state="FAILED", finished_at=time.time())
        atomic(record, state)
        if state["state"] not in ("WAITING_STABLE", "OBSERVING"):
            break
        time.sleep(300)
    print(json.dumps(state, sort_keys=True))
    return 0 if state["state"] == "COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
