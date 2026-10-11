import hashlib
import json
import os
import zipfile
from types import SimpleNamespace

import pytest

from scripts import qualify_compressed_validation as q


def test_probe_code_syntax_and_writable_negative_control():
    compile(q.CODE, "qualification", "exec")
    assert "ignore_check_constraints=ON" in q.CODE and "DATABASE_CORRUPT" in q.CODE
    assert "module.snapshot" in q.CODE and "EXPECTED_BACKUP_HASH" in q.CODE


@pytest.mark.parametrize("exit_code", [0, 1])
def test_owned_probe_limits_readonly_mounts_cleanup_and_failed_qualification(
    tmp_path, monkeypatch, exit_code
):
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(q.socket, "gethostname", lambda: "vmi3224957")
    module = tmp_path / "module.py"
    module.write_text("# candidate")
    wheel = tmp_path / "package.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("apsw/__init__.cpython-311-x86_64-linux-gnu.so", b"fixture native")
    monkeypatch.setattr(q, "WHEEL_HASH", hashlib.sha256(wheel.read_bytes()).hexdigest())
    backup = tmp_path / "closed.db"
    backup.write_bytes(b"preserved")
    monkeypatch.setattr(q, "BACKUP", backup)
    monkeypatch.setattr(q, "BACKUP_BYTES", backup.stat().st_size)
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

    def inspect(name):
        if name == q.IMAGE:
            return {"Config": {"Labels": {"org.opencontainers.image.revision": q.BASE}}}
        if name.startswith("cw-compressed-"):
            return probe
        return {
            "Id": name + "-old",
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
            probe.update(
                Id="owned-probe",
                Name="/" + name,
                Image=q.IMAGE,
                State={"OOMKilled": False},
                Config={"Labels": {q.MARKER: name}},
            )
            mounts = [args[i + 1] for i, x in enumerate(args) if x == "--mount"]
            assert len(mounts) == 3 and all(x.endswith(",readonly") for x in mounts)
            assert "--env-file" not in args and args[args.index("--network") + 1] == "none"
            assert args[args.index("--user") + 1] == "10001:10001"
            assert (
                args[args.index("--memory") + 1] == args[args.index("--memory-swap") + 1] == str(4 * 1024**3)
            )
            assert kwargs["timeout"] == 930
            return SimpleNamespace(returncode=exit_code)
        if args[:2] == ["docker", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([probe]))
        assert args == ["docker", "rm", "-f", "owned-probe"]
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(q.subprocess, "run", run)
    if exit_code:
        with pytest.raises(AssertionError, match="did not pass"):
            q.qualify(module, hashlib.sha256(module.read_bytes()).hexdigest(), wheel)
    else:
        q.qualify(module, hashlib.sha256(module.read_bytes()).hexdigest(), wheel)
    assert calls[-1] == ["docker", "rm", "-f", "owned-probe"]
    assert backup.read_bytes() == b"preserved" and not list(tmp_path.glob("tmp*"))
