import hashlib
import json
import os
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from cryptowatcher.config import OwnerSettings
from cryptowatcher.disk_validation import validated_disk
from cryptowatcher.market_storage import enable
from cryptowatcher.reset_history import prepare, private_state, signature
from cryptowatcher.storage import StateError, Store
from scripts import fresh_measurement as operation

OWNER = 233382575
GENERATION = "a" * 32


@pytest.fixture
def archived(tmp_path):
    source = (tmp_path / "archive.sqlite3").resolve()
    with closing(Store(source, OWNER)) as store:
        store.initialize(OwnerSettings(alert_universe_mode="top", profile="custom"))
        enable(store.db)
        store.bounded = True
        store.put_runtime("history_generation", {"id": GENERATION})
        store.put_runtime("market-old", {"exact": 0.1234567890123456})
        store.mute("spot/BTCUSDT", 9999999999)
        store.set_enabled(False)
        preserved = private_state(store)
    raw = source.read_bytes()
    return source, raw, hashlib.sha256(raw).hexdigest(), preserved


def test_fresh_measurement_preserves_archive_and_exact_private_state(tmp_path, archived):
    source, raw, digest, preserved = archived
    fresh = (tmp_path / "fresh.sqlite3").resolve()
    proof = prepare(
        source,
        fresh,
        (tmp_path / "private.json").resolve(),
        OWNER,
        expected_generation=GENERATION,
        preserved_archive_sha256=digest,
    )
    assert source.read_bytes() == raw
    with closing(Store(source, OWNER, readonly=True)) as old:
        assert old.get_runtime("market-old") == {"exact": 0.1234567890123456}
    with closing(Store(fresh, OWNER, readonly=True)) as current:
        assert signature(private_state(current)) == signature(preserved)
        assert current.get_runtime("market-old") is None
        generation = current.get_runtime("history_generation")
        assert generation["id"] != GENERATION
        assert generation["previous_market_history"] == "PRESERVED_IN_ARCHIVE"
        assert generation["reason"] == "OWNER_AUTHORIZED_FRESH_MEASUREMENT_20261017"
        assert current.bounded
    assert proof["private_restore"] == proof["integrity"] == "PASS"
    assert proof["preserved_archive_sha256"] == digest
    assert proof["previous_generation_id"] == GENERATION


@pytest.mark.parametrize("which", ["generation", "hash", "missing-hash", "missing-generation", "automatic"])
def test_fresh_measurement_rejects_unknown_archive_or_automatic_repeat(tmp_path, archived, which):
    source, raw, digest, _ = archived
    options = {"expected_generation": GENERATION, "preserved_archive_sha256": digest}
    if which == "generation":
        options["expected_generation"] = "b" * 32
    elif which == "hash":
        options["preserved_archive_sha256"] = "0" * 64
    elif which.startswith("missing-"):
        options.pop("preserved_archive_sha256" if which == "missing-hash" else "expected_generation")
    else:
        options = {}
    fresh = (tmp_path / "fresh.sqlite3").resolve()
    recovery = (tmp_path / "private.json").resolve()
    with pytest.raises(StateError):
        prepare(source, fresh, recovery, OWNER, **options)
    assert source.read_bytes() == raw and not fresh.exists() and not recovery.exists()


def test_explicit_repeat_discard_keeps_private_state_without_claiming_an_archive(tmp_path, archived):
    source, raw, _, preserved = archived
    fresh, recovery = (tmp_path / "fresh.sqlite3").resolve(), (tmp_path / "private.json").resolve()
    proof = prepare(source, fresh, recovery, OWNER, expected_generation=GENERATION, discard_test_history=True)
    assert source.read_bytes() == raw  # Candidate preparation never deletes its source.
    assert proof["private_source_sha256"] == signature(preserved)
    assert proof["discard_test_history"] is True and "preserved_archive_sha256" not in proof
    with closing(Store(fresh, OWNER, readonly=True)) as current:
        assert private_state(current) == preserved
        assert current.get_runtime("market-old") is None
        assert (
            current.get_runtime("history_generation")["previous_market_history"]
            == "DISCARDED_BY_OWNER_DECISION"
        )
        assert (
            current.get_runtime("history_generation")["reason"]
            == "OWNER_AUTHORIZED_TEST_HISTORY_DISCARD_20261022"
        )
    assert proof["integrity"] == proof["private_restore"] == "PASS"


