"""One-time isolated conversion with owner-approved exact-byte recovery proof reuse."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Iterator, cast

import apsw
import zstandard

from . import compressed_validation as cv
from .history_rebuild import DISK_RESERVE, closed, copy_snapshot, fingerprint, guard, refusal_code
from .recovery_bytes import verify_archive_bytes
from .storage import IntegrityError, StateError


@contextmanager
def closed_image(
    source: Path, directory: Path, *, encoding: str, expected: str, deadline: float
) -> Iterator[tuple[Any, cv.Image]]:
    closed(source)
    encoded_hash = fingerprint(source, deadline)
    archive_encoding = {"sqlite": None, "gzip": "GZIP", "zstd": "ZSTD"}[encoding]
    image = cv.Image(
        source,
        limit=cv.MAX_COMPRESSED_BYTES,
        deadline=deadline,
        archive_encoding=cast(Any, archive_encoding),
    )
    if image.sha256 != expected:
        raise StateError("Decoded source fingerprint mismatch")
    with tempfile.TemporaryDirectory(prefix="cw-conversion-source-", dir=directory) as temporary:
        sentinel = Path(temporary) / "sentinel.sqlite3"
        sentinel.touch(mode=0o600)
        vfs = cv.frozen_vfs(image, sentinel)
        db = None
        try:
            db = apsw.Connection(str(sentinel), flags=apsw.SQLITE_OPEN_READWRITE, vfs=vfs.name)
            db.execute("PRAGMA query_only=ON")
            cv.configure_cache(db, image)
            cv.full_check(db, deadline)
            yield db, image
            if image.reconstructed_hash() != expected:
                raise StateError("Decoded source changed during rebuild")
            image.assert_source_unchanged()
            if fingerprint(source, deadline) != encoded_hash:
                raise StateError("Source changed; candidate not qualified")
            closed(source)
        finally:
            if db is not None:
                db.close()
            vfs.unregister()


def qualify_conversion(
    source: Path,
    directory: Path,
    owner: int,
    expected_revision: int,
    *,
    max_candidate_bytes: int,
    max_archive_bytes: int,
    timeout_seconds: float = 900,
    source_encoding: str = "live",
    expected_source_sha256: str | None = None,
    backend: str = "native",
) -> dict[str, Any]:
    """No promotion API; failed partial outputs remain available for diagnosis."""
    start = time.monotonic()
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 900:
        raise StateError("Invalid rebuild budget")
    deadline = start + timeout_seconds
    if (
        source_encoding not in ("live", "sqlite", "gzip", "zstd")
        or type(owner) is not int
        or owner <= 0
        or type(expected_revision) is not int
        or expected_revision <= 0
        or type(max_candidate_bytes) is not int
        or max_candidate_bytes < 32768
        or type(max_archive_bytes) is not int
        or not 0 < max_archive_bytes <= cv.MAX_COMPRESSED_BYTES
    ):
        raise StateError("Invalid conversion inputs")
    if source_encoding != "live" and (
        not isinstance(expected_source_sha256, str)
        or not re.fullmatch("[a-f0-9]{64}", expected_source_sha256)
    ):
        raise StateError("Expected qualified source SHA256 required")
    if any(path.is_symlink() for path in (source, *source.parents)):
        raise StateError("Symlink refused")
    if not source.is_file() or not directory.is_dir():
        raise StateError("Source or destination directory unavailable")
    closed(directory)
    if os.name != "nt":
        meta = directory.stat()
        if meta.st_uid != os.geteuid() or meta.st_mode & 0o077:
            raise StateError("Destination directory must be private and owned")
    if shutil.disk_usage(directory).free < max_candidate_bytes + max_archive_bytes + DISK_RESERVE:
        raise StateError("Insufficient disk for bounded isolated candidate")
    candidate, archive = directory / "candidate.sqlite3", directory / "source.sqlite3.zst"
    if any(path.exists() or path.is_symlink() for path in (candidate, archive)):
        raise StateError("Destination must be new isolated files")
    with (
        cv.classified_errors(deadline),
        tempfile.TemporaryDirectory(prefix="cw-conversion-", dir=directory) as scratch_name,
    ):
        scratch = Path(scratch_name)
        with ExitStack() as source_scope:
            capture = (
                cv.snapshot_image(source, scratch, deadline=deadline)
                if source_encoding == "live"
                else closed_image(
                    source,
                    scratch,
                    encoding=source_encoding,
                    expected=cast(str, expected_source_sha256),
                    deadline=deadline,
                )
            )
            original, image = source_scope.enter_context(capture)
            if list(original.execute("SELECT owner_id,revision FROM owner_settings")) != [
                (owner, expected_revision)
            ]:
                raise StateError("Source owner/revision changed")
            source_bytes = image.size
            digest = hashlib.sha256()
            handle = os.open(archive, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(handle, "wb") as output:
                sink = cv.ArchiveWriter(output, deadline)
                compressor = zstandard.ZstdCompressor(level=1, write_checksum=True, threads=0)
                with compressor.stream_writer(
                    cast(IO[bytes], sink), size=source_bytes, closefd=False
                ) as writer:
                    for block in image.iter_bytes():
                        guard(deadline)
                        digest.update(block)
                        writer.write(block)
                        if sink.size > max_archive_bytes:
                            raise StateError("Recovery archive exceeds byte limit")
                output.flush()
                os.fsync(output.fileno())
            source_hash = digest.hexdigest()
            # The source context compares these same frozen bytes with its pre-check
            # image at release. Closed sources additionally have a caller-pinned identity.
            if (source_encoding != "live" and source_hash != image.sha256) or sink.size > max_archive_bytes:
                raise StateError("Persisted source archive byte identity mismatch")
            archive_hash, archive_bytes = sink.digest.hexdigest(), sink.size
            candidate.touch(mode=0o600, exist_ok=False)

            def source_copied() -> None:
                nonlocal image
                source_scope.close()  # Complete source unchanged-byte gates before candidate readback.
                image = None  # type: ignore[assignment]

            copied = copy_snapshot(
                original,
                candidate,
                owner,
                max_candidate_bytes,
                deadline,
                backend=backend,
                source_copied=source_copied,
                compressed_candidate_validation=True,
            )
        del original, image
        candidate_hash = fingerprint(candidate, deadline)
        # Only reached after full source/unchanged checks and all candidate CHECK/manifests/private bytes.
        # Expected identities come from this operation's checked source and persisted writer, never the caller.
        restored = verify_archive_bytes(
            archive,
            encoding="zstd",
            encoded_sha256=archive_hash,
            encoded_bytes=archive_bytes,
            decoded_sha256=source_hash,
            decoded_bytes=source_bytes,
            deadline=deadline,
        )
        if fingerprint(archive, deadline) != archive_hash or archive.stat().st_size != archive_bytes:
            raise StateError("Persisted archive encoding changed")
        guard(deadline)
        return {
            "status": "ISOLATED_CONVERSION_PASS",
            "at_utc": datetime.now(timezone.utc).isoformat(),
            "source_encoding": source_encoding,
            "source_sha256": source_hash,
            "source_bytes": source_bytes,
            "source_full_integrity": "PASS",
            "candidate_sha256": candidate_hash,
            "candidate_bytes": candidate.stat().st_size,
            "candidate_full_integrity": "PASS",
            "full_logical_rows_and_private_bytes": "PASS",
            "recovery": restored,
            "recovery_integrity_basis": "SAME_QUALIFIED_BYTES",
            "recovery_verification": "STRICT_FRAME_FULL_DECODE_AND_SHA256",
            "promotion_eligible": False,
            "production_migration": "NOT_RUN",
            "live_writer_may_advance": source_encoding == "live",
            "elapsed_seconds": round(time.monotonic() - start, 3),
            **copied,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--owner-id", type=int, required=True)
    parser.add_argument("--expected-owner-revision", type=int, required=True)
    parser.add_argument("--max-candidate-bytes", type=int, required=True)
    parser.add_argument("--max-archive-bytes", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=900)
    parser.add_argument("--source-encoding", choices=("live", "sqlite", "gzip", "zstd"), default="live")
    parser.add_argument("--qualified-source-sha256")
    parser.add_argument("--backend", choices=("auto", "python", "native"), default="native")
    args = parser.parse_args()
    try:
        result = qualify_conversion(
            args.source,
            args.directory,
            args.owner_id,
            args.expected_owner_revision,
            max_candidate_bytes=args.max_candidate_bytes,
            max_archive_bytes=args.max_archive_bytes,
            timeout_seconds=args.timeout_seconds,
            source_encoding=args.source_encoding,
            expected_source_sha256=args.qualified_source_sha256,
            backend=args.backend,
        )
        print(json.dumps(result))
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "failure_code": error.code if isinstance(error, IntegrityError) else refusal_code(error),
                    "candidate": "UNQUALIFIED",
                    "production_migration": "NOT_RUN",
                }
            )
        )
        return error.exit_status if isinstance(error, IntegrityError) else 2


if __name__ == "__main__":
    raise SystemExit(main())
