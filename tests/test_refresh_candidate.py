import hashlib
import sqlite3
import time
from contextlib import closing

import pytest
import zstandard

from cryptowatcher import refresh_candidate as refresh
from cryptowatcher.framed_rebuild import copy_validated
from cryptowatcher.reconcile_delta import ENTRY
from cryptowatcher.storage import RECORD_FORMAT_KEY, StateError

from .test_framed_rebuild import source_history


def fixture(tmp_path):
    source = (tmp_path / "source.sqlite3").resolve()
    candidate = (tmp_path / "candidate.sqlite3").resolve()
    source_history(source)
    raw = bytearray()
    with closing(sqlite3.connect(source)) as incoming:
        incoming.execute("PRAGMA query_only=ON")
        copy_validated(
            incoming, candidate, owner=233382575, maximum=16 * 1024**2, deadline=time.monotonic() + 20
        )
        with closing(sqlite3.connect(candidate)) as target:
            current = iter(
                target.execute(
                    "SELECT key,payload_json FROM runtime WHERE key!=? ORDER BY rowid", (RECORD_FORMAT_KEY,)
                )
            )
            for rowid, key, payload in incoming.execute(
                "SELECT rowid,key,payload_json FROM runtime ORDER BY rowid"
            ):
                old_key, physical = next(current)
                assert key == old_key
                raw.extend(
                    ENTRY.pack(
                        rowid,
                        hashlib.sha256(key.encode()).digest(),
                        hashlib.sha256(payload.encode()).digest(),
                        hashlib.sha256(physical.encode()).digest(),
                    )
                )
    manifest = (tmp_path / "baseline.zst").resolve()
    manifest.write_bytes(zstandard.ZstdCompressor(write_checksum=True).compress(raw))
    opts = dict(
        source=source,
        candidate=candidate,
        manifest=manifest,
        candidate_sha256=hashlib.sha256(candidate.read_bytes()).hexdigest(),
        manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
        decoded_manifest_sha256=hashlib.sha256(raw).hexdigest(),
        baseline_rows=len(raw) // ENTRY.size,
        owner=233382575,
        maximum=16 * 1024**2,
    )
    with sqlite3.connect(source) as db:
        db.execute("INSERT INTO runtime VALUES('new','123')")
        db.execute("UPDATE runtime SET payload_json='456' WHERE key='transport:immutable'")
        db.execute("UPDATE delivery_preferences SET enabled=0")
    opts["source_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    return opts


def test_refresh_preserves_history_updates_private_state_and_verifies_all_rows(tmp_path):
    opts = fixture(tmp_path)
    before = opts["source_sha256"]
    result = refresh.refresh(**opts)
    assert result["state"] == "QUALIFIED_REFRESHED_SNAPSHOT"
    assert result["complete_row_readback"] == "PASS" and not result["production_replacement"]
    assert hashlib.sha256(opts["source"].read_bytes()).hexdigest() == before
    with closing(sqlite3.connect(opts["candidate"])) as db:
        assert db.execute("SELECT payload_json FROM runtime WHERE key='new'").fetchone() == ("123",)
        assert db.execute("SELECT payload_json FROM runtime WHERE key='transport:immutable'").fetchone() == (
            "456",
        )
        assert db.execute("SELECT enabled FROM delivery_preferences").fetchone() == (0,)
        assert db.execute("PRAGMA user_version").fetchone() == (2,)
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    repeated = refresh.refresh(**opts, resume=True)
    assert repeated["candidate_sha256"] == result["candidate_sha256"]


@pytest.mark.parametrize("stage", ["DISCOVER", "DELETE", "UPSERT", "VERIFY"])
def test_committed_progress_survives_interruption_and_resumes(tmp_path, stage):
    opts = fixture(tmp_path)

    def interrupt(**value):
        if value.get("durable") and value["phase"] == stage:
            raise RuntimeError("controlled interruption")

    with pytest.raises(RuntimeError, match="controlled"):
        refresh.refresh(**opts, progress=interrupt)
    with closing(sqlite3.connect(opts["candidate"])) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (0,)
    assert refresh.refresh(**opts, resume=True)["state"] == "QUALIFIED_REFRESHED_SNAPSHOT"


def test_replaced_keys_and_reused_source_rowids_are_deleted_before_upsert(tmp_path):
    opts = fixture(tmp_path)
    with sqlite3.connect(opts["source"]) as db:
        old = db.execute("SELECT rowid FROM runtime WHERE key='transport:immutable'").fetchone()[0]
        db.execute("DELETE FROM runtime WHERE key='transport:immutable'")
        db.execute("INSERT INTO runtime(rowid,key,payload_json) VALUES(?, 'moved', '999')", (old,))
        db.execute("INSERT INTO runtime VALUES('transport:immutable','789')")
        db.execute(
            "INSERT INTO runtime(rowid,key,payload_json) VALUES(?, 'minimum-rowid', '42')", (-(2**63),)
        )
    opts["source_sha256"] = hashlib.sha256(opts["source"].read_bytes()).hexdigest()
    refresh.refresh(**opts)
    with closing(sqlite3.connect(opts["candidate"])) as db:
        assert db.execute(
            "SELECT key,payload_json FROM runtime WHERE key IN('moved','transport:immutable') ORDER BY key"
        ).fetchall() == [("moved", "999"), ("transport:immutable", "789")]
        assert db.execute("SELECT payload_json FROM runtime WHERE key='minimum-rowid'").fetchone() == ("42",)


def test_unrelated_candidate_corruption_is_detected_on_complete_readback(tmp_path):
    opts = fixture(tmp_path)

    def interrupt(**value):
        if value.get("durable") and value["phase"] == "VERIFY":
            raise RuntimeError("pause")

    with pytest.raises(RuntimeError):
        refresh.refresh(**opts, progress=interrupt)
    with sqlite3.connect(opts["candidate"]) as db:
        key = db.execute("SELECT key FROM runtime WHERE key LIKE 'historyblob:%' LIMIT 1").fetchone()[0]
        db.execute("UPDATE runtime SET payload_json='{}' WHERE key=?", (key,))
    with pytest.raises(StateError, match="row hash"):
        refresh.refresh(**opts, resume=True)


def test_uncommitted_candidate_write_and_ledger_progress_roll_back_together(tmp_path, monkeypatch):
    opts = fixture(tmp_path)
    original = refresh.converted
    count = 0

    def fail_second(*args):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("between writes and commit")
        return original(*args)

    monkeypatch.setattr(refresh, "converted", fail_second)
    with pytest.raises(RuntimeError, match="between writes"):
        refresh.refresh(**opts)
    with closing(sqlite3.connect(opts["candidate"])) as db:
        assert (
            db.execute("SELECT payload_json FROM runtime WHERE key='transport:immutable'").fetchone()[0]
            != "456"
        )
    ledger = opts["candidate"].with_name(opts["candidate"].name + ".refresh.sqlite3")
    with closing(sqlite3.connect(ledger)) as db:
        assert db.execute("SELECT count(*) FROM upserts WHERE target_id IS NOT NULL").fetchone() == (0,)
    monkeypatch.setattr(refresh, "converted", original)
    assert refresh.refresh(**opts, resume=True)["state"] == "QUALIFIED_REFRESHED_SNAPSHOT"
