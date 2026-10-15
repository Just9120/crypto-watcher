import hashlib
import json
import shutil
import sqlite3
import time

import pytest

from cryptowatcher import page_delta as pages
from cryptowatcher.compressed_validation import BLOCK
from cryptowatcher.storage import StateError, check_integrity, create_schema


def prepared(tmp_path, data=None):
    old = b"a" * BLOCK + b"b" * BLOCK + b"c" * 4096
    base = (tmp_path / "baseline.sqlite3").resolve()
    base.write_bytes(old)
    indexed = (tmp_path / "index").resolve()
    pages.make_index(base, indexed, expected_sha256=hashlib.sha256(old).hexdigest())
    current = (tmp_path / "current.sqlite3").resolve()
    current.write_bytes(data if data is not None else b"a" * BLOCK + b"d" * BLOCK + b"e" * BLOCK + b"tail")
    destination = (tmp_path / "patch").resolve()
    result = pages.capture_closed(
        current,
        indexed / "base.json",
        destination,
        expected_baseline_sha256=hashlib.sha256((indexed / "base.json").read_bytes()).hexdigest(),
        max_archive_bytes=1024**2,
    )
    work = (tmp_path / "working.sqlite3").resolve()
    shutil.copyfile(base, work)
    descriptor = destination / "delta.json"
    arguments = dict(
        work=work,
        descriptor=descriptor,
        expected_descriptor_sha256=hashlib.sha256(descriptor.read_bytes()).hexdigest(),
    )
    return base, current, result, arguments


@pytest.mark.parametrize(
    "data", [None, b"small", b"a" * BLOCK + b"b" * BLOCK + b"c" * 4096], ids=["grow", "shrink", "unchanged"]
)
def test_closed_snapshot_growth_shrink_and_unchanged_preserve_all_bytes(tmp_path, data):
    base, source, result, args = prepared(tmp_path, data)
    old_hash = hashlib.sha256(base.read_bytes()).hexdigest()
    restored = pages.apply_to_working_copy(**args)
    assert args["work"].read_bytes() == source.read_bytes()
    assert hashlib.sha256(base.read_bytes()).hexdigest() == old_hash
    assert result["state"] == "CAPTURED_BYTES_NOT_QUALIFIED"
    assert restored["state"] == "RECONSTRUCTED_BYTES_NOT_QUALIFIED"
    assert restored["full_source_integrity"] == "PENDING"
    assert restored["production_replacement"] is False
    assert result["changed_blocks"] < result["blocks"] or len(source.read_bytes()) < BLOCK


def test_interrupted_copy_replays_changed_pages_without_losing_original(tmp_path, monkeypatch):
    base, source, _, args = prepared(tmp_path)
    original = pages.budget
    calls = 0

    def interrupt(deadline):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("operator interrupted")
        return original(deadline)

    monkeypatch.setattr(pages, "budget", interrupt)
    with pytest.raises(RuntimeError):
        pages.apply_to_working_copy(**args)
    assert base.read_bytes() != source.read_bytes()
    monkeypatch.setattr(pages, "budget", original)
    with pytest.raises(StateError, match="resume"):
        pages.apply_to_working_copy(**args)
    pages.apply_to_working_copy(**args, resume=True)
    assert args["work"].read_bytes() == source.read_bytes()


def test_corrupted_delta_is_rejected_before_work_copy_changes(tmp_path):
    _, _, _, args = prepared(tmp_path)
    before = args["work"].read_bytes()
    data = args["descriptor"].parent / "pages.delta"
    data.write_bytes(data.read_bytes()[:-1])
    with pytest.raises(StateError, match="encoded identity"):
        pages.apply_to_working_copy(**args)
    assert args["work"].read_bytes() == before


