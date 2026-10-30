"""Full no-CD entrypoint safety and actual result accounting, without heavy runs."""

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from scripts import full_validation as full

SHA = "a" * 40


def ready(tmp_path, monkeypatch):
    monkeypatch.setattr(full, "ROOT", tmp_path)
    monkeypatch.setattr(full.sys, "platform", "linux")
    monkeypatch.setattr(full.sys, "version_info", (3, 11))
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setattr(full.shutil, "which", lambda tool: "/local/" + tool)
    monkeypatch.setattr(full, "git", lambda *args: "" if args[0] == "status" else SHA)
    monkeypatch.setattr(
        full.subprocess, "check_output", Mock(side_effect=["unix:///var/run/docker.sock", "linux"])
    )
    return tmp_path / ".cache-g1/full-proof"


@pytest.mark.parametrize("failure", ["sha", "head", "main", "dirty", "platform", "remote", "remote_context"])
def test_full_profile_rejects_unqualified_source_platform_and_remote_docker_without_writes(
    tmp_path, monkeypatch, failure
):
    directory = ready(tmp_path, monkeypatch)
    revision = SHA
    if failure == "sha":
        revision = "main"
    if failure == "head":
        monkeypatch.setattr(full, "git", lambda *a: "b" * 40)
    if failure == "main":
        monkeypatch.setattr(full, "git", lambda *a: "b" * 40 if a[-1] == "refs/remotes/origin/main" else SHA)
    if failure == "dirty":
        monkeypatch.setattr(full, "git", lambda *a: " M tracked.py" if a[0] == "status" else SHA)
    if failure == "platform":
        monkeypatch.setattr(full.sys, "platform", "win32")
    if failure == "remote":
        monkeypatch.setenv("DOCKER_HOST", "ssh://shared-vps")
    if failure == "remote_context":
        monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
        monkeypatch.setenv("DOCKER_CONTEXT", "remote")
        monkeypatch.setattr(full.subprocess, "check_output", Mock(return_value="ssh://shared-vps"))
    with pytest.raises(ValueError):
        full.preflight(revision, directory)
    assert not directory.exists()


@pytest.mark.parametrize("existing", [False, True])
def test_full_profile_never_reuses_reports_or_writes_outside_owned_cache(tmp_path, monkeypatch, existing):
    ready(tmp_path, monkeypatch)
    directory = tmp_path / (".cache-g1/existing" if existing else "outside")
    if existing:
        directory.mkdir(parents=True)
        (directory / "preserved").write_text("unknown user data")
    with pytest.raises(ValueError, match="New report"):
        full.preflight(SHA, directory)
    if existing:
        assert (directory / "preserved").read_text() == "unknown user data"
    else:
        assert not directory.exists()


def test_full_inventory_and_manual_workflow_have_no_delivery_schedule_or_credentials():
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github/workflows/full-validation.yml").read_text()
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert "secrets." not in text and "environment:" not in text
    assert "schedule:" not in text and "release.yml" not in text
    assert "scripts/checks.py full" in text and '"$EXPECTED_REVISION"' in text
    commands = full.commands(SHA, root / ".cache-g1/fixture", "cryptowatcher-full:owned", "123")
    assert [name for name, _ in commands] == [name for name, _ in full.INVENTORY]
    assert not any(
        "deliver" in word or "push" == word or "ssh" == word for _, cmd in commands for word in cmd
    )
    assert any("scripts/image_checks.py" in cmd for _, cmd in commands)
    assert any("scripts/qualify_numeric_boundary.py" in cmd for _, cmd in commands)


@pytest.mark.parametrize("mode", ["native_failure", "missing_junit", "success_with_skip"])
def test_full_result_stops_on_failure_records_skips_and_only_removes_created_package_files(
    tmp_path, monkeypatch, mode
):
    directory = tmp_path / ".cache-g1/full-proof"
    monkeypatch.setattr(full, "ROOT", tmp_path)
    monkeypatch.setattr(full, "preflight", lambda *a: {"revision": SHA})
    monkeypatch.setattr(full, "git", lambda *a: "" if a[0] == "status" else SHA)
    observed = []

    def run(command, **kwargs):
        observed.append(command)
        if "test" in command and mode == "success_with_skip":
            (directory / "pytest.xml").write_text(
                '<testsuites><testsuite><testcase name="works"/><testcase name="platform"><skipped message="explicit platform limitation"/></testcase></testsuite></testsuites>'
            )
        if "scripts/package_release.py" in command:
            package = directory / "package"
            package.mkdir()
            for name in ("image.tar.gz", "bundle.tar", "manifest.json"):
                (package / name).write_text("owned fixture")
        return subprocess.CompletedProcess(command, 7 if mode == "native_failure" else 0)

    monkeypatch.setattr(full.subprocess, "run", run)
    result = full.execute(SHA, directory)
    record = json.loads((directory / "result.json").read_text())
    if mode == "success_with_skip":
        assert result == 0 and record["state"] == "PASS"
        assert record["pytest"]["tests"] == 2 and len(record["pytest"]["skipped"]) == 1
        assert (directory / "package/manifest.json").exists()
        assert not (directory / "package/bundle.tar").exists()
        assert len(observed) == len(full.INVENTORY)
    else:
        assert result != 0 and record["state"] == "FAIL"
        assert len(observed) == (1 if mode == "native_failure" else 3)
        assert not (directory / "package").exists()
