import hashlib
import json
import os
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from cryptowatcher import change_journal, page_delta
from cryptowatcher.maintenance import Unit
from cryptowatcher.storage import Store
from scripts import closed_page_capture as capture
from scripts import page_capture_worker as worker

TARGET = Unit("a" * 64, "sha256:" + "b" * 64, "c" * 40)


def prepared(tmp_path, monkeypatch):
    root = tmp_path / "operation"
    inputs = root / "input"
    inputs.mkdir(parents=True)
    (root / "work").mkdir()
    index = b"a" * 32
    (inputs / "base.index").write_bytes(index)
    record = {
        "schema": "cryptowatcher.page-base.v1",
        "index": "base.index",
        "blocks": 1,
        "index_sha256": hashlib.sha256(index).hexdigest(),
    }
    (inputs / "base.json").write_text(json.dumps(record))
    value = {
        "schema": "cryptowatcher.closed-page-capture.v1",
        "journal_epoch": "d" * 32,
        "baseline_sha256": hashlib.sha256((inputs / "base.json").read_bytes()).hexdigest(),
        "max_archive_bytes": 3 * 1024**3,
    }
    for path in (root / "capture-plan.json", inputs / "capture-plan.json"):
        path.write_text(json.dumps(value))
    old_lstat = Path.lstat

    def metadata(path):
        data = list(old_lstat(path))
        data[0] = 0o100600
        data[4] = 10001 if path.parent == inputs else 0
        return os.stat_result(data)

    monkeypatch.setattr(Path, "lstat", metadata)
    monkeypatch.setattr(capture.common, "private_directory", lambda path, uid: None)
    return root, value


def test_plan_binds_input_bytes_and_explicit_capacity(tmp_path, monkeypatch):
    root, expected = prepared(tmp_path, monkeypatch)
    assert capture.plan(root) == expected
    (root / "input/capture-plan.json").write_text("{}")
    with pytest.raises(RuntimeError, match="differs"):
        capture.plan(root)


@pytest.mark.parametrize("fault", ["index", "extra", "capacity", "descriptor"])
def test_plan_tampering_is_rejected_before_stop(tmp_path, monkeypatch, fault):
    root, value = prepared(tmp_path, monkeypatch)
    if fault == "index":
        (root / "input/base.index").write_bytes(b"b" * 32)
    elif fault == "extra":
        (root / "input/unknown").write_text("unknown")
    elif fault == "descriptor":
        (root / "input/base.json").write_text("{}")
    else:
        value["max_archive_bytes"] += 1
        for path in (root / "capture-plan.json", root / "input/capture-plan.json"):
            path.write_text(json.dumps(value))
    with pytest.raises(RuntimeError):
        capture.plan(root)


@pytest.mark.parametrize("fault", [None, "disk", "memory", "journal", "expiry"])
def test_capture_preflight_keeps_resources_and_new_write_journal_required(tmp_path, monkeypatch, fault):
    root, value = prepared(tmp_path, monkeypatch)
    ops = capture.PageOperations(root, root / "code", "f" * 64, "owned-worker")
    baseline = {"private_sha256": "preserved"}
    monkeypatch.setattr(ops, "verify_binding", lambda expected: baseline)
    monkeypatch.setattr(
        capture.shutil,
        "disk_usage",
        lambda p: SimpleNamespace(free=4 * 1024**3 if fault == "disk" else 8 * 1024**3),
    )
    old_read = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda p, *a, **kw: (
            ("MemAvailable: " + str(4 * 1024**2 if fault == "memory" else 8 * 1024**2) + " kB\n")
            if p.as_posix().endswith("/proc/meminfo")
            else old_read(p, *a, **kw)
        ),
    )
    journal = {
        "epoch": value["journal_epoch"],
        "valid": 0 if fault == "journal" else 1,
        "expires": time.time() + (800 if fault == "expiry" else 3600),
    }
    monkeypatch.setattr(capture.common, "call", lambda *a, **kw: json.dumps(journal))
    if fault:
        with pytest.raises(RuntimeError):
            ops.verify(TARGET)
    else:
        assert ops.verify(TARGET) == baseline
        # Capture admission does not lower the old full-qualification admission.
        with pytest.raises(RuntimeError, match="disk admission"):
            capture.common.DockerOperations.verify(ops, TARGET)


