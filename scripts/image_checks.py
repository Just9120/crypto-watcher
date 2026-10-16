"""Offline image checks on disposable fixtures; no Telegram/Bybit calls."""

import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

OWNER = "233382575"


def run(
    image: str,
    directory: Path,
    *command: str,
    success: bool = True,
    validation: bool = False,
    temporary_size: str = "16m",
) -> str:
    validation_options = (
        [
            "--tmpfs=/validation:rw,noexec,nosuid,nodev,size=128m,mode=0700,uid=10001,gid=10001",
            "--memory=256m",
            "--memory-swap=256m",
            "--cpus=1",
        ]
        if validation
        else ["--memory=512m", "--memory-swap=512m", "--cpus=1"]
    )
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            f"--tmpfs=/tmp:rw,noexec,nosuid,size={temporary_size}",
            *validation_options,
            "-v",
            f"{directory.resolve()}:/data",
            "-e",
            f"OWNER_USER_ID={OWNER}",
            "--entrypoint=python",
            image,
            *command,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if (result.returncode == 0) != success:
        raise RuntimeError(f"Image fixture failed: exit={result.returncode}, stderr={result.stderr}")
    return result.stdout


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("revision")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="cw-image-") as temporary:
        directory = Path(temporary)
        # These are generated non-secret fixtures, never a real data mount.
        directory.chmod(0o777)
        source = directory / "legacy.json"
        source.write_text(
            json.dumps(
                {
                    "version": 2,
                    "chats": {
                        OWNER: {
                            "enabled": False,
                            "settings": {
                                "custom_pairs": [],
                                "alert_universe_mode": "custom",
                                "price_move_min": 7,
                            },
                            "mutes": {"BYBIT:BTCUSDT": 2000000000},
                        }
                    },
                }
            )
        )
        original = hashlib.sha256(source.read_bytes()).hexdigest()
        version = json.loads(run(args.image, directory, "-m", "cryptowatcher", "--version"))
        assert version["source_revision"] == args.revision
        assert run(args.image, directory, "-c", "import os; print(os.getuid())").strip() == "10001"
        migration = (
            "-m",
            "cryptowatcher",
            "migrate",
            "--legacy",
            "/data/legacy.json",
            "--database",
            "/data/store.db",
            "--backup-dir",
            "/data",
            "--owner-id",
            OWNER,
        )
        assert json.loads(run(args.image, directory, *migration))["status"] == "imported"
        assert json.loads(run(args.image, directory, *migration))["status"] == "already_imported"
        assert hashlib.sha256(source.read_bytes()).hexdigest() == original
        run(
            args.image,
            directory,
            "-m",
            "cryptowatcher",
            "backup",
            "--database",
            "/data/store.db",
            "--destination",
            "/data/restore.db",
            "--owner-id",
            OWNER,
        )
        run(
            args.image,
            directory,
            "-c",
            "from pathlib import Path; from cryptowatcher.storage import Store; "
            "s=Store(Path('/data/restore.db'),233382575,readonly=True); "
            "assert s.snapshot().settings.price_move_min==7; "
            "assert s.snapshot().settings.custom_pairs==(); assert not s.enabled(); "
            "assert s.muted('BTCUSDT',100); s.close()",
        )
        state = json.loads(
            run(
                args.image,
                directory,
                "-m",
                "cryptowatcher",
                "state-check",
                "--database",
                "/data/store.db",
                "--validation-directory",
                "/validation",
                validation=True,
            )
        )
        assert state["validation_mode"] == "TMPFS" and not state["alerts_enabled"]
        proof = json.loads(
            run(
                args.image,
                directory,
                "-m",
                "cryptowatcher",
                "backup",
                "--database",
                "/data/store.db",
                "--destination",
                "/data/ram-restore.db",
                "--validation-directory",
                "/validation",
                validation=True,
            )
        )["backup"]
        actual_hash = run(
            args.image,
            directory,
            "-c",
            "import hashlib; from pathlib import Path; print(hashlib.sha256(Path('/data/ram-restore.db').read_bytes()).hexdigest())",
        ).strip()
        assert proof["phase"] == "COMPLETE" and proof["validation_mode"] == "TMPFS"
        assert proof["sha256"] == actual_hash
        run(
            args.image,
            directory,
            "-c",
            "from pathlib import Path; from cryptowatcher.native_validation import available; "
            "from cryptowatcher.compressed_validation import Image,frozen_vfs,MAX_COMPRESSED_BYTES; "
            "import time; assert available(); "
            "i=Image(Path('/data/ram-restore.db'),limit=MAX_COMPRESSED_BYTES,deadline=time.monotonic()+30); "
            "v=frozen_vfs(i,Path('/tmp/native-sentinel.sqlite3')); "
            "assert v.stats()['vfs_backend']=='native'; v.unregister()",
            temporary_size="64m",
        )
        for command in ("state-check", "backup"):
            extra = ["--destination", "/data/compressed-restore.db"] if command == "backup" else []
            compressed = json.loads(
                run(
                    args.image,
                    directory,
                    "-m",
                    "cryptowatcher",
                    command,
                    "--database",
                    "/data/store.db",
                    "--validation-directory",
                    "/validation",
                    "--compressed-validation",
                    *extra,
                    validation=True,
                )
            )
            if command == "state-check":
                assert compressed["validation_mode"] == "COMPRESSED_RAM"
                assert compressed["configuration_revision"] == state["configuration_revision"]
            else:
                assert compressed["backup"]["validation_mode"] == "COMPRESSED_RAM"
                assert compressed["backup"]["sha256"] == actual_hash
        archive = json.loads(
            run(
                args.image,
                directory,
                "-m",
                "cryptowatcher",
                "backup",
                "--database",
                "/data/store.db",
                "--destination",
                "/data/archived.sqlite3.zst",
                "--validation-directory",
                "/validation",
                "--compressed-validation",
                "--archive-backup",
                validation=True,
            )
        )["backup"]
        assert archive["phase"] == "COMPLETE" and archive["archive_encoding"] == "ZSTD"
        restored_hash = run(
            args.image,
            directory,
            "-c",
            """import hashlib,sqlite3,zstandard
from pathlib import Path
p=Path('/data/archived.sqlite3.zst')
assert hashlib.sha256(p.read_bytes()).hexdigest() == """
            + repr(archive["archive_sha256"])
            + """
with p.open('rb') as source,Path('/data/archive-restored.sqlite3').open('xb') as target:
    zstandard.ZstdDecompressor().copy_stream(source,target)
with sqlite3.connect('/data/archive-restored.sqlite3') as db:
    assert db.execute('PRAGMA integrity_check').fetchall()==[('ok',)]
    with sqlite3.connect('/data/ram-restore.db') as reference:
        for name in ('owner_settings','delivery_preferences','mutes','runtime','legacy_import'):
            assert db.execute('SELECT * FROM '+name+' ORDER BY 1').fetchall() == reference.execute('SELECT * FROM '+name+' ORDER BY 1').fetchall()
print(hashlib.sha256(Path('/data/archive-restored.sqlite3').read_bytes()).hexdigest())
""",
        ).strip()
        assert restored_hash == archive["sha256"]
        run(
            args.image,
            directory,
            "-c",
            """from contextlib import closing
import sqlite3, time
from pathlib import Path
from cryptowatcher.validation import validate_backup
from cryptowatcher.storage import IntegrityError
with closing(sqlite3.connect('/data/constraint.db')) as db:
    db.execute('CREATE TABLE checked(value INTEGER CHECK(value>0))')
    db.execute('PRAGMA ignore_check_constraints=ON')
    db.execute('INSERT INTO checked VALUES (-1)')
    db.commit()
try:
    validate_backup(Path('/data/constraint.db'), Path('/validation'), deadline=time.monotonic()+30)
except IntegrityError as error:
    assert error.code == 'DATABASE_CORRUPT'
else:
    raise AssertionError('Constraint violation was accepted')
""",
            validation=True,
        )
        run(
            args.image,
            directory,
            "-c",
            """import time
from pathlib import Path
from cryptowatcher.validation import validate_backup
from cryptowatcher.storage import IntegrityError
try:
    validate_backup(Path('/data/constraint.db'), Path('/validation'), deadline=time.monotonic()+30, compressed=True)
except IntegrityError as error:
    assert error.code == 'DATABASE_CORRUPT'
else:
    raise AssertionError('Compressed validation accepted a constraint violation')
""",
            validation=True,
        )
        rebuilt = json.loads(
            run(
                args.image,
                directory,
                "-c",
                r"""
import hashlib,json,subprocess,sys,time
from pathlib import Path
from cryptowatcher.config import OwnerSettings
from cryptowatcher.events import History
from cryptowatcher.storage import Store,check_integrity
work=Path('/tmp/rebuild-fixture');work.mkdir(mode=0o700)
source=work/'source.sqlite3';target=work/'candidate.sqlite3'
s=Store(source,233382575);s.initialize(OwnerSettings());h=History(s)
inputs=[{'start':i,'close':100,'provenance':'fixture_'*30} for i in range(24)]
with s.db:
    for i in range(20):
        for kind in ('event','outcome'):
            key=f'fixture-{i}'
            value={'schema':f'cryptowatcher.{kind}.v1',kind+'_id':key,
                'revision':1,'event_id':key,'event_revision':1,'episode_id':'episode',
                'market_observation_ms':1800000000000,'available_at_ms':1800000000001,
                'market_type':'spot','symbol':'BTCUSDT','detector_id':'price',
                'timeframe':'1m','direction':'UP','strength':2,'state':'COMPLETE',
                'horizon_min':60,'methodology_version':'1','classification':'CONTINUATION',
                'metrics':{'price_return_pct':2},'source_inputs':inputs,
                'settings_snapshot':s.snapshot().settings.payload()}
            h.put(f'{kind}:{key}:0000000001',value);h.put(f'{kind}-head:{key}',value)
s.db.execute('PRAGMA journal_mode=DELETE')
check_integrity(s.db,deadline=time.monotonic()+30);s.close()
expected=hashlib.sha256(source.read_bytes()).hexdigest()
r=subprocess.run([sys.executable,'-m','cryptowatcher.history_rebuild',
    '--source',str(source),'--destination',str(target),'--owner-id','233382575',
    '--qualified-source-sha256',expected,'--timeout-seconds','30','--backend','native'],
    capture_output=True,text=True,check=True)
value=json.loads(r.stdout)
assert hashlib.sha256(source.read_bytes()).hexdigest()==expected
assert value['heads_converted']==40 and value['candidate_bytes']<source.stat().st_size
assert value['conversion_backend']=='native-v1'
print(json.dumps(value))
""",
                validation=True,
                temporary_size="2g",
            )
        )
        assert rebuilt["status"] == "ISOLATED_CANDIDATE_PASS"
        assert rebuilt["candidate_full_integrity"] == "PASS"
        assert rebuilt["immutable_rows_and_resolved_heads"] == "PASS"
        assert rebuilt["production_migration"] == "NOT_RUN"
        run(
            args.image,
            directory,
            "-m",
            "cryptowatcher",
            "health",
            "--database",
            "/data/store.db",
            "--owner-id",
            OWNER,
            success=False,
        )
        run(
            args.image,
            directory,
            "-c",
            "import time; from pathlib import Path; "
            "from cryptowatcher.storage import Store; s=Store(Path('/data/store.db'),233382575); "
            "s.put_runtime('heartbeat',{'at':time.time(),'revision':'" + args.revision + "'}); s.close()",
        )
        assert json.loads(
            run(
                args.image,
                directory,
                "-m",
                "cryptowatcher",
                "health",
                "--database",
                "/data/store.db",
                "--owner-id",
                OWNER,
            )
        )["live"]
        reset = json.loads(
            run(
                args.image,
                directory,
                "-B",
                "-m",
                "cryptowatcher.reset_history",
                "--source",
                "/data/store.db",
                "--candidate",
                "/data/fresh.db",
                "--private-recovery",
                "/data/private.json",
                "--owner-id",
                OWNER,
                "--receipt",
                "/data/reset.json",
            )
        )
        assert reset["state"] == "PASS" and reset["private_restore"] == "PASS"
        capacity = json.loads(
            run(
                args.image,
                directory,
                "-m",
                "cryptowatcher",
                "capacity",
                "--database",
                "/data/fresh.db",
                "--capacity-ledger",
                "--capacity-cohorts",
            )
        )
        assert capacity["runtime_ledger"]["state"] == "MEASURED"
        assert capacity["source_revision"] == args.revision
        assert capacity["storage"]["history_generation"]["id"]
        run(
            args.image,
            directory,
            "-B",
            "-c",
            """import json
from pathlib import Path
from cryptowatcher.storage import Store
from cryptowatcher.reset_history import verify_private
from cryptowatcher.storage_meter import report,measure_indexes
s=Store(Path('/data/fresh.db'),233382575)
assert s.bounded and s.snapshot().settings.price_move_min==7 and not s.enabled()
assert s.muted('BTCUSDT',100) and s.snapshot().settings.custom_pairs==()
assert verify_private(s.path,Path('/data/private.json'),233382575)['state']=='PASS'
indexes=measure_indexes(s,5)
assert indexes['state']=='PASS' and indexes['total_index_bytes']>0
assert report(s)['working_bytes']<1024*1024
s.close()
""",
        )
        disk = json.loads(
            run(
                args.image,
                directory,
                "-m",
                "cryptowatcher",
                "backup",
                "--database",
                "/data/fresh.db",
                "--destination",
                "/data/fresh-backup.db",
                "--owner-id",
                OWNER,
            )
        )["backup"]
        assert disk["phase"] == "COMPLETE" and disk["validation_mode"] == "DISK_STREAMING"
        assert disk["bytes"] > 0 and len(disk["sha256"]) == 64
        # Execute the same canonical fixture with the actual product SQLite/codecs.
        # Only this disposable source file is mounted; no production data or APIs.
        (directory / "measure.py").write_bytes(
            Path(__file__).with_name("measure_market_storage.py").read_bytes()
        )
        measurement = json.loads(
            run(
                args.image,
                directory,
                "-B",
                "-c",
                "import runpy,sys; sys.argv=['measure','--database','/data/measured.db','--instruments','5']; "
                "runpy.run_path('/data/measure.py',run_name='__main__')",
            )
        )
        assert measurement["events"] == 200 and measurement["all_outcomes_complete"]
        assert measurement["analytics_equality"] == "PASS" and measurement["indexes"]["state"] == "PASS"
        print(json.dumps({"image_storage_measurement": measurement}, sort_keys=True))
        # Qualify the actual old layout/CW2 -> mixed views/CW3 transition with
        # product Python/SQLite, full disk backup and restart. No API requests.
        run(
            args.image,
            directory,
            "-B",
            "-c",
            """import runpy
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from cryptowatcher.storage import Store
from cryptowatcher.market_storage import encoded
from cryptowatcher.record_codec import pack_binary_v2
from cryptowatcher.analytics import cohort_rows
from cryptowatcher.storage_repack import work
m=runpy.run_path('/data/measure.py')
with patch('cryptowatcher.expiry_index.install'),patch('cryptowatcher.quality_layout.install'),patch('cryptowatcher.record_codec.pack_binary',pack_binary_v2),patch('cryptowatcher.quality_summary.encode_facts',encoded):
    old=m['measure'](Path('/data/upgrade.db'),1)
with closing(Store(Path('/data/upgrade.db'),233382575)) as s:
    before=list(cohort_rows(s))
    private=tuple(s.db.execute('SELECT * FROM owner_settings').fetchone())
    for _ in range(1000):
        if work(s,1800000000000,batch=32,seconds=1)['state']=='COMPLETE':break
    assert s.get_runtime('storage_repack')['state']=='COMPLETE'
    assert list(cohort_rows(s))==before and tuple(s.db.execute('SELECT * FROM owner_settings').fetchone())==private
    assert not s.db.execute('SELECT 1 FROM market_expiry_legacy').fetchone()
    assert not s.db.execute('SELECT 1 FROM market_quality_legacy').fetchone()
    assert s.backup(Path('/data/upgrade-backup.db'),timeout_seconds=30)['phase']=='COMPLETE'
with closing(Store(Path('/data/upgrade-backup.db'),233382575,readonly=True)) as restored:
    assert list(cohort_rows(restored))==before
print('Mixed storage upgrade/public analytics/private readback/restart/full disk backup: PASS')
""",
        )
        source.write_text('{"version":99,"chats":{}}')
        run(args.image, directory, *migration, success=False)
        assert source.read_text() == '{"version":99,"chats":{}}'
    print(
        "Image non-root / offline migration / full validation / backup hash / isolated rebuild / bounded reset and disk backup / restart / liveness: PASS"
    )


if __name__ == "__main__":
    main()
