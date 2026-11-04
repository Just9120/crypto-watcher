"""Fail closed on changed workload/gates, then bounded native correctness only."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import signal
import subprocess
import sys
from pathlib import Path

FROZEN = (
    "scripts/qualify_processing.py",
    "scripts/cpu_pair_budget.py",
    "scripts/qualify_group_pair.py",
    "tests/test_processing_replay.py",
    "tests/processing_observation.py",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-bootstrap.txt",
    "Dockerfile",
)


class WithoutTrial(ast.NodeTransformer):
    def visit_If(self, node):
        if any(isinstance(item, ast.Name) and item.id == "subject_services" for item in ast.walk(node.test)):
            return None
        return self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        if node.name == "replay":
            assert node.args.kwonlyargs[-1].arg == "subject_services"
            node.args.kwonlyargs.pop()
            node.args.kw_defaults.pop()
        return self.generic_visit(node)


def verify(baseline, candidate):
    assert baseline == "19e0051b3ed009c90d6badd93a75b14a6d097e6a"
    assert re.fullmatch(r"[0-9a-f]{40}", candidate)
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == candidate
    subprocess.run(["git", "merge-base", "--is-ancestor", baseline, candidate], check=True)
    records = []
    for name in FROZEN:
        original = subprocess.check_output(["git", "show", baseline + ":" + name])
        actual = Path(name).read_bytes()
        assert original == actual, name
        records.append({"path": name, "sha256": hashlib.sha256(actual).hexdigest()})
    path = "tests/processing_replay.py"
    original = ast.parse(subprocess.check_output(["git", "show", baseline + ":" + path]))
    actual = WithoutTrial().visit(ast.parse(Path(path).read_text()))
    assert ast.dump(original) == ast.dump(actual), "Non-trial replay logic changed"
    print(
        json.dumps(
            {
                "frozen": records,
                "replay_delta": "only explicit trial argument/install/report; identical generator/clock/consumers/assertions",
            }
        )
    )


def native(directory):
    from scripts.qualify_framed_backup import require_native_limits

    require_native_limits(cpu_limit=1)
    assert directory == Path("/work") and not (directory / "native.json").exists()
    signal.alarm(120)
    import pytest

    code = pytest.main(
        [
            "tests/test_service_repository.py",
            "tests/test_runtime_repository.py",
            "tests/test_repository_meter.py",
            "tests/test_outcomes.py",
            "tests/test_agent_delivery.py",
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "asyncio_mode=auto",
            "-o",
            "asyncio_default_fixture_loop_scope=function",
            "--basetemp",
            str(directory / "pytest"),
            "--junitxml",
            str(directory / "native.xml"),
        ]
    )
    (directory / "native.json").write_text(
        json.dumps(
            {
                "state": "PASS" if code == 0 else "FAIL",
                "pytest_exit": code,
                "environment": "Linux3.11/native realSQLite bounded fixture only; not full regression or VPS",
            }
        )
    )
    # Validate the exact owned path before removing only synthetic pytest data.
    import shutil

    target = directory / "pytest"
    if target.resolve().parent != directory.resolve() or target.is_symlink():
        raise ValueError("Native temporary path changed")
    if target.exists():
        shutil.rmtree(target)
    signal.alarm(0)
    return code


if __name__ == "__main__":
    if sys.argv[1] == "native":
        raise SystemExit(native(Path(sys.argv[2])))
    verify(*sys.argv[1:])
