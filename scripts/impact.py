"""Conservative CI selector; unknown paths require full validation."""

import argparse
import subprocess


def code_required(paths: list[str]) -> bool:
    return not paths or any(
        not (path.endswith(".md") and ("/" not in path or path.startswith("docs/"))) for path in paths
    )


def backup_io_required(paths: list[str]) -> bool:
    exact = {
        "Dockerfile",
        ".github/workflows/ci.yml",
        "scripts/impact.py",
        "scripts/build_native.py",
        "scripts/qualify_backup_io.py",
        "cryptowatcher/compressed_validation.py",
        "cryptowatcher/framed_backup.py",
        "cryptowatcher/native_validation.py",
        "cryptowatcher/validation_archive.py",
        "cryptowatcher/validation_io.py",
        "cryptowatcher/source_validation.py",
        "cryptowatcher/validation.py",
        "cryptowatcher/storage.py",
    }
    return not paths or any(path in exact or path.startswith(("native/", "requirements")) for path in paths)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("base")
    parser.add_argument("head")
    args = parser.parse_args()
    paths = subprocess.check_output(
        ["git", "diff", "--name-only", args.base, args.head], text=True
    ).splitlines()
    print(f"code={'true' if code_required(paths) else 'false'}")
    print(f"backup_io={'true' if backup_io_required(paths) else 'false'}")


if __name__ == "__main__":
    main()
