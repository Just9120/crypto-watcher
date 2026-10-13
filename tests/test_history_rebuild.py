import hashlib
import json
import os
import sqlite3
import sys
import time
from contextlib import closing

import pytest

from cryptowatcher.config import OwnerSettings
from cryptowatcher.events import PREFIX, History
from cryptowatcher.history_rebuild import _CopyCodec, feed, rebuild
from cryptowatcher.storage import IntegrityError, StateError, Store, encode

from .test_history_heads import record


@pytest.mark.parametrize("kind", ["event", "outcome", "episode", "comparison"])
def test_head_verification_keeps_large_body_validation_in_full_readback(store, kind):
    from cryptowatcher.history_codec import BLOB_PREFIX, MARKER
    from cryptowatcher.history_rebuild import verified_head_revision

    history = History(store)
    value = record(kind)
    value[f"{kind}_id"] = "one"
    with store.db:
        history.version(kind, value)
    head = store.get_runtime(PREFIX + f"{kind}-head:one")
    stored_revision = verified_head_revision(history, f"{kind}-head:one", head)
    # Identity/projection checks retain the packed body for one full restoration.
    assert MARKER in stored_revision and "source_inputs" not in stored_revision
    assert history.codec.canonical_json(stored_revision) == encode(value).encode()
    body = stored_revision[MARKER]["fields"]["source_inputs"]["ids"][0]
    with store.db:
        store.db.execute("DELETE FROM runtime WHERE key=?", (BLOB_PREFIX + body,))
    verified_head_revision(history, f"{kind}-head:one", head)
    # The subsequent mandatory full readback still rejects missing source data.
    with pytest.raises(StateError, match="Missing shared snapshot"):
        history.codec.canonical_json(stored_revision)


def test_logical_manifest_preserves_types_and_field_boundaries():
    values = [(1,), (1.0,), ("1",), (None,), ("null",), ("a", "bc"), ("ab", "c"), ("a",), ("a", "")]
    hashes = []
    for row in values:
        manifest = hashlib.sha256()
        feed(manifest, row)
        hashes.append(manifest.digest())
    assert len(set(hashes)) == len(values)


def test_manifest_batching_retains_existing_v2_wire_bytes_at_buffer_boundaries():
    rows = [
        (),
        (None, False, 1, 1.0, -0.0, 2**100),
        ("Пример\x00", "😀"),
        ("x" * 4096, "y" * 4097, ""),
        tuple("z" * 4000 for _ in range(18)),
        ("large" * 20000, "tail"),
    ]
    legacy = hashlib.sha256()
    actual = hashlib.sha256()
    for row in rows:
        legacy.update(len(row).to_bytes(8, "big"))
        for value in row:
            raw = value.encode() if isinstance(value, str) else encode(value).encode()
            legacy.update(b"s" if isinstance(value, str) else b"j")
            legacy.update(len(raw).to_bytes(8, "big"))
            legacy.update(raw)
        feed(actual, row)
    assert actual.digest() == legacy.digest()


def test_source_cache_release_preserves_frozen_bytes_and_readonly_snapshot(tmp_path):
    import apsw

    from cryptowatcher.compressed_validation import Image, frozen_vfs
    from cryptowatcher.history_rebuild import release_source_cache

    source = tmp_path / "cache-source.sqlite3"
    with closing(sqlite3.connect(source)) as db:
        db.execute("CREATE TABLE samples (id INTEGER PRIMARY KEY, payload BLOB)")
        db.executemany(
            "INSERT INTO samples VALUES (?,?)", ((i, bytes([i % 256]) * 4096) for i in range(2048))
        )
        db.commit()
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    deadline = time.monotonic() + 20
    image = Image(source, limit=32 * 1024**2, deadline=deadline)
    sentinel = tmp_path / "sentinel.sqlite3"
    sentinel.touch()
    vfs = frozen_vfs(image, sentinel)
    db = apsw.Connection(str(sentinel), flags=apsw.SQLITE_OPEN_READWRITE, vfs=vfs.name)
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA cache_size=-8192")
        assert list(db.execute("SELECT SUM(length(payload)) FROM samples")) == [(2048 * 4096,)]
        before = db.status(apsw.SQLITE_DBSTATUS_CACHE_USED)[0]
        release_source_cache(db, deadline)
        assert db.status(apsw.SQLITE_DBSTATUS_CACHE_USED)[0] < before
        assert list(db.execute("PRAGMA query_only")) == [(1,)]
        assert list(db.execute("SELECT payload FROM samples WHERE id=1025")) == [(b"\x01" * 4096,)]
        assert image.reconstructed_hash() == expected
        assert hashlib.sha256(source.read_bytes()).hexdigest() == expected
    finally:
        db.close()
        vfs.unregister()


