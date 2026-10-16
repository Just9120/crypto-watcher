"""Root bootstrap faults on isolated files; Unix metadata, SSH and Docker mocked."""

import base64
import importlib.util
import json
import os
import stat
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "cd_bootstrap", Path(__file__).resolve().parents[1] / "scripts/bootstrap_cd.py"
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
CANDIDATE = "c" * 40
PUBLIC_RAW = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + b"\x01" * 32
PUBLIC = b"ssh-ed25519 " + base64.b64encode(PUBLIC_RAW) + b" fixture\n"
PUBLIC_FP = m.public_identity(PUBLIC)[0]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    incoming, app = tmp_path / "incoming", tmp_path / "app"
    incoming.mkdir()
    (app / "rehearsals").mkdir(parents=True)
    (app / "data/sqlite").mkdir(parents=True)
    (app / ".env").write_text("synthetic env secret")
    (app / ".deploy.lock").write_bytes(b"")
    (app / "data/sqlite/cryptowatcher.sqlite3").write_bytes(b"unchanged state")
    (app / "data/sqlite/.g1-migration.json").write_text(
        json.dumps({"schema": 1, "restore_rehearsal": "PASS", "legacy_sha256": "a" * 64})
    )
    first_path = app / "releases/first-38046940245-20261010T172100Z-775978f9/deployment.json"
    first_path.parent.mkdir(parents=True)
    first = {
        "state": "RUNTIME_PASS_OWNER_SMOKE_PENDING",
        "production_migration": "PASS",
        "source_sha": m.FIRST_SOURCE,
        "build_run": m.FIRST_RUN,
        "bundle_sha256": m.FIRST_BUNDLE,
        "image_id": m.FIRST_OCI,
        "image_config_digest": m.FIRST_CONFIG,
        "image_manifest_digest": m.FIRST_OCI,
        "legacy_sha256": "a" * 64,
        "post_checks": {"health": "PASS", "persistence": "PASS", "source_image": "PASS"},
        "migration_readback": {"all_tables_restore_readback": "PASS"},
    }
    first_path.write_text(json.dumps(first))
    release = app / "releases" / f"{m.SAMPLE_SOURCE}-{m.BUNDLE_HASH[:16]}"
    release.mkdir(parents=True)
    record = {
        "state": "PASS",
        "source_sha": m.SAMPLE_SOURCE,
        "build_run": m.BUILD_RUN,
        "bundle_sha256": m.BUNDLE_HASH,
        "image_id": m.OCI_DIGEST,
        "image_config_digest": m.CONFIG_DIGEST,
        "image_manifest_digest": m.OCI_DIGEST,
    }
    (release / "deployment.json").write_text(json.dumps(record))
    root = tmp_path / "root"
    (root / ".ssh").mkdir(parents=True)
    keys = root / ".ssh/authorized_keys"
    keys.write_bytes(b"existing-key-one\nexisting-key-two")
    usr, etc = tmp_path / "usr/lib", tmp_path / "etc"
    usr.mkdir(parents=True)
    etc.mkdir()
    receiver, target = usr / "cryptowatcher/deploy_receiver.py", etc / "cryptowatcher/deploy-target.json"
    # This is a frozen historical bootstrap, not the routine receiver updater.
    # Model its independently pinned artifact; current receiver needs an adjacent
    # reset helper and must never silently replace this old bootstrap input.
    artifact = b"# isolated reviewed bootstrap receiver fixture\n"
    (incoming / "deploy_receiver.py").write_bytes(artifact)
    monkeypatch.setattr(m, "RECEIVER_HASH", m.sha(artifact))
    pub = PUBLIC
    (incoming / "cryptowatcher-cd.pub").write_bytes(pub)
    host = etc / "host.pub"
    host.write_bytes(pub)
    monkeypatch.setattr(m, "HOST_FP", m.public_identity(pub)[0])
    for field, value in (
        ("APP", app),
        ("RECEIVER", receiver),
        ("TARGET", target),
        ("KEYS", keys),
        ("HOST_PUBLIC", host),
        ("__file__", str(incoming / "bootstrap_cd.py")),
    ):
        monkeypatch.setattr(m, field, value)
    monkeypatch.setattr(m.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(m.socket, "gethostname", lambda: "vmi3224957")
    monkeypatch.setenv("SSH_CONNECTION", "192.0.2.2 1234 192.0.2.1 22")
    real_trusted = m.trusted
    monkeypatch.setattr(
        m,
        "trusted",
        lambda path, *args, **kwargs: real_trusted(root if path == Path("/root") else path, *args, **kwargs),
    )
    original_stat = Path.stat

    def linux_stat(path, **kwargs):
        values = list(original_stat(path, **kwargs))
        values[0] = stat.S_IFMT(values[0]) | (0o700 if stat.S_ISDIR(values[0]) else 0o600)
        values[4] = values[5] = 0
        return os.stat_result(values)

    monkeypatch.setattr(Path, "stat", linux_stat)
    monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace(LOCK_EX=2, flock=lambda *args: None))
    monkeypatch.setattr(m, "main_revision", lambda: CANDIDATE)
    ssh = {
        "authorizedkeysfile": ".ssh/authorized_keys .ssh/authorized_keys2",
        "permitrootlogin": "yes",
        "strictmodes": "yes",
        "pubkeyauthentication": "yes",
        "authenticationmethods": "any",
        "usedns": "no",
        "forcecommand": "none",
        "authorizedkeyscommand": "none",
        "pubkeyacceptedalgorithms": "ssh-ed25519",
    }
    current = {
        "Image": m.OCI_DIGEST,
        "State": {"Running": True, "Health": {"Status": "healthy"}},
        "Config": {
            "User": "10001:10001",
            "Labels": {
                "org.opencontainers.image.revision": m.SAMPLE_SOURCE,
                "io.cryptowatcher.build-run": str(m.BUILD_RUN),
                "com.docker.compose.project": "crypto_watcher",
                "com.docker.compose.service": "crypto_watcher",
            },
        },
        "Mounts": [{"Source": str(app / "data"), "Destination": "/data"}],
    }
    state = {"status": "integrity_ok", "schema": 1, "owner_id": 233382575, "legacy_sha256": "a" * 64}
    calls = []

    def run(args):
        calls.append(args)
        if args[:2] == ["/usr/sbin/sshd", "-T"]:
            return "\n".join(k + " " + v for k, v in ssh.items())
        if args == ["docker", "inspect", "crypto-watcher"]:
            return json.dumps([current])
        if args == ["docker", "exec", "crypto-watcher", "python", "-m", "cryptowatcher", "state-check"]:
            return json.dumps(state)
        raise AssertionError("Unexpected operation")

    monkeypatch.setattr(m, "run", run)
    return SimpleNamespace(
        app=app,
        incoming=incoming,
        keys=keys,
        receiver=receiver,
        target=target,
        host=host,
        record=record,
        release=release,
        current=current,
        state=state,
        ssh=ssh,
        calls=calls,
        first=first,
        first_path=first_path,
    )


