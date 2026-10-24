"""Retain the exact immutable Actions ZIP for independent receiver verification."""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

if __package__:
    from .deploy_receiver import extract_trusted_artifact
else:
    from deploy_receiver import extract_trusted_artifact


def download(revision: str, run_id: int, destination: Path) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or run_id <= 0:
        raise ValueError("Invalid release identity")
    repository = "Just9120/crypto-watcher"
    data = json.loads(
        subprocess.check_output(
            [
                "gh",
                "api",
                f"repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100",
            ],
            timeout=30,
        )
    )
    candidates = [a for a in data["artifacts"] if a["name"] == f"cryptowatcher-{revision}-{run_id}"]
    if data["total_count"] > 100 or len(candidates) != 1 or candidates[0]["expired"]:
        raise ValueError("Ambiguous release artifact")
    artifact = candidates[0]
    if type(artifact["id"]) is not int or artifact["id"] <= 0:
        raise ValueError("Invalid artifact identity")
    destination.mkdir(exist_ok=False)
    path = destination / "artifact.zip"
    with path.open("xb") as output:
        subprocess.run(
            ["gh", "api", f"repos/{repository}/actions/artifacts/{artifact['id']}/zip"],
            stdout=output,
            check=True,
            timeout=180,
        )
    with path.open("rb") as source:
        digest = "sha256:" + hashlib.file_digest(source, "sha256").hexdigest()
    if digest != artifact["digest"] or path.stat().st_size != artifact["size_in_bytes"]:
        raise ValueError("Downloaded artifact differs from GitHub digest")
    extract_trusted_artifact(path, destination)
    (destination / "artifact-manifest.json").rename(destination / "manifest.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("revision")
    parser.add_argument("run_id", type=int)
    parser.add_argument("--output", type=Path, default=Path("release"))
    args = parser.parse_args()
    download(args.revision, args.run_id, args.output)