@pytest.mark.parametrize("encoding", ["gzip", "zstd"])
def test_archive_rebuild_preserves_full_rows_without_a_raw_source_copy(tmp_path, encoding):
    import gzip

    import zstandard

    source, _ = closed_backup(tmp_path)
    with sqlite3.connect(source) as db:
        for key in (PREFIX + "event:record-0:0000000001", PREFIX + "event-head:record-0"):
            value = json.loads(
                db.execute("SELECT payload_json FROM runtime WHERE key=?", (key,)).fetchone()[0]
            )
            value["strength"] = 2.095370825591371
            value["source_inputs"] = [
                {"start": 60000, "step": 60000, "close": -0.0, "integer": 2**100 + 123, "label": "Пример"}
            ]
            db.execute("UPDATE runtime SET payload_json=? WHERE key=?", (encode(value), key))
    original = source.read_bytes()
    expected = hashlib.sha256(original).hexdigest()
    archive = tmp_path / ("source." + encoding)
    archive.write_bytes(
        gzip.compress(original) if encoding == "gzip" else zstandard.ZstdCompressor().compress(original)
    )
    archive_bytes = archive.read_bytes()
    raw_result = rebuild(
        source, tmp_path / "raw-candidate.sqlite3", 233382575, expected, time.monotonic() + 10
    )
    result = rebuild(
        archive,
        tmp_path / "archive-candidate.sqlite3",
        233382575,
        expected,
        time.monotonic() + 10,
        max_candidate_bytes=2 * 1024 * 1024,
        source_encoding=encoding,
    )
    assert result["logical_row_manifests"] == raw_result["logical_row_manifests"]
    assert result["table_rows"] == raw_result["table_rows"]
    assert result["candidate_full_integrity"] == result["source_full_qualification"] == "PASS"
    assert result["source_sha256"] == result["source_validation"]["sha256"] == expected
    assert result["source_bytes"] == len(original)
    assert result["source_archive_sha256"] == hashlib.sha256(archive_bytes).hexdigest()
    assert source.read_bytes() == original and archive.read_bytes() == archive_bytes
    assert not list(tmp_path.glob("cw-rebuild-source-*"))


def test_archive_rebuild_rejects_unqualified_hash_and_check_violation(tmp_path):
    import gzip

    source, _ = closed_backup(tmp_path)
    archive = tmp_path / "source.gz"
    archive.write_bytes(gzip.compress(source.read_bytes()))
    with pytest.raises(StateError, match="explicit candidate byte limit"):
        rebuild(
            archive,
            tmp_path / "unbounded.sqlite3",
            233382575,
            "0" * 64,
            time.monotonic() + 10,
            source_encoding="gzip",
        )
    with pytest.raises(StateError, match="Decoded source fingerprint"):
        rebuild(
            archive,
            tmp_path / "wrong-hash.sqlite3",
            233382575,
            "0" * 64,
            time.monotonic() + 10,
            max_candidate_bytes=2**20,
            source_encoding="gzip",
        )
    with sqlite3.connect(source) as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute("UPDATE owner_settings SET revision=0")
    corrupted = source.read_bytes()
    archive.write_bytes(gzip.compress(corrupted))
    preserved = archive.read_bytes()
    with pytest.raises(IntegrityError) as failure:
        rebuild(
            archive,
            tmp_path / "corrupt-candidate.sqlite3",
            233382575,
            hashlib.sha256(corrupted).hexdigest(),
            time.monotonic() + 10,
            max_candidate_bytes=2**20,
            source_encoding="gzip",
        )
    assert failure.value.code == "DATABASE_CORRUPT"
    assert archive.read_bytes() == preserved and source.read_bytes() == corrupted


def test_truncated_archive_is_classified_and_never_qualified(tmp_path):
    import gzip

    source, _ = closed_backup(tmp_path)
    raw = source.read_bytes()
    archive = tmp_path / "truncated.gz"
    broken = gzip.compress(raw)[:-7]
    archive.write_bytes(broken)
    with pytest.raises(IntegrityError) as failure:
        rebuild(
            archive,
            tmp_path / "candidate.sqlite3",
            233382575,
            hashlib.sha256(raw).hexdigest(),
            time.monotonic() + 10,
            max_candidate_bytes=2**20,
            source_encoding="gzip",
        )
    assert failure.value.code == "DATABASE_ERROR"
    assert archive.read_bytes() == broken and source.read_bytes() == raw


