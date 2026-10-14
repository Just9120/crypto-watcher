import hashlib
import json
import sqlite3
import time
from contextlib import closing

import pytest

from cryptowatcher.config import OwnerSettings
from cryptowatcher.events import HEAD_FIELDS, PREFIX, History
from cryptowatcher.framed_rebuild import build_candidate, copy_validated
from cryptowatcher.storage import StateError, Store, check_integrity

from .test_history_heads import record


def source_history(path):
    store = Store(path, 233382575)
    store.initialize(OwnerSettings())
    history = History(store)
    expected = {}
    with store.db:
        store.put_runtime("transport:immutable", {"pending": True, "bytes": "exact"})
        for kind in HEAD_FIELDS:
            for index in range(12):
                identifier = f"record-{index}"
                value = record(kind, identifier)
                value[kind + "_id"] = identifier
                reference = f"{kind}:{identifier}:0000000001"
                if index % 2:
                    history.version(kind, value)
                else:
                    history.put(reference, value)
                    history.put(kind + "-head:" + identifier, value)
                expected[kind + "-head:" + identifier] = value
    store.close()
    return expected


def test_framed_copy_shrinks_mixed_history_and_preserves_every_value(tmp_path):
    source, candidate = tmp_path / "source.sqlite3", tmp_path / "candidate.sqlite3"
    expected = source_history(source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    deadline = time.monotonic() + 30
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        check_integrity(db, deadline=deadline)
        result = copy_validated(db, candidate, owner=233382575, maximum=8 * 1024**2, deadline=deadline)
    assert result["state"] == "CANDIDATE_ROWS_VERIFIED"
    assert result["framed_records"] == result["collapsed_heads"] == 24
    assert candidate.stat().st_size < source.stat().st_size
    with closing(Store(candidate, 233382575)) as store:
        history = History(store)
        for key, value in expected.items():
            assert history.get(key) == value
        assert store.get_runtime("transport:immutable") == {"pending": True, "bytes": "exact"}
        assert store.snapshot().revision == 1
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


@pytest.mark.parametrize(
    "fault",
    ["missing_revision", "corrupt_blob", "unknown_trigger", "deadline", "existing", "bad_existing_head"],
)
def test_framed_copy_refuses_and_never_changes_source(tmp_path, fault):
    source, candidate = tmp_path / "source.sqlite3", tmp_path / "candidate.sqlite3"
    source_history(source)
    with sqlite3.connect(source) as db:
        if fault == "missing_revision":
            db.execute("DELETE FROM runtime WHERE key=?", (PREFIX + "event:record-0:0000000001",))
        elif fault == "corrupt_blob":
            db.execute("UPDATE runtime SET payload_json='{}' WHERE key GLOB 'historyblob:v1:*'")
        elif fault == "unknown_trigger":
            db.execute("CREATE TRIGGER unknown_rule AFTER INSERT ON runtime BEGIN SELECT 1; END")
        elif fault == "bad_existing_head":
            key = PREFIX + "event-head:record-1"
            value = json.loads(
                db.execute("SELECT payload_json FROM runtime WHERE key=?", (key,)).fetchone()[0]
            )
            value["strength"] += 1
            db.execute("UPDATE runtime SET payload_json=? WHERE key=?", (json.dumps(value), key))
    if fault == "existing":
        candidate.write_bytes(b"user candidate")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    deadline = time.monotonic() + (-1 if fault == "deadline" else 30)
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        with pytest.raises(StateError):
            copy_validated(db, candidate, owner=233382575, maximum=8 * 1024**2, deadline=deadline)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    if fault == "existing":
        assert candidate.read_bytes() == b"user candidate"


def test_one_budget_qualification_covers_source_candidate_and_recovery(tmp_path):
    from cryptowatcher.compressed_validation import disk_backing
    from cryptowatcher.frame_archive import verify

    source = (tmp_path / "source.sqlite3").resolve()
    expected = source_history(source)
    work = (tmp_path / "work").resolve()
    work.mkdir(mode=0o700)
    if not disk_backing(work / "source.frames"):
        pytest.skip("Qualification requires a disk-backed workspace")
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    report = build_candidate(
        source, work, owner=233382575, encoded_limit=8 * 1024**2, maximum=8 * 1024**2, seconds=30
    )
    assert report["state"] == "QUALIFIED_SNAPSHOT_CANDIDATE"
    assert report["source_full_integrity"] == "PASS"
    assert report["recovery"]["state"] == "RECOVERY_BYTES_VERIFIED"
    assert report["production_replacement"] is False
    assert report["elapsed_seconds"] < 30
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    with closing(Store(work / "candidate.sqlite3", 233382575)) as target:
        history = History(target)
        assert all(history.get(key) == value for key, value in expected.items())
    assert (
        verify(work / "source.recovery.json", deadline=time.monotonic() + 15)["state"]
        == "RECOVERY_BYTES_VERIFIED"
    )


def test_whole_qualification_cannot_extend_budget_or_overwrite_work(tmp_path):
    source = (tmp_path / "source.sqlite3").resolve()
    source_history(source)
    with pytest.raises(StateError, match="900s"):
        build_candidate(
            source, tmp_path.resolve(), owner=233382575, encoded_limit=1024, maximum=1024, seconds=901
        )
    (tmp_path / "candidate.sqlite3").write_bytes(b"keep")
    with pytest.raises(StateError, match="Fresh qualification"):
        build_candidate(source, tmp_path.resolve(), owner=233382575, encoded_limit=1024, maximum=1024)
    assert (tmp_path / "candidate.sqlite3").read_bytes() == b"keep"
