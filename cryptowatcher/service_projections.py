"""Logical compatibility readers over canonical trial services and legacy history.

Scheduling bindings are derived from indexed columns. They are never persisted
as duplicate JSON records. Legacy keys keep their old path without conversion.
"""

from __future__ import annotations

import heapq
import json
from typing import Any, Iterator

from .storage import StateError, encode

PREFIX = "radar:v1:"
MISSING = object()
QUEUES = {
    "pending:telegram:": ("telegram", "outbox"),
    "agent:intent-due:": ("agent", "intent"),
    "agent:send-due:": ("agent", "outbox"),
}


def delivery_binding(repo: Any, key: str) -> Any:
    ready = key.startswith("agent:ready:")
    if ready:
        parts = key.split(":", 4)
        if len(parts) != 5:
            return None
        _, _, stage, classification, rest = parts
        if stage not in ("intent", "send") or classification not in ("fresh", "history"):
            return None
        root = "agent:intent-due:" if stage == "intent" else "agent:send-due:"
        channel, kind = QUEUES[root]
    else:
        queue_root = next((p for p in QUEUES if key.startswith(p)), None)
        if queue_root is None:
            return None
        root = queue_root
        channel, kind = QUEUES[root]
        rest = key[len(root) :]
    due, separator, identifier = rest.partition(":")
    if not separator or not due.isdecimal():
        return None
    sql = "SELECT * FROM subject_deliveries WHERE channel=? AND stage=? AND queue_id=? AND due_ms=?"
    row = repo.store.db.execute(sql, (channel, kind, identifier, int(due))).fetchone()
    if row is None:
        return None
    return row, ready, root


