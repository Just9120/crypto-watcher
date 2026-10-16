"""Private Windows receiver for a pinned read-only VPS snapshot. No secret output."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from ctypes import wintypes as wt
from pathlib import Path, PurePosixPath

_JOB = None


def windows_limits() -> None:
    global _JOB
    if sys.platform != "win32":
        raise RuntimeError("This receiver requires the reviewed Windows host")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    class Basic(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_int64),
            ("job_time", ctypes.c_int64),
            ("flags", wt.DWORD),
            ("minimum", ctypes.c_size_t),
            ("maximum", ctypes.c_size_t),
            ("active", wt.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority", wt.DWORD),
            ("scheduling", wt.DWORD),
        ]

    class Io(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ro", "wo", "oo", "rb", "wb", "ob")]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("basic", Basic),
            ("io", Io),
            ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t),
            ("peak_process", ctypes.c_size_t),
            ("peak_job", ctypes.c_size_t),
        ]

    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
    kernel.CreateJobObjectW.restype = wt.HANDLE
    kernel.GetCurrentProcess.restype = wt.HANDLE
    kernel.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
    kernel.SetInformationJobObject.restype = wt.BOOL
    kernel.QueryInformationJobObject.argtypes = [
        wt.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.c_void_p,
    ]
    kernel.QueryInformationJobObject.restype = wt.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    kernel.AssignProcessToJobObject.restype = wt.BOOL
    kernel.SetProcessAffinityMask.argtypes = [wt.HANDLE, ctypes.c_size_t]
    kernel.SetProcessAffinityMask.restype = wt.BOOL
    kernel.GetProcessAffinityMask.argtypes = [
        wt.HANDLE,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel.GetProcessAffinityMask.restype = wt.BOOL
    job = kernel.CreateJobObjectW(None, None)
    settings = Extended()
    settings.basic.flags = 0x100 | 0x200 | 0x2000 | 0x8
    settings.basic.active = 2
    settings.process_memory = settings.job_memory = 4 * 1024**3
    process = kernel.GetCurrentProcess()
    if (
        not job
        or not kernel.SetInformationJobObject(job, 9, ctypes.byref(settings), ctypes.sizeof(settings))
        or not kernel.AssignProcessToJobObject(job, process)
        or not kernel.SetProcessAffinityMask(process, 1)
    ):
        raise RuntimeError("Receiver CPU1/total4GiB job unavailable")
    checked = Extended()
    mask, total = ctypes.c_size_t(), ctypes.c_size_t()
    if (
        not kernel.QueryInformationJobObject(job, 9, ctypes.byref(checked), ctypes.sizeof(checked), None)
        or not kernel.GetProcessAffinityMask(process, ctypes.byref(mask), ctypes.byref(total))
        or checked.job_memory != 4 * 1024**3
        or mask.value != 1
    ):
        raise RuntimeError("Receiver resource limits readback failed")
    _JOB = job


def connection(value: dict) -> list[str]:
    args = value["SshArguments"]
    fixed = {
        "BatchMode": "yes",
        "StrictHostKeyChecking": "yes",
        "ForwardAgent": "no",
        "ForwardX11": "no",
        "PermitLocalCommand": "no",
        "ClearAllForwardings": "yes",
        "RequestTTY": "no",
        "RemoteCommand": "none",
        "ControlMaster": "no",
        "ControlPath": "none",
        "ProxyCommand": "none",
        "ProxyJump": "none",
        "KnownHostsCommand": "none",
        "ConnectTimeout": "15",
        "ConnectionAttempts": "1",
        "HostName": "167.86.68.98",
        "User": "codex-ops",
        "Port": "22",
        "IdentitiesOnly": "yes",
        "GlobalKnownHostsFile": "none",
        "UpdateHostKeys": "no",
        "PreferredAuthentications": "publickey",
        "PasswordAuthentication": "no",
        "KbdInteractiveAuthentication": "no",
        "ServerAliveInterval": "30",
        "ServerAliveCountMax": "3",
        "HostKeyAlgorithms": "ssh-ed25519",
    }
    if value.get("Destination") != "codex-ops@167.86.68.98" or not isinstance(args, list) or len(args) % 2:
        raise RuntimeError("Pinned SSH destination required")
    options = {}
    flags = {}
    for flag, item in zip(args[::2], args[1::2], strict=True):
        if not isinstance(item, str) or any(c in item for c in ("\r", "\n", "\x00")):
            raise RuntimeError("Unsafe SSH argument")
        if flag == "-o":
            name, sep, data = item.partition("=")
            if not sep or name in options:
                raise RuntimeError("SSH option identity refused")
            options[name] = data
        elif flag in ("-F", "-i") and flag not in flags:
            flags[flag] = item
        else:
            raise RuntimeError("Unreviewed SSH argument")
    if (
        flags.get("-F") != "none"
        or set(options) != set(fixed) | {"UserKnownHostsFile"}
        or any(options.get(k) != v for k, v in fixed.items())
    ):
        raise RuntimeError("Pinned SSH safety options refused")
    for name in (flags.get("-i", ""), options["UserKnownHostsFile"].strip('"')):
        if not Path(name).is_file():
            raise RuntimeError("Pinned SSH identity/host file missing")
    executable = Path(value["SshExecutable"])
    if executable.name.lower() != "ssh.exe" or not executable.is_file():
        raise RuntimeError("Windows OpenSSH required")
    return [str(executable), *args, "-n", value["Destination"]]


def receive(configuration: dict, root: str, directory: Path, *, seconds: float = 900) -> dict:
    if not 15 < seconds <= 900:
        raise RuntimeError("Receiver deadline refused")
    path = PurePosixPath(root)
    if (
        path.parent != PurePosixPath("/opt/crypto_watcher/rehearsals")
        or re.fullmatch("online-snapshot-[a-z0-9_-]+", path.name) is None
    ):
        raise RuntimeError("Reviewed online snapshot path required")
    if directory.resolve() != directory or any(
        p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction())
        for p in (directory, *directory.parents)
    ):
        raise RuntimeError("Private PC destination required")
    directory.mkdir(mode=0o700)
    if shutil.disk_usage(directory).free < 8 * 1024**3 + 1536 * 1024**2:
        raise RuntimeError("PC stream disk reserve unavailable")
    ssh = connection(configuration)
    command = f"sudo -n env -u DOCKER_CONTEXT DOCKER_HOST=unix:///var/run/docker.sock PYTHONPATH={root}/operator /usr/bin/python3 -B {root}/operator/scripts/online_capture.py --root {root} --launch"
    started = time.monotonic()
    deadline = started + seconds
    digest = hashlib.sha256()
    size = 0
    last = started
    with (
        (directory / "producer.stderr").open("xb") as errors,
        (directory / "pages.delta").open("xb") as output,
    ):
        process = subprocess.Popen(
            [*ssh, command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=errors,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        timer = threading.Timer(seconds, process.kill)
        timer.daemon = True
        timer.start()
        try:
            assert process.stdout is not None
            for block in iter(lambda: process.stdout.read(1024**2), b""):
                if time.monotonic() >= deadline or size + len(block) > 8 * 1024**3:
                    raise RuntimeError("PC stream budget exceeded")
                if shutil.disk_usage(directory).free < 1536 * 1024**2:
                    raise RuntimeError("PC stream reserve exhausted")
                output.write(block)
                digest.update(block)
                size += len(block)
                if time.monotonic() - last >= 30:
                    print(
                        json.dumps(
                            {
                                "state": "RECEIVING",
                                "bytes": size,
                                "elapsed_seconds": round(time.monotonic() - started, 3),
                            }
                        ),
                        flush=True,
                    )
                    last = time.monotonic()
            status = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            if status:
                raise RuntimeError("Online producer did not finish successfully")
            output.flush()
            os.fsync(output.fileno())
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("PC stream whole deadline exhausted")
    metadata = subprocess.run(
        [*ssh, f"sudo -n cat -- {root}/work/snapshot.json {root}/preparation.json {root}/operation.json"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=min(15, remaining),
        check=True,
    )
    encoded_metadata = metadata.stdout.decode("utf-8")
    decoder = json.JSONDecoder()
    records = []
    for _ in range(3):
        record, end = decoder.raw_decode(encoded_metadata.lstrip())
        records.append(record)
        encoded_metadata = encoded_metadata.lstrip()[end:]
    if encoded_metadata.strip():
        raise RuntimeError("Unexpected producer metadata suffix")
    value, producer, operation = records
    if (
        value.get("state") != "CAPTURED_SNAPSHOT_NOT_QUALIFIED"
        or value.get("data_sha256") != digest.hexdigest()
        or value.get("data_bytes") != size
        or operation.get("state") != "ONLINE_CAPTURE_FINISHED_PC_READBACK_REQUIRED"
        or operation.get("snapshot") != value
        or operation.get("runtime_unchanged") is not True
        or producer.get("journal_epoch") != value.get("journal_epoch")
    ):
        raise RuntimeError("Independent PC stream identity refused")
    body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    with (directory / "snapshot.json").open("xb") as output:
        output.write(body)
        output.flush()
        os.fsync(output.fileno())
    result = {
        "state": "PC_STREAM_FSYNCED_IDENTITY_PASS",
        "directory": str(directory),
        "data_sha256": digest.hexdigest(),
        "data_bytes": size,
        "descriptor_sha256": hashlib.sha256(body).hexdigest(),
        "raw_bytes": value["raw_bytes"],
        "journal_epoch": value["journal_epoch"],
        "journal_watermark": value["journal_watermark"],
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "full_source_integrity": "PENDING",
        "production_replacement": False,
        "producer_root": root,
        "producer_revision": producer["revision"],
        "producer_image": producer["image"],
        "producer_container": producer["container"],
        "producer_code_revision": producer["code_revision"],
    }
    with (directory / "receiver.json").open("x", encoding="utf-8") as output:
        json.dump(result, output, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    with (directory / "producer.json").open("x", encoding="utf-8") as output:
        json.dump({"preparation": producer, "operation": operation}, output, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--connection", type=Path, required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    windows_limits()
    result = receive(json.loads(args.connection.read_text(encoding="utf-8-sig")), args.root, args.directory)
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
