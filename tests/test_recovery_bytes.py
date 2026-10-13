import gzip
import hashlib
import sqlite3
import time

import pytest
import zstandard

from cryptowatcher import recovery_bytes as m
from cryptowatcher.storage import IntegrityError


def fixture(tmp_path, encoding):
    p = tmp_path / "source.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE kept(value TEXT CHECK(length(value)>0))")
    c.execute("INSERT INTO kept VALUES('1.000')")
    c.commit()
    assert c.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    c.close()
    raw = p.read_bytes()
    encoded = (
        gzip.compress(raw)
        if encoding == "gzip"
        else zstandard.ZstdCompressor(write_checksum=True).compress(raw)
    )
    archive = tmp_path / "archive"
    archive.write_bytes(encoded)
    return (
        archive,
        raw,
        dict(
            encoding=encoding,
            encoded_sha256=hashlib.sha256(encoded).hexdigest(),
            encoded_bytes=len(encoded),
            decoded_sha256=hashlib.sha256(raw).hexdigest(),
            decoded_bytes=len(raw),
            deadline=time.monotonic() + 5,
        ),
    )


@pytest.mark.parametrize("encoding", ["gzip", "zstd"])
def test_independent_restore_all_bytes_without_mutation(tmp_path, encoding):
    p, raw, k = fixture(tmp_path, encoding)
    before = p.read_bytes()
    v = m.verify_archive_bytes(p, **k)
    assert v["decoded_bytes"] == len(raw) and p.read_bytes() == before


@pytest.mark.parametrize("fault", ["changed", "replaced", "not_file", "encoded_size", "nan_deadline"])
def test_archive_identity_and_finite_deadline_required(tmp_path, monkeypatch, fault):
    p, raw, kwargs = fixture(tmp_path, "zstd")
    if fault == "not_file":
        p = tmp_path
    elif fault == "encoded_size":
        kwargs["encoded_bytes"] += 1
    elif fault == "nan_deadline":
        kwargs["deadline"] = float("nan")
    else:
        original_identity = m.identity
        calls = 0

        def identity_after_read(path):
            nonlocal calls
            calls += 1
            value = original_identity(path)
            if calls == 2:
                # Changed metadata or inode invalidates the proof even if bytes match.
                value = (value[0], value[1] + int(fault == "replaced"), value[2], value[3] + 1)
            return value

        monkeypatch.setattr(m, "identity", identity_after_read)
    with pytest.raises(m.RestoreRefused):
        m.verify_archive_bytes(p, **kwargs)


def test_valid_bytes_alone_never_assert_database_integrity(tmp_path):
    raw = b"not a SQLite database"
    encoded = zstandard.ZstdCompressor(write_checksum=True).compress(raw)
    p = tmp_path / "archive"
    p.write_bytes(encoded)
    result = m.verify_archive_bytes(
        p,
        encoding="zstd",
        encoded_sha256=hashlib.sha256(encoded).hexdigest(),
        encoded_bytes=len(encoded),
        decoded_sha256=hashlib.sha256(raw).hexdigest(),
        decoded_bytes=len(raw),
        deadline=time.monotonic() + 5,
    )
    assert result["database_integrity"] == "NOT_ASSERTED_BY_BYTE_VERIFIER"


@pytest.mark.parametrize("encoding", ["gzip", "zstd"])
@pytest.mark.parametrize(
    "fault",
    [
        "encoded_hash",
        "decoded_hash",
        "smaller",
        "larger",
        "truncated",
        "corrupted",
        "appended_frame",
        "expired",
    ],
)
def test_bad_proof_and_corruption_never_reused(tmp_path, encoding, fault):
    p, raw, k = fixture(tmp_path, encoding)
    if fault == "encoded_hash":
        k["encoded_sha256"] = "0" * 64
    elif fault == "decoded_hash":
        k["decoded_sha256"] = "0" * 64
    elif fault == "smaller":
        k["decoded_bytes"] -= 1
    elif fault == "larger":
        k["decoded_bytes"] += 1
    elif fault == "expired":
        k["deadline"] = time.monotonic() - 1
    else:
        encoded = p.read_bytes()
        if fault == "truncated":
            encoded = encoded[:-5]
        elif fault == "corrupted":
            encoded = (
                encoded[: len(encoded) // 2]
                + bytes([encoded[len(encoded) // 2] ^ 1])
                + encoded[len(encoded) // 2 + 1 :]
            )
        else:
            encoded += (
                gzip.compress(b"extra")
                if encoding == "gzip"
                else zstandard.ZstdCompressor(write_checksum=True).compress(b"extra")
            )
        p.write_bytes(encoded)
        k["encoded_sha256"] = hashlib.sha256(encoded).hexdigest()
        k["encoded_bytes"] = len(encoded)
    before = p.read_bytes()
    with pytest.raises((m.RestoreRefused, IntegrityError)):
        m.verify_archive_bytes(p, **k)
    assert p.read_bytes() == before


@pytest.mark.parametrize("fault", ["wrong_type", "unknown_encoding", "invalid_hash"])
def test_input_requires_exact_identity(tmp_path, fault):
    p, raw, k = fixture(tmp_path, "gzip")
    if fault == "wrong_type":
        k["decoded_bytes"] = True
    elif fault == "unknown_encoding":
        k["encoding"] = "zip"
    else:
        k["decoded_sha256"] = "not-a-hash"
    with pytest.raises((m.RestoreRefused, IntegrityError)):
        m.verify_archive_bytes(p, **k)


@pytest.mark.parametrize("removed", [1, 2, 3, 4])
def test_incomplete_checksum_trailer_refused_even_when_reader_returns_all_bytes(tmp_path, removed):
    raw = b"a" * 100000
    encoded = zstandard.ZstdCompressor(write_checksum=True).compress(raw)[:-removed]
    p = tmp_path / "archive"
    p.write_bytes(encoded)
    with pytest.raises(m.RestoreRefused, match="INCOMPLETE_ZSTD_FRAME"):
        m.verify_archive_bytes(
            p,
            encoding="zstd",
            encoded_sha256=hashlib.sha256(encoded).hexdigest(),
            encoded_bytes=len(encoded),
            decoded_sha256=hashlib.sha256(raw).hexdigest(),
            decoded_bytes=len(raw),
            deadline=time.monotonic() + 5,
        )


@pytest.mark.parametrize("content_size", [False, True])
@pytest.mark.parametrize("size", [1, 257, 131072, 1500000])
def test_single_segment_and_stream_headers_and_multiple_blocks(tmp_path, content_size, size):
    import io
    import random

    raw = random.Random(10).randbytes(size)
    out = io.BytesIO()
    with zstandard.ZstdCompressor(write_checksum=True).stream_writer(
        out, size=size if content_size else -1, closefd=False
    ) as writer:
        writer.write(raw)
    encoded = out.getvalue()
    p = tmp_path / "archive"
    p.write_bytes(encoded)
    assert (
        m.verify_archive_bytes(
            p,
            encoding="zstd",
            encoded_sha256=hashlib.sha256(encoded).hexdigest(),
            encoded_bytes=len(encoded),
            decoded_sha256=hashlib.sha256(raw).hexdigest(),
            decoded_bytes=size,
            deadline=time.monotonic() + 5,
        )["decoded_bytes"]
        == size
    )
