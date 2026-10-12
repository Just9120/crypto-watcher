"""Exact-target, pre-merge compressed validation qualification; live mounts are read-only."""

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

IMAGE = "sha256:fe4ac4a15dbe3077a0247fb35a1ced23057c006fb9cb92cbe878dd80df3a62db"
BASE = "9634558a654d5f13279ff0e76710585bf1105d66"
BACKUP = Path("/opt/crypto_watcher/rehearsals/backup-archive-6crp6k3t/qualified.sqlite3.gz")
BACKUP_BYTES = 5_422_329_856
BACKUP_HASH = "61e4f08763b2db0dc437c5103af94bb3185269b0b731eff719a587c145ec6e22"
ARCHIVE_BYTES = 780_833_702
ARCHIVE_HASH = "d0cc5a880c459cab31d36716fbc25d814d5ad155abb8c64749dbb34c3d63619e"
WHEEL_HASH = "cca0a8a4498a355326fb491d852187c2fd2031d3e61925e349ba297d3ab884f4"
ZSTD_WHEEL_HASH = "9300d02ea7c6506f00e627e287e0492a5eb0371ec1670ae852fefffa6164b072"
LIMIT = 4 * 1024**3
RESERVE = 1536 * 1024**2
MARKER = "cryptowatcher.compressed-validation-qualification"


def emit(kind, **fields):
    print(json.dumps({"kind": kind, "at_utc": datetime.now(timezone.utc).isoformat(), **fields}), flush=True)


def inspect(name):
    value = subprocess.run(["docker", "inspect", name], capture_output=True, text=True, timeout=20)
    assert value.returncode == 0, "Exact Docker object unavailable"
    return json.loads(value.stdout)[0]


CODE = r"""
import gc, hashlib, json, resource, sqlite3, sys, time, types
from pathlib import Path
import apsw,zstandard
assert apsw.apswversion() == '3.53.4.0'
assert zstandard.__version__ == '0.25.0'
module = types.ModuleType('cryptowatcher.compressed_validation')
sys.modules[module.__name__] = module
exec(compile(MODULE_SOURCE, 'qualified_compressed_validation.py', 'exec'), module.__dict__)
started=time.monotonic();stage_started=started;deadline=started+885;last=started;phase='NEGATIVE_CONTROL'
def metrics():
    cpu=resource.getrusage(resource.RUSAGE_SELF)
    io=dict(x.split(':',1) for x in Path('/proc/self/io').read_text().splitlines())
    return {'elapsed_seconds':round(time.monotonic()-started,3),'stage_elapsed_seconds':round(time.monotonic()-stage_started,3),'user_cpu_seconds':round(cpu.ru_utime,3),
        'system_cpu_seconds':round(cpu.ru_stime,3),'physical_read_bytes':int(io['read_bytes']),
        'memory_current_bytes':int(Path('/sys/fs/cgroup/memory.current').read_text())}
def emit(kind, **fields):print(json.dumps({'kind':kind,**metrics(),**fields}),flush=True)
original_budget=module.budget
def monitored(d):
    global last
    original_budget(d)
    if time.monotonic()-last>=30:
        emit('compressed_progress',phase=phase);last=time.monotonic()
module.budget=monitored
def progress(**fields):
    global phase
    phase='CLOSED_'+fields['phase'];monitored(deadline)
negative=Path('/scratch/negative.sqlite3')
db=sqlite3.connect(negative)
db.execute('CREATE TABLE checked(value INTEGER CHECK(value>0))')
db.execute('PRAGMA ignore_check_constraints=ON');db.execute('INSERT INTO checked VALUES(-1)')
db.commit();db.close()
try:module.validate_closed(negative,directory=Path('/scratch'),deadline=deadline)
except module.QualificationError as error:assert error.code=='DATABASE_CORRUPT'
else:raise RuntimeError('Constraint negative control failed')
negative.unlink();emit('compressed_constraint_negative_control',state='PASS')
proof=module.validate_closed(Path('/backup/database.sqlite3.gz'),directory=Path('/scratch'),deadline=deadline,progress=progress,archive_encoding='GZIP')
assert proof['sha256']==EXPECTED_BACKUP_HASH and proof['bytes']==5422329856
emit('compressed_closed_backup',state='PASS',**proof)
gc.collect();phase='LIVE_SNAPSHOT';stage_started=time.monotonic();deadline=stage_started+885
original_check=module.full_check
context='LIVE'
def full_check(db,d):
    global phase
    phase=context+'_FULL_INTEGRITY';original_check(db,d);phase=context+'_BYTE_IDENTITY'
module.full_check=full_check
with module.snapshot(Path('/live/cryptowatcher.sqlite3'),Path('/scratch'),deadline=deadline) as snapshot:
    owner,revision=snapshot.execute('SELECT owner_id,revision FROM owner_settings').fetchone()
    assert owner==233382575 and type(revision) is int and revision>0
    size=snapshot.execute('PRAGMA page_count').fetchone()[0]*snapshot.execute('PRAGMA page_size').fetchone()[0]
emit('compressed_live_snapshot',state='PASS',snapshot_bytes=size,configuration_revision=revision,
    sqlite_version=apsw.sqlitelibversion(),source_mount_readonly=True)
gc.collect();context='ARCHIVE';phase='ARCHIVE_SNAPSHOT';stage_started=time.monotonic();deadline=stage_started+885
archive=Path('/backup-output/qualified.sqlite3.zst')
proof=module.archive_backup(Path('/live/cryptowatcher.sqlite3'),archive,Path('/scratch'),deadline=deadline)
assert proof['archive_encoding']=='ZSTD' and proof['bytes']>=size
emit('compressed_durable_backup',state='PASS',**proof)
gc.collect();monitored(deadline);emit('qualification_complete',state='PASS',production_files_changed=False)
"""


