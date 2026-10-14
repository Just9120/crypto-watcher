import hashlib
import json
import sqlite3
import sys
import time
from contextlib import closing

import pytest

from cryptowatcher.config import OwnerSettings
from cryptowatcher.events import HEAD_FIELDS, HEAD_SCHEMA, PREFIX, History
from cryptowatcher.framed_rebuild import build_candidate, copy_validated, metadata
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


def test_closed_gzip_qualification_and_recopy_preserve_history(tmp_path):
    import gzip

    from cryptowatcher.compressed_validation import disk_backing

    source = (tmp_path / "source.sqlite3").resolve()
    expected = source_history(source)
    archive = (tmp_path / "source.gz").resolve()
    with gzip.open(archive, "wb") as output:
        output.write(source.read_bytes())
    work = (tmp_path / "work").resolve()
    work.mkdir(mode=0o700)
    if not disk_backing(work / "source.frames"):
        pytest.skip("Qualification requires a disk-backed workspace")
    before = hashlib.sha256(archive.read_bytes()).hexdigest()
    report = build_candidate(
        archive,
        work,
        owner=233382575,
        encoded_limit=8 * 1024**2,
        maximum=8 * 1024**2,
        seconds=30,
        archive_encoding="GZIP",
    )
    assert report["source_full_integrity"] == "PASS"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == before
    recopy = tmp_path / "recopy.sqlite3"
    with closing(sqlite3.connect((work / "candidate.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        result = copy_validated(
            db, recopy, owner=233382575, maximum=8 * 1024**2, deadline=time.monotonic() + 30
        )
    assert result["collapsed_heads"] == 0
    with closing(Store(recopy, 233382575)) as target:
        assert all(History(target).get(key) == value for key, value in expected.items())
        assert target.db.execute("PRAGMA user_version").fetchone()[0] == 2


def test_native_projection_matches_sql_for_exact_numbers_and_compact_reference():
    from cryptowatcher.native_history import load

    if sys.platform != "linux":
        pytest.skip("Required Linux/image native artifact check")
    accelerator = load("native")
    with sqlite3.connect(":memory:") as db:
        for kind, fields in HEAD_FIELDS.items():
            for number in (1, 1.0, -0.0, 5e-324, 1.0000000000000002, 2**100):
                value = {name: number for name in fields}
                value.update(schema=HEAD_SCHEMA, revision_key=f"{kind}:exact:0000000001")
                payload = json.dumps(value)
                expected = metadata(db, payload, kind)
                actual = metadata(db, payload, kind, accelerator)
                assert actual == expected
                assert json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)


def test_copy_without_compressible_records_retains_version1(tmp_path):
    source, target = tmp_path / "source.sqlite3", tmp_path / "target.sqlite3"
    with closing(Store(source, 233382575)) as store:
        store.initialize(OwnerSettings())
        store.put_runtime("pending:job", {"identity": "keep", "ready": False})
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        report = copy_validated(db, target, owner=233382575, maximum=1024**2, deadline=time.monotonic() + 15)
    assert report["framed_records"] == report["collapsed_heads"] == 0
    with closing(Store(target, 233382575)) as store:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert store.get_runtime("pending:job") == {"identity": "keep", "ready": False}


def test_framed_history_keeps_filtered_export_cohorts_and_causal_cutoff(tmp_path):
    from cryptowatcher.analytics import cohorts
    from cryptowatcher.export import Selection, ndjson
    from cryptowatcher.storage import encode

    from .test_export import DAY, NOW, populate

    source, target = tmp_path / "source.sqlite3", tmp_path / "target.sqlite3"
    with closing(Store(source, 233382575)) as store:
        store.initialize(OwnerSettings())
        history = populate(store, count=3)
        with store.db:
            rows = store.db.execute(
                "SELECT key,payload_json FROM runtime WHERE key>=? AND key<?", (PREFIX, PREFIX + "~")
            ).fetchall()
            for key, payload in rows:
                kind = key.removeprefix(PREFIX).partition(":")[0]
                if kind in HEAD_FIELDS:
                    value = history.codec.read(payload)
                    value["original_note"] = "retained-history " * 200
                    store.db.execute("UPDATE runtime SET payload_json=? WHERE key=?", (encode(value), key))
        before_cohorts = cohorts(store)
        before_causal = cohorts(store, cutoff_ms=NOW)
        before_late = cohorts(store, cutoff_ms=NOW + DAY)
    selection = Selection(detector="price", market="spot")
    with closing(Store(source, 233382575, readonly=True)) as store:
        before_export = [json.loads(row) for row in ndjson(store, selection)]
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        report = copy_validated(
            db, target, owner=233382575, maximum=8 * 1024**2, deadline=time.monotonic() + 30
        )
    assert report["framed_records"] >= 15
    with closing(Store(target, 233382575, readonly=True)) as store:
        assert cohorts(store) == before_cohorts
        assert cohorts(store, cutoff_ms=NOW) == before_causal
        assert cohorts(store, cutoff_ms=NOW + DAY) == before_late
        after_export = [json.loads(row) for row in ndjson(store, selection)]
    for records in (before_export, after_export):
        records[0].pop("snapshot_at_ms")
    assert after_export == before_export


def test_compact_candles_preserve_source_windows_and_future_outcome_rows(tmp_path):
    from dataclasses import asdict

    from cryptowatcher.outcomes import read_candles
    from cryptowatcher.sources import Candle, Sources
    from cryptowatcher.storage import encode

    source, target = tmp_path / "source.sqlite3", tmp_path / "target.sqlite3"
    start = 1800000000000
    candles = [
        Candle.parse(
            [start + i * 60000, 100, 102, 99, 101, 1, 400],
            "1m",
            "rest",
            start + (i + 1) * 60000,
            start + (i + 1) * 60000,
        )
        for i in range(64)
    ]
    with closing(Store(source, 233382575)) as store:
        store.initialize(OwnerSettings())
        sources = Sources(store)
        sources.put("spot/BTCUSDT/1m", candles)
        before = sources.rows("spot/BTCUSDT/1m", 64)
        raw = {row[0]: row[1] for row in store.db.execute("SELECT key,payload_json FROM runtime")}
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        report = copy_validated(db, target, owner=233382575, maximum=1024**2, deadline=time.monotonic() + 15)
    assert report["compact_candles"] == 64
    assert target.stat().st_size < source.stat().st_size
    with closing(Store(target, 233382575)) as store:
        sources = Sources(store)
        assert sources.rows("spot/BTCUSDT/1m", 64) == before
        assert read_candles(store, "spot/BTCUSDT/1m", start, start + 64 * 60000) == candles
        for candle in candles:
            key = Sources.prefix("spot/BTCUSDT/1m") + f"{candle.start:016d}"
            assert store.get_runtime(key) == asdict(candle)
            assert encode(store.get_runtime(key)) == raw[key]
        sources.put("spot/BTCUSDT/1m", candles, live=True)
        assert sources.rows("spot/BTCUSDT/1m", 64) == before
