"""Canonical checks. Run from the repository root with the locked dev environment."""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("docs", "quality", "test", "all"), nargs="?", default="quality")
    args = parser.parse_args()
    commands = [[sys.executable, "scripts/validate_docs.py"]]
    if args.stage in ("quality", "all"):
        commands += [
            [sys.executable, "-m", "ruff", "check", "."],
            [sys.executable, "-m", "ruff", "format", "--check", "."],
            [sys.executable, "-m", "mypy"],
        ]
    if args.stage in ("test", "all"):
        commands.append([sys.executable, "-m", "pytest", "--basetemp", ".cache-g1/pytest-temp", "-q"])
    for command in commands:
        result = subprocess.run(command, cwd=ROOT, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
