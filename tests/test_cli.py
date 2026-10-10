import json
import sys
import time

import pytest

from cryptowatcher.__main__ import main


def cli(monkeypatch, infrastructure, *args):
    monkeypatch.setenv("OWNER_USER_ID", str(infrastructure.owner_id))
    monkeypatch.setenv("DATABASE_FILE", str(infrastructure.database))
    monkeypatch.setattr(sys, "argv", ["cryptowatcher", *args])
    return main()


def test_health_requires_recent_heartbeat_and_state_readback(monkeypatch, infrastructure, store, capsys):
    assert cli(monkeypatch, infrastructure, "health") == 1
    store.put_runtime("heartbeat", {"at": time.time(), "revision": "a" * 40})
    assert cli(monkeypatch, infrastructure, "health") == 0
    store.put_runtime("heartbeat", {"at": time.time() + 300})
    assert cli(monkeypatch, infrastructure, "health") == 1
    assert cli(monkeypatch, infrastructure, "state-check") == 0
    state = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert state["owner_id"] == infrastructure.owner_id
    assert state["configuration_revision"] == 1
    assert state["legacy_sha256"] is None
    assert len(state["settings_sha256"]) == 64


@pytest.mark.parametrize("revision", ["never-show-this-secret", "a" * 40])
def test_version_redacts_invalid_environment(monkeypatch, infrastructure, capsys, revision):
    monkeypatch.setenv("SOURCE_REVISION", revision)
    assert cli(monkeypatch, infrastructure, "--version") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source_revision"] == (revision if len(revision) == 40 else "UNSET")


def test_missing_configuration_fail_closed_without_token_output(monkeypatch, infrastructure, capsys):
    monkeypatch.delenv("EXPECTED_BOT_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "never-show-this-secret")
    assert cli(monkeypatch, infrastructure, "check-config") == 2
    output = capsys.readouterr().err
    assert "never-show" not in output
    assert "Invalid TELEGRAM_BOT_TOKEN" in output
