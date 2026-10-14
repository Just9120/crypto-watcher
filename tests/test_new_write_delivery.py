import json
from subprocess import CompletedProcess
from unittest.mock import Mock

import pytest

from cryptowatcher.events import History
from cryptowatcher.new_write_check import probe
from cryptowatcher.storage import StateError, encode
from scripts import deploy_receiver as receiver
from scripts.deliver_release import delivery_result

from .test_delivery_capture import DIGEST, IMAGE, REVISION, RUN, success
from .test_history_codec import large_record


def seed(store):
    history = History(store)
    with store.db:
        store.db.execute("INSERT INTO legacy_import VALUES (?,?,?)", ("f" * 64, "{}", "legacy.json"))
        for kind in ("event", "outcome", "episode", "comparison"):
            value = large_record(kind)
            history.put(f"{kind}:one:0000000001", value)
            history.put(kind + "-head:one", value)
        history.put("history:1800000000000000:one", {"event_id": "one"})


def test_bounded_probe_preserves_source_and_restores_configuration(store, tmp_path):
    seed(store)
    before = list(store.db.execute("SELECT key,payload_json FROM runtime ORDER BY key"))
    destination = tmp_path / "settings.json"
    result = probe(store.path, tmp_path, destination=destination)
    assert result["fixture_write_restart"] == "PASS"
    assert result["source_readonly"] is True
    assert result["full_history_backup"] == "FULL_HISTORY_BACKUP_NOT_RUN_OWNER_EXCEPTION"
    assert json.loads(destination.read_text())["tables"]["owner_settings"][0][0] == store.owner_id
    assert list(store.db.execute("SELECT key,payload_json FROM runtime ORDER BY key")) == before
    with pytest.raises(StateError, match="destination"):
        probe(store.path, tmp_path, destination=destination)


def test_post_probe_rejects_full_head_after_switch_and_accepts_compact(store, tmp_path):
    seed(store)
    with pytest.raises(StateError, match="duplicates"):
        probe(store.path, tmp_path, after_ms=1)
    history = History(store)
    value = large_record("event", revision=2)
    with store.db:
        history.version("event", value)
    result = probe(store.path, tmp_path, after_ms=1)
    assert result["new_write_status"] == "PASS" and result["recent_compact_events"] == 1


@pytest.mark.parametrize("pr", [31, 33, 34])
def test_permit_is_root_scoped_tree_bound_and_does_not_change_default(tmp_path, monkeypatch, pr):
    permit_path = tmp_path / "permit.json"
    target = tmp_path / "target.json"
    target.write_text("{}")
    monkeypatch.setattr(receiver, "NEW_WRITE_PERMIT", permit_path)
    monkeypatch.setattr(receiver, "TARGET", target)
    assert receiver.new_write_permit(REVISION) is None
    baseline = {
        33: "047c66e5bb9ef5b9c0650ef91d2e5b92275072c4",
        34: "ef5daeb08a253c4062beed375f90f3b6628843a2",
    }.get(pr, REVISION)
    permit = {
        "schema": receiver.NEW_WRITE_POLICY,
        "state": "ARMED",
        "pr": pr,
        "tree_sha": "a" * 40,
        "target_sha256": receiver.fingerprint(target),
        "previous_container": "same",
        "previous_image": IMAGE,
        "previous_revision": baseline,
    }
    permit_path.write_text(json.dumps(permit, sort_keys=True))
    permit_path.chmod(0o600)
    original = type(permit_path).stat

    def root_stat(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if path == permit_path:

            class Metadata:
                st_uid = 0
                st_mode = 0o600
                st_size = value.st_size

            return Metadata()
        return value

    monkeypatch.setattr(type(permit_path), "stat", root_stat)
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = json.dumps({"tree": {"sha": "b" * 40}}).encode()
    monkeypatch.setattr(receiver.urllib.request, "urlopen", lambda *a, **k: response)
    assert receiver.new_write_permit(REVISION) is None
    response.read.return_value = json.dumps({"tree": {"sha": "a" * 40}}).encode()
    live = {
        "Id": "same",
        "Image": IMAGE,
        "State": {"Running": True},
        "Config": {"Labels": {"org.opencontainers.image.revision": permit["previous_revision"]}},
    }
    monkeypatch.setattr(receiver, "run", lambda *a, **k: json.dumps([live]))
    assert receiver.new_write_permit(REVISION) == permit
    if pr in (33, 34):
        permit["previous_revision"] = REVISION
        permit_path.write_text(json.dumps(permit))
        with pytest.raises(receiver.DeploymentError, match="compatibility baseline"):
            receiver.new_write_permit(REVISION)
        permit["previous_revision"] = baseline
    permit["pr"] = 35
    permit_path.write_text(json.dumps(permit))
    with pytest.raises(receiver.DeploymentError, match="Unapproved permit"):
        receiver.new_write_permit(REVISION)
    permit["pr"] = pr
    permit_path.write_text(json.dumps(permit))
    live["Id"] = "other"
    with pytest.raises(receiver.DeploymentError, match="runtime"):
        receiver.new_write_permit(REVISION)
    permit["state"] = "CONSUMED"
    permit_path.write_text(json.dumps(permit))
    assert receiver.new_write_permit(REVISION) is None


def test_bounded_container_cannot_write_production_database():
    args = ["docker", "run", "-v", str(receiver.APP / "data") + ":/data", IMAGE]
    bounded = receiver.bounded_args(args)
    assert str(receiver.APP / "data") + ":/data:ro" in bounded
    assert "512m" in bounded and "1" in bounded and bounded[-1] == IMAGE


@pytest.mark.parametrize("fault", [None, "tree", "readonly", "snapshot", "post"])
def test_capture_accepts_only_explicit_additive_proof(fault):
    proof = {
        "state": "BOUNDED_COMPATIBILITY_PASS",
        "fixture_write_restart": "PASS",
        "source_readonly": True,
        "full_history_integrity": "NOT_RUN_OWNER_EXCEPTION",
        "full_history_backup": "FULL_HISTORY_BACKUP_NOT_RUN_OWNER_EXCEPTION",
        "configuration_sha256": "f" * 64,
    }
    value = success(
        backup_validation={},
        delivery_policy=receiver.NEW_WRITE_POLICY,
        permit_tree="a" * 40,
        bounded_validation=proof.copy(),
        additive_post_checks=proof.copy(),
    )
    if fault == "tree":
        value["permit_tree"] = None
    elif fault == "readonly":
        value["bounded_validation"]["source_readonly"] = False
    elif fault == "snapshot":
        value["bounded_validation"]["configuration_sha256"] = "invalid"
    elif fault == "post":
        value["additive_post_checks"] = {}
    result = CompletedProcess([], 0, stdout=encode(value).encode(), stderr=b"")
    record, status = delivery_result(result, revision=REVISION, digest=DIGEST, build_run=RUN)
    assert status == (0 if fault is None else 1)
    if fault is None:
        assert "backup_validation" not in record and record["bounded_validation"] == proof
