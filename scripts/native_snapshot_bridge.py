"""Bounded compression around pinned SQLite rsync roles, never a DB writer itself."""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import BinaryIO

LINUX_SHA256 = "0268b5b4567269169241a5184b4a30a1b9c75637ef26868e927f7022a5b5a4f3"
WINDOWS_SHA256 = "8b74ab7ce6b529389cd2f4b4161a59b6c2169eae7f19effca0361ca9c3090bbd"
MAXIMUM = 128 * 1024**3


class OriginTranscript:
    """Check complete upstream v2 messages without interpreting or retaining DB pages."""

    def __init__(self) -> None:
        self.digest = hashlib.sha256()
        self.buffer = bytearray()
        self.pages = 0
        self.page_bytes = 0
        self.messages = 0
        self.transactions = 0
        self.ended = False
        self.remaining = 0
        self.command: int | None = None
        self.header = bytearray()
        self.bytes = 0

    def feed(self, body: bytes) -> None:
        if self.ended and body:
            raise RuntimeError("Trailing native origin bytes refused")
        self.digest.update(body)
        self.bytes += len(body)
        if self.bytes > MAXIMUM:
            raise RuntimeError("Native transcript size refused")
        self.buffer.extend(body)
        while self.buffer:
            if self.command is None:
                self.command = self.buffer.pop(0)
                self.header.clear()
                command = self.command
                if command == 0x42:
                    if self.transactions != 1 or self.buffer:
                        raise RuntimeError("Incomplete native transaction")
                    self.ended = True
                    self.command = None
                    return
                lengths = {0x41: 6, 0x44: 4, 0x45: 4, 0x47: 8, 0x48: 0}
                if command not in lengths or self.transactions:
                    raise RuntimeError("Native origin error or unknown message")
                self.remaining = lengths[command]
            take = min(self.remaining, len(self.buffer))
            if self.command != -1:
                self.header.extend(self.buffer[:take])
            del self.buffer[:take]
            self.remaining -= take
            if self.remaining:
                return
            command, self.command = self.command, None
            header = self.header
            if command == 0x41:
                if self.pages or header[0] != 2 or not 9 <= header[1] <= 16:
                    raise RuntimeError("Native protocol/page size refused")
                self.page_bytes = 1 << header[1]
                self.pages = int.from_bytes(header[2:], "big")
                if not 0 < self.pages * self.page_bytes <= 64 * 1024**3:
                    raise RuntimeError("Native origin extent refused")
            elif not self.pages:
                raise RuntimeError("Native message before origin identity")
            elif command == 0x44:
                page = int.from_bytes(header, "big")
                if not 1 <= page <= self.pages:
                    raise RuntimeError("Native page outside origin")
                self.command = -1
                self.remaining = self.page_bytes
                self.messages += 1
            elif command == 0x45:
                if int.from_bytes(header, "big") != self.pages:
                    raise RuntimeError("Native commit extent mismatch")
                self.transactions += 1

    def finish(self) -> dict:
        if not self.ended or self.command is not None or self.remaining or self.transactions != 1:
            raise RuntimeError("Native transcript lacks a complete committed snapshot")
        return {
            "protocol": 2,
            "pages": self.pages,
            "page_bytes": self.page_bytes,
            "updated_pages": self.messages,
            "bytes": self.bytes,
            "sha256": self.digest.hexdigest(),
        }


def binary(path: Path, *, windows: bool) -> None:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("Pinned native executable required")
    expected = WINDOWS_SHA256 if windows else LINUX_SHA256
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise RuntimeError("Native executable hash mismatch")


def bridge(
    command: list[str],
    incoming: BinaryIO,
    outgoing: BinaryIO,
    *,
    role: str,
    errors: BinaryIO,
    seconds: float,
    cancelled: threading.Event | None = None,
) -> dict:
    """Full duplex, constant IO buffers; an explicit deadline releases both processes."""
    if role not in ("origin", "replica") or not 0 < seconds <= 900:
        raise RuntimeError("Native bridge scope refused")
    import zstandard

    deadline = time.monotonic() + seconds
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=errors,
        bufsize=0,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert process.stdin is not None and process.stdout is not None
    transcript = OriginTranscript()
    failures: list[BaseException] = []
    counts = {"encoded_sent": 0, "encoded_received": 0}
    decoded_bytes = 0

    def receive() -> None:
        nonlocal decoded_bytes
        try:
            decoder = zstandard.ZstdDecompressor(max_window_size=64 * 1024**2).decompressobj()
            while True:
                body = os.read(incoming.fileno(), 1024)
                if not body:
                    if not decoder.eof:
                        raise RuntimeError("Truncated native compressed input")
                    break
                counts["encoded_received"] += len(body)
                if counts["encoded_received"] > 8 * 1024**3:
                    raise RuntimeError("Native incoming compressed bound exceeded")
                raw = decoder.decompress(body)
                decoded_bytes += len(raw)
                if decoded_bytes > MAXIMUM:
                    raise RuntimeError("Native decoded input bound exceeded")
                if decoder.unused_data:
                    raise RuntimeError("Trailing native compressed input refused")
                if role == "replica":
                    transcript.feed(raw)
                pending = memoryview(raw)
                while pending:
                    written = process.stdin.write(pending)
                    if not written:
                        raise RuntimeError("Native input pipe closed")
                    pending = pending[written:]
                if decoder.eof:
                    break
        except BaseException as exc:
            failures.append(exc)
            if process.poll() is None:
                process.kill()
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    def send() -> None:
        try:
            encoder = zstandard.ZstdCompressor(level=1, write_checksum=True).compressobj()
            while True:
                raw = os.read(process.stdout.fileno(), 65536)
                if not raw:
                    break
                if role == "origin":
                    transcript.feed(raw)
                body = encoder.compress(raw) + encoder.flush(zstandard.COMPRESSOBJ_FLUSH_BLOCK)
                counts["encoded_sent"] += len(body)
                if counts["encoded_sent"] > 8 * 1024**3:
                    raise RuntimeError("Native outgoing compressed bound exceeded")
                outgoing.write(body)
                outgoing.flush()
            tail = encoder.flush()
            counts["encoded_sent"] += len(tail)
            outgoing.write(tail)
            outgoing.flush()
        except BaseException as exc:
            failures.append(exc)
            if process.poll() is None:
                process.kill()

    readers = [threading.Thread(target=receive, daemon=True), threading.Thread(target=send, daemon=True)]
    for thread in readers:
        thread.start()
    try:
        while process.poll() is None:
            if failures or (cancelled is not None and cancelled.is_set()):
                raise RuntimeError("Native bridge cancelled")
            if time.monotonic() >= deadline:
                raise RuntimeError("Native bridge deadline exhausted")
            try:
                process.wait(timeout=min(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
        for thread in readers:
            thread.join(timeout=max(0.001, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in readers) or time.monotonic() >= deadline:
            raise RuntimeError("Native bridge deadline exhausted")
        if failures:
            raise RuntimeError("Native bridge transport refused") from failures[0]
        if process.returncode:
            raise RuntimeError("Native process failed")
        if errors.tell():
            raise RuntimeError("Native process diagnostics require review")
        return {"state": "NATIVE_COMMITTED_SNAPSHOT", "origin": transcript.finish(), **counts}
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()