@pytest.mark.parametrize(
    "generation,digest", [(None, None), ("unknown", None), ("b" * 32, None), (GENERATION, "0" * 64)]
)
def test_discard_requires_exact_existing_generation_and_no_archive_claim(
    tmp_path, archived, generation, digest
):
    source, raw, _, _ = archived
    fresh, recovery = (tmp_path / "fresh.sqlite3").resolve(), (tmp_path / "private.json").resolve()
    with pytest.raises(StateError):
        prepare(
            source,
            fresh,
            recovery,
            OWNER,
            expected_generation=generation,
            preserved_archive_sha256=digest,
            discard_test_history=True,
        )
    assert source.read_bytes() == raw and not fresh.exists() and not recovery.exists()


def test_discard_stages_only_working_files_then_removes_them_after_fresh_reopen(
    tmp_path, archived, monkeypatch
):
    old, raw, _, preserved = archived
    source = (tmp_path / "cryptowatcher.sqlite3").resolve()
    old.rename(source)
    folder = (tmp_path / "previous-working").resolve()
    folder.mkdir()
    keep = tmp_path / "other-backup.sqlite3"
    keep.write_bytes(b"keep previous measurement")
    fresh = (tmp_path / "fresh.sqlite3").resolve()
    prepare(
        source,
        fresh,
        (tmp_path / "private.json").resolve(),
        OWNER,
        expected_generation=GENERATION,
        discard_test_history=True,
    )
    monkeypatch.setattr(operation, "require_no_handles", lambda path: None)
    monkeypatch.setattr(operation, "sync_directory", lambda path: None)
    plan = operation.test_discard_plan(source, folder)
    operation.stage_test_discard(plan, source, folder)
    assert (folder / source.name).read_bytes() == raw
    fresh.rename(source)
    with closing(Store(source, OWNER, readonly=True)) as current:
        assert private_state(current) == preserved
    operation.remove_staged_test_history(plan, source, folder)
    assert source.exists() and keep.read_bytes() == b"keep previous measurement"
    assert not any(folder.iterdir())


@pytest.mark.parametrize("tamper", ["foreign", "changed", "duplicate", "open"])
def test_discard_rejects_changed_or_unrelated_files_before_any_deletion(tmp_path, monkeypatch, tamper):
    source = (tmp_path / "cryptowatcher.sqlite3").resolve()
    source.write_bytes(b"old test database")
    sidecar = Path(str(source) + "-wal")
    sidecar.write_bytes(b"wal")
    folder = (tmp_path / "previous-working").resolve()
    folder.mkdir()
    monkeypatch.setattr(operation, "require_no_handles", lambda path: None)
    monkeypatch.setattr(operation, "sync_directory", lambda path: None)
    plan = operation.test_discard_plan(source, folder)
    operation.stage_test_discard(plan, source, folder)
    keep = (tmp_path / "keep.sqlite3").resolve()
    keep.write_bytes(b"unrelated archive")
    if tamper == "foreign":
        plan[0]["path"] = str(keep)
    elif tamper == "changed":
        (folder / source.name).write_bytes(b"unexpected changed database")
    elif tamper == "duplicate":
        plan.append(plan[0])
    else:

        def still_open(path):
            raise RuntimeError("Working source still has open handles")

        monkeypatch.setattr(operation, "require_no_handles", still_open)
    with pytest.raises(RuntimeError):
        operation.remove_staged_test_history(plan, source, folder)
    assert keep.read_bytes() == b"unrelated archive"
    assert (folder / source.name).exists() and (folder / sidecar.name).exists()


