import gzip
import hashlib
import sqlite3
import sys

import pytest
import zstandard

from cryptowatcher import conversion_qualification as cq
from cryptowatcher.history_rebuild import TABLES
from cryptowatcher.storage import StateError

from .test_history_rebuild import closed_backup


def inputs(tmp_path, encoding="sqlite"):
    source, expected = closed_backup(tmp_path)
    raw = source.read_bytes()
    if encoding in ("gzip", "zstd"):
        encoded = (
            gzip.compress(raw)
            if encoding == "gzip"
            else zstandard.ZstdCompressor(write_checksum=True).compress(raw)
        )
        source = tmp_path / "source.archive"
        source.write_bytes(encoded)
    output = tmp_path / "output"
    output.mkdir(mode=0o700)
    return (
        source,
        raw,
        dict(
            directory=output,
            owner=233382575,
            expected_revision=1,
            source_encoding=encoding,
            expected_source_sha256=expected,
            max_candidate_bytes=10 * 1024**2,
            max_archive_bytes=10 * 1024**2,
            timeout_seconds=20,
            backend="python",
        ),
    )


@pytest.mark.parametrize("encoding", ["sqlite", "gzip", "zstd", "live"])
def test_full_conversion_keeps_history_private_bytes_and_qualified_recovery(tmp_path, encoding):
    source, raw, kwargs = inputs(tmp_path, encoding)
    before = source.read_bytes()
    result = cq.qualify_conversion(source, **kwargs)
    assert source.read_bytes() == before
    assert result["status"] == "ISOLATED_CONVERSION_PASS"
    assert result["source_full_integrity"] == result["candidate_full_integrity"] == "PASS"
    recovered = zstandard.ZstdDecompressor().decompress(
        (kwargs["directory"] / "source.sqlite3.zst").read_bytes()
    )
    assert result["source_sha256"] == hashlib.sha256(recovered).hexdigest()
    if encoding != "live":
        assert recovered == raw
    else:
        # SQLite online backup can change header bytes; compare every captured row instead.
        restored_path = tmp_path / "restored.sqlite3"
        restored_path.write_bytes(recovered)
        with sqlite3.connect(source) as original, sqlite3.connect(restored_path) as restored:
            for table in TABLES:
                assert (
                    original.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                    == restored.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                )
    assert result["recovery"]["decoded_sha256"] == result["source_sha256"]
    assert result["recovery"]["database_integrity"] == "NOT_ASSERTED_BY_BYTE_VERIFIER"
    assert result["recovery_integrity_basis"] == "SAME_QUALIFIED_BYTES"
    assert result["production_migration"] == "NOT_RUN" and result["promotion_eligible"] is False
    assert set(result["logical_row_manifests"]) == {
        "owner_settings",
        "delivery_preferences",
        "mutes",
        "runtime",
        "legacy_import",
    }
    with sqlite3.connect(kwargs["directory"] / "candidate.sqlite3") as db:
        assert db.execute("SELECT original_json FROM legacy_import").fetchone() == ('{"original":true}',)
        assert db.execute("SELECT payload_json FROM runtime WHERE key LIKE '%private-outbox'").fetchone() == (
            '{"fixture":"PRESERVE_WITHOUT_EXPORT"}',
        )


@pytest.mark.parametrize("stage", ["source", "candidate", "source_release"])
def test_upstream_refusal_never_reaches_byte_reuse(tmp_path, monkeypatch, stage):
    source, raw, kwargs = inputs(tmp_path)
    called = False

    def reject(*args, **kw):
        raise StateError("fixture upstream refusal")

    def forbidden(*args, **kw):
        nonlocal called
        called = True
        pytest.fail("Recovery cannot inherit an incomplete source/candidate proof")

    monkeypatch.setattr(cq, "verify_archive_bytes", forbidden)
    if stage == "source":
        monkeypatch.setattr(cq.cv, "full_check", reject)
    elif stage == "candidate":
        monkeypatch.setattr(cq, "copy_snapshot", reject)
    else:
        actual = cq.fingerprint
        n = 0

        def changed(*args, **kw):
            nonlocal n
            n += 1
            return "0" * 64 if n == 2 else actual(*args, **kw)

        monkeypatch.setattr(cq, "fingerprint", changed)
    with pytest.raises(StateError):
        cq.qualify_conversion(source, **kwargs)
    assert not called and source.read_bytes() == raw


@pytest.mark.parametrize("fault", ["owner_revision", "timeout", "existing", "source_hash"])
def test_preconditions_preserve_existing_files(tmp_path, fault):
    source, raw, kwargs = inputs(tmp_path)
    if fault == "owner_revision":
        kwargs["expected_revision"] = 2
    elif fault == "timeout":
        kwargs["timeout_seconds"] = 901
    elif fault == "source_hash":
        kwargs["expected_source_sha256"] = "0" * 64
    else:
        (kwargs["directory"] / "candidate.sqlite3").write_bytes(b"existing user data")
    with pytest.raises(StateError):
        cq.qualify_conversion(source, **kwargs)
    assert source.read_bytes() == raw
    if fault == "existing":
        assert (kwargs["directory"] / "candidate.sqlite3").read_bytes() == b"existing user data"


def test_archive_change_after_byte_verifier_blocks_final_qualification(tmp_path, monkeypatch):
    source, raw, kwargs = inputs(tmp_path)
    verify = cq.verify_archive_bytes

    def changed(path, **kw):
        result = verify(path, **kw)
        with path.open("ab") as archive:
            archive.write(b"changed after decode")
        return result

    monkeypatch.setattr(cq, "verify_archive_bytes", changed)
    with pytest.raises(StateError, match="Persisted archive encoding changed"):
        cq.qualify_conversion(source, **kwargs)
    assert source.read_bytes() == raw


@pytest.mark.skipif(sys.platform != "linux", reason="Native Linux conversion helper")
def test_native_copy_reaches_identical_manifests_and_full_recovery_gate(tmp_path):
    source, raw, kwargs = inputs(tmp_path)
    reference = cq.qualify_conversion(source, **kwargs)
    second = tmp_path / "native"
    second.mkdir(mode=0o700)
    kwargs.update(directory=second, backend="native")
    native = cq.qualify_conversion(source, **kwargs)
    assert native["conversion_backend"] == "native-v1"
    assert native["logical_row_manifests"] == reference["logical_row_manifests"]
    assert native["table_rows"] == reference["table_rows"]
    assert native["recovery"] == reference["recovery"]
    assert source.read_bytes() == raw