def base_args():
    return [
        "--expected-main",
        CANDIDATE,
        "--public-key-file",
        str(Path(m.__file__).parent / "cryptowatcher-cd.pub"),
    ]


def execute_args():
    return [*base_args(), "--execute", "--approve-key", PUBLIC_FP]


def test_preflight_no_production_mutations(setup, capsys):
    before = {str(p): p.read_bytes() for p in setup.app.rglob("*") if p.is_file()}
    keys = setup.keys.read_bytes()
    assert m.main(base_args()) == 0
    assert json.loads(capsys.readouterr().out)["files_changed"] is False
    assert before == {str(p): p.read_bytes() for p in setup.app.rglob("*") if p.is_file()}
    assert setup.keys.read_bytes() == keys and not setup.receiver.exists() and not setup.target.exists()


@pytest.mark.parametrize(
    "args",
    [
        ["--execute"],
        ["--execute", "--approve-key", "wrong"],
    ],
)
def test_missing_gate_blocks_without_mutation(setup, args, capsys):
    keys = setup.keys.read_bytes()
    assert m.main([*base_args(), *args]) == 1
    assert json.loads(capsys.readouterr().out)["state"] == "BLOCKED"
    assert setup.keys.read_bytes() == keys and not setup.receiver.exists()


def test_install_preserves_keys_and_does_not_restart_or_enable_cd(setup, capsys):
    original = setup.keys.read_bytes()
    assert m.main(execute_args()) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["state"] == "PASS" and record["existing_keys_preserved"] is True
    assert (
        record["automatic_cd"] == "NOT_ENABLED"
        and record["runtime_restarted"] is False
        and record["github_changed"] is False
    )
    assert setup.keys.read_bytes().startswith(original)
    assert setup.keys.read_bytes().count(b'restrict,command="/usr/bin/python3') == 1
    assert (
        m.sha(setup.receiver.read_bytes()) == m.RECEIVER_HASH
        and json.loads(setup.target.read_text()) == m.EXPECTED_TARGET
    )
    assert (setup.app / "data/sqlite/cryptowatcher.sqlite3").read_bytes() == b"unchanged state"
    assert not any("restart" in args or "up" in args or "migrate" in args for args in setup.calls)
    assert m.main(execute_args()) == 1
    assert json.loads(capsys.readouterr().out)["state"] == "BLOCKED"
    assert setup.keys.read_bytes().count(b'restrict,command="/usr/bin/python3') == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "FAIL"),
        ("source_sha", "other"),
        ("build_run", 1),
        ("bundle_sha256", "other"),
        ("image_config_digest", "other"),
    ],
)
def test_wrong_hotfix_delivery_blocks(setup, field, value, capsys):
    setup.record[field] = value
    (setup.release / "deployment.json").write_text(json.dumps(setup.record))
    assert m.main(execute_args()) == 1
    assert json.loads(capsys.readouterr().out)["state"] == "BLOCKED" and not setup.receiver.exists()


