"""Resume an isolated Windows copy inside one explicit immutable time window."""

import argparse
import hashlib
import json
import os
import sys
import tarfile
import threading
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from cryptowatcher import compressed_validation as cv
from cryptowatcher.frame_archive import manifest, verify
from cryptowatcher.framed_rebuild import copy_validated
from cryptowatcher.history_rebuild import refusal_code
from cryptowatcher.storage import StateError, encode
from scripts.windows_copy_limits import process_memory_bytes, windows_limits

WINDOW_SCHEMA = "cryptowatcher.local-copy-window.v1"


def window_deadline(window, *, now=None):
    now = datetime.now(timezone.utc) if now is None else now
    if window.get("schema") != WINDOW_SCHEMA or window.get("owner_authorized") is not True:
        raise StateError("Explicit local operation window required")
    seconds = window.get("seconds")
    if type(seconds) is not int or not 0 < seconds <= 5400:
        raise StateError("Invalid local operation window")
    if seconds > 900 and window.get("one_time_local") is not True:
        raise StateError("Explicit one-time local window required")
    if window.get("ordinary_backup_cd_seconds") != 900:
        raise StateError("Ordinary operation limits must remain unchanged")
    started = datetime.fromisoformat(window["started_at_utc"])
    if started.tzinfo is None:
        raise StateError("UTC operation start required")
    elapsed = (now - started).total_seconds()
    if not 0 <= elapsed < seconds:
        raise StateError("Original local operation window exhausted")
    return time.monotonic() + seconds - elapsed


def digest(path, deadline, progress=lambda **kw: None):
    before = path.stat()
    h = hashlib.sha256()
    total = 0
    last = time.monotonic()
    with path.open("rb") as f:
        for data in iter(lambda: f.read(1024 * 1024), b""):
            cv.budget(deadline)
            h.update(data)
            total += len(data)
            if time.monotonic() - last >= 30:
                progress(bytes=total, total_bytes=before.st_size)
                last = time.monotonic()
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise StateError("Source changed; candidate not qualified")
    return h.hexdigest()


def admit_source(window, descriptor):
    """Bind the primary full-SQL result and unchanged validator to identical bytes."""
    wrapper = json.loads(Path(window["source_proof"]).read_text(encoding="utf-8-sig"))
    record = wrapper["record"]
    actual = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
    if actual != window["source_proof_sha256"] or wrapper["record_sha256"] != actual:
        raise StateError("Source proof identity mismatch")
    if (
        record.get("schema") != "cryptowatcher.frozen-source-integrity-evidence.v1"
        or record.get("state") != "SOURCE_FULL_INTEGRITY_PASS"
        or record.get("scope") != "HISTORICAL_SOURCE_ONLY_NOT_CANDIDATE_QUALIFICATION_OR_PROMOTION"
        or record.get("production_replacement") is not False
        or record["source_record"].get("phase") != "SOURCE_FULL_INTEGRITY_PASS"
        or record["source_record"].get("raw_sha256") != descriptor["raw_sha256"]
        or record["source_record"].get("bytes") != descriptor["raw_bytes"]
        or record["data_sha256"] != descriptor["data_sha256"]
        or record["index_sha256"] != descriptor["index_sha256"]
    ):
        raise StateError("Full source proof does not cover this snapshot")
    recovery_bytes = Path(window["recovery"]).read_bytes()
    if hashlib.sha256(recovery_bytes).hexdigest() != record["descriptor_sha256"]:
        raise StateError("Recovery descriptor identity mismatch")
    archive = Path(window["code_archive"])
    if archive.stat().st_size > 16 * 1024**2:
        raise StateError("Source validator archive size refused")
    if hashlib.sha256(archive.read_bytes()).hexdigest() != record["code_sha256"]:
        raise StateError("Source validator archive identity mismatch")
    with tarfile.open(archive) as files:
        old_validator = files.extractfile("cryptowatcher/compressed_validation.py")
        if old_validator is None or old_validator.read() != Path(cv.__file__).read_bytes():
            raise StateError("Source validator changed; full source proof required")
    return actual


class Reporter:
    def __init__(self, work):
        self.work = work

    def publish(self, **fields):
        fields["at_utc"] = datetime.now(timezone.utc).isoformat()
        fields["pid"] = os.getpid()
        text = encode(fields)
        temporary = self.work / "latest.tmp"
        try:
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(self.work / "latest.json")
        except OSError:
            # Volatile diagnostics are separate from the atomic SQLite checkpoint.
            fields["progress_file"] = "UNAVAILABLE"
        try:
            print(encode(fields), flush=True)
        except BrokenPipeError:
            pass  # A dropped observer never discards committed copy progress.


