"""Mock host orchestration; real DB/lease tests live in test_shared_activation.

No Docker, systemd, root permissions, live resources or VPS acceptance is claimed.
"""

import copy
import hashlib
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import enable_shared_storage as operator
from scripts import fresh_measurement

SOURCE = "b" * 40
GENERATION = "c" * 32
PRIVATE = "d" * 64
MAIN = "1" * 64
IMAGE = "sha256:" + "2" * 64


@pytest.fixture
def host(tmp_path, monkeypatch):
    app = tmp_path / "app"
    data = app / "data/sqlite"
    data.mkdir(parents=True)
    backups = data / "backups"
    backups.mkdir()
    database = data / "cryptowatcher.sqlite3"
    database.write_bytes(b"retained current generation bytes")
    (data / ".g1-migration.json").write_text(
        json.dumps(dict(schema=1, restore_rehearsal="PASS", legacy_sha256="a" * 64))
    )
    archive = backups / "previous.sqlite3"
    archive.write_bytes(b"retained old archive")
    (app / "rehearsals").mkdir()
    (app / ".deploy.lock").touch()
    (app / ".env").write_text("TEST_ONLY=retained\n")
    release = app / "releases" / (SOURCE + "-bundle")
    release.mkdir(parents=True)
    (release / "deployment.json").write_text(
        json.dumps(dict(state="PASS", source_sha=SOURCE, image_id=IMAGE))
    )
    target = tmp_path / "target.json"
    target.write_text(
        json.dumps(
            dict(
                app_dir=str(app),
                data_dir=str(app / "data"),
                env_file=str(app / ".env"),
                compose_project="crypto_watcher",
                service="crypto_watcher",
                automatic_delivery_enabled=True,
            )
        )
    )
    monkeypatch.setattr(operator, "APP", app)
    monkeypatch.setattr(operator, "TARGET", target)
    monkeypatch.setattr(fresh_measurement, "APP", app)
    monkeypatch.setattr(operator, "host_checks", lambda: None)
    monkeypatch.setattr(
        operator.os,
        "statvfs",
        lambda path: SimpleNamespace(f_bavail=16_777_216, f_frsize=4096),
        raising=False,
    )
    monkeypatch.setattr(
        operator, "root_file", lambda path, mode=None: operator.require(path.is_file(), "Missing mocked file")
    )
    monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=lambda *args: None))
    container = dict(
        Id=MAIN,
        Name="/crypto-watcher",
        Image=IMAGE,
        Config={
            "Labels": {
                "org.opencontainers.image.revision": SOURCE,
                "com.docker.compose.project": "crypto_watcher",
                "com.docker.compose.service": "crypto_watcher",
            }
        },
        State=dict(Running=True, Restarting=False, Health={"Status": "healthy"}, StartedAt="before"),
        Mounts=[dict(Source=str(app / "data"), Destination="/data", RW=True)],
    )
    state = dict(layout=None, failure=None, stop_unknown=False, bad_post_private=False, observer=False)
    state.update(candidate=None, wrong_candidate=False, retain_candidate=False, health_late=False)
    state["disappear_candidate"] = False
    calls = []

    def token():
        return dict(
            phase="READ_ONLY_SNAPSHOT",
            source_revision=SOURCE,
            history_generation={"id": GENERATION},
            configuration_revision=5,
            private_sha256=(
                "0" * 64
                if state["bad_post_private"] and container["State"]["StartedAt"] == "after"
                else PRIVATE
            ),
            layout=state["layout"],
        )

    def run(args, **kwargs):
        calls.append(list(args))
        if args[0] == "systemctl":
            return SimpleNamespace(
                returncode=0, stdout="cryptowatcher-capacity-old.service active" if state["observer"] else ""
            )
        assert args[:3] == operator.DOCKER
        command = args[3]
        if command == "inspect":
            if args[4] in ("crypto-watcher", MAIN):
                output = json.dumps([copy.deepcopy(container)])
            else:
                assert state["candidate"] is not None and args[4] == state["candidate"]["Id"]
                if state["disappear_candidate"]:
                    state["candidate"] = None
                    return SimpleNamespace(returncode=1, stdout="")
                output = json.dumps([state["candidate"]])
        elif command == "exec":
            output = json.dumps(
                token() if args[-1] == "shared-storage-status" else dict(state="TEST_ONLY_CAPACITY")
            )
        elif command == "stop":
            if args[-1] == MAIN:
                container["State"]["Running"] = False
                if state["stop_unknown"]:
                    raise subprocess.TimeoutExpired(args, 45)
                output = MAIN
            else:
                assert state["candidate"] is not None and args[-1] == state["candidate"]["Id"]
                output = args[-1]
                state["candidate"] = None
        elif command == "start":
            assert args[-1] == MAIN
            container["State"].update(Running=True, StartedAt="after")
            if state["health_late"]:
                container["State"]["Health"]["Status"] = "unhealthy"
            output = MAIN
        elif command == "ps":
            output = state["candidate"]["Id"] if state["candidate"] else ""
        elif command == "run":
            assert not container["State"]["Running"]
            assert (
                "--memory=4g" in args
                and "--memory-swap=4g" in args
                and "--cpus=1" in args
                and "--network=none" in args
            )
            assert "--discard-test-history" not in args and "--approve-fresh-database" not in args
            if state["failure"]:
                if state["retain_candidate"]:
                    state["candidate"] = dict(
                        Id="e" * 64,
                        Name="/" + args[args.index("--name") + 1],
                        Image="sha256:" + "f" * 64 if state["wrong_candidate"] else IMAGE,
                        Config={"Labels": {}},
                        State={"Running": True},
                    )
                return SimpleNamespace(returncode=2, stdout="")
            backup = backups / args[args.index("--destination") + 1].rsplit("/", 1)[1]
            backup.write_bytes(database.read_bytes())
            proof = token()
            proof.update(
                phase="COMPLETE",
                backup=dict(phase="COMPLETE", sha256=hashlib.sha256(backup.read_bytes()).hexdigest()),
            )
            state["layout"] = "3"
            output = json.dumps(proof)
        else:
            raise AssertionError("Unexpected external command: " + command)
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(operator.subprocess, "run", run)
    return SimpleNamespace(
        app=app,
        target=target,
        database=database,
        archive=archive,
        state=state,
        calls=calls,
        container=container,
        args=(SOURCE, GENERATION, 5, PRIVATE, operator.fingerprint(target)),
    )


