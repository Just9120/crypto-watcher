import json
import sys
import time

import pytest

from cryptowatcher.__main__ import main
from cryptowatcher.storage import IntegrityError


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


def test_export_cli_is_readonly_filtered_and_never_overwrites(
    monkeypatch, infrastructure, store, capsys, tmp_path
):
    from .test_export import NOW, populate

    populate(store, 3)
    assert cli(monkeypatch, infrastructure, "export", "--market", "spot", "--detector", "price") == 0
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert output[0]["selected_events"] == 3 and output[-1]["type"] == "complete"
    destination = tmp_path / "summary.csv"
    assert (
        cli(monkeypatch, infrastructure, "export", "--format", "csv", "--destination", str(destination)) == 0
    )
    original = destination.read_bytes()
    assert b"sample_size" in original
    assert (
        cli(monkeypatch, infrastructure, "export", "--format", "csv", "--destination", str(destination)) == 2
    )
    assert destination.read_bytes() == original
    assert cli(monkeypatch, infrastructure, "context", "--event-id", "unknown", "--cutoff-ms", str(NOW)) == 0
    assert json.loads(capsys.readouterr().out)["event"] is None


def test_export_cli_rejects_invalid_period_and_required_causal_cutoff(
    monkeypatch, infrastructure, store, capsys
):
    assert cli(monkeypatch, infrastructure, "export", "--start-ms", "20", "--end-ms", "10") == 2
    assert capsys.readouterr().out == ""
    assert cli(monkeypatch, infrastructure, "context", "--event-id", "unknown") == 2
    assert "cutoff-ms" in capsys.readouterr().err


@pytest.mark.parametrize("format_name", ["csv", "ndjson"])
def test_export_is_private_at_creation_and_refuses_links(
    monkeypatch, infrastructure, store, tmp_path, format_name
):
    import os
    import stat

    import cryptowatcher.__main__ as command

    destination = tmp_path / ("private." + format_name)
    original_open = os.open
    calls = []

    def inspect_creation(path, flags, mode=0o777, **kwargs):
        descriptor = original_open(path, flags, mode, **kwargs)
        if path == destination:
            calls.append((flags, mode))
            assert flags & os.O_EXCL and flags & os.O_CREAT
            assert mode == 0o600
            if os.name != "nt":
                assert stat.S_IMODE(os.fstat(descriptor).st_mode) == 0o600
            assert os.fstat(descriptor).st_size == 0
        return descriptor

    monkeypatch.setattr(command.os, "open", inspect_creation)
    previous = os.umask(0)
    try:
        assert (
            cli(
                monkeypatch,
                infrastructure,
                "export",
                "--format",
                format_name,
                "--destination",
                str(destination),
            )
            == 0
        )
    finally:
        os.umask(previous)
    assert len(calls) == 1
    original = destination.read_bytes()
    assert (
        cli(monkeypatch, infrastructure, "export", "--format", format_name, "--destination", str(destination))
        == 2
    )
    assert destination.read_bytes() == original
    if os.name != "nt":
        link = tmp_path / "dangling"
        link.symlink_to(tmp_path / "absent")
        assert cli(monkeypatch, infrastructure, "export", "--destination", str(link)) == 2
        assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize(
    ("code", "exit_status"),
    [
        ("TIME_BUDGET_EXCEEDED", 3),
        ("DATABASE_CORRUPT", 4),
        ("INTERRUPTED", 5),
        ("IO_ERROR", 6),
        ("DATABASE_ERROR", 7),
    ],
)
def test_integrity_failure_cli_has_safe_distinct_code(
    monkeypatch, infrastructure, store, capsys, code, exit_status
):
    def fail(*args, **kwargs):
        raise IntegrityError(code, "Safe integrity failure")

    monkeypatch.setattr("cryptowatcher.disk_validation.validated_disk", fail)
    assert cli(monkeypatch, infrastructure, "state-check") == exit_status
    assert json.loads(capsys.readouterr().err) == {
        "status": "blocked",
        "reason": "Safe integrity failure",
        "failure_code": code,
    }
