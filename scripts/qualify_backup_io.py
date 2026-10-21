"""Finite Linux/cgroup backup comparison using only generated temporary data."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import closing, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def private_rows(database) -> list[tuple]:
    """Compare SQL values across sqlite3.Row and APSW tuple representations."""
    return [tuple(row) for row in database.execute("SELECT * FROM owner_settings")]


def worker(directory: Path) -> dict:
    import apsw
    import zstandard

    from cryptowatcher import compressed_validation as cv
    from cryptowatcher import framed_backup, native_validation, source_validation, validation_io
    from cryptowatcher.__main__ import main as backup_cli
    from cryptowatcher.config import OwnerSettings
    from cryptowatcher.storage import Store
    from cryptowatcher.validation import fingerprint, scratch_space
    from cryptowatcher.validation_archive import ArchiveArena

    assert sys.platform == "linux" and native_validation.available() and validation_io._supported()
    assert Path("/sys/fs/cgroup/memory.max").read_text().strip() == str(4 * 1024**3)
    assert Path("/sys/fs/cgroup/memory.swap.max").read_text().strip() == "0"
    quota, period = map(int, Path("/sys/fs/cgroup/cpu.max").read_text().split())
    assert quota == period
    assert not any(directory.iterdir())
    scratch = Path("/validation")
    scratch_space(scratch, 0)
    assert not any(scratch.iterdir())
    progress = {
        "state": "RUNNING",
        "phase": "GENERATED_SOURCE",
        "kernel": os.uname().release,
        "free_bytes": shutil.disk_usage(directory).free,
        "temporary_free_bytes": shutil.disk_usage("/tmp").free,
    }

    def save_progress(phase: str) -> None:
        progress["phase"] = phase
        (directory / "result.json").write_text(json.dumps(progress, indent=2) + "\n")

    save_progress("GENERATED_SOURCE")
    source = directory / "owned.sqlite3"
    deadline = time.monotonic() + 1050
    owner = 233382575
    store = Store(source, owner)
    store.initialize(OwnerSettings())
    private = private_rows(store.db)
    store.db.execute("PRAGMA journal_mode=DELETE")
    # Index sorting belongs to the generated fixture, not the 16 MiB CLI /tmp.
    # Its bounded ~50 MiB sort stays inside the unchanged 4 GiB cgroup cap.
    store.db.execute("PRAGMA temp_store=MEMORY")
    store.db.execute("PRAGMA cache_size=-8192")
    store.db.execute("CREATE TABLE fixture(id INTEGER PRIMARY KEY, key BLOB, payload BLOB)")
    expected = hashlib.sha256()
    # About 6.5 GiB: half varying entropy and half repeated bytes. The archive
    # plus scanned source exceeds the unchanged 4 GiB cgroup file-cache budget.
    count = 786432
    with store.db:
        for start in range(0, count, 256):
            rows = []
            for index in range(start, min(count, start + 256)):
                seed = index.to_bytes(8, "little")
                key = hashlib.sha256(seed).digest()
                value = hashlib.shake_256(seed).digest(4096) + key * 128
                expected.update(seed + key + value)
                rows.append((index, key, value))
            store.db.executemany("INSERT INTO fixture VALUES(?,?,?)", rows)
    progress.update(
        source_bytes=source.stat().st_size,
        free_after_rows=shutil.disk_usage(directory).free,
        sort_store="MEMORY_WITHIN_SAME_4GIB_CAP",
    )
    save_progress("GENERATED_INDEX")
    store.db.execute("CREATE INDEX fixture_key ON fixture(key)")
    store.close()
    assert source.stat().st_size > 6 * 1024**3
    with source.open("rb") as handle:
        source_hash = hashlib.file_digest(handle, "sha256").hexdigest()
    results = []
    supported = validation_io._supported
    raw_hash = None
    original_snapshot = framed_backup.snapshot_image
    original_source = framed_backup.open_source
    original_fingerprints = framed_backup.independent_fingerprints

    def old_source(path, deadline):
        return source_validation.open_source(path, deadline, native=False)

    def separate_fingerprints(image, arena):
        return image.reconstructed_hash(), fingerprint(arena.path, deadline)

    def io_bytes() -> int:
        return int(
            dict(line.split(":", 1) for line in Path("/proc/self/io").read_text().splitlines())["read_bytes"]
        )

    for enabled in (False, True, True, False):
        assert time.monotonic() < deadline
        save_progress("COMPLETE_BACKUP_ADVISED" if enabled else "COMPLETE_BACKUP_DEFAULT")
        # Only the generated fixture's clean cache is discarded, never a host
        # global cache. Every run starts from the same owned, closed source.
        with source.open("rb") as handle:
            os.posix_fadvise(handle.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        archive = directory / "owned.sqlite3.zst"
        before_io, began, cpu = io_bytes(), time.monotonic(), time.process_time()
        phase, phase_io = "PIN_SOURCE", before_io
        reads_by_phase: dict[str, int] = {}

        def measured_snapshot(*args, **kwargs):
            progress = kwargs["progress"]

            def stage(value):
                nonlocal phase, phase_io
                current = io_bytes()
                reads_by_phase[phase] = reads_by_phase.get(phase, 0) + current - phase_io
                phase, phase_io = value, current
                progress(value)

            kwargs["progress"] = stage
            return original_snapshot(*args, **kwargs)

        validation_io._supported = supported if enabled else lambda: False
        framed_backup.open_source = original_source if enabled else old_source
        framed_backup.independent_fingerprints = original_fingerprints if enabled else separate_fingerprints
        framed_backup.snapshot_image = measured_snapshot
        previous_argv = sys.argv
        try:
            sys.argv = [
                "cryptowatcher",
                "backup",
                "--database",
                str(source),
                "--owner-id",
                str(owner),
                "--destination",
                str(archive),
                "--validation-directory",
                str(scratch),
                "--archive-backup",
                "--framed-archive-validation",
                "--timeout-seconds",
                str(min(900, deadline - time.monotonic())),
            ]
            output = io.StringIO()
            with redirect_stdout(output):
                status = backup_cli()
            assert status == 0, "Actual backup CLI refused the generated fixture"
            proof = json.loads(output.getvalue())["backup"]
        finally:
            sys.argv = previous_argv
            validation_io._supported = supported
            framed_backup.snapshot_image = original_snapshot
            framed_backup.open_source = original_source
            framed_backup.independent_fingerprints = original_fingerprints
        elapsed, cpu_used, read_bytes = (
            time.monotonic() - began,
            time.process_time() - cpu,
            io_bytes() - before_io,
        )
        assert proof["phase"] == "COMPLETE"
        assert proof["sqlite_cache_kib"] == 2097152
        if enabled:
            assert proof["source_reader"]["vfs_backend"] == "native-read-ahead"
            assert proof["source_io"] == {
                "policy": "SEQUENTIAL_NOREUSE",
                "reason": "APPLIED",
                "restored": True,
            }
        else:
            assert proof["source_io"]["policy"] == "DEFAULT"
            assert proof["source_reader"] == {"vfs_backend": "sqlite-default"}
        if raw_hash is None:
            raw_hash = proof["sha256"]
        assert proof["sha256"] == raw_hash
        progress["pending_run"] = {
            "optimized": enabled,
            "wall_seconds": round(elapsed, 3),
            "cpu_seconds": round(cpu_used, 3),
            "physical_read_bytes": read_bytes,
            "physical_reads_by_phase": reads_by_phase,
            "archive_bytes": proof["archive_bytes"],
            "raw_sha256": proof["sha256"],
            "phases": proof["phase_seconds"],
            "source_io": proof["source_io"],
            "source_reader": proof["source_reader"],
        }
        save_progress("STANDARD_ZSTD_RESTORE")
        # A generic standard ZSTD decoder independently restores every byte.
        digest, size = hashlib.sha256(), 0
        with (
            archive.open("rb") as handle,
            zstandard.ZstdDecompressor().stream_reader(handle, read_across_frames=True) as reader,
        ):
            while chunk := reader.read(1024**2):
                digest.update(chunk)
                size += len(chunk)
        assert digest.hexdigest() == raw_hash and size == proof["bytes"]
        save_progress("ALL_ORIGINAL_ROWS_AND_PRIVATE")
        arena = ArchiveArena.open(
            archive,
            max_raw_bytes=proof["bytes"],
            max_archive_bytes=proof["archive_limit_bytes"],
            deadline=deadline,
        )
        sentinel = scratch / "sentinel.sqlite3"
        sentinel.touch()
        vfs = native_validation.NativeVFS(cv.Image.from_archive_store(arena), sentinel)
        try:
            with closing(
                apsw.Connection(str(sentinel), flags=apsw.SQLITE_OPEN_READWRITE, vfs=vfs.name)
            ) as reader:
                reader.execute("PRAGMA query_only=ON")
                reader.execute("PRAGMA cache_size=-8192")
                assert private_rows(reader) == private
                actual = hashlib.sha256()
                rows_read = 0
                for index, key, value in reader.execute("SELECT * FROM fixture ORDER BY id"):
                    if rows_read % 1024 == 0:
                        assert time.monotonic() < deadline
                    actual.update(index.to_bytes(8, "little") + key + value)
                    rows_read += 1
                assert rows_read == count and actual.digest() == expected.digest()
        finally:
            vfs.unregister()
            arena.close()
            sentinel.unlink()
        results.append(
            {
                "optimized": enabled,
                "wall_seconds": round(elapsed, 3),
                "cpu_seconds": round(cpu_used, 3),
                "physical_read_bytes": read_bytes,
                "physical_reads_by_phase": reads_by_phase,
                "archive_bytes": proof["archive_bytes"],
                "source_reader": proof["source_reader"],
                "phases": proof["phase_seconds"],
            }
        )
        print(json.dumps({"qualification_run": len(results), **results[-1]}), flush=True)
        progress["completed_runs"] = results
        progress.pop("pending_run", None)
        save_progress("VERIFIED_RUN")
        # This exact fixture archive is owned by this finite qualification;
        # its identity and all rows have been verified before removal.
        archive.unlink()
        assert not any(scratch.iterdir())
    with source.open("rb") as handle:
        assert hashlib.file_digest(handle, "sha256").hexdigest() == source_hash
    events = dict(line.split() for line in Path("/sys/fs/cgroup/memory.events").read_text().splitlines())
    assert int(events["oom"]) == 0 and int(events["oom_kill"]) == 0
    baseline = [run for run in results if not run["optimized"]]
    candidate = [run for run in results if run["optimized"]]
    wall_ratio = statistics.mean(run["wall_seconds"] for run in candidate) / statistics.mean(
        run["wall_seconds"] for run in baseline
    )
    physical_before = statistics.mean(run["physical_read_bytes"] for run in baseline)
    physical_after = statistics.mean(run["physical_read_bytes"] for run in candidate)
    seal_before = statistics.mean(run["physical_reads_by_phase"]["SNAPSHOT_SEAL"] for run in baseline)
    seal_after = statistics.mean(run["physical_reads_by_phase"]["SNAPSHOT_SEAL"] for run in candidate)
    return {
        "state": "EXACT_BACKUP_AND_STANDARD_RESTORE_PASS",
        "kernel": os.uname().release,
        "source_bytes": source.stat().st_size,
        "source_sha256": source_hash,
        "raw_sha256": raw_hash,
        "original_rows": count,
        "cpu_quota": 1,
        "memory_max": 4 * 1024**3,
        "swap_max": 0,
        "memory_peak": int(Path("/sys/fs/cgroup/memory.peak").read_text()),
        "oom": 0,
        "runs": results,
        "mean_wall_ratio": round(wall_ratio, 4),
        "mean_physical_read_reduction": round(1 - physical_after / physical_before, 4)
        if physical_before
        else None,
        "mean_seal_physical_read_reduction": round(1 - seal_after / seal_before, 4) if seal_before else None,
        "meaningful_improvement": wall_ratio < 0.9
        or (seal_before > 256 * 1024**2 and seal_after < 0.75 * seal_before and wall_ratio < 1.1),
        "limits": "Generated Linux/cgroup comparison, not VPS timing, whole capacity or live measurement acceptance.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", nargs="?")
    parser.add_argument("--worker", type=Path)
    args = parser.parse_args()
    if args.worker is not None:
        try:
            result = worker(args.worker)
        except BaseException as error:
            receipt = args.worker / "result.json"
            result = json.loads(receipt.read_text()) if receipt.exists() else {}
            result.update(state="FAILED", error_class=type(error).__name__)
            receipt.write_text(json.dumps(result, indent=2) + "\n")
            raise
        (args.worker / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
        return
    if not args.image:
        parser.error("image is required")
    if sys.platform != "linux":
        parser.error("the Docker/cgroup qualification requires Linux")
    receipt = ROOT / ".cache-g1/backup-io-qualification.json"
    receipt.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cw-backup-io-") as temporary:
        directory = Path(temporary)
        directory.chmod(0o777)
        container = "cw-backup-io-" + uuid.uuid4().hex
        try:
            process = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--name",
                    container,
                    "--user",
                    f"{os.getuid()}:{os.getgid()}",
                    "--network=none",
                    "--read-only",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges:true",
                    "--memory=4g",
                    "--memory-swap=4g",
                    "--cpus=1",
                    "--tmpfs=/tmp:rw,noexec,nosuid,size=16m",
                    f"--tmpfs=/validation:rw,noexec,nosuid,nodev,size=64m,mode=0700,uid={os.getuid()},gid={os.getgid()}",
                    "-v",
                    f"{directory.resolve()}:/qualification",
                    "-v",
                    f"{Path(__file__).resolve()}:/app/operator/qualify_backup_io.py:ro",
                    "--entrypoint=python",
                    args.image,
                    "/app/operator/qualify_backup_io.py",
                    "--worker",
                    "/qualification",
                ],
                check=False,
                timeout=1100,
            )
        finally:
            # Only this invocation's random, owned container, including on a
            # client timeout. No surviving helper or background observation.
            subprocess.run(
                ["docker", "rm", "--force", container], capture_output=True, timeout=15, check=False
            )
            partial = directory / "result.json"
            if partial.exists():
                receipt.write_bytes(partial.read_bytes())
        assert process.returncode == 0, "Generated qualification failed; bounded phase receipt retained"
        result = json.loads((directory / "result.json").read_text())
        receipt.write_text(json.dumps(result, indent=2) + "\n")
        assert result["state"] == "EXACT_BACKUP_AND_STANDARD_RESTORE_PASS"
        assert result["meaningful_improvement"], (
            "Exact Linux backup passed, but the combined source/hash path has no qualified performance benefit"
        )
    assert not directory.exists()
    print("Owned temporary source and archives cleaned; bounded qualification receipt retained.")


if __name__ == "__main__":
    main()
