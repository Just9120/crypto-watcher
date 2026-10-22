"""Small exact instances exercise the guarded combined Linux qualification."""

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from cryptowatcher import capacity, shared_record_work
from cryptowatcher.storage import Store
from scripts import qualify_shared_boundary as boundary


def test_combined_geometry_uses_current_full_reader_and_exact_schema(tmp_path: Path) -> None:
    path = tmp_path / "owned.sqlite3"
    built = boundary.materialize(path, 3, 2)
    assert built["frames"] == 3 and built["metrics"] == 7260
    assert built["shared_groups"] == 2 and built["shared_packets"] == 42
    result = boundary.measure(path)
    assert result["seconds"] < 50
    value = result["measurement"]
    assert value["state"] == value["numeric_frames"]["state"] == "MEASURED"
    assert value["numeric_frames"]["owned_blocks"] == 192
    assert value["shared_records"]["state"] == "MEASURED"
    assert value["shared_records"]["packets_checked"] == 42
    with closing(Store(path, 233382575)) as store:
        shared_record_work.validate(store.db)
        with pytest.raises(sqlite3.IntegrityError, match="Immutable shared record"):
            store.db.execute("UPDATE market_shared_records SET initial=initial WHERE id=1")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="must be absent"):
        boundary.materialize(path, 3, 2)
    assert path.read_bytes() == before


def test_actual_last_descriptor_corruption_is_not_hidden_by_full_counts(tmp_path: Path) -> None:
    path = tmp_path / "owned.sqlite3"
    boundary.materialize(path, 1, 2)
    with closing(Store(path, 233382575)) as store:
        arena, offset = store.db.execute(
            "SELECT arena,offset FROM market_packets ORDER BY id DESC LIMIT 1"
        ).fetchone()
        with store.db.blobopen("market_packet_arenas", "payload", arena) as blob:
            blob.seek(offset + 47)
            old = blob.read(1)
            blob.seek(offset + 47)
            blob.write(bytes([old[0] ^ 1]))
    with pytest.raises(ValueError, match="Corrupt shared descriptor"):
        boundary.measure(path)


def test_exhausted_joint_deadline_never_claims_a_complete_boundary(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "owned.sqlite3"
    boundary.materialize(path, 1, 2)
    monkeypatch.setattr(boundary.numeric, "READ_SECONDS", 0)
    result = boundary.measure(path)["measurement"]
    assert result["state"] == "PARTIAL_BUDGET"
    assert result["shared_records"]["state"] != "MEASURED"
    reasons = capacity.boundary_blockers({"runtime_ledger": result})
    assert "BOUNDARY_RUNTIME_LEDGER_INCOMPLETE" in reasons


@pytest.mark.parametrize("count", [0, -1, True, boundary.GROUP_COUNT + 1])
def test_invalid_shared_geometry_creates_no_files(tmp_path: Path, count: int) -> None:
    path = tmp_path / "absent.sqlite3"
    with pytest.raises(ValueError, match="finite fixture"):
        boundary.materialize(path, 3, count)
    assert not list(tmp_path.iterdir())


def test_changed_seed_is_rejected_before_decoding(tmp_path: Path) -> None:
    path = tmp_path / "seed.json"
    path.write_bytes(boundary.SEED.read_bytes() + b" ")
    with pytest.raises(ValueError, match="pinned content"):
        boundary.seed(path)


@pytest.mark.parametrize("damage", ["initial", "spans", "coverage"])
def test_seed_geometry_is_checked_independently(tmp_path: Path, monkeypatch, damage: str) -> None:
    value = json.loads(boundary.SEED.read_bytes())
    if damage == "initial":
        value["initial"] += 1
    elif damage == "spans":
        value["spans"][0] = [0, 65]
    else:
        value["spans"][0][0] += 1
    path = tmp_path / "seed.json"
    raw = json.dumps(value).encode()
    path.write_bytes(raw)
    monkeypatch.setattr(boundary, "SEED_SHA256", hashlib.sha256(raw).hexdigest())
    with pytest.raises(ValueError, match="(seed|descriptor)"):
        boundary.seed(path)
