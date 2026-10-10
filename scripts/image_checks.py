"""Offline image checks on disposable fixtures; no Telegram/Bybit calls."""

import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

OWNER = "233382575"


def run(image: str, directory: Path, *command: str, success: bool = True) -> str:
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
    print("Image non-root / offline migration / backup readback / restart / liveness: PASS")


if __name__ == "__main__":
    main()
