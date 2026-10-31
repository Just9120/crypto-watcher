"""Functional backup qualification guards; no Windows/native capacity claim."""

import hashlib
import json
import time

import pytest
import zstandard

from cryptowatcher import native_disk_backup
from cryptowatcher.market_storage import enable
from cryptowatcher.outcomes import MINUTE, OutcomeWorker
from cryptowatcher.sources import Candle, Sources
from cryptowatcher.storage import StateError
from scripts.qualify_framed_backup import qualify

from .test_outcomes import NOW, observe


def snapshot(store):
    enable(store.db)
    store.bounded = True
    store.clock_ms = lambda: NOW
    worker = OutcomeWorker(store)
    observe(store, worker)
    end = NOW + 60 * MINUTE
    Sources(store).put(
        "spot/BTCUSDT/1m",
        [
            Candle.parse([t, 102, 104, 101, 103, 1, 400], "1m", "rest", end, end)
            for t in range(NOW, end, MINUTE)
        ],
    )
    worker.work(end)
    path, owner = store.path, store.owner_id
    store.close()
    return path, owner


def arguments(store, tmp_path):
    source, owner = snapshot(store)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    return dict(
        source=source,
        work=tmp_path / "qualification",
        scratch=scratch,
        owner=owner,
        expected_source_bytes=source.stat().st_size,
        deadline=time.monotonic() + 20,
        native_required=False,
        minimum_source_bytes=1,
    )


def test_qualification_restores_exact_snapshot_preserves_source_and_cleans_owned_duplicates(
    store, tmp_path, monkeypatch
):
    args = arguments(store, tmp_path)
    before = hashlib.sha256(args["source"].read_bytes()).hexdigest()
    monkeypatch.setattr(native_disk_backup, "scratch_space", lambda *args: None)
    result = qualify(**args)
    assert result["state"] == "FUNCTIONAL_PASS_ONLY"
    assert result["source_unchanged"] is True
    assert result["source_sha256"] == before == hashlib.sha256(args["source"].read_bytes()).hexdigest()
    assert result["validated_snapshot_sha256"] == result["restored_sha256"]
    assert result["private_numeric_equality"]["outcome_samples"] > 0
    assert result["private_numeric_equality"]["readonly_changes"] == 0
    assert (args["work"] / "result.json").exists()
    assert (args["work"] / "qualified.sqlite3.zst").exists()
    assert not (args["work"] / "input.sqlite3").exists()
    assert not (args["work"] / "restored.sqlite3").exists()
    assert not list(args["work"].glob("input.sqlite3-*"))
    assert not list(args["work"].glob("restored.sqlite3-*"))
    assert json.loads((args["work"] / "cleanup.json").read_text())["state"] == "COMPLETE"
    assert not list(args["source"].parent.glob(args["source"].name + "-*"))


def test_archive_modified_after_capture_is_rejected_without_changing_source(store, tmp_path, monkeypatch):
    args = arguments(store, tmp_path)
    before = args["source"].read_bytes()
    original = native_disk_backup.backup
    monkeypatch.setattr(native_disk_backup, "scratch_space", lambda *args: None)

    def tamper(source, destination, directory, **options):
        proof = original(source, destination, directory, **options)
        destination.write_bytes(destination.read_bytes()[:-1])
        return proof

    monkeypatch.setattr(native_disk_backup, "backup", tamper)
    with pytest.raises((StateError, zstandard.ZstdError)):
        qualify(**args)
    assert args["source"].read_bytes() == before
    assert not (args["work"] / "result.json").exists()
    assert (args["work"] / "input.sqlite3").exists()


def test_source_size_and_existing_output_rejected_before_copy(store, tmp_path):
    args = arguments(store, tmp_path)
    with pytest.raises(StateError, match="size"):
        qualify(**{**args, "expected_source_bytes": args["expected_source_bytes"] + 1})
    assert not args["work"].exists()
    args["work"].mkdir()
    sentinel = args["work"] / "owner.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    with pytest.raises(StateError, match="exclusive"):
        qualify(**args)
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_active_sidecars_block_snapshot_qualification_before_any_copy(store, tmp_path):
    args = arguments(store, tmp_path)
    sidecar = args["source"].with_name(args["source"].name + "-wal")
    sidecar.write_bytes(b"preserve existing sidecar")
    before = args["source"].read_bytes()
    with pytest.raises(StateError, match="closed regular snapshot"):
        qualify(**args)
    assert not args["work"].exists()
    assert args["source"].read_bytes() == before
    assert sidecar.read_bytes() == b"preserve existing sidecar"