def test_resume_does_not_hide_damage_to_unchanged_pages(tmp_path):
    _, _, _, args = prepared(tmp_path)
    pages.apply_to_working_copy(**args)
    with args["work"].open("r+b") as data:
        data.seek(0)
        data.write(b"X")
    with pytest.raises(StateError, match="Reconstructed"):
        pages.apply_to_working_copy(**args, resume=True)


def test_source_must_be_closed_and_cannot_change_during_capture(tmp_path):
    base = (tmp_path / "source").resolve()
    base.write_bytes(b"a" * 2 * BLOCK)
    index = (tmp_path / "index").resolve()
    pages.make_index(base, index, expected_sha256=hashlib.sha256(base.read_bytes()).hexdigest())
    descriptor = index / "base.json"
    args = dict(
        source=base,
        baseline=descriptor,
        destination=(tmp_path / "delta").resolve(),
        expected_baseline_sha256=hashlib.sha256(descriptor.read_bytes()).hexdigest(),
        max_archive_bytes=1024**2,
    )
    wal = base.with_name(base.name + "-wal")
    wal.write_bytes(b"not closed")
    with pytest.raises(StateError, match="closed source"):
        pages.capture_closed(**args)
    wal.unlink()

    def modify(**kw):
        with base.open("r+b") as f:
            f.write(b"changed")

    with pytest.raises(StateError, match="source changed"):
        pages.capture_closed(**args, progress=modify)
    assert not (args["destination"] / "delta.json").exists()


def test_capacity_and_foreign_resume_are_refused(tmp_path):
    _, _, _, args = prepared(tmp_path)
    pages.apply_to_working_copy(**args)
    marker = args["work"].with_name(args["work"].name + ".page-delta.json")
    value = json.loads(marker.read_text())
    value["identity"]["inode"] += 1
    marker.write_text(json.dumps(value))
    with pytest.raises(StateError, match="resume identity"):
        pages.apply_to_working_copy(**args, resume=True)


def test_roundtrip_real_sqlite_rows_and_constraints(tmp_path):
    base = (tmp_path / "base.sqlite3").resolve()
    with sqlite3.connect(base) as db:
        create_schema(db)
        db.execute("INSERT INTO runtime VALUES('old','{}')")
    index = (tmp_path / "index").resolve()
    pages.make_index(base, index, expected_sha256=hashlib.sha256(base.read_bytes()).hexdigest())
    source = (tmp_path / "source.sqlite3").resolve()
    shutil.copyfile(base, source)
    with sqlite3.connect(source) as db:
        db.execute("INSERT INTO runtime VALUES('new',?)", (json.dumps({"useful": "history" * 1000}),))
    delta = (tmp_path / "delta").resolve()
    pages.capture_closed(
        source,
        index / "base.json",
        delta,
        expected_baseline_sha256=hashlib.sha256((index / "base.json").read_bytes()).hexdigest(),
        max_archive_bytes=1024**2,
    )
    work = (tmp_path / "work.sqlite3").resolve()
    shutil.copyfile(base, work)
    pages.apply_to_working_copy(
        work,
        delta / "delta.json",
        expected_descriptor_sha256=hashlib.sha256((delta / "delta.json").read_bytes()).hexdigest(),
    )
    with sqlite3.connect(work) as db:
        check_integrity(db, deadline=time.monotonic() + 10)
        assert {row[0] for row in db.execute("SELECT key FROM runtime")} == {"old", "new"}
    assert work.read_bytes() == source.read_bytes()


def test_capture_capacity_failure_never_publishes_descriptor(tmp_path):
    _, source, _, args = prepared(tmp_path)
    descriptor = args["descriptor"].parent.parent / "index/base.json"
    destination = (tmp_path / "too-small").resolve()
    with pytest.raises(StateError, match="capacity"):
        pages.capture_closed(
            source,
            descriptor,
            destination,
            expected_baseline_sha256=hashlib.sha256(descriptor.read_bytes()).hexdigest(),
            max_archive_bytes=1,
        )
    assert not (destination / "delta.json").exists()
