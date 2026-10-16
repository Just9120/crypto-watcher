"""Bind a Docker image archive and Compose file to exact source/build identity."""

import argparse
import gzip
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

if __package__:
    from .deploy_receiver import archive_image_identity, loaded_image_id, run
else:
    from deploy_receiver import archive_image_identity, loaded_image_id, run


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("revision")
    parser.add_argument("run_id", type=int)
    parser.add_argument("image")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.revision) or args.run_id <= 0:
        raise ValueError("Invalid release identity")
    inspection = json.loads(subprocess.check_output(["docker", "image", "inspect", args.image]))[0]
    if inspection["Config"]["Labels"].get("org.opencontainers.image.revision") != args.revision:
        raise ValueError("Image source mismatch")
    output = args.output
    output.mkdir(parents=True, exist_ok=False)
    archive = output / "image.tar.gz"
    with (
        archive.open("wb") as destination,
        gzip.GzipFile(fileobj=destination, mode="wb", mtime=0) as compressed,
    ):
        process = subprocess.Popen(["docker", "save", args.image], stdout=subprocess.PIPE)
        assert process.stdout is not None
        shutil.copyfileobj(process.stdout, compressed)
        process.stdout.close()
        if process.wait():
            raise RuntimeError("Image archive failed")
    shutil.copyfile("docker-compose.yml", output / "docker-compose.yml")
    manifest = {
        "schema": "cryptowatcher.release.v1",
        "source_sha": args.revision,
        "build_run": args.run_id,
        "image_id": inspection["Id"],
        "image_archive_sha256": sha256(archive),
        "compose_sha256": sha256(output / "docker-compose.yml"),
        "database_schema": 1,
        "supported_database_schemas": [1, 2],
        "migration_class": "NONE",
        "architecture": inspection["Architecture"],
    }
    identity = archive_image_identity(archive, manifest)
    run(["docker", "load", "-i", str(archive)], timeout=180)
    loaded_image_id(identity, manifest)
    manifest.update(
        image_config_digest=identity["config_digest"],
        image_manifest_digest=identity["manifest_digest"],
        archive_round_trip="PASS",
    )
    (output / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    bundle = output / "bundle.tar"
    with tarfile.open(bundle, "w") as handle:
        for name in ("image.tar.gz", "manifest.json", "docker-compose.yml"):
            handle.add(output / name, arcname=name, recursive=False)
    (output / "bundle.sha256").write_text(sha256(bundle), encoding="ascii")
    print(json.dumps(manifest, sort_keys=True))


if __name__ == "__main__":
    main()
