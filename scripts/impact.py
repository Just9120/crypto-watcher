"""Conservative CI selector; unknown paths require full validation."""

import argparse
import subprocess


def code_required(paths: list[str]) -> bool:
    return not paths or any(
        not (path.endswith(".md") and ("/" not in path or path.startswith("docs/"))) for path in paths
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("base")
    parser.add_argument("head")
    args = parser.parse_args()
    paths = subprocess.check_output(
        ["git", "diff", "--name-only", args.base, args.head], text=True
    ).splitlines()
    print(f"code={'true' if code_required(paths) else 'false'}")


if __name__ == "__main__":
    main()
