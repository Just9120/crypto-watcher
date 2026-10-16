from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading

import pytest
import zstandard

from scripts import native_snapshot as operator
from scripts.native_snapshot_bridge import OriginTranscript, binary, bridge


def transcript() -> bytes:
    return b"A\x02\x0c\x00\x00\x00\x02D\x00\x00\x00\x02" + b"x" * 4096 + b"E\x00\x00\x00\x02B"


def test_fragmented_native_protocol_is_complete_and_hashed():
    parser = OriginTranscript()
    body = transcript()
    for byte in body:
        parser.feed(bytes([byte]))
    assert parser.finish() == {
        "protocol": 2,
        "pages": 2,
        "page_bytes": 4096,
        "updated_pages": 1,
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"B",
        transcript()[:-1],
        transcript()[:-6],
        b"A\x01\x0c\x00\x00\x00\x02",
        b"A\x02\x20\x00\x00\x00\x02",
        b"A\x02\x0c\x00\x00\x00\x02D\x00\x00\x00\x03",
        b"A\x02\x0c\x00\x00\x00\x02E\x00\x00\x00\x03B",
        b"C\x00\x00\x00\x03err",
        transcript() + b"x",
    ],
)
def test_incomplete_error_or_wrong_native_protocol_is_refused(body):
    parser = OriginTranscript()
    with pytest.raises(RuntimeError):
        parser.feed(body)
        parser.finish()


def test_trailing_later_packet_is_refused():
    parser = OriginTranscript()
    parser.feed(transcript())
    with pytest.raises(RuntimeError, match="Trailing"):
        parser.feed(b"later")


def test_executable_is_not_accepted_by_filename(tmp_path):
    exe = tmp_path / "sqlite3_rsync"
    exe.write_bytes(b"wrong executable")
    with pytest.raises(RuntimeError, match="hash mismatch"):
        binary(exe, windows=False)


def test_native_source_mounts_keep_db_and_wal_read_only(tmp_path):
    value = {"worker": "cw-online-snapshot-" + "a" * 32, "image": "sha256:" + "b" * 64}
    argv = operator.command(tmp_path, value, 800)
    mounts = [argv[i + 1] for i, x in enumerate(argv) if x == "--mount"]
    assert "type=bind,src=" + str(operator.common.DATABASE.parent) + ",dst=/history,readonly" in mounts
    assert [m for m in mounts if not m.endswith(",readonly")] == [
        "type=bind,src=" + str(tmp_path / "work") + ",dst=/scratch",
        "type=bind,src=" + str(operator.common.DATABASE) + "-shm,dst=/history/cryptowatcher.sqlite3-shm",
    ]
    assert argv[argv.index("--memory") + 1] == "4g"
    assert argv[argv.index("--memory-swap") + 1] == "4g"
    assert argv[argv.index("--cpus") + 1] == "1"
    assert argv[argv.index("--network") + 1] == "none"
    assert "-i" in argv and "--read-only" in argv


def test_full_duplex_native_bridge_commits_matching_transcript(tmp_path):
    a, b = os.pipe()
    c, d = os.pipe()
    results = {}
    failures = []
    body = transcript()
    origin = (
        "import sys;sys.stdout.buffer.write(bytes.fromhex("
        + repr(body.hex())
        + "));sys.stdout.buffer.flush();sys.stdin.buffer.read()"
    )
    replica = (
        "import sys;assert len(sys.stdin.buffer.read("
        + str(len(body))
        + "))=="
        + str(len(body))
        + ';sys.stdout.buffer.write(b"done");sys.stdout.buffer.flush()'
    )

    def run(role, code, i, o):
        try:
            with (
                os.fdopen(i, "rb", buffering=0) as incoming,
                os.fdopen(o, "wb", buffering=0) as outgoing,
                (tmp_path / (role + ".stderr")).open("xb") as errors,
            ):
                results[role] = bridge(
                    [sys.executable, "-c", code], incoming, outgoing, role=role, errors=errors, seconds=10
                )
        except BaseException as error:
            failures.append(error)

    threads = [
        threading.Thread(target=run, args=("origin", origin, a, d)),
        threading.Thread(target=run, args=("replica", replica, c, b)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert not any(t.is_alive() for t in threads)
    assert not failures
    assert results["origin"]["origin"] == results["replica"]["origin"]
    assert results["origin"]["encoded_sent"] == results["replica"]["encoded_received"]
    assert results["replica"]["encoded_sent"] == results["origin"]["encoded_received"]


@pytest.mark.parametrize("payload", [b"not-zstd", zstandard.ZstdCompressor().compress(transcript())[:-2]])
def test_compressed_corruption_or_truncation_never_yields_snapshot(tmp_path, payload):
    incoming = tmp_path / "input"
    incoming.write_bytes(payload)
    with (
        incoming.open("rb") as source,
        (tmp_path / "out").open("wb") as out,
        (tmp_path / "err").open("wb") as errors,
    ):
        with pytest.raises(RuntimeError):
            bridge(
                [sys.executable, "-c", "import sys;sys.stdin.buffer.read()"],
                source,
                out,
                role="replica",
                errors=errors,
                seconds=3,
            )


def test_bridge_deadline_releases_a_stalled_child(tmp_path):
    incoming = tmp_path / "input"
    incoming.write_bytes(zstandard.ZstdCompressor().compress(b""))
    with (
        incoming.open("rb") as source,
        (tmp_path / "out").open("wb") as out,
        (tmp_path / "err").open("wb") as errors,
    ):
        with pytest.raises((RuntimeError, subprocess.TimeoutExpired)):
            bridge(
                [sys.executable, "-c", "import time;time.sleep(30)"],
                source,
                out,
                role="origin",
                errors=errors,
                seconds=0.1,
            )


def test_root_native_launcher_imports_without_third_party_packages():
    result = subprocess.run(
        [sys.executable, "-S", "-c", "from scripts import native_snapshot"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