def test_historical_frozen_windows_share_one_lossless_value_while_live_uses_blocks(tmp_path):
    from cryptowatcher.history_codec import MARKER, Codec

    window = [{"start": i * 60000, "step": 60000, "close": 2.095370825591371} for i in range(40)]
    value = {"source_inputs": window, "future_inputs": window, "strength": 2.095370825591371}
    with closing(Store(tmp_path / "candidate.sqlite3", 233382575)) as store:
        copier = _CopyCodec(store.db)
        live = Codec(store.db)
        with store.db:
            old = copier.pack_canonical(encode(value))
            current = live.pack(value)
        fields = old[MARKER]["fields"]
        assert fields["source_inputs"]["mode"] == "value"
        assert fields["source_inputs"] == fields["future_inputs"]
        assert len(fields["source_inputs"]["ids"]) == 1
        assert current[MARKER]["fields"]["source_inputs"]["mode"] == "window"
        assert len(current[MARKER]["fields"]["source_inputs"]["ids"]) == 3
        assert live.canonical_json(old) == live.canonical_json(current) == encode(value).encode()
        store.db.execute("PRAGMA query_only=ON")
        assert Codec(store.db).canonical_json(old) == encode(value).encode()


def test_historical_window_above_single_snapshot_bound_uses_bounded_blocks(tmp_path, monkeypatch):
    from cryptowatcher import history_rebuild
    from cryptowatcher.history_codec import MARKER, Codec

    monkeypatch.setattr(history_rebuild, "MAX_BYTES", 256)
    value = {"source_inputs": [{"start": i * 60000, "step": 60000, "close": 100} for i in range(40)]}
    with closing(Store(tmp_path / "candidate.sqlite3", 233382575)) as store:
        with store.db:
            packed = _CopyCodec(store.db).pack_canonical(encode(value))
        assert packed[MARKER]["fields"]["source_inputs"]["mode"] == "window"
        assert Codec(store.db).canonical_json(packed) == encode(value).encode()


def test_rebuild_preserves_real_production_strength_in_revision_and_head(tmp_path):
    source, _ = closed_backup(tmp_path)
    with sqlite3.connect(source) as database:
        for key in (PREFIX + "event:record-0:0000000001", PREFIX + "event-head:record-0"):
            value = json.loads(
                database.execute("SELECT payload_json FROM runtime WHERE key=?", (key,)).fetchone()[0]
            )
            value["strength"] = 2.095370825591371
            database.execute("UPDATE runtime SET payload_json=? WHERE key=?", (encode(value), key))
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    destination = tmp_path / "real-strength.sqlite3"
    rebuild(source, destination, 233382575, expected, time.monotonic() + 10)
    with closing(Store(destination, 233382575, readonly=True)) as store:
        history = History(store)
        for key in ("event:record-0:0000000001", "event-head:record-0"):
            assert history.get(key)["strength"].hex() == (2.095370825591371).hex()


def test_rebuild_keeps_large_integer_projections_without_json1_rounding(tmp_path):
    source, _ = closed_backup(tmp_path)
    database = Store(source, 233382575)
    with database.db:
        history = History(database)
        value = {**record("event", "record-0"), "strength": 2**100 + 123}
        history.put("event:record-0:0000000001", value)
        history.put("event-head:record-0", value)
    database.db.execute("PRAGMA journal_mode=DELETE")
    database.close()
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    candidate = tmp_path / "large-int.sqlite3"
    rebuild(source, candidate, 233382575, expected, time.monotonic() + 10)
    with closing(Store(candidate, 233382575, readonly=True)) as restored:
        assert History(restored).get("event-head:record-0")["strength"] == 2**100 + 123