@pytest.mark.parametrize(
    "field,value", [("status", "corrupt"), ("schema", 2), ("owner_id", 1), ("legacy_sha256", None)]
)
def test_persistent_identity_failure_blocks(setup, field, value, capsys):
    setup.state[field] = value
    assert m.main(execute_args()) == 1
    assert not setup.receiver.exists() and "Persistent" in json.loads(capsys.readouterr().out)["reason"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("forcecommand", "other"),
        ("authorizedkeysfile", "other"),
        ("strictmodes", "no"),
        ("pubkeyauthentication", "no"),
        ("pubkeyacceptedalgorithms", "rsa-only"),
    ],
)
def test_unknown_ssh_config_is_preserved_and_blocks(setup, field, value, capsys):
    setup.ssh[field] = value
    original = setup.keys.read_bytes()
    assert m.main(execute_args()) == 1
    assert setup.keys.read_bytes() == original and not setup.receiver.exists()
    assert json.loads(capsys.readouterr().out)["state"] == "BLOCKED"


@pytest.mark.parametrize("name", ["deploy_receiver.py", "cryptowatcher-cd.pub"])
def test_changed_bootstrap_input_blocks(setup, name, capsys):
    with (setup.incoming / name).open("ab") as out:
        out.write(b"changed")
    assert m.main(execute_args()) == 1
    assert not setup.receiver.exists() and json.loads(capsys.readouterr().out)["state"] == "BLOCKED"


def test_unknown_destination_not_overwritten(setup, capsys):
    setup.receiver.parent.mkdir()
    setup.receiver.write_bytes(b"owner data")
    assert m.main(execute_args()) == 1
    assert (
        setup.receiver.read_bytes() == b"owner data"
        and json.loads(capsys.readouterr().out)["state"] == "BLOCKED"
    )


def test_concurrent_keys_edit_preserved(setup, monkeypatch, capsys):
    original = setup.keys.read_bytes()
    publish = m.publish_new

    def changed(path, content, mode):
        publish(path, content, mode)
        if path == setup.target:
            setup.keys.write_bytes(original + b"\nnew-owner-key\n")

    monkeypatch.setattr(m, "publish_new", changed)
    assert m.main(execute_args()) == 1
    assert (
        setup.keys.read_bytes() == original + b"\nnew-owner-key\n"
        and b"restrict,command=" not in setup.keys.read_bytes()
    )
    assert "partial" in json.loads(capsys.readouterr().out)["reason"].lower()
    record = json.loads(next((setup.app / "rehearsals").rglob("bootstrap.json")).read_text())
    assert record["state"] == "PARTIAL_INSTALLATION_STOPPED"


def test_failure_keeps_access_and_secret_safe_error(setup, monkeypatch, capsys):
    original = setup.keys.read_bytes()
    monkeypatch.setattr(
        m, "publish_new", lambda *args: (_ for _ in ()).throw(RuntimeError("synthetic secret"))
    )
    assert m.main(execute_args()) == 1
    assert setup.keys.read_bytes() == original and not setup.receiver.exists()
    assert "synthetic secret" not in capsys.readouterr().out
    assert "synthetic secret" not in next((setup.app / "rehearsals").rglob("bootstrap.json")).read_text()