def qualify(module_path, module_hash, wheel, zstd_wheel):
    assert os.geteuid() == 0 and socket.gethostname() == "vmi3224957", "Wrong host/account"
    content = module_path.read_bytes()
    assert hashlib.sha256(content).hexdigest() == module_hash, "Candidate module changed"
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == WHEEL_HASH, "APSW wheel changed"
    assert hashlib.sha256(zstd_wheel.read_bytes()).hexdigest() == ZSTD_WHEEL_HASH, "Zstandard wheel changed"
    assert BACKUP.is_file() and not any(p.is_symlink() for p in (BACKUP, *BACKUP.parents)), (
        "Closed backup changed"
    )
    assert all(not Path(str(BACKUP) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")), (
        "Backup not closed"
    )
    before = BACKUP.stat()
    assert before.st_size == ARCHIVE_BYTES, "Archive size changed"
    archive_hash = hashlib.sha256()
    with BACKUP.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            archive_hash.update(chunk)
    assert archive_hash.hexdigest() == ARCHIVE_HASH, "Archive hash changed"
    names = subprocess.check_output(["docker", "ps", "--format", "{{.Names}}"], text=True).splitlines()
    assert not any(
        n.startswith(("cw-verify-", "cw-ram-", "cw-compressed-", "cw-integrity-", "cw-prefetch-"))
        for n in names
    ), "Concurrent validation"
    baseline = {
        name: inspect(name) for name in ("crypto-watcher", "trading-agent-api", "trading-agent-postgres")
    }
    assert all(c["State"]["Running"] for c in baseline.values()), "Live unit unavailable"
    assert (
        baseline["crypto-watcher"]["Config"]["Labels"]["org.opencontainers.image.revision"]
        == "afd10770aa3a330e6c5a28e3ea85a676060e6c29"
    ), "Live revision changed"
    assert inspect(IMAGE)["Config"]["Labels"]["org.opencontainers.image.revision"] == BASE, (
        "Qualification image changed"
    )
    mem = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    available = int(mem["MemAvailable"].split()[0]) * 1024
    assert available >= LIMIT + RESERVE, "Insufficient memory; production unchanged"
    emit(
        "compressed_preflight",
        memory_available_bytes=available,
        memory_limit_bytes=LIMIT,
        host_reserve_bytes=RESERVE,
        compressed_limit_bytes=3 * 1024**3,
        stage_budget_seconds=885,
        maximum_combined_budget_seconds=2655,
        module_sha256=module_hash,
        source_mount_readonly=True,
        production_files_changed=False,
    )
    name = "cw-compressed-" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="cw-compressed-native-", dir="/tmp") as directory:
        work = Path(directory)
        # The preserved root-owned archive keeps its permissions. Only this
        # temporary encoded copy and isolated output are readable by the validator.
        assert shutil.disk_usage(work).free > ARCHIVE_BYTES + 3 * 1024**3 + 512 * 1024**2, (
            "Insufficient qualification disk"
        )
        backup_copy = work / "source.sqlite3.gz"
        shutil.copyfile(BACKUP, backup_copy)
        os.chown(backup_copy, 10001, 10001)
        backup_copy.chmod(0o600)
        output = work / "backup-output"
        output.mkdir(mode=0o700)
        os.chown(output, 10001, 10001)
        native = Path(directory) / "native"
        package = native / "apsw"
        package.mkdir(parents=True)
        native.chmod(0o755)
        package.chmod(0o755)
        with zipfile.ZipFile(wheel) as archive:
            names = [
                n
                for n in archive.namelist()
                if n.startswith("apsw/__init__.cpython-311-") and n.endswith(".so") and "/" not in n[5:]
            ]
            assert len(names) == 1, "Unexpected wheel layout"
            library = package / Path(names[0]).name
            library.write_bytes(archive.read(names[0]))
            library.chmod(0o644)
        with zipfile.ZipFile(zstd_wheel) as archive:
            for name in ("zstandard/__init__.py", "zstandard/backend_c.cpython-311-x86_64-linux-gnu.so"):
                target = native / name
                target.parent.mkdir(exist_ok=True)
                target.parent.chmod(0o755)
                target.write_bytes(archive.read(name))
                target.chmod(0o644)
        code = (
            "MODULE_SOURCE="
            + repr(content.decode("utf-8"))
            + "\nEXPECTED_BACKUP_HASH="
            + repr(BACKUP_HASH)
            + "\n"
            + CODE
        )
        command = [
            "docker",
            "run",
            "--name",
            name,
            "--label",
            MARKER + "=" + name,
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--no-healthcheck",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "10001:10001",
            "--log-driver",
            "none",
            "--memory",
            str(LIMIT),
            "--memory-swap",
            str(LIMIT),
            "--cpus",
            "1",
            "--pids-limit",
            "64",
            "--ulimit",
            "core=0",
            "--tmpfs",
            "/scratch:rw,noexec,nosuid,nodev,size=64m,mode=0700,uid=10001,gid=10001",
            "--mount",
            "type=bind,src=" + str(native) + ",dst=/native,readonly",
            "--mount",
            "type=bind,src=" + str(backup_copy) + ",dst=/backup/database.sqlite3.gz,readonly",
            "--mount",
            "type=bind,src=" + str(output) + ",dst=/backup-output",
            "--mount",
            "type=bind,src=/opt/crypto_watcher/data/sqlite,dst=/live,readonly",
            "-e",
            "PYTHONPATH=/native",
            "--entrypoint",
            "python",
            "-i",
            IMAGE,
            "-B",
            "-",
        ]
        passed = False
        changed_units = []
        try:
            result = subprocess.run(command, input=code, text=True, timeout=2700)
            process = inspect(name)
            emit(
                "compressed_process",
                exit_code=result.returncode,
                oom_killed=process["State"].get("OOMKilled", False),
            )
            passed = result.returncode == 0 and not process["State"].get("OOMKilled", False)
        finally:
            found = subprocess.run(["docker", "inspect", name], capture_output=True, text=True, timeout=20)
            if found.returncode == 0:
                own = json.loads(found.stdout)[0]
                assert (
                    own["Name"] == "/" + name
                    and own["Image"] == IMAGE
                    and own["Config"]["Labels"].get(MARKER) == name
                ), "Probe identity changed"
                subprocess.run(
                    ["docker", "rm", "-f", own["Id"]], stdout=subprocess.DEVNULL, check=True, timeout=20
                )
                emit("compressed_cleanup", state="REMOVED")
            for unit, original in baseline.items():
                current = inspect(unit)
                unchanged = current["Id"] == original["Id"]
                running = current["State"]["Running"]
                if not unchanged or not running:
                    changed_units.append(unit)
                emit("live_readback", unit=unit, container_unchanged=unchanged, running=running)
            after = BACKUP.stat()
            assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ), "Closed backup changed"
    assert passed, "Compressed qualification did not pass; receiver unchanged"
    assert not changed_units, "Concurrent live change; receiver unchanged"
    emit(
        "compressed_qualification",
        state="PASS",
        production_files_changed=False,
        production_migration="NOT_RUN",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--module", type=Path, required=True)
    parser.add_argument("--module-sha256", required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--zstd-wheel", type=Path, required=True)
    args = parser.parse_args()
    qualify(args.module, args.module_sha256, args.wheel, args.zstd_wheel)