def receipt(host):
    files = list((host.app / "rehearsals").glob("shared-storage-*/operation.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text())


def assert_preserved(host):
    assert host.database.read_bytes() == b"retained current generation bytes"
    assert host.archive.read_bytes() == b"retained old archive"
    assert host.container["Image"] == IMAGE


def test_target_bound_operation_retains_current_data_records_backup_and_starts_same_image(host):
    result = operator.execute(*host.args)
    assert result["state"] == "PASS" and result["phase"] == "COMPLETE"
    assert receipt(host)["all_original_data_retained"]
    assert result["postflight"]["history_generation"]["id"] == GENERATION
    assert host.container["State"]["Running"] and host.container["State"]["StartedAt"] == "after"
    assert sum(call[3] == "stop" for call in host.calls if call[0] == "docker") == 1
    assert sum(call[3] == "start" for call in host.calls if call[0] == "docker") == 1
    assert_preserved(host)


def test_installed_layout_returns_no_change_without_stop_or_another_backup(host):
    host.state["layout"] = "3"
    result = operator.execute(*host.args)
    assert result["state"] == "NO_CHANGE" and result["phase"] == "ALREADY_ENABLED"
    assert not any(call[3] in ("run", "stop", "start") for call in host.calls if call[0] == "docker")
    assert len(list(host.archive.parent.iterdir())) == 1
    assert_preserved(host)


def test_active_observer_blocks_before_any_container_or_data_mutation(host):
    host.state["observer"] = True
    with pytest.raises(RuntimeError, match="Active capacity observer"):
        operator.execute(*host.args)
    assert not any(call[3] in ("run", "stop", "start") for call in host.calls if call[0] == "docker")
    assert_preserved(host)


def test_wrong_target_hash_is_rejected_before_docker(host):
    with pytest.raises(RuntimeError, match="target changed"):
        operator.execute(*host.args[:-1], "0" * 64)
    assert not host.calls
    assert_preserved(host)


def test_backup_failure_confirms_candidate_absence_before_recovering_same_image(host):
    host.state["failure"] = True
    with pytest.raises(RuntimeError, match="command failed"):
        operator.execute(*host.args)
    record = receipt(host)
    assert record["state"] == "FAILED" and record["offline_candidate_stopped_or_absent_confirmed"]
    assert record["same_image_restarted_for_forward_recovery"]
    assert host.container["State"]["Running"]
    assert len(list(host.archive.parent.iterdir())) == 1
    assert_preserved(host)


def test_unknown_stop_outcome_is_read_before_forward_start(host):
    host.state["stop_unknown"] = True
    with pytest.raises(subprocess.TimeoutExpired):
        operator.execute(*host.args)
    record = receipt(host)
    assert record["state"] == "FAILED" and record["same_image_restarted_for_forward_recovery"]
    commands = [call[3] for call in host.calls if call[0] == "docker"]
    assert commands[commands.index("stop") + 1] == "inspect"
    assert "run" not in commands
    assert_preserved(host)


def test_failed_private_postcheck_is_never_reported_as_pass_or_reset(host):
    host.state["bad_post_private"] = True
    with pytest.raises(RuntimeError, match="identity changed"):
        operator.execute(*host.args)
    assert receipt(host)["state"] == "FAILED"
    assert host.container["State"]["Running"]
    assert_preserved(host)


def test_owned_offline_candidate_is_stopped_before_restarting_main(host):
    host.state.update(failure=True, retain_candidate=True)
    with pytest.raises(RuntimeError, match="command failed"):
        operator.execute(*host.args)
    record = receipt(host)
    assert (
        record["offline_candidate_stopped_or_absent_confirmed"]
        and record["same_image_restarted_for_forward_recovery"]
    )
    stops = [call[-1] for call in host.calls if call[0] == "docker" and call[3] == "stop"]
    assert stops == [MAIN, "e" * 64]
    assert host.state["candidate"] is None and host.container["State"]["Running"]
    assert_preserved(host)


def test_foreign_candidate_image_is_preserved_and_main_is_not_started_over_it(host):
    host.state.update(failure=True, retain_candidate=True, wrong_candidate=True)
    with pytest.raises(RuntimeError, match="Offline recovery identity changed"):
        operator.execute(*host.args)
    assert receipt(host)["state"] == "FAILED"
    assert receipt(host)["phase"] == "FAILED_CANDIDATE_INSPECTION"
    assert host.state["candidate"] is not None and not host.container["State"]["Running"]
    assert not any(call[3] == "start" for call in host.calls if call[0] == "docker")
    assert_preserved(host)


def test_health_wait_is_finite_and_does_not_hide_failed_postcheck(host, monkeypatch):
    host.state["health_late"] = True
    clock = SimpleNamespace(now=0.0)

    def sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(
        operator, "time", SimpleNamespace(monotonic=lambda: clock.now, time=lambda: clock.now, sleep=sleep)
    )
    with pytest.raises(RuntimeError, match="health post-check expired"):
        operator.execute(*host.args)
    assert operator.POST_SECONDS <= clock.now < 900
    assert receipt(host)["state"] == "FAILED" and host.container["State"]["Running"]
    assert_preserved(host)


def test_autoremoval_between_inventory_and_inspect_is_confirmed_before_start(host):
    host.state.update(failure=True, retain_candidate=True, disappear_candidate=True)
    with pytest.raises(RuntimeError, match="command failed"):
        operator.execute(*host.args)
    record = receipt(host)
    assert (
        record["offline_candidate_stopped_or_absent_confirmed"]
        and record["same_image_restarted_for_forward_recovery"]
    )
    assert host.state["candidate"] is None and host.container["State"]["Running"]
    assert_preserved(host)


def test_host_binding_refuses_a_different_root_host(monkeypatch):
    monkeypatch.setattr(operator.sys, "platform", "linux")
    monkeypatch.setattr(operator.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(operator.socket, "gethostname", lambda: "different-vps")
    with pytest.raises(RuntimeError, match="Wrong CryptoWatcher host"):
        operator.host_checks()
