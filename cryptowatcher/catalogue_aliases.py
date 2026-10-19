"""Indexed public history/membership bindings; no implicit publication or DDL."""

from __future__ import annotations

import re
import sqlite3
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from heapq import merge
from typing import Any

from . import record_catalog, record_masks
from .catalogue_reader import MISSING, PREFIX, UUID, reading, uuid_range
from .record_blocks import Coordinate
from .storage import StateError, encode

HISTORY = PREFIX + "history:"
MEMBERS = PREFIX + "episode-event:"
POINT = re.compile(rf"(?:(history):([0-9]{{16}}):({UUID})|(episode-event):({UUID}):([0-9]{{16}}):({UUID}))")


@dataclass(frozen=True)
class Alias:
    event: str
    observed: int
    episode: str | None = None

    @property
    def key(self) -> str:
        return (
            (HISTORY if self.episode is None else MEMBERS + self.episode + ":")
            + f"{self.observed:016d}:"
            + self.event
        )


def point(key: str) -> Alias | None:
    if not isinstance(key, str) or not key.startswith(PREFIX):
        return None
    matched = POINT.fullmatch(key[len(PREFIX) :])
    if matched is None:
        return None
    history, observed, event, _, episode, member_time, member_event = matched.groups()
    return Alias(event, int(observed)) if history else Alias(member_event, int(member_time), episode)


def active(db: sqlite3.Connection) -> bool:
    return reading(db) and record_catalog.layout(db) == 3


def lookup(db: sqlite3.Connection, key: str, hot: Any = MISSING) -> Any:
    selected = point(key)
    if selected is None or not active(db):
        return hot
    if selected.episode is None:
        present = db.execute(
            "SELECT 1 FROM market_record_parents WHERE public_uuid=? AND observed=? AND history_published=1",
            (record_catalog.binary(selected.event), selected.observed),
        ).fetchone()
    else:
        present = db.execute(
            "SELECT 1 FROM market_record_members WHERE episode=? AND observed=? AND event_uuid=?",
            (
                record_catalog.binary(selected.episode),
                selected.observed,
                record_catalog.binary(selected.event),
            ),
        ).fetchone()
        if (
            present is not None
            and db.execute(
                "SELECT 1 FROM market_record_parents WHERE public_uuid=? AND observed=?",
                (record_catalog.binary(selected.event), selected.observed),
            ).fetchone()
            is None
        ):
            raise StateError("Missing retained membership parent")
    if present is None:
        return hot
    value = {"event_id": selected.event}
    if hot is not MISSING:
        if encode(hot) != encode(value):
            raise StateError("Conflicting mixed immutable history binding")
        return hot
    return value


def publish(db: sqlite3.Connection, key: str, value: Any, cache: Any = None) -> bool:
    """Compact only a canonical exact binding to its verified retained event.

    Unknown fields/keys and unavailable event bodies remain ordinary hot rows.
    The caller owns publication and removal of the old public row in one tx.
    """
    if not db.in_transaction or record_catalog.layout(db) != 3:
        raise ValueError("Alias publication requires a known outer writer transaction")
    selected = point(key)
    if selected is None or encode(value) != encode({"event_id": selected.event}):
        return False
    root = db.execute(
        "SELECT id,history_published FROM market_record_parents WHERE public_uuid=? AND observed=?",
        (record_catalog.binary(selected.event), selected.observed),
    ).fetchone()
    first = db.execute(
        "SELECT revision FROM market_records WHERE kind=0 AND public_uuid=? ORDER BY revision LIMIT 1",
        (record_catalog.binary(selected.event),),
    ).fetchone()
    if root is None or first is None:
        return False
    body = record_catalog.read(db, Coordinate("event", selected.event, first[0]), cache)
    saved = record_catalog.row(db, Coordinate("event", selected.event, first[0]))
    if (
        body is None
        or type(body.get("market_observation_ms")) is not int
        or body["market_observation_ms"] != selected.observed
        or saved is None
        or saved[2] != root[0]
    ):
        return False
    if selected.episode is None:
        if not root[1]:
            db.execute("UPDATE market_record_parents SET history_published=1 WHERE id=?", (root[0],))
    else:
        record_catalog.member(db, selected.episode, selected.observed, root[0])
    return True


@dataclass(frozen=True)
class Selection:
    member: bool
    where: str
    parameters: tuple[Any, ...]
    complete: bool

    @property
    def source(self) -> str:
        return "market_record_members" if self.member else "market_record_parents"


