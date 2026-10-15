import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

from cryptowatcher.copy_checkpoint import TABLE
from cryptowatcher.events import History
from cryptowatcher.framed_rebuild import copy_validated
from cryptowatcher.history_rebuild import TABLES
from cryptowatcher.storage import StateError, Store, check_integrity

from .test_framed_rebuild import source_history


def invoke(source, candidate, identity, *, resume=False, progress=lambda **kw: None, **options):
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        deadline = time.monotonic() + 30
        check_integrity(db, deadline=deadline)
        return copy_validated(
            db,
            candidate,
            owner=233382575,
            maximum=8 * 1024**2,
            deadline=deadline,
            source_identity=identity,
            resume=resume,
            checkpoint_rows=4,
            progress=progress,
            **options,
        )


def stop_after_commit(**v):
    if v["phase"] == "COPY" and v["table"] == "runtime":
        assert v["durable"] and v["rows"] == 4
        raise RuntimeError("Reporting file unavailable")


def interrupted(tmp_path):
    source, candidate = tmp_path / "source.sqlite3", tmp_path / "candidate.sqlite3"
    expected = source_history(source)
    identity = hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="Reporting"):
        invoke(source, candidate, identity, progress=stop_after_commit)
    return source, candidate, identity, expected


def test_reporting_error_keeps_committed_rows_and_resume_skips_them(tmp_path):
    source, candidate, identity, expected = interrupted(tmp_path)
    with sqlite3.connect(candidate) as db:
        assert db.execute("SELECT count(*) FROM runtime").fetchone()[0] == 4
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        state = json.loads(db.execute(f"SELECT state_json FROM {TABLE}").fetchone()[0])
        assert state["rows"] == 4
    with pytest.raises(StateError, match="Unsupported"):
        Store(candidate, 233382575, readonly=True)
    progress = []
    result = invoke(source, candidate, identity, resume=True, progress=lambda **v: progress.append(v))
    assert result["copy_resumed"] is True
    assert min(v["rows"] for v in progress if v["phase"] == "COPY") == 8
    assert result["framed_records"] == result["collapsed_heads"] == 24
    with closing(Store(candidate, 233382575, readonly=True)) as store:
        assert all(History(store).get(key) == value for key, value in expected.items())
        names = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert names == set(TABLES)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == identity


def test_hard_exit_mid_batch_recovers_to_the_atomic_checkpoint(tmp_path):
    source, candidate = tmp_path / "source.sqlite3", tmp_path / "candidate.sqlite3"
    expected = source_history(source)
    identity = hashlib.sha256(source.read_bytes()).hexdigest()
    script = """
import os,sqlite3,sys,time
from pathlib import Path
import cryptowatcher.framed_rebuild as f
source=sqlite3.connect(Path(sys.argv[1]).as_uri()+'?mode=ro',uri=True)
source.execute('PRAGMA query_only=ON')
original=sqlite3.connect
seen=0
def connect(*a,**kw):
    db=original(*a,**kw)
    def trace(sql):
        global seen
        if sql.startswith('INSERT INTO runtime'):
            seen+=1
            if seen==6:os._exit(73)
    db.set_trace_callback(trace)
    return db
f.sqlite3.connect=connect
f.copy_validated(source,Path(sys.argv[2]),owner=233382575,maximum=8*1024**2,
    deadline=time.monotonic()+20,source_identity=sys.argv[3],checkpoint_rows=4)
raise AssertionError('Kill point was not reached')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(source), str(candidate), identity],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 73, result.stderr
    with sqlite3.connect(candidate) as db:
        assert db.execute("SELECT count(*) FROM runtime").fetchone()[0] == 4
        assert json.loads(db.execute(f"SELECT state_json FROM {TABLE}").fetchone()[0])["rows"] == 4
    assert invoke(source, candidate, identity, resume=True)["state"] == "CANDIDATE_ROWS_VERIFIED"
    with closing(Store(candidate, 233382575, readonly=True)) as store:
        assert all(History(store).get(key) == value for key, value in expected.items())
    assert hashlib.sha256(source.read_bytes()).hexdigest() == identity


@pytest.mark.parametrize("fault", ["identity", "cursor", "state", "row", "schema", "version"])
def test_bad_checkpoint_never_becomes_a_qualified_copy(tmp_path, fault):
    source, candidate, identity, _ = interrupted(tmp_path)
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    if fault == "identity":
        supplied = "0" * 64
    else:
        supplied = identity
        with sqlite3.connect(candidate) as db:
            if fault == "row":
                db.execute("UPDATE runtime SET payload_json='{}' WHERE key='transport:immutable'")
            elif fault == "schema":
                db.execute("CREATE TRIGGER unexpected AFTER INSERT ON runtime BEGIN SELECT 1; END")
            elif fault == "version":
                db.execute("PRAGMA user_version=1")
            else:
                state = json.loads(db.execute(f"SELECT state_json FROM {TABLE}").fetchone()[0])
                state["cursor" if fault == "cursor" else "table_index"] = 2**60
                db.execute(f"UPDATE {TABLE} SET state_json=?", (json.dumps(state),))
    with pytest.raises(StateError):
        invoke(source, candidate, supplied, resume=True)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    with sqlite3.connect(candidate) as db:
        assert db.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0] == 1


def test_restart_after_finalization_rechecks_without_copying(tmp_path):
    source, candidate = tmp_path / "source.sqlite3", tmp_path / "candidate.sqlite3"
    source_history(source)
    identity = hashlib.sha256(source.read_bytes()).hexdigest()

    def stop_before_full_integrity(**v):
        if v["phase"] == "CANDIDATE_FULL_INTEGRITY":
            raise RuntimeError("Interrupted verification")

    with pytest.raises(RuntimeError, match="Interrupted"):
        invoke(source, candidate, identity, progress=stop_before_full_integrity)
    before = hashlib.sha256(candidate.read_bytes()).hexdigest()
    progress = []
    result = invoke(source, candidate, identity, resume=True, progress=lambda **v: progress.append(v))
    assert result["state"] == "CANDIDATE_ROWS_VERIFIED"
    assert not any(v["phase"] == "COPY" for v in progress)
    assert hashlib.sha256(candidate.read_bytes()).hexdigest() == before