def test_host_identity_changed_blocks(setup, capsys):
    raw = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + b"x" * 32
    setup.host.write_bytes(b"ssh-ed25519 " + base64.b64encode(raw) + b" comment\n")
    assert m.main(execute_args()) == 1
    assert (
        not setup.receiver.exists()
        and json.loads(capsys.readouterr().out)["reason"] == "Trusted SSH host identity changed"
    )


def first_runtime(setup):
    setup.current["Image"] = m.FIRST_OCI
    setup.current["Config"]["Labels"]["org.opencontainers.image.revision"] = m.FIRST_SOURCE
    setup.current["Config"]["Labels"]["io.cryptowatcher.build-run"] = str(m.FIRST_RUN)


def test_verified_first_migration_can_bootstrap_without_manual_sample_hotfix(setup, capsys):
    first_runtime(setup)
    # No sample delivery record is needed; it will be deployed through CD.
    (setup.release / "deployment.json").unlink()
    assert m.main(execute_args()) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["state"] == "PASS" and record["runtime"]["source_sha"] == m.FIRST_SOURCE
    assert record["runtime"]["expected_main"] == CANDIDATE and record["runtime_restarted"] is False
    assert (setup.app / "data/sqlite/cryptowatcher.sqlite3").read_bytes() == b"unchanged state"


@pytest.mark.parametrize(
    "field,value",
    [
        ("production_migration", "FAIL"),
        ("state", "FAILED"),
        ("build_run", 1),
        ("image_manifest_digest", "other"),
        ("legacy_sha256", "b" * 64),
    ],
)
def test_failed_first_runtime_cannot_enable_cd(setup, field, value, capsys):
    first_runtime(setup)
    setup.first[field] = value
    setup.first_path.write_text(json.dumps(setup.first))
    assert m.main(execute_args()) == 1
    assert not setup.receiver.exists() and json.loads(capsys.readouterr().out)["state"] == "BLOCKED"


def test_unreviewed_running_image_blocks_even_if_source_label_matches(setup, capsys):
    setup.current["Image"] = "sha256:" + "d" * 64
    assert m.main(execute_args()) == 1
    assert not setup.receiver.exists()
    assert "image differs" in json.loads(capsys.readouterr().out)["reason"]


def test_superseded_main_blocks_before_install(setup, monkeypatch, capsys):
    monkeypatch.setattr(m, "main_revision", lambda: "d" * 40)
    assert m.main(execute_args()) == 1
    assert not setup.receiver.exists()
    assert "superseded" in json.loads(capsys.readouterr().out)["reason"]


def test_missing_first_post_checks_block_even_when_sample_record_passes(setup, capsys):
    setup.first["post_checks"]["persistence"] = "PENDING"
    setup.first_path.write_text(json.dumps(setup.first))
    assert m.main(execute_args()) == 1
    assert not setup.receiver.exists() and json.loads(capsys.readouterr().out)["state"] == "BLOCKED"


def test_unknown_source_not_assumed_to_be_legacy(setup, capsys):
    setup.current["Config"]["Labels"]["org.opencontainers.image.revision"] = "d" * 40
    assert m.main(execute_args()) == 1
    assert not setup.receiver.exists() and "Unreviewed" in json.loads(capsys.readouterr().out)["reason"]


@pytest.mark.parametrize("change", ["stopped", "unhealthy", "other_unit", "root_user", "wrong_mount"])
def test_unusable_or_other_runtime_cannot_authorize_bootstrap(setup, change, capsys):
    if change == "stopped":
        setup.current["State"]["Running"] = False
    elif change == "unhealthy":
        setup.current["State"]["Health"]["Status"] = "unhealthy"
    elif change == "other_unit":
        setup.current["Config"]["Labels"]["com.docker.compose.service"] = "trading_agent"
    elif change == "root_user":
        setup.current["Config"]["User"] = "0:0"
    else:
        setup.current["Mounts"][0]["Source"] = "/opt/other_app/data"
    original = setup.keys.read_bytes()
    assert m.main(execute_args()) == 1
    assert not setup.receiver.exists() and setup.keys.read_bytes() == original
    assert json.loads(capsys.readouterr().out)["state"] == "BLOCKED"


def test_changed_bootstrap_artifact_is_refused_before_any_install(setup, capsys):
    incoming = Path(m.__file__).with_name("deploy_receiver.py")
    incoming.write_bytes(incoming.read_bytes() + b"# unreviewed change\n")
    original = setup.keys.read_bytes()
    assert m.main(execute_args()) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "Bootstrap artifact changed"
    assert setup.keys.read_bytes() == original and not setup.receiver.exists()
