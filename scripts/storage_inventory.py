"""Bounded metadata-only inventory; backups/experiments never count as working DB."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def inventory(app: Path, seconds: float = 2, limit: int = 10000) -> dict:
    deadline = time.monotonic() + seconds
    seen: set[tuple[int, int]] = set()
    count = 0
    result = {}
    for category, root in (
        ("backups", app / "data/sqlite/backups"),
        ("reset_private_and_staging", app / "data/sqlite"),
        ("experiments", app / "rehearsals"),
        ("release_artifacts", app / "releases"),
    ):
        value = {"logical_bytes": 0, "unique_allocated_bytes": 0, "files": 0, "state": "COMPLETE"}
        pending = [root]
        while pending:
            path = pending.pop()
            if count >= limit or time.monotonic() >= deadline:
                value["state"] = "PARTIAL_BOUND_REACHED"
                break
            if path.is_symlink() or not path.exists():
                continue
            if path.is_dir():
                try:
                    for child in path.iterdir():
                        # The live DB/sidecars/backups are separate categories.
                        if (
                            category == "reset_private_and_staging"
                            and path == root
                            and not child.name.startswith("history-reset-")
                        ):
                            continue
                        pending.append(child)
                        count += 1
                        if count >= limit:
                            value["state"] = "PARTIAL_BOUND_REACHED"
                            break
                except PermissionError:
                    value["state"] = "PARTIAL_ACCESS"
            elif path.is_file():
                meta = path.stat()
                value["files"] += 1
                value["logical_bytes"] += meta.st_size
                key = (meta.st_dev, meta.st_ino)
                if key not in seen:
                    value["unique_allocated_bytes"] += (
                        getattr(meta, "st_blocks", (meta.st_size + 511) // 512) * 512
                    )
                    seen.add(key)
        result[category] = value
    return {
        "schema": "cryptowatcher.external-storage-inventory.v1",
        "at_ms": int(time.time() * 1000),
        "root": str(app),
        "included_in_working_budget": False,
        "files_changed": False,
        "categories": result,
        "elapsed_seconds": round(time.monotonic() - deadline + seconds, 3),
        "limitations": [
            "Metadata only; hardlinks counted physically once across categories",
            "Partial categories are lower bounds; no deletions",
        ],
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--app", type=Path, required=True)
    a = p.parse_args()
    if a.app.resolve() != a.app or a.app.is_symlink():
        raise ValueError("Use an absolute real app path")
    print(json.dumps(inventory(a.app), sort_keys=True))


if __name__ == "__main__":
    main()
