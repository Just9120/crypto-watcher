"""The Python and Docker native build paths must admit the same reviewed bytes."""

import json
from pathlib import Path

from scripts.build_native import verify_vendor


def test_vendor_sources_and_independent_docker_hash_pins_are_consistent():
    root = Path(__file__).resolve().parents[1]
    for name in ("yyjson", "zstd"):
        verify_vendor(root / "native/vendor" / name)
    metadata = json.loads((root / "native/vendor/yyjson/upstream.json").read_text())
    dockerfile = (root / "Dockerfile").read_text()
    for name in ("yyjson.h", "yyjson.c"):
        assert f"'{metadata['files'][name]}  {name}' | sha256sum -c -" in dockerfile