def closed_backup(tmp_path, *, conflict=None):
    path = tmp_path / "closed.sqlite3"
    source = Store(path, 233382575)
    source.initialize(OwnerSettings())
    history = History(source)
    with source.db:
        for kind in ("event", "outcome"):
            for i in range(30):
                value = record(kind, f"record-{i}")
                # Model the real legacy writer's full mutable copies.
                history.put(f"{kind}:record-{i}:0000000001", value)
                history.put(f"{kind}-head:record-{i}", value)
        history.version("event", record("event", "already-compact"))
        history.put("private-outbox", {"fixture": "PRESERVE_WITHOUT_EXPORT"})
        source.db.execute(
            "INSERT INTO legacy_import VALUES (?,?,?)", ("a" * 64, '{"original":true}', "legacy.bak")
        )
        source.db.execute("INSERT INTO mutes VALUES (?,?)", ("spot/BTCUSDT", 1800000000))
        if conflict == "missing":
            history.delete("event:record-0:0000000001")
        elif conflict == "different":
            history.put("event-head:record-0", {**record("event", "record-0"), "source_inputs": []})
        elif conflict == "unknown_table":
            source.db.execute("CREATE TABLE additional_data (x TEXT)")
        elif conflict == "reserved_lookalike":
            source.db.execute("CREATE TABLE sqlitex_data (x TEXT)")
    source.db.execute("PRAGMA journal_mode=DELETE")
    source.close()
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_copy_can_release_source_before_full_candidate_validation(tmp_path, monkeypatch):
    from cryptowatcher import history_rebuild

    source, expected = closed_backup(tmp_path)
    baseline = rebuild(source, tmp_path / "baseline.sqlite3", 233382575, expected, time.monotonic() + 10)
    original = sqlite3.connect(source.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    checked = []
    real_check = history_rebuild.check_integrity

    def check_after_close(database, *, deadline):
        with pytest.raises(sqlite3.ProgrammingError):
            original.execute("SELECT 1")
        checked.append(True)
        real_check(database, deadline=deadline)

    monkeypatch.setattr(history_rebuild, "check_integrity", check_after_close)
    result = history_rebuild.copy_snapshot(
        original,
        tmp_path / "released.sqlite3",
        233382575,
        2**20,
        time.monotonic() + 10,
        source_copied=original.close,
    )
    assert checked == [True]
    assert result["logical_row_manifests"] == baseline["logical_row_manifests"]
    assert result["table_rows"] == baseline["table_rows"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == expected


def test_source_release_failure_never_proceeds_to_candidate_validation(tmp_path, monkeypatch):
    from cryptowatcher import history_rebuild

    source, expected = closed_backup(tmp_path)
    original = sqlite3.connect(source.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)

    def failed_release():
        original.close()
        raise StateError("Source qualification failed before release")

    def unexpected_check(*args, **kwargs):
        pytest.fail("Candidate validation must not bypass failed source qualification")

    monkeypatch.setattr(history_rebuild, "check_integrity", unexpected_check)
    with pytest.raises(StateError, match="Source qualification failed"):
        history_rebuild.copy_snapshot(
            original,
            tmp_path / "unqualified.sqlite3",
            233382575,
            2**20,
            time.monotonic() + 10,
            source_copied=failed_release,
        )
    assert hashlib.sha256(source.read_bytes()).hexdigest() == expected


def test_candidate_verification_cache_uses_only_remaining_memory(monkeypatch):
    from cryptowatcher import compressed_validation as cv
    from cryptowatcher.history_rebuild import configure_candidate_cache

    resident = 3328 * 1024**2
    monkeypatch.setattr(cv, "resident_memory_bytes", lambda: resident)
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE TABLE values_to_preserve (value TEXT)")
        db.execute("INSERT INTO values_to_preserve VALUES ('1.000')")
        db.execute("PRAGMA query_only=ON")
        configure_candidate_cache(db, time.monotonic() + 10)
        allocated = -db.execute("PRAGMA cache_size").fetchone()[0] * 1024
        assert allocated + resident + cv.VALIDATION_OVERHEAD_BYTES <= cv.VALIDATION_MEMORY_BYTES
        assert db.execute("PRAGMA query_only").fetchone() == (1,)
        assert db.execute("SELECT value FROM values_to_preserve").fetchall() == [("1.000",)]
        before = db.execute("PRAGMA cache_size").fetchone()[0]
        monkeypatch.setattr(cv, "resident_memory_bytes", lambda: cv.VALIDATION_MEMORY_BYTES)
        with pytest.raises(IntegrityError, match="Insufficient verification cache"):
            configure_candidate_cache(db, time.monotonic() + 10)
        assert db.execute("PRAGMA cache_size").fetchone()[0] == before


@pytest.mark.parametrize("compressed_validation", [False, True])
def test_rebuild_compacts_history_without_losing_any_revision_or_owner_state(tmp_path, compressed_validation):
    source, expected = closed_backup(tmp_path)
    destination = tmp_path / "candidate.sqlite3"
    before = source.read_bytes()
    result = rebuild(
        source,
        destination,
        233382575,
        expected,
        time.monotonic() + 10,
        compressed_candidate_validation=compressed_validation,
    )
    assert result["status"] == "ISOLATED_CANDIDATE_PASS"
    assert result["heads_converted"] == 60 and result["payload_bytes_saved"] > 700000
    assert result["candidate_bytes"] < source.stat().st_size * 0.7
    assert source.read_bytes() == before
    assert result["source_full_qualification"] == "REQUIRED_SEPARATELY_FOR_EXPECTED_HASH"
    assert result["candidate_validation"] == ("COMPRESSED_RAM" if compressed_validation else "SQLITE_FILE")
    old = Store(source, 233382575, readonly=True)
    new = Store(destination, 233382575, readonly=True)
    try:
        assert new.snapshot() == old.snapshot()
        assert new.db.execute("SELECT * FROM mutes").fetchall()[0][0] == "spot/BTCUSDT"
        assert new.db.execute("SELECT original_json FROM legacy_import").fetchone()[0] == '{"original":true}'
        for row in old.db.execute("SELECT key,payload_json FROM runtime"):
            relative = row[0][len(PREFIX) :]
            if "-head:" in relative or relative.split(":", 1)[0] in (
                "event",
                "outcome",
                "episode",
                "comparison",
            ):
                assert encode(History(new).get(relative)) == encode(History(old).get(relative))
            else:
                assert (
                    new.db.execute("SELECT payload_json FROM runtime WHERE key=?", (row[0],)).fetchone()[0]
                    == row[1]
                )
    finally:
        old.close()
        new.close()
    # A repeated reconstruction accepts mixed/new heads but performs no additional conversion.
    second = tmp_path / "second.sqlite3"
    again = rebuild(destination, second, 233382575, result["candidate_sha256"], time.monotonic() + 10)
    assert again["heads_converted"] == 0 and again["shared_bodies_added"] == 0
    assert again["table_rows"]["runtime"] == result["table_rows"]["runtime"] + result["shared_bodies_added"]
    assert {k: v for k, v in again["table_rows"].items() if k != "runtime"} == {
        k: v for k, v in result["table_rows"].items() if k != "runtime"
    }


def test_compressed_candidate_checks_constraints_and_refuses_writes(store):
    import apsw

    from cryptowatcher.history_rebuild import compressed_candidate

    path = store.path
    snapshot = store.snapshot()
    with compressed_candidate(store, path, time.monotonic() + 10) as frozen:
        assert frozen.snapshot() == snapshot
        assert frozen.db.execute("PRAGMA query_only").fetchone() == (1,)
        with pytest.raises(apsw.ReadOnlyError):
            frozen.db.execute("UPDATE delivery_preferences SET enabled=0")
    with closing(Store(path, 233382575)) as invalid:
        invalid.db.execute("PRAGMA ignore_check_constraints=ON")
        with invalid.db:
            invalid.db.execute("UPDATE delivery_preferences SET enabled=2")
        with pytest.raises(IntegrityError, match="DATABASE_CORRUPT"):
            with compressed_candidate(invalid, path, time.monotonic() + 10):
                pytest.fail("Candidate with a violated CHECK constraint was admitted")


def test_frozen_body_cache_reuses_exact_verified_bytes_and_rechecks_scanned_payload(store):
    from cryptowatcher.history_codec import BLOB_PREFIX, Codec
    from cryptowatcher.history_rebuild import _FrozenReadbackCodec, compressed_candidate

    with store.db:
        key = Codec(store.db).save({"window": [{"close": 1.0, "notes": "цена"}]})
    with pytest.raises(StateError, match="Frozen candidate required"):
        _FrozenReadbackCodec(store)
    with compressed_candidate(store, store.path, time.monotonic() + 10) as frozen:
        queries = []

        def traced(db, sql, bindings):
            if sql.startswith("SELECT payload_json FROM runtime WHERE key="):
                queries.append(sql)
            return True

        frozen.db.set_exec_trace(traced)
        codec = _FrozenReadbackCodec(frozen)
        first = codec.load_raw(key)
        assert codec.load_raw(key) == first and len(queries) == 1
        payload = frozen.db.execute(
            "SELECT payload_json FROM runtime WHERE key=?", (BLOB_PREFIX + key,)
        ).fetchone()[0]
        assert codec.validate_raw(key, payload) == first
        with pytest.raises(StateError):
            codec.validate_raw(key, encode({"schema": "corrupt"}))
        with pytest.raises(StateError, match="Invalid shared snapshot identity"):
            codec.load_raw("invalid")
        with pytest.raises(StateError, match="Missing shared snapshot"):
            codec.load_raw("f" * 64)
        codec.validated.clear()  # An evicted body is read and verified again.
        codec.validated_bytes = 0
        assert codec.load_raw(key) == first and len(queries) == 4
        frozen.db.set_exec_trace(None)


def test_frozen_body_cache_never_qualifies_changed_source_file(store):
    from cryptowatcher.history_codec import BLOB_PREFIX, Codec
    from cryptowatcher.history_rebuild import _FrozenReadbackCodec, compressed_candidate

    with store.db:
        key = Codec(store.db).save({"source": "original"})
    path = store.path
    with pytest.raises((StateError, IntegrityError), match="Candidate changed|SOURCE_CHANGED"):
        with compressed_candidate(store, path, time.monotonic() + 10) as frozen:
            codec = _FrozenReadbackCodec(frozen)
            original = codec.load_raw(key)
            with closing(sqlite3.connect(path)) as changed, changed:
                changed.execute("DELETE FROM runtime WHERE key=?", (BLOB_PREFIX + key,))
            assert codec.load_raw(key) == original


def test_compressed_candidate_rejects_changed_file_and_preserves_original_bytes(store):
    from cryptowatcher.history_rebuild import compressed_candidate

    path = store.path
    with store.db:
        store.put_runtime("sentinel", {"state": "original"})
    with pytest.raises((StateError, IntegrityError), match="Candidate changed|SOURCE_CHANGED"):
        with compressed_candidate(store, path, time.monotonic() + 10) as frozen:
            with closing(sqlite3.connect(path)) as changed, changed:
                changed.execute(
                    "UPDATE runtime SET payload_json=? WHERE key='sentinel'", (encode({"state": "changed"}),)
                )
            assert frozen.get_runtime("sentinel") == {"state": "original"}
    assert not list(path.parent.glob("cw-candidate-readback-*"))


@pytest.mark.parametrize("failure", ["missing", "different", "unknown_table", "reserved_lookalike"])
def test_rebuild_rejects_unknown_or_conflicting_history_without_changing_source(tmp_path, failure):
    source, expected = closed_backup(tmp_path, conflict=failure)
    original = source.read_bytes()
    destination = tmp_path / "rejected.sqlite3"
    with pytest.raises(StateError):
        rebuild(source, destination, 233382575, expected, time.monotonic() + 10)
    assert source.read_bytes() == original and destination.exists()


@pytest.mark.parametrize("failure", ["hash", "owner", "existing", "live_sidecars", "byte_limit", "deadline"])
def test_rebuild_preconditions_never_replace_source_or_existing_destination(tmp_path, failure):
    source, expected = closed_backup(tmp_path)
    original = source.read_bytes()
    destination = tmp_path / "candidate.sqlite3"
    owner, deadline, maximum = 233382575, time.monotonic() + 10, None
    if failure == "hash":
        expected = "f" * 64
    elif failure == "owner":
        owner = 999
    elif failure == "existing":
        destination.write_bytes(b"UNKNOWN_USER_FILE")
    elif failure == "live_sidecars":
        source.with_name(source.name + "-wal").write_bytes(b"UNTOUCHED")
    elif failure == "byte_limit":
        maximum = 32768
    else:
        deadline = time.monotonic() - 1
    with pytest.raises((StateError, sqlite3.DatabaseError)):
        rebuild(source, destination, owner, expected, deadline, max_candidate_bytes=maximum)
    assert source.read_bytes() == original
    if failure == "existing":
        assert destination.read_bytes() == b"UNKNOWN_USER_FILE"


def test_candidate_insert_enforces_constraints_and_preserves_invalid_source(tmp_path):
    source, _ = closed_backup(tmp_path)
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("UPDATE delivery_preferences SET enabled=2")
    original = source.read_bytes()
    expected = hashlib.sha256(original).hexdigest()
    with pytest.raises(sqlite3.IntegrityError):
        rebuild(source, tmp_path / "rejected.sqlite3", 233382575, expected, time.monotonic() + 10)
    assert source.read_bytes() == original


def test_numeric_provenance_mismatch_does_not_get_silently_normalized(tmp_path):
    source, _ = closed_backup(tmp_path)
    with sqlite3.connect(source) as connection:
        key = PREFIX + "event-head:record-0"
        value = json.loads(
            connection.execute("SELECT payload_json FROM runtime WHERE key=?", (key,)).fetchone()[0]
        )
        value["source_inputs"][0]["close"] = 100.0
        connection.execute("UPDATE runtime SET payload_json=? WHERE key=?", (encode(value), key))
    original = source.read_bytes()
    with pytest.raises(StateError, match="conflicts"):
        rebuild(
            source,
            tmp_path / "rejected.sqlite3",
            233382575,
            hashlib.sha256(original).hexdigest(),
            time.monotonic() + 10,
        )
    assert source.read_bytes() == original


def test_same_columns_with_unknown_collation_are_not_silently_reinterpreted(tmp_path):
    source, _ = closed_backup(tmp_path)
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql=replace(sql,'key TEXT PRIMARY KEY','key TEXT PRIMARY KEY COLLATE NOCASE') WHERE name='runtime'"
        )
    original = source.read_bytes()
    with pytest.raises(StateError, match="collation"):
        rebuild(
            source,
            tmp_path / "rejected.sqlite3",
            233382575,
            hashlib.sha256(original).hexdigest(),
            time.monotonic() + 10,
        )
    assert source.read_bytes() == original


@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), 1000])
def test_unbounded_or_extended_budget_is_not_accepted(tmp_path, deadline):
    source, expected = closed_backup(tmp_path)
    destination = tmp_path / "candidate.sqlite3"
    if deadline == 1000:
        deadline += time.monotonic()
    with pytest.raises(StateError, match="deadline"):
        rebuild(source, destination, 233382575, expected, deadline)
    assert not destination.exists()


