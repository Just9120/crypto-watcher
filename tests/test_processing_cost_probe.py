"""Diagnostic transparency and complete raw counters; no runtime performance claim."""

import importlib.util
import json
import sqlite3
import threading
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "cost_probe_for_test", Path(__file__).parents[1] / "scripts" / "processing_cost_probe.py"
)
probe_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe_module)


class Library:
    def __init__(self):
        self.role = 0
        self.entered = self.left = 0

    def cw_probe_role(self, role):
        old, self.role = self.role, role
        return old

    def cw_probe_callback_enter(self, kind):
        self.entered += 1
        return 1

    def cw_probe_callback_leave(self):
        self.left += 1


def fake_probe():
    value = SimpleNamespace(lib=Library(), phase=1, tls=threading.local(), threads=[])
    value.thread = MethodType(probe_module.Probe.thread, value)
    return value


@pytest.mark.parametrize("writer", [False, True])
def test_real_sqlite_wrappers_preserve_factory_udf_values_exception_and_rollback(writer, monkeypatch):
    from cryptowatcher.writer_memory import Connection

    probe = fake_probe()
    factory = Connection if writer else sqlite3.Connection
    original = sqlite3.connect
    with probe_module.installed(probe):
        db = sqlite3.connect(":memory:", factory=factory)
        assert isinstance(db, factory)
        db.execute("CREATE TABLE facts(value TEXT)")
        db.create_function("same", 1, lambda value: value, deterministic=True)
        with db:
            db.execute("INSERT INTO facts VALUES(?)", ("сохранить\x00😀",))
        assert db.execute("SELECT same(value) FROM facts").fetchone()[0] == "сохранить\x00😀"
        assert probe.lib.entered == probe.lib.left == 1
        with pytest.raises(RuntimeError), db:
            db.execute("INSERT INTO facts VALUES('rollback')")
            raise RuntimeError("abort")
        assert db.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1
        assert any("rollback" in key for row in probe.threads for key in row["transactions"])
        db.close()
    assert sqlite3.connect is original
    assert probe.lib.role == 0


@pytest.mark.parametrize("empty", [False, True])
def test_group_role_is_restored_on_error_and_no_input_is_replaced(monkeypatch, empty):
    from cryptowatcher.ingestion import BybitRadar

    frame = ({"unchanged": True}, 123)
    frames = [] if empty else [frame]
    allowed = {"unchanged"}

    def original(self, market, received, names):
        assert received is frames and names is allowed and market == "spot"
        raise RuntimeError("same failure")

    monkeypatch.setattr(BybitRadar, "accept_received_group", original)
    probe = fake_probe()
    with probe_module.installed(probe), pytest.raises(RuntimeError, match="same failure"):
        BybitRadar.accept_received_group(object(), "spot", frames, allowed)
    assert probe.lib.role == 0
    assert len(probe.threads[0]["groups"]) == 1
    assert BybitRadar.accept_received_group is original


def test_samples_preserve_cpu_throttling_pressure_and_task_scheduling(tmp_path, monkeypatch):
    group = tmp_path / "cgroup"
    task = tmp_path / "proc" / "task" / "123"
    group.mkdir()
    task.mkdir(parents=True)
    (group / "cpu.stat").write_text("usage_usec 12345\nnr_periods 20\nnr_throttled 3\nthrottled_usec 90000\n")
    for name in ("cpu.pressure", "io.pressure"):
        (group / name).write_text(
            "some avg10=0.00 avg60=0.00 avg300=0.00 total=60000\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=10000\n"
        )
    (group / "io.stat").write_text("259:0 rbytes=1 wbytes=2 rios=3 wios=4\n")
    fields = ["S"] + ["0"] * 40
    fields[11], fields[12], fields[39] = "2", "3", "7"
    (task / "stat").write_text("123 (worker ) label) " + " ".join(fields))
    (task / "schedstat").write_text("1000 2000 3")
    (task / "wchan").write_text("io_schedule")
    (tmp_path / "proc" / "io").write_text("read_bytes: 16\nwrite_bytes: 32\n")

    def mapped(path):
        if str(path) == "/sys/fs/cgroup":
            return group
        if str(path) == "/proc/123/task":
            return task.parent
        if str(path) == "/proc/123/io":
            return tmp_path / "proc" / "io"
        raise AssertionError(path)

    monkeypatch.setattr(probe_module, "Path", mapped)
    sample = probe_module.sample(123)
    assert sample["cpu_stat"]["nr_throttled"] == 3
    assert sample["cpu_stat"]["throttled_usec"] == 90000
    assert sample["cpu_pressure"]["some"]["total"] == 60000
    assert sample["io_pressure"]["full"]["total"] == 10000
    assert sample["threads"][0]["schedstat"] == [1000, 2000, 3]
    assert sample["threads"][0]["blkio_delay_ticks"] == 7
    assert sample["threads"][0]["wchan"] == "io_schedule"
    assert sample["process_io"]["write_bytes"] == 32


def test_reports_are_bounded_and_atomic_without_overwriting_foreign_temporary_file(tmp_path):
    path = tmp_path / "report.json"
    probe_module.write_json(path, {"source": probe_module.RUNTIME, "state": "FAIL"})
    assert json.loads(path.read_text())["state"] == "FAIL"
    with pytest.raises(ValueError):
        probe_module.write_json(path, {"too_large": "x" * probe_module.BOUND})
    foreign = path.with_name(path.name + ".new")
    foreign.write_text("preserve")
    with pytest.raises(FileExistsError):
        probe_module.write_json(path, {"state": "PASS"})
    assert json.loads(path.read_text())["state"] == "FAIL"
    assert foreign.read_text() == "preserve"
