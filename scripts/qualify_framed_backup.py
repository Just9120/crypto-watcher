"""Qualify current backup and independent restore from an explicit closed snapshot.

Run only in the bounded isolated Linux qualification container described in the
storage runbook. Never points at or replaces the working database. Existing
snapshots are read as files; all SQLite reads use a new private input copy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from contextlib import closing
from pathlib import Path

import zstandard

from cryptowatcher import frozen_codec_native, native_disk_backup
from cryptowatcher.events import PREFIX, History
from cryptowatcher.reset_history import private_state, signature, write_new
from cryptowatcher.storage import StateError, Store, encode
from cryptowatcher.validation import budget, fingerprint
from cryptowatcher.validation_archive import BLOCK

RESERVE = 7 * 1024**3


def closed_identity(path: Path) -> tuple[int, int, int, int]:
    if (
        not path.is_absolute()
        or path.resolve() != path
        or any(p.is_symlink() for p in (path, *path.parents))
        or not path.is_file()
        or any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))
    ):
        raise StateError("Qualification source must be an explicit closed regular snapshot")
    st = path.stat()
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


def data_signature(path: Path, owner: int) -> dict:
    with closing(Store(path, owner, readonly=True)) as store:
        history = History(store)
        samples = [
            history.metadata(key.removeprefix(PREFIX))
            for key, _ in store.scan_runtime(PREFIX + "outcome-head:", limit=8)
        ]
        return {
            "private_sha256": signature(private_state(store)),
            "outcome_sample_sha256": hashlib.sha256(encode(samples).encode()).hexdigest(),
            "outcome_samples": len(samples),
            "numeric_samples": sum(bool(value and value.get("metrics")) for value in samples),
            "sqlite_bytes": store.db.execute("PRAGMA page_count").fetchone()[0]
            * store.db.execute("PRAGMA page_size").fetchone()[0],
            "readonly_changes": store.db.total_changes,
        }


def require_native_limits() -> None:
    if sys.platform != "linux" or not frozen_codec_native.available():
        raise StateError("Linux/native qualification environment required")
    import resource

    cpu_soft, cpu_hard = resource.getrlimit(resource.RLIMIT_CPU)
    if not 0 < cpu_soft <= cpu_hard <= 450:
        raise StateError("Qualification requires an inherited total process CPU limit at most450seconds")
    cgroup = Path("/sys/fs/cgroup")
    quota, period = (cgroup / "cpu.max").read_text().split()
    memory = (cgroup / "memory.max").read_text().strip()
    swap = (cgroup / "memory.swap.max").read_text().strip()
    if (
        quota == "max"
        or int(quota) > int(period)
        or memory == "max"
        or int(memory) > 4 * 1024**3
        or swap != "0"
    ):
        raise StateError("Qualification requires at most1CPU/4GiB without swap")


def qualify(
    source: Path,
    work: Path,
    scratch: Path,
    *,
    owner: int,
    expected_source_bytes: int,
    deadline: float,
    native_required: bool = True,
    minimum_source_bytes: int = 2 * 1024**3,
) -> dict:
    began, cpu = time.monotonic(), time.process_time()
    budget(deadline)
    if deadline - began > 510:
        raise StateError("Qualification exceeds agreed wall limit")
    if native_required:
        require_native_limits()
    initial = closed_identity(source)
    if not minimum_source_bytes <= initial[2] == expected_source_bytes <= 35_000_000_000:
        raise StateError("Closed snapshot size does not match the admitted representative input")
    if (
        not work.is_absolute()
        or work.resolve() != work
        or any(p.is_symlink() for p in (work, *work.parents))
        or work.exists()
        or not work.parent.is_dir()
        or source.is_relative_to(work)
        or scratch == work
        or scratch.is_relative_to(work)
    ):
        raise StateError("Qualification requires a new exclusive physical work directory")
    # Original already occupies disk. New input copy + pinned raw + archive,
    # then input + archive + restore. Raw capture is removed before restore.
    frames = (expected_source_bytes + BLOCK - 1) // BLOCK
    archive_overhead = 2 * BLOCK + 128 + frames * 144
    peak_extra = 3 * expected_source_bytes + archive_overhead
    if shutil.disk_usage(work.parent).free < peak_extra + RESERVE:
        raise StateError("Insufficient full qualification peak and shared-VPS reserve")
    work.mkdir(mode=0o700)
    private_input = work / "input.sqlite3"
    source_sha, copied = fingerprint(source, deadline, destination=private_input)
    if copied != expected_source_bytes or closed_identity(source) != initial:
        raise StateError("Closed snapshot changed during input copy")
    expected = data_signature(private_input, owner)
    if (
        expected["readonly_changes"] != 0
        or expected["numeric_samples"] == 0
        or expected["sqlite_bytes"] > expected_source_bytes
    ):
        raise StateError("Representative private/numeric snapshot unavailable")
    archive = work / "qualified.sqlite3.zst"
    proof = native_disk_backup.backup(private_input, archive, scratch, owner=owner, deadline=deadline)
    if proof.get("phase") != "COMPLETE" or proof.get("raw_snapshot_removed") is not True:
        raise StateError("Full native capture and independent readback required")
    restored = work / "restored.sqlite3"
    digest, restored_bytes = hashlib.sha256(), 0
    with (
        archive.open("rb") as raw,
        zstandard.ZstdDecompressor(max_window_size=BLOCK).stream_reader(
            raw, read_across_frames=True
        ) as reader,
    ):
        with os.fdopen(
            os.open(restored, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb", buffering=0
        ) as output:
            while True:
                budget(deadline)
                chunk = reader.read(1024 * 1024)
                if not chunk:
                    break
                restored_bytes += len(chunk)
                if restored_bytes > proof["bytes"] or shutil.disk_usage(work).free < RESERVE:
                    raise StateError("Restore exceeds admitted bytes or physical reserve")
                if output.write(chunk) != len(chunk):
                    raise StateError("Incomplete restore write")
                digest.update(chunk)
            os.fsync(output.fileno())
    restored_sha, restored_size = fingerprint(restored, deadline)
    actual = data_signature(restored, owner)
    final_sha, _ = fingerprint(source, deadline)
    archive_sha, _ = fingerprint(archive, deadline)
    if (
        restored_bytes != restored_size
        or restored_size != proof["bytes"]
        or digest.hexdigest() != restored_sha
        or restored_sha != proof["sha256"]
        or actual != expected
        or final_sha != source_sha
        or closed_identity(source) != initial
        or archive_sha != proof["archive_sha256"]
    ):
        raise StateError("Independent restore, private/numeric or unchanged source identity mismatch")
    budget(deadline)
    result = {
        "schema": "cryptowatcher.backup-qualification.v1",
        "state": "PASS" if native_required else "FUNCTIONAL_PASS_ONLY",
        "environment": sys.platform,
        "native_backend": frozen_codec_native.available(),
        "source_bytes": expected_source_bytes,
        "source_sha256": source_sha,
        "source_unchanged": True,
        "validated_snapshot_sha256": proof["sha256"],
        "restored_sha256": restored_sha,
        "restored_bytes": restored_size,
        "private_numeric_equality": actual,
        "backup": proof,
        "peak_extra_admission_bytes": peak_extra,
        "shared_disk_reserve_bytes": RESERVE,
        "elapsed_seconds": time.monotonic() - began,
        "cpu_seconds": time.process_time() - cpu,
        "limitations": [
            "Closed explicitly selected snapshot, not online working-history observation",
            "Full validated snapshot byte equality covers every restored page; additional numeric sample is bounded8",
            "Historical snapshot population and current episode-table regression are distinct evidence",
            "Whole-host peak/resources require the outer qualification controller; no35GB or24h claim",
        ],
    }
    duplicates = {}
    for database in (private_input, restored):
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(database) + suffix)
            if path.exists():
                if path.is_symlink() or not path.is_file() or (suffix == "-wal" and path.stat().st_size):
                    raise StateError("Unexpected owned validation sidecar; all files preserved")
                st = path.stat()
                duplicates[path] = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        if Path(str(database) + "-journal").exists():
            raise StateError("Unexpected owned validation journal; all files preserved")
    result["duplicate_cleanup_receipt"] = "cleanup.json"
    write_new(work / "result.json", json.dumps(result, sort_keys=True).encode())
    # Only new verified redundant copies/empty-WAL sidecars are deleted. The
    # immutable result is written first; missing cleanup receipt means PENDING.
    for path, expected_identity in duplicates.items():
        st = path.lstat()
        if (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns) != expected_identity or path.is_symlink():
            raise StateError("Owned duplicate changed before cleanup")
        path.unlink()
    write_new(
        work / "cleanup.json",
        json.dumps({"state": "COMPLETE", "removed": [p.name for p in duplicates]}, sort_keys=True).encode(),
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-source-bytes", type=int, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--owner", type=int, required=True)
    parser.add_argument("--seconds", type=int, default=480)
    args = parser.parse_args()
    if not 20 <= args.seconds <= 510:
        parser.error("Qualification wall limit outside20..510seconds")
    print(
        json.dumps(
            qualify(
                args.source,
                args.work,
                args.scratch,
                owner=args.owner,
                expected_source_bytes=args.expected_source_bytes,
                deadline=time.monotonic() + args.seconds,
            )
        )
    )


if __name__ == "__main__":
    main()