def test_source_metadata_change_during_rebuild_invalidates_candidate(tmp_path, monkeypatch):
    from cryptowatcher import history_rebuild

    source, expected = closed_backup(tmp_path)
    original = source.read_bytes()
    real = history_rebuild.fingerprint
    calls = 0

    def fingerprint(path, deadline):
        nonlocal calls
        if path == source:
            calls += 1
            if calls == 2:
                meta = source.stat()
                os.utime(source, ns=(meta.st_atime_ns, meta.st_mtime_ns + 10000000))
        return real(path, deadline)

    monkeypatch.setattr(history_rebuild, "fingerprint", fingerprint)
    with pytest.raises(StateError, match="metadata changed"):
        rebuild(source, tmp_path / "candidate.sqlite3", 233382575, expected, time.monotonic() + 10)
    assert source.read_bytes() == original


@pytest.mark.parametrize(
    "fault",
    [
        "lost_revision",
        "extra_record",
        "private_delivery",
        "numeric_type",
        "candle",
        "cached_head_type",
        "cached_head_identity",
        "shared_body_after_copy",
    ],
)
def test_complete_logical_manifest_detects_loss_or_changes_after_copy(tmp_path, monkeypatch, fault):
    from cryptowatcher import history_rebuild

    source, expected = closed_backup(tmp_path)
    original = source.read_bytes()
    real_check = history_rebuild.check_integrity

    def changed(database, **kwargs):
        real_check(database, **kwargs)
        with database:
            if fault == "lost_revision":
                database.execute("DELETE FROM runtime WHERE key=?", (PREFIX + "event:record-0:0000000001",))
            elif fault == "extra_record":
                database.execute("INSERT INTO runtime VALUES (?,?)", ("unexpected-row", "{}"))
            elif fault == "private_delivery":
                database.execute(
                    "UPDATE runtime SET payload_json=? WHERE key=?",
                    ('{"fixture":"CHANGED"}', PREFIX + "private-outbox"),
                )
            elif fault == "numeric_type":
                key = PREFIX + "event:record-0:0000000001"
                value = json.loads(
                    database.execute("SELECT payload_json FROM runtime WHERE key=?", (key,)).fetchone()[0]
                )
                value["reference_price"] = 100.0
                database.execute("UPDATE runtime SET payload_json=? WHERE key=?", (encode(value), key))
            elif fault in ("cached_head_type", "cached_head_identity"):
                key = PREFIX + "event-head:record-0"
                value = json.loads(
                    database.execute("SELECT payload_json FROM runtime WHERE key=?", (key,)).fetchone()[0]
                )
                if fault == "cached_head_type":
                    value["strength"] = float(value["strength"]) if type(value["strength"]) is int else 2
                else:
                    value["event_id"] = "foreign-record"
                database.execute("UPDATE runtime SET payload_json=? WHERE key=?", (encode(value), key))
            elif fault == "shared_body_after_copy":
                key = database.execute(
                    "SELECT key FROM runtime WHERE key LIKE 'historyblob:%' LIMIT 1"
                ).fetchone()[0]
                database.execute(
                    "UPDATE runtime SET payload_json=? WHERE key=?", ('{"schema":"corrupt"}', key)
                )
            else:
                database.execute(
                    "INSERT INTO runtime VALUES (?,?)", ("source:v1:candle:unexpected", '{"close":1}')
                )

    monkeypatch.setattr(history_rebuild, "check_integrity", changed)
    with pytest.raises(StateError):
        rebuild(source, tmp_path / "unqualified.sqlite3", 233382575, expected, time.monotonic() + 10)
    assert source.read_bytes() == original