def test_discard_policy_conflict_is_rejected_before_host_or_data_access():
    with pytest.raises(RuntimeError, match="Conflicting"):
        operation.execute("a" * 40, "b" * 32, approved=True, archive_original=True, discard_test_history=True)
    with pytest.raises(RuntimeError, match="Reviewed reset helper"):
        operation.execute("a" * 40, "b" * 32, approved=True, discard_test_history=True)


@pytest.mark.parametrize("postcheck_ok", [True, False])
def test_full_discard_operator_deletes_only_after_successful_private_runtime_readback(
    tmp_path, archived, monkeypatch, postcheck_ok
):
    from cryptowatcher.reset_history import verify_private

    source_old, _, _, preserved = archived
    app = (tmp_path / "app").resolve()
    database = app / "data/sqlite/cryptowatcher.sqlite3"
    database.parent.mkdir(parents=True)
    source_old.rename(database)
    (app / ".deploy.lock").touch()
    (app / ".env").write_bytes(b"retained private configuration")
    helper = app / "reset_history.py"
    helper.write_bytes(b"reviewed helper fixture")
    revision, image = "a" * 40, "sha256:" + "b" * 64
    delivery = app / "releases" / (revision + "-artifact")
    delivery.mkdir(parents=True)
    (delivery / "deployment.json").write_text(json.dumps({"state": "PASS", "image_id": image}))
    keep = database.parent / "backups/keep.sqlite3"
    keep.parent.mkdir()
    keep.write_bytes(b"previous measurement archive")
    monkeypatch.setattr(operation, "APP", app)
    monkeypatch.setattr(operation.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(operation.os, "chown", lambda *args: None, raising=False)
    if os.name == "nt":
        monkeypatch.setattr(operation.os, "fchmod", lambda *args: None, raising=False)
    monkeypatch.setattr(operation.socket, "gethostname", lambda: "vmi3224957")
    monkeypatch.setattr(
        operation.os, "statvfs", lambda path: SimpleNamespace(f_bavail=10**9, f_frsize=4096), raising=False
    )
    monkeypatch.setattr(operation, "require_no_handles", lambda path: None)
    monkeypatch.setattr(operation, "sync_directory", lambda path: None)
    if os.name == "nt":
        monkeypatch.setitem(
            sys.modules, "fcntl", SimpleNamespace(flock=lambda *a: None, LOCK_EX=2, LOCK_NB=4)
        )
    original_stat, original_read = Path.stat, Path.read_text

    class Metadata:
        def __init__(self, path, actual):
            self.path, self.actual = path, actual

        @property
        def st_uid(self):
            return 0 if self.path in (helper, app / ".env") else 10001

        @property
        def st_mode(self):
            if self.path in (helper, app / ".env"):
                return 0o100644 if self.path == helper else 0o100600
            return self.actual.st_mode

        def __getattr__(self, name):
            return getattr(self.actual, name)

    monkeypatch.setattr(Path, "stat", lambda path, *a, **kw: Metadata(path, original_stat(path, *a, **kw)))
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, *a, **kw: (
            "MemAvailable: 10000000 kB\n"
            if path.as_posix() == "/proc/meminfo"
            else original_read(path, *a, **kw)
        ),
    )
    running = True
    actions = []

    def inspect():
        return [
            {
                "Id": "container-id",
                "Image": image,
                "Config": {
                    "Labels": {
                        "org.opencontainers.image.revision": revision,
                        "com.docker.compose.project": "crypto_watcher",
                    }
                },
                "State": {"Running": running, "Health": {"Status": "healthy"}, "StartedAt": "controlled"},
                "Mounts": [{"Source": str(app / "data"), "Destination": "/data"}],
            }
        ]

    def host_path(value):
        return (
            app / value.removeprefix("/data/")
            if not value.startswith("/data/")
            else app / "data" / value.removeprefix("/data/")
        )

    def command(args, **kwargs):
        nonlocal running
        if args[0] == "systemctl":
            return ""
        verb = args[3]
        if verb == "inspect":
            return json.dumps(inspect())
        if verb in ("stop", "start"):
            running = verb == "start"
            actions.append(verb)
            return "container-id"
        assert verb == "run"
        words = args[args.index("-B") + 1 :]
        if "--discard-test-history" in words:

            def arg(name):
                return words[words.index(name) + 1]

            proof = prepare(
                host_path(arg("--source")),
                host_path(arg("--candidate")),
                host_path(arg("--private-recovery")),
                OWNER,
                expected_generation=arg("--expected-generation"),
                discard_test_history=True,
            )
            host_path(arg("--receipt")).write_text(json.dumps(proof))
            return json.dumps(proof)
        if "private_sha256" in words[1]:
            with closing(Store(database, OWNER, readonly=True)) as current:
                return json.dumps(
                    {
                        "private_sha256": signature(private_state(current)),
                        "generation": current.get_runtime("history_generation"),
                    }
                )
        assert "verify_private" in words[1]
        assert list(database.parent.glob("fresh-measurement-*/previous-working/cryptowatcher.sqlite3"))
        recovery = next(database.parent.glob("fresh-measurement-*/private.json"))
        verified = verify_private(database, recovery, OWNER)
        return json.dumps(verified if postcheck_ok else {"state": "FAIL"})

    monkeypatch.setattr(operation.subprocess, "check_output", command)
    options = dict(
        approved=True,
        discard_test_history=True,
        reset_helper=helper,
        reset_helper_sha256=operation.fingerprint(helper),
    )
    if postcheck_ok:
        result = operation.execute(revision, GENERATION, **options)
        assert result["state"] == "PASS" and result["old_working_files_removed"] >= 1
        assert result["all_original_data_retained"] is False
        assert result["archive_backup"]["old_history_integrity"] == "NOT_CLAIMED"
        assert not list(database.parent.glob("fresh-measurement-*/previous-working/cryptowatcher.sqlite3"))
    else:
        with pytest.raises(RuntimeError, match="Private readback failed"):
            operation.execute(revision, GENERATION, **options)
        assert list(database.parent.glob("fresh-measurement-*/previous-working/cryptowatcher.sqlite3"))
        receipt = json.loads(next(database.parent.glob("fresh-measurement-*/operation.json")).read_text())
        assert receipt["state"] == "FAILED" and receipt["promoted"] is True
    with closing(Store(database, OWNER, readonly=True)) as current:
        assert private_state(current) == preserved
    assert keep.read_bytes() == b"previous measurement archive"
    assert (app / ".env").read_bytes() == b"retained private configuration"
    assert actions == ["stop", "start"]


