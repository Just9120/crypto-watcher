"""Reversible encoding of the secondary owner registry; runtime keys stay public.

Stable integer IDs/FKs do not change. Registry equality uses both legacy text and
the canonical binary token until the bounded physical pass completes.
"""

from __future__ import annotations

import re
import sqlite3
import time
from typing import Any

from .compact_json import integer

MAGIC = b"CK1\x00"
ROOTS = (
    "",
    "historyblob:v1:",
    "radar:v1:event:",
    "radar:v1:event-head:",
    "radar:v1:outcome:",
    "radar:v1:outcome-head:",
    "radar:v1:episode:",
    "radar:v1:episode-head:",
    "radar:v1:comparison:",
    "radar:v1:comparison-head:",
    "radar:v1:outcome-job:",
    "radar:v1:comparison-job:",
    "radar:v1:history:",
    "radar:v1:episode-event:",
    "radar:v1:decision:telegram:",
    "radar:v1:hold:",
    "quality-provenance:",
    "radar:v1:",
)
PART = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{64}|[0-9]{10,}")
MAXIMUM = 8192


def pack(key: Any) -> str | bytes:
    if isinstance(key, bytes):
        unpack(key)
        return key
    if not isinstance(key, str):
        raise ValueError("Invalid owner registry key")
    original = key.encode()
    if len(original) > MAXIMUM:
        return key
    root = next((i for i, prefix in enumerate(ROOTS[1:], 1) if key.startswith(prefix)), 0)
    suffix = key[len(ROOTS[root]) :]
    result = bytearray(MAGIC + bytes((root,)))

    def literal(value: str) -> None:
        if value:
            raw = value.encode()
            result.append(0)
            result.extend(integer(len(raw)))
            result.extend(raw)

    cursor = 0
    for match in PART.finditer(suffix):
        literal(suffix[cursor : match.start()])
        value = match.group()
        if "-" in value or len(value) == 64:
            result.append(1 if "-" in value else 2)
            result.extend(bytes.fromhex(value.replace("-", "")))
        elif len(value) <= 19:
            result.append(3)
            result.extend(integer(len(value)))
            result.extend(integer(int(value)))
        else:
            literal(value)
        cursor = match.end()
    literal(suffix[cursor:])
    return bytes(result) if len(result) < len(original) else key


def unpack(token: Any) -> str:
    if isinstance(token, str):
        return token
    if not isinstance(token, bytes) or not 5 <= len(token) <= MAXIMUM or not token.startswith(MAGIC):
        raise ValueError("Invalid encoded registry key")
    root = token[4]
    if root >= len(ROOTS):
        raise ValueError("Unknown registry key dictionary")
    cursor = 5

    def number() -> int:
        nonlocal cursor
        value = 0
        for shift in range(0, 70, 7):
            if cursor >= len(token):
                raise ValueError("Truncated registry coordinate")
            part = token[cursor]
            cursor += 1
            value |= (part & 127) << shift
            if part < 128:
                return value
        raise ValueError("Registry coordinate exceeds bound")

    def take(n: int) -> bytes:
        nonlocal cursor
        if n > len(token) - cursor:
            raise ValueError("Truncated registry token")
        value = token[cursor : cursor + n]
        cursor += n
        return value

    fragments = [ROOTS[root]]
    size = len(fragments[0].encode())
    while cursor < len(token):
        tag = take(1)[0]
        if tag == 0:
            part = take(number()).decode()
        elif tag in (1, 2):
            value = take(16 if tag == 1 else 32).hex()
            part = (
                "-".join((value[:8], value[8:12], value[12:16], value[16:20], value[20:]))
                if tag == 1
                else value
            )
        elif tag == 3:
            width = number()
            value = str(number())
            if not 10 <= width <= 19 or len(value) > width:
                raise ValueError("Invalid padded registry coordinate")
            part = value.zfill(width)
        else:
            raise ValueError("Unknown registry token tag")
        size += len(part.encode())
        if size > MAXIMUM:
            raise ValueError("Expanded registry key exceeds bound")
        fragments.append(part)
    result = "".join(fragments)
    if pack(result) != token:
        raise ValueError("Noncanonical encoded registry key")
    return result


def install_functions(db: sqlite3.Connection) -> None:
    db.create_function("cw_key_pack", 1, pack, deterministic=True)
    db.create_function("cw_key_unpack", 1, unpack, deterministic=True)


def drain(db: sqlite3.Connection, state: dict[str, Any], batch: int, deadline: float) -> bool:
    if "key_ceiling" not in state:
        last = db.execute("SELECT id FROM market_runtime_keys ORDER BY id DESC LIMIT 1").fetchone()
        state.update(key_cursor=-(2**63), key_ceiling=last[0] if last else 0)
    if state["key_cursor"] > state["key_ceiling"]:
        return True
    rows = db.execute(
        "SELECT id,external FROM market_runtime_keys WHERE id>=? AND id<=? ORDER BY id LIMIT ?",
        (state["key_cursor"], state["key_ceiling"], batch),
    ).fetchall()
    processed = 0
    for identity, external in rows:
        if processed and time.monotonic() >= deadline:
            break
        logical = unpack(external)
        token = pack(logical)
        if unpack(token) != logical:
            raise ValueError("Registry encoding changed key identity")
        if external != token:
            db.execute(
                "UPDATE market_runtime_keys SET external=? WHERE id=? AND external=?",
                (token, identity, external),
            )
        size = len(token.encode()) if isinstance(token, str) else len(token)
        previous = db.execute("SELECT key_bytes FROM market_expiry WHERE rid=?", (identity,)).fetchone()
        db.execute(
            "UPDATE market_expiry SET key_bytes=? WHERE rid=? AND key_bytes<>?",
            (size, identity, size),
        )
        if previous is not None and previous[0] > size:
            saved = state.setdefault("saved_categories", {})
            saved["service_indexes"] = saved.get("service_indexes", 0) + previous[0] - size
        state["key_cursor"] = identity + 1
        processed += 1
    return processed == len(rows) and (len(rows) < batch or state["key_cursor"] > state["key_ceiling"])
