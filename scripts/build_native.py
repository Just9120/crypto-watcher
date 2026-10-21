"""Build history, frozen-VFS and bounded numeric byte/owner proof helpers."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify_vendor(directory: Path) -> None:
    metadata = json.loads((directory / "upstream.json").read_text())
    for name, expected in metadata["files"].items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError("Native upstream content differs from pinned source")


def build_vfs(compiler: str, output: Path) -> None:
    sqlite = ROOT / "native/vendor/sqlite"
    zstd = ROOT / "native/vendor/zstd"
    verify_vendor(sqlite)
    verify_vendor(zstd)
    subprocess.run(
        ["bash", str(ROOT / "native/build_vfs.sh"), str(output.resolve()), compiler],
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compiler", default="gcc")
    parser.add_argument("--output", type=Path, default=ROOT / "cryptowatcher/_native/history.so")
    args = parser.parse_args()
    vendor = ROOT / "native/vendor/yyjson"
    metadata = json.loads((vendor / "upstream.json").read_text())
    for name in ("yyjson.c", "yyjson.h", "LICENSE"):
        if hashlib.sha256((vendor / name).read_bytes()).hexdigest() != metadata["files"][name]:
            raise RuntimeError("Native upstream content differs from pinned source")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    build_vfs(args.compiler, args.output.with_name("compressed_vfs.so"))
    numeric = args.output.with_name("numeric_frame.so")
    subprocess.run(
        ["bash", str(ROOT / "native/build_numeric.sh"), str(numeric.resolve()), args.compiler],
        check=True,
    )
    subprocess.run(
        [
            args.compiler,
            "-std=c99",
            "-O3",
            "-fPIC",
            "-shared",
            "-I",
            str(vendor),
            str(vendor / "yyjson.c"),
            str(ROOT / "native/history_extract.c"),
            "-lm",
            "-o",
            str(args.output),
        ],
        check=True,
    )
    print(
        json.dumps(
            {
                "native_build": "PASS",
                "upstream": metadata["revision"],
                "artifact_sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
                "numeric_frame_sha256": hashlib.sha256(numeric.read_bytes()).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
