"""Counter comparison mode, reversed order and acceptance separation."""

import json
from pathlib import Path

import pytest

from scripts import qualify_counter_pair as driver
from scripts import qualify_group_pair as group
from tests.test_group_pair import sample

REV = "a" * 40


def configure(monkeypatch):
    monkeypatch.setattr(driver.sys, "platform", "linux")
    monkeypatch.setattr(driver.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(driver.signal, "SIGALRM", 99, raising=False)
    monkeypatch.setattr(driver.signal, "alarm", lambda n: None, raising=False)
    monkeypatch.setattr(driver.signal, "signal", lambda *args: None)
    images = {}
    for label, source, flag in (("baseline", driver.BASELINE, "0"), ("candidate", REV, "1")):
        images[label] = {
            "Id": "sha256:" + label,
            "Config": {
                "Labels": {
                    "org.opencontainers.image.revision": source,
                    "io.cryptowatcher.measurement-revision": REV,
                    "io.cryptowatcher.counter-delta": "temp-sql-ledger-v1",
                },
                "Env": ["CW_COUNTER_DELTA=" + flag],
            },
        }
    monkeypatch.setattr(driver.q, "docker", lambda *args: json.dumps([images[args[-1]]]))

    class Budget:
        def reason(self):
            return None

        def admit_second(self):
            return True

        def used(self):
            return 1

        def elapsed(self):
            return 2

    monkeypatch.setattr(driver.b, "Budget", Budget)
    monkeypatch.setattr(group.q, "verdict", lambda r: r["failures_fixture"])
    return images


def test_reversed_order_remains_baseline_candidate_comparison_and_failure_not_acceptance(
    tmp_path, monkeypatch
):
    configure(monkeypatch)
    order = []

    def one(image, measurement, directory, label, cpu, budget, **options):
        order.append(label)
        assert cpu == 1 and measurement == REV
        assert options["supervisor_module"] == "scripts.qualify_group_pair"
        assert options["expected_revision"] == (driver.BASELINE if label == "baseline" else REV)
        value = sample(("SOURCE_LATE",))
        value["label"] = label
        value["result"]["counter_delta"] = {
            "enabled": label == "candidate",
            "activations": 1 if label == "candidate" else 0,
            "flushes": 10,
            "durable_counter_changes": 10,
            "snapshots": [{"market_counts": [["events", 1, 1, 1, 0]]}],
        }
        return value

    monkeypatch.setattr(driver.pair, "one", one)
    folder = tmp_path / "pair"
    assert driver.run(("baseline", "candidate"), REV, folder, True) == 0
    result = json.loads((folder / "pair.json").read_text())
    assert order == ["candidate", "baseline"]
    assert result["state"] == "COMPARISON_COMPLETE"
    assert not result["candidate_processing_gates"] and not result["acceptance_qualified"]
    assert [c["label"] for c in result["cases"]] == ["baseline", "candidate"]


def test_changed_image_mode_rejects_before_creating_report(tmp_path, monkeypatch):
    images = configure(monkeypatch)
    images["candidate"]["Config"]["Env"] = ["CW_COUNTER_DELTA=0"]
    folder = tmp_path / "reject"
    with pytest.raises(ValueError, match="identity"):
        driver.run(("baseline", "candidate"), REV, folder)
    assert not folder.exists()


def test_unexercised_counter_path_cannot_complete_pair(tmp_path, monkeypatch):
    configure(monkeypatch)

    def one(image, measurement, directory, label, cpu, budget, **options):
        value = sample()
        value["label"] = label
        value["result"]["counter_delta"] = {
            "enabled": label == "candidate",
            "activations": 0,
            "flushes": 0,
            "durable_counter_changes": 0,
        }
        return value

    monkeypatch.setattr(driver.pair, "one", one)
    monkeypatch.setattr(driver.pair, "save", lambda *args: None)
    assert driver.run(("baseline", "candidate"), REV, tmp_path / "unused") != 0
    result = json.loads((tmp_path / "unused/pair.json").read_text())
    assert result["state"] != "COMPARISON_COMPLETE"
    assert result["cases"][-1]["error"] == "COUNTER_MODE_OR_PATH_NOT_EXERCISED"


def test_native_docker_overlays_include_every_new_runtime_input_and_keep_source_frozen():
    import yaml

    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/full-validation.yml").read_text())
    job = workflow["jobs"]["counter-delta-pair"]
    assert "counter-delta-prototype-20261104" in job["if"] and "github.run_attempt == 1" in job["if"]
    text = (root / "tests/counter_pair.Dockerfile").read_text()
    assert "scripts/counter_supervisor.py /app/scripts/qualify_group_pair.py" in text
    assert "CW_COUNTER_DELTA=" in text and "SOURCE_REVISION=" in text
    commands = "\n".join(s.get("run", "") for s in job["steps"])
    assert " --reverse" in commands
    assert commands.count("600s") == 3
    assert "cryptowatcher" in commands and "git" in commands
    assert not any(word in commands for word in ("ssh ", "prune", "workflow run", "gh pr merge"))


def test_mismatched_final_counter_values_cannot_complete(tmp_path, monkeypatch):
    configure(monkeypatch)

    def one(image, measurement, directory, label, cpu, budget, **options):
        value = sample()
        value["label"] = label
        value["result"]["counter_delta"] = {
            "enabled": label == "candidate",
            "activations": 1,
            "flushes": 1,
            "durable_counter_changes": 1,
            "snapshots": [{"count": 1 if label == "baseline" else 2}],
        }
        return value

    monkeypatch.setattr(driver.pair, "one", one)
    folder = tmp_path / "mismatch"
    assert driver.run(("baseline", "candidate"), REV, folder) == 1
    result = json.loads((folder / "pair.json").read_text())
    assert result["state"] == "PAIR_COUNTER_MISMATCH"
    assert not result["same_exact_counter_rows"] and not result["acceptance_qualified"]