def selection(prefix: str, *, member: bool) -> Selection | None:
    root = MEMBERS if member else HISTORY
    if root.startswith(prefix):
        suffix = ""
    elif prefix.startswith(root):
        suffix = prefix[len(root) :]
    else:
        return None
    complete = not suffix
    clauses = [] if member else ["history_published=1"]
    parameters: list[Any] = []
    if member:
        episode, separator, suffix = suffix.partition(":")
        bounds = uuid_range(episode)
        if bounds is None or (separator and len(episode) != 36):
            return None
        if len(episode) == 36:
            clauses.append("episode=?")
            parameters.append(bounds[0])
        elif episode:
            clauses.append("episode>=?")
            parameters.append(bounds[0])
            if bounds[1] is not None:
                clauses.append("episode<?")
                parameters.append(bounds[1])
        if not separator:
            return Selection(member, " AND ".join(clauses) or "1", tuple(parameters), complete)
    stamp, separator, event = suffix.partition(":")
    if len(stamp) > 16 or any(c not in "0123456789" for c in stamp) or (separator and len(stamp) != 16):
        return None
    if stamp:
        width = 10 ** (16 - len(stamp))
        lower = int(stamp) * width
        if len(stamp) == 16:
            clauses.append("observed=?")
            parameters.append(lower)
        else:
            clauses.append("observed>=? AND observed<?")
            parameters.extend((lower, lower + width))
    if separator:
        bounds = uuid_range(event)
        if bounds is None:
            return None
        field = "event_uuid" if member else "public_uuid"
        if len(event) == 36:
            clauses.append(field + "=?")
            parameters.append(bounds[0])
        elif event:
            clauses.append(field + ">=?")
            parameters.append(bounds[0])
            if bounds[1] is not None:
                clauses.append(field + "<?")
                parameters.append(bounds[1])
    return Selection(member, " AND ".join(clauses) or "1", tuple(parameters), complete)


def rows(
    db: sqlite3.Connection, selected: Selection, reverse: bool, limit: int | None
) -> Generator[tuple[str, Any], None, None]:
    fields = "episode,observed,event_uuid" if selected.member else "observed,public_uuid"
    order = " DESC" if reverse else " ASC"
    sql = (
        "SELECT "
        + fields
        + " FROM "
        + selected.source
        + " WHERE "
        + selected.where
        + " ORDER BY "
        + (",".join(f + order for f in fields.split(",")))
    )
    parameters = selected.parameters
    if limit is not None:
        sql += " LIMIT ?"
        parameters += (limit,)
    cursor = db.execute(sql, parameters)
    try:
        for row in cursor:
            if selected.member:
                episode, observed, event = row
                if (
                    db.execute(
                        "SELECT 1 FROM market_record_parents WHERE public_uuid=? AND observed=?",
                        (event, observed),
                    ).fetchone()
                    is None
                ):
                    raise StateError("Missing retained membership parent")
                binding = Alias(
                    str(uuid.UUID(bytes=bytes(event))), observed, str(uuid.UUID(bytes=bytes(episode)))
                )
            else:
                observed, event = row
                binding = Alias(str(uuid.UUID(bytes=bytes(event))), observed)
            if type(observed) is not int or not 0 <= observed < 10**16:
                raise StateError("Invalid retained history time")
            yield binding.key, {"event_id": binding.event}
    finally:
        cursor.close()


def scan(
    db: sqlite3.Connection, prefix: str, *, reverse: bool = False, limit: int | None = None
) -> Generator[tuple[str, Any], None, None]:
    if not isinstance(prefix, str) or (
        limit is not None and (type(limit) is not int or not 0 <= limit < 2**63)
    ):
        raise ValueError("Invalid catalogue alias bounds")
    if limit == 0:
        return
    selected = [value for member in (False, True) if (value := selection(prefix, member=member)) is not None]
    if not selected or not active(db):
        return
    streams = [rows(db, value, reverse, limit) for value in selected]
    try:
        for number, item in enumerate(merge(*streams, key=lambda row: row[0], reverse=reverse)):
            yield item
            if limit is not None and number + 1 == limit:
                return
    finally:
        for stream in streams:
            stream.close()


def count(db: sqlite3.Connection, prefix: str) -> int:
    if not isinstance(prefix, str):
        raise ValueError("Invalid catalogue alias prefix")
    selected = [value for member in (False, True) if (value := selection(prefix, member=member)) is not None]
    if not selected or not active(db):
        return 0
    totals = record_masks.tallies(db, "record_aliases")
    return sum(
        totals["membership" if value.member else "history"]
        if value.complete
        else db.execute(
            "SELECT COUNT(*) FROM " + value.source + " WHERE " + value.where,
            value.parameters,
        ).fetchone()[0]
        for value in selected
    )