def test_partial_or_falsely_qualified_capture_receipt_is_not_accepted(tmp_path, monkeypatch):
    root, config = prepared(tmp_path, monkeypatch)
    ops = capture.PageOperations(root, root / "code", "f" * 64, "owned-worker")
    target = root / "work/pages/delta.json"
    target.parent.mkdir()
    target.write_text("{}")
    value = {
        "state": "CAPTURED_BYTES_NOT_QUALIFIED",
        "full_source_integrity": "PENDING",
        "journal_epoch": config["journal_epoch"],
        "journal_watermark": 42,
        "descriptor_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    }
    monkeypatch.setattr(ops, "run_worker", lambda *a, **kw: value)
    assert ops.capture(TARGET, 500) == value
    value["state"] = "QUALIFIED_SNAPSHOT_CANDIDATE"
    with pytest.raises(RuntimeError, match="evidence"):
        ops.capture(TARGET, 500)


@pytest.mark.parametrize("fault", [None, "root", "memory", "swap", "cpu"])
def test_worker_kernel_limits_are_required(monkeypatch, fault):
    monkeypatch.setattr(worker.os, "geteuid", lambda: 0 if fault == "root" else 10001, raising=False)
    values = {
        "cpu.max": "200000 100000" if fault == "cpu" else "100000 100000",
        "memory.max": str(8 * 1024**3 if fault == "memory" else 4 * 1024**3),
        "memory.swap.max": "1" if fault == "swap" else "0",
    }
    monkeypatch.setattr(Path, "read_text", lambda p: values[p.name])
    if fault:
        with pytest.raises(RuntimeError):
            worker.resources()
    else:
        worker.resources()


@pytest.mark.parametrize("initial", [False, True], ids=["existing-valid-journal", "uncaptured-overflow"])
def test_worker_roundtrip_preserves_new_rows_and_binds_journal_point(store, tmp_path, monkeypatch, initial):
    with store.db:
        store.db.execute("UPDATE owner_settings SET revision=3")
    baseline = (tmp_path / "old.sqlite3").resolve()
    store.backup(baseline)
    epoch = "e" * 32
    change_journal.install(
        store.db,
        epoch=epoch,
        owner=store.owner_id,
        max_keys=1 if initial else 1000,
        max_bytes=1024**2,
        expires=int(time.time()) + 3600,
    )
    store.put_runtime("new-after-old-snapshot", {"useful": "history"})
    if initial:
        store.put_runtime("during-invalid-journal", {"also": "useful"})
    source = store.path.resolve()
    store.close()
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    inputs = (tmp_path / "input").resolve()
    page_delta.make_index(baseline, inputs, expected_sha256=hashlib.sha256(baseline.read_bytes()).hexdigest())
    value = {
        "schema": "cryptowatcher.closed-page-capture.v1",
        "journal_epoch": epoch,
        "baseline_sha256": hashlib.sha256((inputs / "base.json").read_bytes()).hexdigest(),
        "max_archive_bytes": 1024**2,
    }
    if initial:
        value.update(
            schema="cryptowatcher.closed-page-capture.v2",
            journal_epoch="f" * 32,
            initial_snapshot={"prior_epoch": epoch, "no_prior_capture": True},
        )
    (inputs / "capture-plan.json").write_text(json.dumps(value))
    scratch = (tmp_path / "scratch").resolve()
    scratch.mkdir(mode=0o700)

    def mapped(value):
        if value.startswith(str(source)):
            return Path(value)
        if value == "/history/cryptowatcher.sqlite3":
            return source
        if value.startswith("/input/"):
            return inputs / value.removeprefix("/input/")
        if value.startswith("/scratch/"):
            return scratch / value.removeprefix("/scratch/")
        raise AssertionError("Unexpected worker path")

    monkeypatch.setattr(worker, "Path", mapped)
    # Kernel limit enforcement has its own tests; exercise the real data path here.
    monkeypatch.setattr(worker, "resources", lambda: None)
    if initial:
        rearm = worker.prepare_initial_journal(60)
        assert rearm["archive"]["prior"]["valid"] == 0
        assert rearm["application_rows_deleted"] == 0
        original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    result = worker.run(120)
    assert result["journal_epoch"] == value["journal_epoch"]
    assert result["journal_watermark"] == 0 if initial else result["journal_watermark"] > 0
    assert result["full_source_integrity"] == "PENDING"
    assert result["state"] == "CAPTURED_BYTES_NOT_QUALIFIED"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    work = (tmp_path / "working.sqlite3").resolve()
    work.write_bytes(baseline.read_bytes())
    page_delta.apply_to_working_copy(
        work, scratch / "pages/delta.json", expected_descriptor_sha256=result["descriptor_sha256"]
    )
    with closing(Store(work, store.owner_id, readonly=True)) as restored:
        assert restored.get_runtime("new-after-old-snapshot") == {"useful": "history"}
        assert restored.snapshot().revision == 3
        if initial:
            assert restored.get_runtime("during-invalid-journal") == {"also": "useful"}
    if initial:
        with closing(Store(source, store.owner_id)) as resumed:
            resumed.put_runtime("after-restart", {"new": True})
            with change_journal.changes(
                resumed.db,
                epoch=value["journal_epoch"],
                owner=store.owner_id,
                after=result["journal_watermark"],
                deadline=time.monotonic() + 10,
            ) as (_, rows):
                assert [r["key"] for r in rows] == ["after-restart"]


def test_capture_worker_command_keeps_source_and_inputs_readonly(tmp_path, monkeypatch):
    root, _ = prepared(tmp_path, monkeypatch)
    ops = capture.PageOperations(root, root / "code", "f" * 64, "owned-worker")
    monkeypatch.setattr(capture.common, "exact_unit", lambda expected: {"State": {"Running": False}})
    seen = []

    def run(command, **kwargs):
        seen.append(command)
        (root / "work/capture.json").write_text('{"state":"CAPTURED_BYTES_NOT_QUALIFIED"}')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(capture.common.subprocess, "run", run)
    ops.run_worker(TARGET, 500, result_name="capture.json", mount_input=True)
    command = seen[0]
    assert command[command.index("--memory") + 1] == command[command.index("--memory-swap") + 1] == "4g"
    assert command[command.index("--cpus") + 1] == "1"
    assert command[command.index("--network") + 1] == "none"
    mounts = [command[i + 1] for i, v in enumerate(command) if v == "--mount"]
    assert any(m.endswith(",dst=/history,readonly") for m in mounts)
    assert any(m.endswith(",dst=/input,readonly") for m in mounts)
    assert not any(v in command for v in ("--privileged", "--env-file", "--one-time-maintenance"))


def initial_plan(root, value):
    value.update(
        schema="cryptowatcher.closed-page-capture.v2",
        initial_snapshot={"prior_epoch": "e" * 32, "no_prior_capture": True},
    )
    for target in (root / "capture-plan.json", root / "input/capture-plan.json"):
        target.write_text(json.dumps(value))


@pytest.mark.parametrize("fault", [None, "missing-attestation", "same-epoch"])
def test_initial_plan_is_explicit_and_cannot_reuse_an_epoch(tmp_path, monkeypatch, fault):
    root, value = prepared(tmp_path, monkeypatch)
    initial_plan(root, value)
    if fault:
        if fault == "missing-attestation":
            value["initial_snapshot"]["no_prior_capture"] = False
        else:
            value["initial_snapshot"]["prior_epoch"] = value["journal_epoch"]
        initial = value["initial_snapshot"]
        for target in (root / "capture-plan.json", root / "input/capture-plan.json"):
            target.write_text(json.dumps({**value, "initial_snapshot": initial}))
        with pytest.raises(RuntimeError, match="uncaptured"):
            capture.plan(root)
    else:
        assert capture.plan(root) == value


@pytest.mark.parametrize("nested", [False, True])
def test_recorded_source_point_blocks_rearm_even_when_not_yet_qualified(tmp_path, monkeypatch, nested):
    app = tmp_path.resolve()
    root = app / "rehearsals/closed-page-capture-old"
    (root / "work").mkdir(parents=True)
    monkeypatch.setattr(capture.common, "APP", app)
    proof = {"state": "CAPTURED_BYTES_NOT_QUALIFIED", "journal_epoch": "e" * 32}
    target = root / ("operation.json" if nested else "work/capture.json")
    target.write_text(json.dumps({"capture": proof} if nested else proof))
    with pytest.raises(RuntimeError, match="consumes"):
        capture.uncaptured_epoch("e" * 32)
    capture.uncaptured_epoch("f" * 32)


def test_initial_rearm_and_second_checkpoint_share_the_capture_budget(tmp_path, monkeypatch):
    root, value = prepared(tmp_path, monkeypatch)
    initial_plan(root, value)
    ops = capture.PageOperations(root, root / "code", "f" * 64, "owned-worker")
    monkeypatch.setattr(capture, "uncaptured_epoch", lambda epoch: None)
    now = [100.0]
    monkeypatch.setattr(capture.time, "monotonic", lambda: now[0])
    calls = []
    descriptor = root / "work/pages/delta.json"
    descriptor.parent.mkdir()
    descriptor.write_text("{}")
    captured = {
        "state": "CAPTURED_BYTES_NOT_QUALIFIED",
        "full_source_integrity": "PENDING",
        "journal_epoch": value["journal_epoch"],
        "journal_watermark": 0,
        "descriptor_sha256": hashlib.sha256(descriptor.read_bytes()).hexdigest(),
    }

    def run(expected, seconds, **options):
        calls.append((seconds, options))
        if options.get("prepare_journal"):
            now[0] += 40
            return {
                "state": "JOURNAL_REARMED_BEFORE_INITIAL_SNAPSHOT",
                "epoch": value["journal_epoch"],
                "application_rows_deleted": 0,
                "prior_snapshot_reused": False,
                "archive": {"prior": {"epoch": "e" * 32}},
            }
        return captured

    def checkpoint(expected, seconds):
        assert seconds <= 30
        now[0] += 5

    monkeypatch.setattr(ops, "run_worker", run)
    monkeypatch.setattr(ops, "remove_owned", lambda *args: None)
    monkeypatch.setattr(ops, "checkpoint", checkpoint)
    assert ops.capture(TARGET, 400) == captured
    assert calls[0] == (90, dict(result_name="journal-rearm.json", mount_input=True, prepare_journal=True))
    assert calls[1] == (355, dict(result_name="capture.json", mount_input=True))


@pytest.mark.parametrize("wrong_epoch", [False, True])
def test_initial_preflight_accepts_only_explicit_uncaptured_prior_journal(tmp_path, monkeypatch, wrong_epoch):
    root, value = prepared(tmp_path, monkeypatch)
    initial_plan(root, value)
    ops = capture.PageOperations(root, root / "code", "f" * 64, "owned-worker")
    baseline = {"private": "preserved"}
    monkeypatch.setattr(ops, "verify_binding", lambda target: baseline)
    monkeypatch.setattr(capture, "uncaptured_epoch", lambda epoch: None)
    monkeypatch.setattr(capture.shutil, "disk_usage", lambda path: SimpleNamespace(free=8 * 1024**3))
    old_read = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, *args, **kw: (
            "MemAvailable: 8388608 kB\n"
            if path.as_posix().endswith("/proc/meminfo")
            else old_read(path, *args, **kw)
        ),
    )
    monkeypatch.setattr(
        capture.common,
        "call",
        lambda *args, **kw: json.dumps(
            {
                "epoch": "0" * 32 if wrong_epoch else "e" * 32,
                "valid": 0,
                "expires": 1,
                "reason": "CAPACITY",
                "max_bytes": 128 * 1024**2,
            }
        ),
    )
    if wrong_epoch:
        with pytest.raises(RuntimeError, match="identity"):
            ops.verify(TARGET)
    else:
        assert ops.verify(TARGET) == baseline


def test_only_explicit_journal_step_has_a_writeable_source_mount(tmp_path, monkeypatch):
    root, value = prepared(tmp_path, monkeypatch)
    initial_plan(root, value)
    ops = capture.PageOperations(root, root / "code", "f" * 64, "owned-worker")
    monkeypatch.setattr(capture.common, "exact_unit", lambda target: {"State": {"Running": False}})
    seen = []

    def run(command, **kwargs):
        seen.append(command)
        (root / "work/journal-rearm.json").write_text('{"state":"JOURNAL_REARMED_BEFORE_INITIAL_SNAPSHOT"}')

    monkeypatch.setattr(capture.common.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="Explicit initial"):
        ops.run_worker(TARGET, 60, result_name="journal-rearm.json", mount_input=True)
    ops.run_worker(TARGET, 60, result_name="journal-rearm.json", mount_input=True, prepare_journal=True)
    command = seen[0]
    assert command[-1] == "--rearm-journal"
    assert any(command[i + 1].endswith(",dst=/history") for i, v in enumerate(command) if v == "--mount")
    assert command[command.index("--memory") + 1] == command[command.index("--memory-swap") + 1] == "4g"
    assert command[command.index("--cpus") + 1] == "1"
