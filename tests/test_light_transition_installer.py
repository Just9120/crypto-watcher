"""Root-operator procedure tested only on isolated Linux temporary paths."""

import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import install_history_reset as installer

from .test_light_policy import GENERATION, old_source

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Linux root metadata/flock; never live VPS")


def fixture(tmp_path, monkeypatch):
    source, _, _ = old_source(tmp_path)
    app = tmp_path / "app"
    data = app / "data/sqlite"
    data.mkdir(parents=True)
    (app / "rehearsals").mkdir()
    canonical = data / "cryptowatcher.sqlite3"
    canonical.write_bytes(source.read_bytes())
    lib, etc = tmp_path / "lib", tmp_path / "etc"
    lib.mkdir()
    etc.mkdir()
    paths = {
        "RECEIVER": lib / "deploy_receiver.py",
        "HELPER": lib / "reset_delivery.py",
        "TARGET": etc / "deploy-target.json",
        "PERMIT": etc / "history-reset-permit.json",
    }
    for name, path in paths.items():
        monkeypatch.setattr(installer, name, path)
    paths["RECEIVER"].write_bytes(b"old receiver")
    paths["HELPER"].write_bytes(b"old helper")
    paths["TARGET"].write_bytes(b"exact deployment target")
    paths["PERMIT"].write_text(json.dumps({"schema": installer.POLICY, "state": "CONSUMED"}))
    paths["PERMIT"].chmod(0o600)
    (app / ".env").write_bytes(b"private infrastructure unchanged")
    (app / ".env").chmod(0o600)
    monkeypatch.setattr(installer, "APP", app)
    monkeypatch.setattr(installer, "TARGET_HASH", installer.sha(paths["TARGET"].read_bytes()))
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(installer.socket, "gethostname", lambda: "vmi3224957")
    live = Mock(
        return_value={
            "Id": "existing-container",
            "Image": "existing-image",
            "State": {"Running": True},
            "Config": {
                "Labels": {
                    "org.opencontainers.image.revision": "a" * 40,
                    "com.docker.compose.project": "crypto_watcher",
                    "com.docker.compose.service": "crypto_watcher",
                }
            },
        }
    )
    monkeypatch.setattr(installer, "live", live)
    gate = Mock()
    monkeypatch.setattr(installer, "gate", gate)
    incoming = {
        "deploy_receiver.py": b"pass # reviewed receiver\n",
        "reset_delivery.py": b"pass # reviewed helper\n",
    }

    def read_url(url):
        if "/git/commits/" in url:
            return json.dumps({"tree": {"sha": "c" * 40}}).encode()
        return incoming[url.rsplit("/", 1)[1]]

    monkeypatch.setattr(installer, "read_url", read_url)
    stat = Path.stat

    class Metadata:
        def __init__(self, value):
            self.value = value

        @property
        def st_uid(self):
            return 0  # The operator is root; these are private pytest paths.

        def __getattr__(self, name):
            return getattr(self.value, name)

    monkeypatch.setattr(Path, "stat", lambda p, *a, **k: Metadata(stat(p, *a, **k)))
    args = (
        "d" * 40,
        "b" * 40,
        12,
        installer.sha(incoming["deploy_receiver.py"]),
        installer.sha(incoming["reset_delivery.py"]),
    )
    options = {
        "light_radar_transition": True,
        "expected_generation": GENERATION,
        "expected_runtime_revision": "a" * 40,
        "expected_receiver_sha256": installer.sha(paths["RECEIVER"].read_bytes()),
        "expected_helper_sha256": installer.sha(paths["HELPER"].read_bytes()),
    }
    return app, canonical, paths, incoming, args, options, gate


def test_light_install_preserves_consumed_records_and_never_restarts_or_changes_database(
    tmp_path, monkeypatch
):
    app, database, paths, incoming, args, options, gate = fixture(tmp_path, monkeypatch)
    before = {name: p.read_bytes() for name, p in paths.items()}
    original = database.read_bytes()
    proof = installer.install(*args, **options)
    assert proof["state"] == "PREFLIGHT_PASS"
    assert {name: p.read_bytes() for name, p in paths.items()} == before
    proof = installer.install(*args, execute=True, approved=True, **options)
    assert proof["state"] == "PASS" and proof["permit_state"] == "ARMED"
    assert proof["database_changed"] is proof["runtime_restarted"] is False
    assert database.read_bytes() == original
    assert paths["RECEIVER"].read_bytes() == incoming["deploy_receiver.py"]
    assert paths["HELPER"].read_bytes() == incoming["reset_delivery.py"]
    saved = Path(proof["record_path"]).parent
    assert (saved / "previous_receiver.py").read_bytes() == before["RECEIVER"]
    assert (saved / "previous_helper.py").read_bytes() == before["HELPER"]
    assert (saved / "previous_permit.json").read_bytes() == before["PERMIT"]
    permit = json.loads(paths["PERMIT"].read_bytes())
    assert permit["expected_generation"] == GENERATION and permit["light_radar_transition"] is True
    assert permit["previous_revision"] == "a" * 40
    assert gate.call_count == 3
    assert sorted(p.name for p in app.iterdir()) == [".deploy.lock", ".env", "data", "rehearsals"]


@pytest.mark.parametrize(
    "changed", ["expected_receiver_sha256", "expected_helper_sha256", "expected_generation"]
)
def test_inexact_light_predecessor_refuses_all_installed_file_changes(tmp_path, monkeypatch, changed):
    _, database, paths, _, args, options, _ = fixture(tmp_path, monkeypatch)
    before = {name: p.read_bytes() for name, p in paths.items()}
    original = database.read_bytes()
    options[changed] = "0" * (32 if changed == "expected_generation" else 64)
    with pytest.raises(RuntimeError):
        installer.install(*args, execute=True, approved=True, **options)
    assert {name: p.read_bytes() for name, p in paths.items()} == before
    assert database.read_bytes() == original
