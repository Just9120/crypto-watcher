"""Measurement preserves SQL results, guards and exact version/scenario binding."""

import json
import sqlite3
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from cryptowatcher.writer_memory import Connection
from scripts import qualify_revision_pair as pair


def test_real_sql_wrapper_preserves_commits_rollback_errors_and_ignores_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_REVISION", pair.REVISIONS[0])
    db = sqlite3.connect(tmp_path / "actual.sqlite3", factory=Connection)
    db.execute("CREATE TABLE direct_probe(id INTEGER PRIMARY KEY,value TEXT)")
    original = Connection.execute
    profile = pair.SQLProfile()
    try:
        with ExitStack() as stack:
            profile.install(stack)
            profile.stage({"stage": "SEED_COMPLETE"})
            with db:
                db.execute("INSERT INTO direct_probe VALUES(1,?)", ("kept",))
            with pytest.raises(RuntimeError), db:
                db.execute("UPDATE direct_probe SET value='rolled back'")
                raise RuntimeError("rollback")
            with pytest.raises(sqlite3.IntegrityError), db:
                db.execute("INSERT INTO direct_probe VALUES(1,'duplicate')")
            assert db.execute("SELECT value FROM direct_probe").fetchone()[0] == "kept"
            before = {key: dict(value) for key, value in profile.rows.items()}
            profile.stage({"stage": "STOPPING_FIXTURE_WORKERS"})
            assert db.execute("SELECT COUNT(*) FROM direct_probe").fetchone()[0] == 1
            assert profile.rows == before
        assert Connection.execute is original
        profile.save(tmp_path / "sql-profile.json")
        result = json.loads((tmp_path / "sql-profile.json").read_text())
        assert result["source"] == pair.REVISIONS[0]
        assert result["phases"]["paced:direct_probe:write"]["failures"] == 1
        assert result["phases"]["paced:transaction_boundary"]["calls"] == 3
    finally:
        db.close()


def test_nested_sql_is_exclusive_and_failures_leave_no_active_timer():
    profile = pair.SQLProfile()
    child = profile.wrap(lambda connection, sql: 7)

    def parent(connection, sql):
        return child(connection, "SELECT value FROM direct_probe")

    assert profile.wrap(parent)(None, "SELECT payload FROM runtime") == 7
    assert not profile.local.stack
    assert all(row["cpu_seconds"] >= 0 and row["wall_seconds"] >= 0 for row in profile.rows.values())

    def broken(connection, sql):
        raise ValueError("original")

    with pytest.raises(ValueError, match="original"):
        profile.wrap(broken)(None, "SELECT payload FROM runtime")
    assert not profile.local.stack
    assert profile.rows["seed:runtime:read"]["failures"] == 1


def test_admission_keeps_35_percent_1_cpu_and_all_memory_disk_guards():
    healthy = {"idle_pct": 35, "available_bytes": pair.q.MEMORY + pair.q.RESERVE}
    assert pair.admission([healthy] * 6, 7 * 1024**3, cpu_limit=1)
    assert not pair.admission([healthy] * 5, 7 * 1024**3, cpu_limit=1)
    assert not pair.admission([{**healthy, "idle_pct": 34.9}] * 6, 7 * 1024**3, cpu_limit=1)
    with pytest.raises(ValueError):
        pair.admission([healthy] * 6, 7 * 1024**3, cpu_limit=1.5)


@pytest.mark.parametrize("mismatch", [None, "revision", "measurement", "inputs", "missing_case"])
def test_pair_identity_and_completion_do_not_turn_failed_gates_green(tmp_path, monkeypatch, mismatch):
    measurement = "a" * 40
    monkeypatch.setattr(pair.sys, "platform", "linux")
    monkeypatch.setattr(pair.os, "geteuid", lambda: 0, raising=False)

    def image(*args, **kwargs):
        index = int(args[-1])
        return json.dumps(
            [
                {
                    "Id": str(index),
                    "Config": {
                        "Labels": {
                            "org.opencontainers.image.revision": "b" * 40
                            if mismatch == "revision"
                            else pair.REVISIONS[index],
                            "io.cryptowatcher.measurement-revision": "b" * 40
                            if mismatch == "measurement"
                            else measurement,
                        }
                    },
                }
            ]
        )

    monkeypatch.setattr(pair.q, "docker", image)
    read_text = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, *a, **k: (
            "model name : fixedhost"
            if str(path) == "\\proc\\cpuinfo" or path.as_posix() == "/proc/cpuinfo"
            else read_text(path, *a, **k)
        ),
    )
    monkeypatch.setattr(
        pair,
        "signal",
        SimpleNamespace(SIGALRM=1, SIGINT=2, SIGTERM=3, signal=lambda *a: None, alarm=lambda *a: None),
    )
    monkeypatch.setattr(
        pair.b,
        "Budget",
        lambda: SimpleNamespace(
            reason=lambda: None, admit_second=lambda: True, used=lambda: 200, elapsed=lambda: 360
        ),
    )
    calls = []

    def one(image, measurement, directory, label, cpu, budget, **kwargs):
        calls.append((image, cpu, kwargs["expected_revision"]))
        return {
            "state": "CASE_INCOMPLETE" if mismatch == "missing_case" else "CASE_COMPLETE",
            "fixture_cleanup": "COMPLETE",
            "result": {
                "failures": ["SOURCE_DEADLINE"],
                "result": {
                    "seed_sha256": str(image) if mismatch == "inputs" else "same",
                    "offered_sha256": "same",
                    "streams": 1648,
                    "offered_closed": 2472,
                },
            },
        }

    monkeypatch.setattr(pair.pair, "one", one)
    monkeypatch.setattr(pair.q, "verdict", lambda value: ["SOURCE_DEADLINE"])
    directory = tmp_path / "pair"
    if mismatch in ("revision", "measurement"):
        with pytest.raises(ValueError, match="binding"):
            pair.run(["0", "1"], measurement, directory)
        assert not directory.exists() and not calls
    else:
        result = pair.run(["0", "1"], measurement, directory)
        value = json.loads((directory / "pair.json").read_text())
        assert value["mode"] == "DIAGNOSTIC_NOT_ACCEPTANCE"
        assert value["cases"][0]["result"]["failures"] == ["SOURCE_DEADLINE"]
        assert all(call[1] == 1 for call in calls)
        assert result == (0 if mismatch is None else 1)
        if mismatch is None:
            assert [call[2] for call in calls] == list(pair.REVISIONS)
            assert value["state"] == "DIAGNOSTIC_COMPLETE"


def test_manual_pair_is_separate_from_main_acceptance_and_overlays_only_measurement():
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github/workflows/full-validation.yml").read_text()
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["on"]["workflow_dispatch"]["inputs"]["mode"]["default"] == "full"
    assert "refs/heads/main" in workflow["jobs"]["full"]["if"]
    job = workflow["jobs"]["revision-pair"]
    assert "codex/sqlite-revision-pair-20261103" in job["if"]
    assert "github.run_attempt == 1" in job["if"]
    assert "secrets." not in text and "environment:" not in text and "schedule:" not in text
    assert "600s" in text and "git archive" in text
    copies = [
        line
        for line in (root / "tests/revision_pair.Dockerfile").read_text().splitlines()
        if line.startswith("COPY")
    ]
    assert len(copies) == 1
    assert "cryptowatcher" not in copies[0] and "tests/" not in copies[0]
