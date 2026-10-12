import hashlib
import json
import os
import sys
import time
import types
import zipfile
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from scripts import qualify_compressed_validation as q


def test_probe_code_syntax_and_writable_negative_control():
    compile(q.CODE, "qualification", "exec")
    assert "ignore_check_constraints=ON" in q.CODE and "DATABASE_CORRUPT" in q.CODE
    assert "module.snapshot" in q.CODE and "EXPECTED_BACKUP_HASH" in q.CODE


@pytest.mark.parametrize("live_seconds,archive_seconds", [(300, 300), (900, 300), (300, 900)])
def test_independent_stage_deadlines_do_not_refresh_inside_live_copy(
    tmp_path, monkeypatch, live_seconds, archive_seconds
):
    clock = SimpleNamespace(value=0.0)
    monkeypatch.setattr(time, "monotonic", lambda: clock.value)
    fixture = types.ModuleType("qualification_fixture")
    calls = []

    class Refused(Exception):
        def __init__(self, code):
            self.code = code

    def budget(deadline):
        if clock.value >= deadline:
            raise Refused("TIME_BUDGET_EXCEEDED")

    def closed(path, *, deadline, **kwargs):
        if path.name == "negative.sqlite3":
            raise Refused("DATABASE_CORRUPT")
        assert deadline == 885
        clock.value += 600
        budget(deadline)
        calls.append("closed-full-proof")
        return {"sha256": "fixture-hash", "bytes": 5422329856}

    def full_check(db, deadline):
        budget(deadline)
        calls.append("live-full-proof")

    class Connection:
        def execute(self, statement):
            if statement.startswith("SELECT owner_id"):
                value = (233382575, 1)
            elif statement == "PRAGMA cache_size":
                value = (-524288,)
            else:
                value = (1024,) if "page_count" in statement else (4096,)
            return SimpleNamespace(fetchone=lambda: value)

        def status(self, op):
            return (0, 0)

    @contextmanager
    def snapshot(path, directory, *, deadline):
        assert deadline == 1485  # Only the completed prior stage grants a new deadline.
        clock.value += live_seconds
        budget(deadline)
        db = Connection()
        sys.modules["cryptowatcher.compressed_validation"].full_check(db, deadline)
        yield db
        clock.value += 10
        budget(deadline)
        calls.append("live-byte-proof")

    def archived(path, destination, directory, *, deadline):
        assert deadline == 1795  # A fresh fixed deadline only after live proof completes.
        clock.value += archive_seconds
        budget(deadline)
        calls.append("archive-full-and-byte-proof")
        return {
            "archive_encoding": "ZSTD",
            "bytes": 4194304,
            "sha256": "fixture-raw",
            "archive_sha256": "fixture-archive",
        }

    fixture.QualificationError = Refused
    fixture.budget = budget
    fixture.validate_closed = closed
    fixture.full_check = full_check
    fixture.snapshot = snapshot
    fixture.archive_backup = archived
    monkeypatch.setitem(sys.modules, "qualification_fixture", fixture)
    monkeypatch.setitem(
        sys.modules,
        "apsw",
        SimpleNamespace(
            apswversion=lambda: "3.53.4.0",
            sqlitelibversion=lambda: "3.53.4",
            SQLITE_DBSTATUS_CACHE_USED=1,
            SQLITE_DBSTATUS_CACHE_HIT=2,
            SQLITE_DBSTATUS_CACHE_MISS=3,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "resource",
        SimpleNamespace(RUSAGE_SELF=0, getrusage=lambda _: SimpleNamespace(ru_utime=0, ru_stime=0)),
    )
    original_read = q.Path.read_text

    def read(path, *args, **kwargs):
        if path.as_posix() == "/proc/self/io":
            return "read_bytes: 0\n"
        if path.as_posix() == "/sys/fs/cgroup/memory.current":
            return "1024"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(q.Path, "read_text", read)
    code = q.CODE.replace("'/scratch/negative.sqlite3'", repr(str(tmp_path / "negative.sqlite3")))
    code = code.replace("'/scratch'", repr(str(tmp_path)))
    namespace = {
        "MODULE_SOURCE": "from qualification_fixture import *",
        "EXPECTED_BACKUP_HASH": "fixture-hash",
    }
    original = sys.modules.get("cryptowatcher.compressed_validation")
    try:
        if live_seconds == 900 or archive_seconds == 900:
            with pytest.raises(Refused) as failure:
                exec(compile(code, "qualified-stage-flow", "exec"), namespace)
            assert failure.value.code == "TIME_BUDGET_EXCEEDED"
            expected = ["closed-full-proof"]
            if live_seconds != 900:
                expected += ["live-full-proof", "live-byte-proof"]
            assert calls == expected
        else:
            exec(compile(code, "qualified-stage-flow", "exec"), namespace)
            assert clock.value == 1210 > 885
            assert calls == [
                "closed-full-proof",
                "live-full-proof",
                "live-byte-proof",
                "archive-full-and-byte-proof",
            ]
    finally:
        if original is None:
            sys.modules.pop("cryptowatcher.compressed_validation", None)
        else:
            sys.modules["cryptowatcher.compressed_validation"] = original


@pytest.mark.parametrize("exit_code", [0, 1])
@pytest.mark.parametrize("changed", [False, True])
def test_owned_probe_limits_readonly_mounts_cleanup_and_failed_qualification(
    tmp_path, monkeypatch, exit_code, changed, capsys
):
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(q.socket, "gethostname", lambda: "vmi3224957")
    module = tmp_path / "module.py"
    module.write_text("# candidate")
    wheel = tmp_path / "package.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("apsw/__init__.cpython-311-x86_64-linux-gnu.so", b"fixture native")
    monkeypatch.setattr(q, "WHEEL_HASH", hashlib.sha256(wheel.read_bytes()).hexdigest())
    zstd = tmp_path / "zstd.whl"
    with zipfile.ZipFile(zstd, "w") as archive:
        archive.writestr("zstandard/__init__.py", b"#fixture")
        archive.writestr("zstandard/backend_c.cpython-311-x86_64-linux-gnu.so", b"fixture native")
    monkeypatch.setattr(q, "ZSTD_WHEEL_HASH", hashlib.sha256(zstd.read_bytes()).hexdigest())
    backup = tmp_path / "closed.db"
    backup.write_bytes(b"preserved")
    monkeypatch.setattr(q, "BACKUP", backup)
    monkeypatch.setattr(q, "ARCHIVE_BYTES", backup.stat().st_size)
    monkeypatch.setattr(q, "ARCHIVE_HASH", hashlib.sha256(backup.read_bytes()).hexdigest())
    monkeypatch.setattr(os, "chown", lambda *args: None, raising=False)
    monkeypatch.setattr(q.shutil, "disk_usage", lambda *args: SimpleNamespace(free=20 * 1024**3))
    original_read = q.Path.read_text

    def read(path, *args, **kwargs):
        if path.as_posix() == "/proc/meminfo":
            return "MemAvailable: 8388608 kB\n"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(q.Path, "read_text", read)
    original_temp = q.tempfile.TemporaryDirectory
    monkeypatch.setattr(q.tempfile, "TemporaryDirectory", lambda **kwargs: original_temp(dir=tmp_path))
    calls = []
    probe = {}
    read_counts = {}

    def inspect(name):
        if name == q.IMAGE:
            return {"Config": {"Labels": {"org.opencontainers.image.revision": q.BASE}}}
        if name.startswith("cw-compressed-"):
            return probe
        read_counts[name] = read_counts.get(name, 0) + 1
        updated = changed and name == "trading-agent-api" and read_counts[name] > 1
        return {
            "Id": name + ("-new" if updated else "-old"),
            "State": {"Running": True},
            "Config": {
                "Labels": {"org.opencontainers.image.revision": "afd10770aa3a330e6c5a28e3ea85a676060e6c29"}
            },
        }

    monkeypatch.setattr(q, "inspect", inspect)
    monkeypatch.setattr(q.subprocess, "check_output", lambda *args, **kwargs: "crypto-watcher\n")

    def run(args, **kwargs):
        calls.append(args)
        if args[:2] == ["docker", "run"]:
            name = args[args.index("--name") + 1]
            assert name.startswith("cw-compressed-") and "/" not in name
            probe.update(
                Id="owned-probe",
                Name="/" + name,
                Image=q.IMAGE,
                State={"OOMKilled": False},
                Config={"Labels": {q.MARKER: name}},
            )
            mounts = [args[i + 1] for i, x in enumerate(args) if x == "--mount"]
            assert len(mounts) == 4
            readonly = [m for m in mounts if m.endswith(",readonly")]
            assert len(readonly) == 3 and any("dst=/live,readonly" in m for m in readonly)
            writable = [m for m in mounts if not m.endswith(",readonly")]
            assert len(writable) == 1 and writable[0].endswith(",dst=/backup-output")
            assert str(tmp_path) in writable[0]  # Only the owned temporary output can be written.
            assert "--env-file" not in args and args[args.index("--network") + 1] == "none"
            assert "--no-healthcheck" in args
            assert args[args.index("--user") + 1] == "10001:10001"
            assert (
                args[args.index("--memory") + 1] == args[args.index("--memory-swap") + 1] == str(4 * 1024**3)
            )
            assert kwargs["timeout"] == 2700
            return SimpleNamespace(returncode=exit_code)
        if args[:2] == ["docker", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([probe]))
        assert args == ["docker", "rm", "-f", "owned-probe"]
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(q.subprocess, "run", run)
    if exit_code:
        with pytest.raises(AssertionError, match="did not pass"):
            q.qualify(module, hashlib.sha256(module.read_bytes()).hexdigest(), wheel, zstd)
    elif changed:
        with pytest.raises(AssertionError, match="Concurrent live change"):
            q.qualify(module, hashlib.sha256(module.read_bytes()).hexdigest(), wheel, zstd)
    else:
        q.qualify(module, hashlib.sha256(module.read_bytes()).hexdigest(), wheel, zstd)
    assert calls[-1] == ["docker", "rm", "-f", "owned-probe"]
    assert backup.read_bytes() == b"preserved" and not list(tmp_path.glob("tmp*"))
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    readbacks = [r for r in records if r["kind"] == "live_readback"]
    assert len(readbacks) == 3
    assert next(r for r in readbacks if r["unit"] == "trading-agent-api")["container_unchanged"] == (
        not changed
    )
