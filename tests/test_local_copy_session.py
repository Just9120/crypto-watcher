import hashlib
import io
import json
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cryptowatcher import compressed_validation as cv
from cryptowatcher.frame_archive import manifest, save_index
from cryptowatcher.storage import StateError
from scripts.qualify_local_copy import WINDOW_SCHEMA, Reporter, admit_source, window_deadline

from .test_framed_rebuild import source_history


def window(now):
    return dict(
        schema=WINDOW_SCHEMA,
        owner_authorized=True,
        seconds=900,
        started_at_utc=now.isoformat(),
        ordinary_backup_cd_seconds=900,
    )


def test_resume_uses_original_absolute_window_instead_of_starting_another_budget():
    now = datetime.now(timezone.utc)
    v = window(now - timedelta(seconds=899))
    before = time.monotonic()
    assert 0 < window_deadline(v, now=now) - before <= 1.1
    with pytest.raises(StateError, match="exhausted"):
        window_deadline(v, now=now + timedelta(seconds=2))
    v.update(seconds=5400, one_time_local=True)
    assert window_deadline(v, now=now) - before > 4490
    v["one_time_local"] = False
    with pytest.raises(StateError, match="one-time"):
        window_deadline(v, now=now)
    v.update(seconds=900, ordinary_backup_cd_seconds=901)
    with pytest.raises(StateError, match="unchanged"):
        window_deadline(v, now=now)


def test_progress_file_failure_does_not_abort_copy_or_claim_durability(tmp_path, monkeypatch, capsys):
    def unavailable(*a, **kw):
        raise PermissionError("Observer denies file replacement")

    monkeypatch.setattr(Path, "replace", unavailable)
    Reporter(tmp_path).publish(state="RUNNING", phase="COPY", rows=10000)
    result = json.loads(capsys.readouterr().out)
    assert result["progress_file"] == "UNAVAILABLE"
    assert result["rows"] == 10000
    assert "durable" not in result


def fixture_proof(tmp_path):
    import apsw

    source = tmp_path / "source.sqlite3"
    source_history(source)
    data = tmp_path / "recovery"
    data.mkdir(mode=0o700)
    deadline = time.monotonic() + 20
    image = cv.Image(source, limit=8 * 1024**2, deadline=deadline, backing=data / "source.frames")
    try:
        with apsw.Connection(str(source)) as sql:
            sql.execute("PRAGMA query_only=ON")
            cv.full_check(sql, deadline)
        descriptor = save_index(image, data, deadline=deadline)
    finally:
        image.blocks.close()
    desc = manifest(descriptor)
    archive = tmp_path / "validator.tar"
    validator = Path(cv.__file__).read_bytes()
    with tarfile.open(archive, "w") as output:
        item = tarfile.TarInfo("cryptowatcher/compressed_validation.py")
        item.size = len(validator)
        output.addfile(item, io.BytesIO(validator))
    record = dict(
        schema="cryptowatcher.frozen-source-integrity-evidence.v1",
        state="SOURCE_FULL_INTEGRITY_PASS",
        scope="HISTORICAL_SOURCE_ONLY_NOT_CANDIDATE_QUALIFICATION_OR_PROMOTION",
        production_replacement=False,
        source_record=dict(
            phase="SOURCE_FULL_INTEGRITY_PASS", raw_sha256=desc["raw_sha256"], bytes=desc["raw_bytes"]
        ),
        data_sha256=desc["data_sha256"],
        index_sha256=desc["index_sha256"],
        descriptor_sha256=hashlib.sha256(descriptor.read_bytes()).hexdigest(),
        code_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
    )
    fingerprint = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
    proof = tmp_path / "source-proof.json"
    proof.write_text(json.dumps(dict(record_sha256=fingerprint, record=record)))
    return dict(
        source=str(source),
        recovery=str(descriptor),
        code_archive=str(archive),
        source_proof=str(proof),
        source_proof_sha256=fingerprint,
    ), desc


@pytest.mark.parametrize("fault", [None, "proof", "source", "validator", "descriptor"])
def test_primary_full_sql_proof_is_bound_to_actual_record_validator_and_bytes(tmp_path, fault):
    v, descriptor = fixture_proof(tmp_path)
    if fault == "proof":
        p = Path(v["source_proof"])
        value = json.loads(p.read_text())
        value["record"]["source_record"]["bytes"] += 1
        p.write_text(json.dumps(value))
    elif fault == "source":
        descriptor["raw_sha256"] = "0" * 64
    elif fault == "validator":
        Path(v["code_archive"]).write_bytes(b"changed archive")
    elif fault == "descriptor":
        Path(v["recovery"]).write_text("changed descriptor")
    if fault is None:
        assert admit_source(v, descriptor) == v["source_proof_sha256"]
    else:
        with pytest.raises(StateError):
            admit_source(v, descriptor)


@pytest.mark.skipif(sys.platform != "win32", reason="Owner-approved Windows offload procedure")
def test_real_local_session_qualifies_small_fixture_with_hard_resource_controls(tmp_path):
    v, _ = fixture_proof(tmp_path)
    v.update(window(datetime.now(timezone.utc)))
    v.update(work=str(tmp_path / "candidate"), maximum=8 * 1024**2, owner=233382575)
    config = tmp_path / "window.json"
    config.write_text(json.dumps(v))
    r = subprocess.run(
        [sys.executable, "-m", "scripts.qualify_local_copy", "--window", str(config)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    result = json.loads((Path(v["work"]) / "qualification.json").read_text())
    assert result["state"] == "QUALIFIED_SNAPSHOT_CANDIDATE"
    assert result["limits"]["process_memory_limit_bytes"] == 4 * 1024**3
    assert result["limits"]["logical_cpu_affinity_mask"] == 1
    assert result["production_replacement"] is False
