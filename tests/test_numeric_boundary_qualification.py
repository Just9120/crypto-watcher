"""The scale qualification uses current cold readers and refuses reused data."""

import hashlib
import json
from pathlib import Path

import pytest

from scripts import qualify_numeric_boundary as boundary


def test_small_complete_geometry_uses_current_guards_and_cold_reader(tmp_path: Path) -> None:
    path = tmp_path / "owned.sqlite3"
    result = boundary.materialize(path, 3)
    assert result["frames"] == 3 and result["owners"] == 192 and result["metrics"] == 7260
    measured = boundary.measure(path)
    assert measured["measurement"]["state"] == "MEASURED"
    with pytest.raises(ValueError, match="must be absent"):
        boundary.materialize(path, 3)


@pytest.mark.parametrize("count", [0, -1, True, boundary.FRAME_COUNT + 1])
def test_invalid_geometry_has_no_created_database(tmp_path: Path, count: int) -> None:
    path = tmp_path / "absent.sqlite3"
    with pytest.raises(ValueError, match="finite fixture"):
        boundary.materialize(path, count)
    assert not path.exists()


def test_seed_identity_is_pinned_before_decoding(tmp_path: Path) -> None:
    path = tmp_path / "seed.json"
    path.write_bytes(boundary.SEED.read_bytes() + b" ")
    with pytest.raises(ValueError, match="pinned content"):
        boundary.seed(path)


def test_an_existing_sidecar_prevents_fixture_creation(tmp_path: Path) -> None:
    path = tmp_path / "absent.sqlite3"
    sidecar = Path(str(path) + "-wal")
    sidecar.write_bytes(b"preserve unknown data")
    with pytest.raises(ValueError, match="must be absent"):
        boundary.materialize(path, 3)
    assert not path.exists() and sidecar.read_bytes() == b"preserve unknown data"


def test_seed_cardinality_is_checked_independently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = json.loads(boundary.SEED.read_bytes())
    value["quality_leases"].pop()
    path = tmp_path / "seed.json"
    raw = json.dumps(value).encode()
    path.write_bytes(raw)
    monkeypatch.setattr(boundary, "SEED_SHA256", hashlib.sha256(raw).hexdigest())
    with pytest.raises(ValueError, match="seed shape"):
        boundary.seed(path)