@pytest.mark.parametrize(
    "code,expected_exit", [("TIME_BUDGET_EXCEEDED", 3), ("DATABASE_CORRUPT", 4), ("IO_ERROR", 6)]
)
def test_cli_distinguishes_failure_class_without_exposing_exception_data(
    monkeypatch, capsys, code, expected_exit
):
    from cryptowatcher import history_rebuild

    def failed(*args, **kwargs):
        raise IntegrityError(code, "SENTINEL_PRIVATE_CONTENT")

    monkeypatch.setattr(history_rebuild, "rebuild", failed)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "history_rebuild",
            "--source",
            "source.db",
            "--destination",
            "new.db",
            "--owner-id",
            "233382575",
            "--qualified-source-sha256",
            "a" * 64,
        ],
    )
    assert history_rebuild.main() == expected_exit
    output = capsys.readouterr().out
    assert "SENTINEL_PRIVATE_CONTENT" not in output
    value = json.loads(output)
    assert value["failure_code"] == code and value["candidate"] == "UNQUALIFIED"
    assert value["production_migration"] == "NOT_RUN"


@pytest.mark.parametrize("failure", ["capacity", "constraint"])
def test_cli_classifies_real_candidate_sqlite_refusal_and_preserves_source(
    tmp_path, monkeypatch, capsys, failure
):
    from cryptowatcher import history_rebuild

    source, _ = closed_backup(tmp_path)
    with sqlite3.connect(source) as db:
        if failure == "capacity":
            db.execute(
                "INSERT INTO runtime VALUES (?,?)", ("private-transport", "SENTINEL_PRIVATE_BYTES" * 10000)
            )
        else:
            db.execute("PRAGMA ignore_check_constraints=ON")
            db.execute("UPDATE owner_settings SET revision=0")
    preserved = source.read_bytes()
    expected = hashlib.sha256(preserved).hexdigest()
    destination = tmp_path / "refused.sqlite3"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "history_rebuild",
            "--source",
            str(source),
            "--destination",
            str(destination),
            "--owner-id",
            "233382575",
            "--qualified-source-sha256",
            expected,
            "--max-candidate-bytes",
            "65536" if failure == "capacity" else str(2**20),
        ],
    )
    assert history_rebuild.main() == 2
    output = capsys.readouterr().out
    assert "SENTINEL_PRIVATE_BYTES" not in output
    result = json.loads(output)
    assert result["failure_code"] == (
        "CANDIDATE_CAPACITY_EXCEEDED" if failure == "capacity" else "CANDIDATE_CONSTRAINT_REJECTED"
    )
    assert result["candidate"] == "UNQUALIFIED" and result["production_migration"] == "NOT_RUN"
    assert source.read_bytes() == preserved


def test_cli_state_refusal_does_not_disclose_unrecognized_exception_text(monkeypatch, capsys):
    from cryptowatcher import history_rebuild

    def fail(*args, **kwargs):
        raise StateError("SENTINEL_PRIVATE_SETTINGS")

    monkeypatch.setattr(history_rebuild, "rebuild", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "history_rebuild",
            "--source",
            "source.sqlite3",
            "--destination",
            "candidate.sqlite3",
            "--owner-id",
            "233382575",
            "--qualified-source-sha256",
            "a" * 64,
        ],
    )
    assert history_rebuild.main() == 2
    output = capsys.readouterr().out
    assert "SENTINEL_PRIVATE_SETTINGS" not in output
    assert json.loads(output)["failure_code"] == "STATE_REJECTED"
