import hashlib
import json
import sqlite3
import time
from contextlib import closing

import pytest
import zstandard

from cryptowatcher import change_journal as journal
from cryptowatcher import reconcile_delta as delta
from cryptowatcher.storage import StateError

EPOCH = "a" * 32


def prepared(store, tmp_path):
    for key in ("a", "b", "c"):
        store.put_runtime(key, {"old": key})
    raw = bytearray()
    old = {}
    for rowid, key, payload in store.db.execute("SELECT rowid,key,payload_json FROM runtime ORDER BY rowid"):
        old[key] = rowid
        raw.extend(
            delta.ENTRY.pack(
                rowid,
                hashlib.sha256(key.encode()).digest(),
                hashlib.sha256(payload.encode()).digest(),
                hashlib.sha256(payload.encode()).digest(),
            )
        )
    manifest = (tmp_path / "baseline.zst").resolve()
    manifest.write_bytes(zstandard.ZstdCompressor(write_checksum=True).compress(raw))
    journal.install(
        store.db,
        epoch=EPOCH,
        owner=store.owner_id,
        max_keys=10000,
        max_bytes=1024**2,
        expires=int(time.time()) + 3600,
    )
    return dict(
        baseline=manifest,
        epoch=EPOCH,
        owner=store.owner_id,
        baseline_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
        baseline_decoded_sha256=hashlib.sha256(raw).hexdigest(),
        baseline_rows=len(old),
        max_archive_bytes=16 * 1024**2,
    ), old


def export(store, tmp_path, options):
    target = (tmp_path / "delta.zst").resolve()
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("PRAGMA query_only=ON")
        result = delta.export_delta(db, destination=target, **options)
        assert not db.in_transaction
    decoded = zstandard.ZstdDecompressor().decompress(target.read_bytes(), max_output_size=1024**2)
    rows = [json.loads(line) for line in decoded.splitlines()]
    body = b"\n".join(decoded.splitlines()[:-1]) + b"\n"
    assert hashlib.sha256(body).hexdigest() == result["decoded_body_sha256"]
    assert result["archive_sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    return result, rows


def test_full_comparison_finds_updates_deletions_and_reused_rowid(store, tmp_path):
    options, old = prepared(store, tmp_path)
    with store.db:
        store.db.execute("DELETE FROM runtime WHERE key='c'")
        store.db.execute("INSERT INTO runtime VALUES('new','123')")  # Reuses c's highest rowid.
        store.db.execute("DELETE FROM runtime WHERE key='b'")
        store.db.execute("UPDATE runtime SET payload_json='456' WHERE key='a'")
    result, rows = export(store, tmp_path, options)
    deletes = [r[1] for r in rows if isinstance(r, list) and r[0] == "delete"]
    upserts = [r for r in rows if isinstance(r, list) and r[0] == "upsert"]
    assert set(deletes) == {old["b"], old["c"]}
    assert {r[2] for r in upserts} == {"a", "new"}
    assert result["upserts"] == result["deletes"] == 2
    assert result["table_rows"]["runtime"] == 2
    assert result["source_full_integrity"] == "PENDING"
    assert rows[-1]["kind"] == "complete"


def test_replaced_key_with_later_rowid_is_deleted_then_upserted(store, tmp_path):
    options, old = prepared(store, tmp_path)
    with store.db:
        store.db.execute("INSERT OR REPLACE INTO runtime VALUES('a','999')")
    _, rows = export(store, tmp_path, options)
    assert ["delete", old["a"]] in rows
    assert [r[2] for r in rows if isinstance(r, list) and r[0] == "upsert"] == ["a"]


def test_consistent_source_snapshot_does_not_lose_concurrent_tail(store, tmp_path, monkeypatch):
    options, _ = prepared(store, tmp_path)
    original = delta.feed
    written = False

    def feed(digest, values):
        nonlocal written
        if values[0] == "a" and not written:
            written = True
            store.put_runtime("a", {"late": True})
            store.put_runtime("after-snapshot", 1)
        return original(digest, values)

    monkeypatch.setattr(delta, "feed", feed)
    result, rows = export(store, tmp_path, options)
    assert result["upserts"] == 0 and result["watermark"] == 0
    assert not [r for r in rows if isinstance(r, list)]
    with closing(sqlite3.connect(store.path)) as db:
        with journal.changes(
            db, epoch=EPOCH, owner=store.owner_id, after=result["watermark"], deadline=time.monotonic() + 10
        ) as (mark, tail):
            assert {r["key"] for r in tail} == {"a", "after-snapshot"}
            assert mark["last_seq"] > result["watermark"]


@pytest.mark.parametrize("fault", ["encoded", "decoded", "count", "truncated"])
def test_wrong_manifest_never_publishes_complete(store, tmp_path, fault):
    options, _ = prepared(store, tmp_path)
    if fault == "encoded":
        options["baseline_sha256"] = "0" * 64
    elif fault == "decoded":
        options["baseline_decoded_sha256"] = "0" * 64
    elif fault == "count":
        options["baseline_rows"] += 1
    else:
        options["baseline"].write_bytes(zstandard.ZstdCompressor().compress(b"short"))
        options["baseline_sha256"] = hashlib.sha256(options["baseline"].read_bytes()).hexdigest()
    with pytest.raises(StateError):
        export(store, tmp_path, options)


def test_existing_delta_is_never_overwritten(store, tmp_path):
    options, _ = prepared(store, tmp_path)
    target = tmp_path / "delta.zst"
    target.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        export(store, tmp_path, options)
    assert target.read_bytes() == b"keep"


def test_delta_capacity_refusal_preserves_source_and_blocks_complete(store, tmp_path):
    options, _ = prepared(store, tmp_path)
    options["max_archive_bytes"] = 1
    store.put_runtime("later", "keep")
    with pytest.raises(StateError, match="capacity"):
        export(store, tmp_path, options)
    assert store.get_runtime("later") == "keep"