@contextmanager
def exclusive_session(work):
    import msvcrt

    with (work / ".session.lock").open("a+b") as lock:
        if not lock.tell():
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def run(window):
    import apsw

    if sys.platform != "win32":
        raise StateError("This procedure requires the approved Windows PC")
    deadline = window_deadline(window)
    source, recovery, work = (Path(window[k]) for k in ("source", "recovery", "work"))
    for path in (source, recovery, work):
        if path.resolve() != path or any(
            p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in (path, *path.parents)
        ):
            raise StateError("Private local paths required")
    if work == source.parent or work == recovery.parent:
        raise StateError("Separate private candidate directory required")
    maximum = window["maximum"]
    if type(maximum) is not int or maximum <= 0:
        raise StateError("Explicit candidate capacity required")
    work.mkdir(mode=0o700, exist_ok=True)
    allowed = {
        "candidate.sqlite3",
        "candidate.sqlite3.copy.json",
        "candidate.sqlite3-journal",
        "windows",
        ".session.lock",
        "latest.json",
        "latest.tmp",
        "qualification.json",
    }
    if any(p.name not in allowed or p.is_symlink() for p in work.iterdir()):
        raise StateError("Unknown local work files preserved")
    reporter = Reporter(work)
    limits = windows_limits()
    stopped = threading.Event()

    def watchdog():
        if stopped.wait(max(0, deadline - time.monotonic())):
            return
        try:
            reporter.publish(state="UNQUALIFIED", reason="TIME_BUDGET_EXCEEDED")
        finally:
            os._exit(3)

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        with exclusive_session(work):
            if (work / "qualification.json").exists():
                raise StateError("Qualification exists; inspect before rerun")
            windows = work / "windows"
            windows.mkdir(mode=0o700, exist_ok=True)
            window_bytes = encode(window).encode()
            window_path = windows / (hashlib.sha256(window_bytes).hexdigest() + ".json")
            if not window_path.exists():
                with window_path.open("xb") as f:
                    f.write(window_bytes)
                    f.flush()
                    os.fsync(f.fileno())
            elif window_path.read_bytes() != window_bytes:
                raise StateError("Local operation window changed")
            descriptor = manifest(recovery)
            proof_sha = admit_source(window, descriptor)
            if source.stat().st_size != descriptor["raw_bytes"]:
                raise StateError("Frozen source size mismatch")
            reporter.publish(state="RUNNING", phase="SOURCE_IDENTITY", limits=limits)
            if (
                digest(
                    source,
                    deadline,
                    progress=lambda **v: reporter.publish(state="RUNNING", phase="SOURCE_IDENTITY", **v),
                )
                != descriptor["raw_sha256"]
            ):
                raise StateError("Source fingerprint mismatch")
            cv.resident_memory_bytes = process_memory_bytes
            candidate = work / "candidate.sqlite3"
            with (
                cv.classified_errors(deadline),
                closing(apsw.Connection(str(source), flags=apsw.SQLITE_OPEN_READWRITE)) as db,
            ):
                db.execute("PRAGMA query_only=ON")
                db.execute("PRAGMA mmap_size=0")
                result = copy_validated(
                    db,
                    candidate,
                    owner=window["owner"],
                    maximum=maximum,
                    deadline=deadline,
                    source_identity=descriptor["raw_sha256"],
                    resume=candidate.exists(),
                    progress=lambda **v: reporter.publish(state="RUNNING", **v),
                )
            reporter.publish(state="RUNNING", phase="SOURCE_POST_IDENTITY")
            if (
                digest(
                    source,
                    deadline,
                    progress=lambda **v: reporter.publish(state="RUNNING", phase="SOURCE_POST_IDENTITY", **v),
                )
                != descriptor["raw_sha256"]
            ):
                raise StateError("Source changed; candidate not qualified")
            reporter.publish(state="RUNNING", phase="INDEPENDENT_RECOVERY")
            recovery_result = verify(recovery, deadline=deadline)
            result.update(
                state="QUALIFIED_SNAPSHOT_CANDIDATE",
                source_full_integrity="PASS",
                source_integrity_reused=True,
                source_integrity_primary_sha256=proof_sha,
                source_raw_sha256=descriptor["raw_sha256"],
                source_bytes=descriptor["raw_bytes"],
                recovery=recovery_result,
                production_replacement=False,
                historical_snapshot_only=True,
                environment="LOCAL_WINDOWS",
                limits=limits,
                operation_window=str(window_path),
            )
            cv.budget(deadline)
            with (work / "qualification.json").open("x", encoding="utf-8") as f:
                f.write(encode(result))
                f.flush()
                os.fsync(f.fileno())
            cv.budget(deadline)
            reporter.publish(state="PASS", phase="COMPLETE", result=result)
            return result
    finally:
        stopped.set()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window", type=Path, required=True)
    args = parser.parse_args()
    try:
        window = json.loads(args.window.read_text(encoding="utf-8-sig"))
        run(window)
    except (StateError, OSError, ValueError, KeyError) as error:
        print(encode(dict(state="UNQUALIFIED", reason=refusal_code(error))), flush=True)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