def test_original_files_and_unrelated_backups_survive_archive_cutover(tmp_path, monkeypatch):
    source = (tmp_path / "cryptowatcher.sqlite3").resolve()
    archive = (tmp_path / "original").resolve()
    archive.mkdir()
    expected = {"": b"database exact bytes", "-wal": b"wal exact bytes", "-shm": b"shm exact bytes"}
    for suffix, raw in expected.items():
        source.with_name(source.name + suffix).write_bytes(raw)
    untouched = tmp_path / "older-backup.sqlite3"
    untouched.write_bytes(b"previous data")
    if os.name == "nt":
        monkeypatch.setattr(operation, "sync_directory", lambda path: None)
    proof = operation.preserve_original(source, archive)
    assert len(proof) == 3 and not source.exists()
    for suffix, raw in expected.items():
        assert (archive / (source.name + suffix)).read_bytes() == raw
    assert untouched.read_bytes() == b"previous data"


def test_archive_preflight_rejects_existing_target_before_any_move(tmp_path):
    source = (tmp_path / "cryptowatcher.sqlite3").resolve()
    source.write_bytes(b"database")
    wal = source.with_name(source.name + "-wal")
    wal.write_bytes(b"wal")
    archive = (tmp_path / "original").resolve()
    archive.mkdir()
    (archive / source.name).write_bytes(b"existing")
    with pytest.raises(RuntimeError, match="already exists"):
        operation.preserve_original(source, archive)
    assert source.read_bytes() == b"database" and wal.read_bytes() == b"wal"
    assert (archive / source.name).read_bytes() == b"existing"
    assert not (archive / wal.name).exists()


