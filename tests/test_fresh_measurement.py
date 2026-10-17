import hashlib
import os
from contextlib import closing

import pytest

from cryptowatcher.config import OwnerSettings
from cryptowatcher.market_storage import enable
from cryptowatcher.reset_history import prepare, private_state, signature
from cryptowatcher.storage import StateError, Store
from scripts import fresh_measurement as operation

OWNER = 233382575
GENERATION = "a" * 32


@pytest.fixture
def archived(tmp_path):
    source = (tmp_path / "archive.sqlite3").resolve()
    with closing(Store(source, OWNER)) as store:
        store.initialize(OwnerSettings(alert_universe_mode="top", profile="custom"))
        enable(store.db)
        store.bounded = True
        store.put_runtime("history_generation", {"id": GENERATION})
        store.put_runtime("market-old", {"exact": 0.1234567890123456})
        store.mute("spot/BTCUSDT", 9999999999)
        store.set_enabled(False)
        preserved = private_state(store)
    raw = source.read_bytes()
    return source, raw, hashlib.sha256(raw).hexdigest(), preserved


def test_fresh_measurement_preserves_archive_and_exact_private_state(tmp_path, archived):
    source, raw, digest, preserved = archived
    fresh = (tmp_path / "fresh.sqlite3").resolve()
    proof = prepare(
        source,
        fresh,
        (tmp_path / "private.json").resolve(),
        OWNER,
        expected_generation=GENERATION,
        preserved_archive_sha256=digest,
    )
    assert source.read_bytes() == raw
    with closing(Store(source, OWNER, readonly=True)) as old:
        assert old.get_runtime("market-old") == {"exact": 0.1234567890123456}
    with closing(Store(fresh, OWNER, readonly=True)) as current:
        assert signature(private_state(current)) == signature(preserved)
        assert current.get_runtime("market-old") is None
        generation = current.get_runtime("history_generation")
        assert generation["id"] != GENERATION
        assert generation["previous_market_history"] == "PRESERVED_IN_ARCHIVE"
        assert generation["reason"] == "OWNER_AUTHORIZED_FRESH_MEASUREMENT_20261017"
        assert current.bounded
    assert proof["private_restore"] == proof["integrity"] == "PASS"
    assert proof["preserved_archive_sha256"] == digest
    assert proof["previous_generation_id"] == GENERATION


@pytest.mark.parametrize("which", ["generation", "hash", "missing-hash", "missing-generation", "automatic"])
def test_fresh_measurement_rejects_unknown_archive_or_automatic_repeat(tmp_path, archived, which):
    source, raw, digest, _ = archived
    options = {"expected_generation": GENERATION, "preserved_archive_sha256": digest}
    if which == "generation":
        options["expected_generation"] = "b" * 32
    elif which == "hash":
        options["preserved_archive_sha256"] = "0" * 64
    elif which.startswith("missing-"):
        options.pop("preserved_archive_sha256" if which == "missing-hash" else "expected_generation")
    else:
        options = {}
    fresh = (tmp_path / "fresh.sqlite3").resolve()
    recovery = (tmp_path / "private.json").resolve()
    with pytest.raises(StateError):
        prepare(source, fresh, recovery, OWNER, **options)
    assert source.read_bytes() == raw and not fresh.exists() and not recovery.exists()


def test_original_files_and_unrelated_backups_survive_archive_cutover(tmp_path, monkeypatch):
    source = (tmp_path / "cryptowatcher.sqlite3").resolve()
    archive = (tmp_path / "original").resolve()
    archive.mkdir()
    expected = {"": b"database exact bytes", "-wal": b"wal exact bytes", "-shm": b"shm exact bytes"}
    for suffix, raw in expected.items():
        source.with_name(source.name + suffix).write_bytes(raw)
    untouched = tmp_path / "older-backup.sqlite3"
    untouched.write_bytes(b"previous data")
    if os.name == "nt":
        monkeypatch.setattr(operation, "sync_directory", lambda path: None)
    proof = operation.preserve_original(source, archive)
    assert len(proof) == 3 and not source.exists()
    for suffix, raw in expected.items():
        assert (archive / (source.name + suffix)).read_bytes() == raw
    assert untouched.read_bytes() == b"previous data"


def test_archive_preflight_rejects_existing_target_before_any_move(tmp_path):
    source = (tmp_path / "cryptowatcher.sqlite3").resolve()
    source.write_bytes(b"database")
    wal = source.with_name(source.name + "-wal")
    wal.write_bytes(b"wal")
    archive = (tmp_path / "original").resolve()
    archive.mkdir()
    (archive / source.name).write_bytes(b"existing")
    with pytest.raises(RuntimeError, match="already exists"):
        operation.preserve_original(source, archive)
    assert source.read_bytes() == b"database" and wal.read_bytes() == b"wal"
    assert (archive / source.name).read_bytes() == b"existing"
    assert not (archive / wal.name).exists()


def test_fresh_operator_requires_explicit_approval_before_host_or_data_access():
    with pytest.raises(RuntimeError, match="approve-fresh-database"):
        operation.execute("a" * 40, "b" * 32, approved=False)
