"""Canonical checks. Run from the repository root with the locked dev environment."""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "stage", choices=("docs", "quality", "test", "all", "full"), nargs="?", default="quality"
    )
    parser.add_argument("--revision")
    parser.add_argument("--report-directory", type=Path)
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--junitxml", type=Path)
    parser.add_argument("--basetemp", type=Path)
    args = parser.parse_args()
    if args.stage == "full":
        if args.junitxml or args.basetemp:
            parser.error("Full owns its test outputs inside the new report directory")
        command = [sys.executable, "scripts/full_validation.py"]
        if args.revision:
            command += ["--revision", args.revision]
        if args.report_directory:
            command += ["--report-directory", str(args.report_directory)]
        if args.plan:
            command.append("--plan")
        return subprocess.run(command, cwd=ROOT, check=False).returncode
    if args.revision or args.report_directory or args.plan:
        parser.error("Revision/report-directory/plan only apply to full")
    if (args.junitxml or args.basetemp) and args.stage not in ("test", "all"):
        parser.error("Test output flags only apply to test/all")
    commands = [[sys.executable, "scripts/validate_docs.py"]]
    if args.stage in ("quality", "all"):
        commands += [
            [sys.executable, "-m", "ruff", "check", "."],
            [sys.executable, "-m", "ruff", "format", "--check", "."],
            [sys.executable, "-m", "mypy"],
        ]
    if args.stage in ("test", "all"):
        (ROOT / ".cache-g1").mkdir(exist_ok=True)
        test = [
            sys.executable,
            "-m",
            "pytest",
            "--basetemp",
            str(args.basetemp or ".cache-g1/pytest-temp"),
            "-q",
        ]
        if args.junitxml:
            test += ["--junitxml", str(args.junitxml)]
        commands.append(test)
    for command in commands:
        result = subprocess.run(command, cwd=ROOT, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