def test_fresh_operator_requires_explicit_approval_before_host_or_data_access():
    with pytest.raises(RuntimeError, match="approve-fresh-database"):
        operation.execute("a" * 40, "b" * 32, approved=False)


def test_stopped_original_can_be_qualified_and_retained_without_second_full_copy(
    tmp_path, archived, monkeypatch
):
    old, raw, digest, preserved = archived
    source = (tmp_path / "cryptowatcher.sqlite3").resolve()
    old.rename(source)
    with validated_disk(source, deadline=time.monotonic() + 10) as db:
        assert next(iter(db.execute("PRAGMA user_version")))[0] == 2
    proof = operation.original_archive_proof(source, {"status": "integrity_ok", "schema": 2})
    assert proof["sha256"] == digest and proof["method"] == "RETAIN_ORIGINAL"
    fresh = (tmp_path / "fresh.sqlite3").resolve()
    prepare(
        source,
        fresh,
        (tmp_path / "private.json").resolve(),
        OWNER,
        expected_generation=GENERATION,
        preserved_archive_sha256=proof["sha256"],
    )
    archive = (tmp_path / "previous-working").resolve()
    archive.mkdir()
    if os.name == "nt":
        monkeypatch.setattr(operation, "sync_directory", lambda path: None)
    operation.preserve_original(source, archive)
    fresh.rename(source)
    assert (archive / source.name).read_bytes() == raw
    assert not (tmp_path / "archive.sqlite3").exists()
    with closing(Store(source, OWNER, readonly=True)) as current:
        assert signature(private_state(current)) == signature(preserved)
        assert current.get_runtime("market-old") is None
    with closing(Store(archive / source.name, OWNER, readonly=True)) as previous:
        assert previous.get_runtime("market-old") is not None


def test_original_archive_never_qualifies_a_failed_integrity_result(tmp_path, archived):
    source, raw, _, _ = archived
    with pytest.raises(RuntimeError, match="full integrity failed"):
        operation.original_archive_proof(source, {"status": "FAILED", "schema": 2})
    assert source.read_bytes() == raw


def test_offline_integrity_binds_owner_when_runtime_env_file_omits_it(archived, monkeypatch, capsys):
    from cryptowatcher import __main__ as cli

    source, raw, _, _ = archived
    monkeypatch.delenv("OWNER_USER_ID", raising=False)
    monkeypatch.setattr(cli, "load_dotenv", lambda **kwargs: None)
    command = operation.offline_command(
        "qualified-image", str(source), ["-m", "cryptowatcher", "state-check"]
    )
    for index, argument in enumerate(command):
        if argument == "-e":
            key, value = command[index + 1].split("=", 1)
            monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "argv", ["cryptowatcher", *command[command.index("cryptowatcher") + 1 :]])
    assert cli.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "integrity_ok" and result["owner_id"] == OWNER
    assert source.read_bytes() == raw


def test_failed_archive_records_safe_code_without_command_or_stderr_secrets():
    exc = subprocess.CalledProcessError(
        2,
        ["command", "secret-token"],
        stderr=json.dumps({"failure_code": "IO_ERROR", "reason": "secret-token"}),
    )
    assert operation.failure_details(exc) == {
        "failure": "CalledProcessError",
        "returncode": 2,
        "failure_code": "IO_ERROR",
    }
    assert "secret-token" not in json.dumps(operation.failure_details(exc))
    exc.stderr = "unstructured secret-token"
    assert operation.failure_details(exc) == {"failure": "CalledProcessError", "returncode": 2}