def get(repo: Any, key: str) -> Any:
    if key.startswith("outcome-job:"):
        value = repo.get_job(key[12:])
        return MISSING if value is None else value
    if key.startswith("outcome-pending:"):
        parsed = outcome_binding(key)
        if parsed is None:
            return MISSING
        due, identifier = parsed
        row = repo.store.db.execute(
            "SELECT id FROM subject_outcomes WHERE id=? AND pending=1 AND queue_ms=?", (identifier, int(due))
        ).fetchone()
        return MISSING if row is None else {"outcome_id": row[0]}
    if key.startswith("hold:"):
        stream, _, identifier = key[5:].rpartition(":")
        row = repo.store.db.execute(
            "SELECT hold_start,hold_end FROM subject_outcomes WHERE id=? AND stream_key=? AND held=1",
            (identifier, stream),
        ).fetchone()
        return MISSING if row is None else {"start": row[0], "end": row[1]}
    if key.startswith(("agent:intent:", "agent:outbox:", "outbox:telegram:")):
        value = repo.get_delivery(key)
        return MISSING if value is None else value
    if key.startswith("agent:attempt:"):
        row = repo.store.db.execute(
            "SELECT payload,digest FROM subject_attempts WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return MISSING
        from .service_repository import decoded_payload

        return json.loads(decoded_payload(row[0], row[1]))
    binding = delivery_binding(repo, key)
    if binding is not None:
        row, ready, root = binding
        if not row[11] or ready and (not row[14] or (":history:" in key) != bool(row[10])):
            return MISSING
        if ready:
            return {"key": f"{root}{row[7]:016d}:{row[13]}", "event_id": row[4], "fresh_until_ms": row[15]}
        return {"key": row[0]} if row[2] == "intent" else {"delivery_id": row[3]}
    return MISSING


def put(history: Any, key: str, value: Any) -> bool:
    repo = history.store.services
    if not managed(key):
        return False
    repo.check(write=True)
    # Existing valid records remain with their original owner, including indexes.
    if history.store.db.execute("SELECT 1 FROM runtime WHERE key=?", (PREFIX + key,)).fetchone():
        return False
    if not isinstance(value, dict):
        return False
    db = history.store.db
    if key.startswith("outcome-job:"):
        changed = repo.update_job(value)
        if changed:
            from .queue_health import sync_job

            sync_job(history, key, value)
        return changed
    if key.startswith("outcome-pending:"):
        parsed = outcome_binding(key)
        if parsed is None:
            return False
        due, _ = parsed
        return bool(
            db.execute(
                "UPDATE subject_outcomes SET pending=1,queue_ms=? WHERE id=?", (due, value["outcome_id"])
            ).rowcount
        )
    if key.startswith("hold:"):
        stream, _, identifier = key[5:].rpartition(":")
        row = db.execute(
            "SELECT hold_start,hold_end FROM subject_outcomes WHERE id=? AND stream_key=?",
            (identifier, stream),
        ).fetchone()
        if row is None:
            return False
        if tuple(row) != (value["start"], value["end"]):
            raise StateError("Immutable subject input lease")
        db.execute("UPDATE subject_outcomes SET held=1 WHERE id=?", (identifier,))
        return True
    if key.startswith(("agent:intent:", "agent:outbox:", "outbox:telegram:")):
        old = db.execute(
            "SELECT event_id,event_revision,expires_ms FROM subject_deliveries WHERE key=?", (key,)
        ).fetchone()
        channel = "telegram" if key.startswith("outbox:") else "agent"
        stage = "intent" if key.startswith("agent:intent:") else "outbox"
        event_id = (
            value.get("event_id")
            or value.get("intent", {}).get("event_id")
            or next(iter(value.get("event_ids", [])), None)
        )
        revision = (
            value.get("event_revision")
            or value.get("intent", {}).get("event_revision")
            or value.get("event_revisions", {}).get(event_id)
        )
        if old is not None:
            event_id = event_id or old[0]
            revision = revision or old[1]
        if not event_id or not revision:
            return False
        frozen = stage == "outbox" and (channel == "agent" or "text" in value)
        return repo.write_delivery(
            history,
            key,
            history._freeze_services(value),
            channel=channel,
            stage=stage,
            event_id=event_id,
            revision=revision,
            historical=value.get("historical", value.get("kind") == "HISTORICAL_OUTCOME_UPDATE"),
            due_ms=value.get("next_attempt_ms", value.get("next_ms", value.get("due_ms", 0))),
            expires_ms=value.get("expires_ms", old[2] if old is not None else 2**63 - 1),
            frozen=frozen,
        )
    if key.startswith("agent:attempt:"):
        _, _, delivery, number = key.split(":")
        parent = db.execute(
            "SELECT event_id,event_revision FROM subject_deliveries WHERE key=?",
            ("agent:outbox:" + delivery,),
        ).fetchone()
        if parent is None:
            return False
        source = db.execute(
            "SELECT expires_ms FROM subject_sources WHERE event_id=? AND revision=?", tuple(parent)
        ).fetchone()
        from .service_repository import checksum, decoded_payload, packed_payload

        plain = encode(value)
        raw = packed_payload(plain)
        # An interrupted attempt has no completed receipt. Completed receipts
        # cannot be overwritten under the same immutable attempt identity.
        saved = db.execute("SELECT payload,digest FROM subject_attempts WHERE key=?", (key,)).fetchone()
        if saved is not None:
            if decoded_payload(saved[0], saved[1]) != plain:
                raise StateError("Conflicting immutable delivery receipt")
            return True
        db.execute(
            "INSERT INTO subject_attempts VALUES(?,?,?,?,?,?,?,?)",
            (key, delivery, int(number), *parent, source[0], raw, checksum(raw)),
        )
        repo.stats["attempt_writes"] += 1
        return True
    root = next((p for p in QUEUES if key.startswith(p)), None)
    if root is not None:
        due_text, _, identifier = key[len(root) :].partition(":")
        channel, stage = QUEUES[root]
        return bool(
            db.execute(
                "UPDATE subject_deliveries SET queued=1,due_ms=? WHERE channel=? AND stage=? AND queue_id=?",
                (int(due_text), channel, stage, identifier),
            ).rowcount
        )
    binding = delivery_binding(repo, key)
    if binding is not None:
        row, ready, _ = binding
        if ready:
            db.execute(
                "UPDATE subject_deliveries SET ready=1,historical=?,fresh_ms=? WHERE key=?",
                (int(":history:" in key), value["fresh_until_ms"], row[0]),
            )
            return True
    return False


def delete(repo: Any, key: str) -> bool:
    if not managed(key):
        return False
    repo.check(write=True)
    db = repo.store.db
    if key.startswith("outcome-job:"):
        return repo.finish_job(key[12:])
    if key.startswith("outcome-pending:"):
        parsed = outcome_binding(key)
        if parsed is None:
            return False
        due, identifier = parsed
        return bool(
            db.execute(
                "UPDATE subject_outcomes SET pending=0 WHERE id=? AND queue_ms=?", (identifier, int(due))
            ).rowcount
        )
    if key.startswith("hold:"):
        stream, _, identifier = key[5:].rpartition(":")
        return bool(
            db.execute(
                "UPDATE subject_outcomes SET held=0 WHERE id=? AND stream_key=?", (identifier, stream)
            ).rowcount
        )
    if key.startswith(("agent:intent:", "agent:outbox:", "outbox:telegram:")):
        return bool(db.execute("DELETE FROM subject_deliveries WHERE key=?", (key,)).rowcount)
    if key.startswith("agent:attempt:"):
        return bool(db.execute("DELETE FROM subject_attempts WHERE key=?", (key,)).rowcount)
    binding = delivery_binding(repo, key)
    if binding is None:
        return False
    row, ready, _ = binding
    if ready and (":history:" in key) != bool(row[10]):
        return True
    db.execute(
        "UPDATE subject_deliveries SET " + ("ready=0" if ready else "queued=0") + " WHERE key=?", (row[0],)
    )
    return True


def rows(
    repo: Any, prefix: str, limit: int | None, reverse: bool, after: str | None, before: str | None
) -> Iterator[tuple[str, Any]]:
    """Bounded indexed queue selection; broad export retains ordinary ordering."""
    db = repo.store.db
    specifications = [
        ("outcome-job:", "subject_outcomes", "'outcome-job:'||id", "1"),
        (
            "outcome-pending:",
            "subject_outcomes",
            "'outcome-pending:'||printf('%04d',length(CAST(queue_ms AS TEXT)))||':'||CAST(queue_ms AS TEXT)||':'||id",
            "pending=1",
        ),
        ("hold:", "subject_outcomes", "'hold:'||stream_key||':'||id", "held=1"),
        ("agent:attempt:", "subject_attempts", "key", "1"),
        ("agent:intent:", "subject_deliveries", "key", "channel='agent' AND stage='intent'"),
        ("agent:outbox:", "subject_deliveries", "key", "channel='agent' AND stage='outbox'"),
        ("outbox:telegram:", "subject_deliveries", "key", "channel='telegram'"),
    ]
    for root, (channel, stage) in QUEUES.items():
        specifications.append(
            (
                root,
                "subject_deliveries",
                repr(root) + "||printf('%016d',due_ms)||':'||queue_id",
                f"queued=1 AND channel='{channel}' AND stage='{stage}'",
            )
        )
    for stage, kind in (("intent", "intent"), ("send", "outbox")):
        for old in (0, 1):
            root = f"agent:ready:{stage}:" + ("history:" if old else "fresh:")
            specifications.append(
                (
                    root,
                    "subject_deliveries",
                    repr(root) + "||printf('%016d',due_ms)||':'||queue_id",
                    f"queued=1 AND ready=1 AND channel='agent' AND stage='{kind}' AND historical={old}",
                )
            )
    collected = []
    for root, table, expression, where in specifications:
        if not (root.startswith(prefix) or prefix.startswith(root)):
            continue
        low = max(prefix, after or prefix)
        high = min(prefix + "~", before or prefix + "~")
        order = (
            "queue_ms,id"
            if root == "outcome-pending:"
            else "due_ms,queue_id"
            if table == "subject_deliveries" and (root in QUEUES or root.startswith("agent:ready:"))
            else "logical_key"
        )
        order = ",".join(part + (" DESC" if reverse else " ASC") for part in order.split(","))
        sql = f"SELECT {expression} AS logical_key FROM {table} WHERE {where} AND {expression}>=? AND {expression}<?"
        args: list[Any] = [low, high]
        if after is not None:
            sql += f" AND {expression}>?"
            args.append(after)
        sql += " ORDER BY " + order
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        for (key,) in db.execute(sql, args).fetchall():
            value = get(repo, key)
            if value is MISSING:
                raise StateError("Missing canonical subject projection")
            collected.append((PREFIX + key, value))
    yield from sorted(collected, key=lambda item: item[0], reverse=reverse)[:limit]


def merge(
    repo: Any,
    legacy: Iterator[tuple[str, Any]],
    prefix: str,
    limit: int | None,
    reverse: bool,
    after: str | None,
    before: str | None,
) -> Iterator[tuple[str, Any]]:
    if not prefix.startswith(PREFIX):
        yield from legacy
        return
    new = rows(
        repo,
        prefix[len(PREFIX) :],
        limit,
        reverse,
        None if after is None else after[len(PREFIX) :],
        None if before is None else before[len(PREFIX) :],
    )
    previous = None
    count = 0
    for key, value in heapq.merge(legacy, new, key=lambda item: item[0], reverse=reverse):
        if key == previous:
            raise StateError("Conflicting legacy and canonical subject ownership")
        previous = key
        if limit is not None and count >= limit:
            break
        yield key, value
        count += 1


def transition(history: Any, key: str, record: dict[str, Any]) -> None:
    if history.store.services is None:
        history.put(key, record)
        return
    from .packet_store import atomic

    if not history.store.db.in_transaction:
        history.store.db.execute("BEGIN IMMEDIATE")
    with atomic(history.store.db):
        _transition(history, key, record)


def _transition(history: Any, key: str, record: dict[str, Any]) -> None:
    """Known delivery status update; public generic put still validates payload."""
    repo = history.store.services
    if (
        repo is None
        or repo.store.db.execute("SELECT 1 FROM subject_deliveries WHERE key=?", (key,)).fetchone() is None
    ):
        history.put(key, record)
        return
    from .service_repository import PAYLOAD_FIELDS
    from .storage_lifecycle import TERMINAL_DELIVERY

    channel = "telegram" if key.startswith("outbox:") else "agent"
    stage = "intent" if key.startswith("agent:intent:") else "outbox"
    payload_fields = PAYLOAD_FIELDS[(channel, stage)]
    mutable = {k: v for k, v in record.items() if k not in payload_fields}
    if not history.store.db.in_transaction:
        history.store.db.execute("BEGIN IMMEDIATE")
    repo.update_delivery(key, mutable)
    if record.get("state") in TERMINAL_DELIVERY:
        # Match existing terminal public shape, release large payloads once.
        keep = {
            k: v
            for k, v in record.items()
            if k in payload_fields
            and k not in ("intent", "body", "episode_snapshot", "text", "text_entities")
        }
        from .service_repository import checksum, packed_payload

        raw = packed_payload(encode(history._freeze_services(keep)))
        saved = history.store.db.execute(
            "SELECT payload,digest,frozen FROM subject_delivery_payloads WHERE key=?", (key,)
        ).fetchone()
        if saved is not None and tuple(saved) == (raw, checksum(raw), 1):
            return
        history.store.db.execute("DELETE FROM subject_delivery_payloads WHERE key=?", (key,))
        history.store.db.execute(
            "INSERT INTO subject_delivery_payloads VALUES(?,?,?,1)", (key, raw, checksum(raw))
        )
        repo.stats["payload_writes"] += 1


def managed(key: str) -> bool:
    return key.startswith(
        (
            "outcome-job:",
            "outcome-pending:",
            "hold:",
            "outbox:telegram:",
            "pending:telegram:",
            "agent:intent:",
            "agent:intent-due:",
            "agent:send-due:",
            "agent:outbox:",
            "agent:ready:",
            "agent:attempt:",
        )
    )


def outcome_binding(key: str) -> tuple[str, str] | None:
    parts = key[len("outcome-pending:") :].split(":", 2)
    if (
        len(parts) != 3
        or not parts[0].isdecimal()
        or not parts[1].isdecimal()
        or int(parts[0]) != len(parts[1])
    ):
        return None
    return parts[1], parts[2]
