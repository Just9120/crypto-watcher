"""Bounded ordered hot/cold reads; caller owns the snapshot and transaction."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from heapq import merge
from itertools import groupby
from typing import Any

from . import catalogue_reader
from .storage import json_payload


def upper(prefix: str) -> str | None:
    for index in range(len(prefix) - 1, -1, -1):
        codepoint = ord(prefix[index]) + 1
        if codepoint < 0x110000:
            if 0xD800 <= codepoint <= 0xDFFF:
                codepoint = 0xE000
            return prefix[:index] + chr(codepoint)
    return None


def hot_rows(
    db: sqlite3.Connection,
    prefix: str,
    reverse: bool,
    limit: int | None,
    after: str | None,
    before: str | None,
    decode: Any = None,
) -> Generator[tuple[str, Any, bool], None, None]:
    sql = "SELECT key,payload_json FROM runtime WHERE key>=?"
    parameters: tuple[Any, ...] = (prefix,)
    for endpoint, operation in ((upper(prefix), "<"), (after, ">"), (before, "<")):
        if endpoint is not None:
            sql += " AND key" + operation + "?"
            parameters += (endpoint,)
    sql += " ORDER BY key " + ("DESC" if reverse else "ASC")
    if limit is not None:
        sql += " LIMIT ?"
        parameters += (limit,)
    cursor = db.execute(sql, parameters)
    try:
        for key, payload in cursor:
            yield key, decode(payload) if decode is not None else json_payload(payload), True
    finally:
        cursor.close()


def scan(
    db: sqlite3.Connection,
    prefix: str,
    *,
    bounded: bool,
    reverse: bool = False,
    limit: int | None = None,
    cache: Any = None,
    after: str | None = None,
    before: str | None = None,
    decode: Any = None,
) -> Generator[tuple[str, Any], None, None]:
    if (
        not isinstance(prefix, str)
        or type(reverse) is not bool
        or (limit is not None and (type(limit) is not int or not 0 <= limit < 2**63))
        or any(value is not None and not isinstance(value, str) for value in (after, before))
    ):
        raise ValueError("Invalid mixed runtime scan bounds")
    if limit == 0:
        return
    hot = hot_rows(db, prefix, reverse, limit, after, before, decode)
    cold = catalogue_reader.scan(
        db, prefix, reverse=reverse, limit=limit if bounded else 0, cache=cache, after=after, before=before
    )
    try:
        ordered = merge(
            hot, ((key, value, False) for key, value in cold), key=lambda row: row[0], reverse=reverse
        )
        for number, (key, group) in enumerate(groupby(ordered, key=lambda row: row[0])):
            versions = list(group)
            if len(versions) == 1:
                value = versions[0][1]
            else:
                value = catalogue_reader.lookup(
                    db, key, next(value for _, value, is_hot in versions if is_hot), cache
                )
            yield key, value
            if limit is not None and number + 1 == limit:
                return
    finally:
        hot.close()
        cold.close()


def count(db: sqlite3.Connection, prefix: str, *, bounded: bool) -> int:
    if not isinstance(prefix, str):
        raise ValueError("Invalid mixed runtime count prefix")
    sql = "SELECT COUNT(*) FROM runtime WHERE key>=?"
    parameters: tuple[Any, ...] = (prefix,)
    endpoint = upper(prefix)
    if endpoint is not None:
        sql += " AND key<?"
        parameters += (endpoint,)
    hot = db.execute(sql, parameters).fetchone()[0]
    if not bounded:
        return hot
    cold = catalogue_reader.count(db, prefix)
    if not cold:
        return hot
    from .catalogue_overlap import count as overlap_count

    overlap = overlap_count(db, prefix)
    if not 0 <= overlap <= min(hot, cold):
        raise ValueError("Inconsistent mixed runtime overlap count")
    return hot + cold - overlap
