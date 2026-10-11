"""Offline image checks on disposable fixtures; no Telegram/Bybit calls."""

import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

OWNER = "233382575"


def run(image: str, directory: Path, *command: str, success: bool = True, validation: bool = False) -> str:
    validation_options = (
        [
            "--tmpfs=/validation:rw,noexec,nosuid,nodev,size=128m,mode=0700,uid=10001,gid=10001",
            "--memory=256m",
            "--memory-swap=256m",
            "--cpus=1",
        ]
        if validation
        else []
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
            "--tmpfs=/tmp:rw,noexec,nosuid,size=16m",
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
        source.write_text('{"version":99,"chats":{}}')
        run(args.image, directory, *migration, success=False)
        assert source.read_text() == '{"version":99,"chats":{}}'
    print(
        "Image non-root / offline migration / full tmpfs and compressed validation / backup hash / restart / liveness: PASS"
    )


if __name__ == "__main__":
    main()
