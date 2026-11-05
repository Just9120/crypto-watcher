"""Regression checks for live queues under ordinary bounded maintenance."""

import json
import sqlite3
from contextlib import closing

import pytest

from cryptowatcher.domain_repository import History
from cryptowatcher.service_diagnostics import pending_usage
from cryptowatcher.service_repository import checksum, decoded_payload, packed_payload
from cryptowatcher.storage import Store
from tests.test_service_repository import job_for
from tests.test_service_repository import subject as subject


def telegram(history, event, key="outbox:telegram:codec"):
    value = dict(
        delivery_id=key.split(":")[-1],
        event_ids=[event["event_id"]],
        event_revisions={event["event_id"]: event["revision"]},
        state="PENDING",
        next_attempt_ms=event["reference_time_ms"],
        text="🔔 точные значения 123.4500 " * 300,
    )
    history.put(key, value)
    return value


def test_maintenance_keeps_typed_retry_and_telegram_but_removes_actual_orphans(subject):
    from cryptowatcher.outcomes import pending_key
    from cryptowatcher.storage_lifecycle import BoundedRetention

    store, history, event, repo = subject
    job = job_for(event)
    existing_pending = history.count("pending:telegram:")
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        repo.reserve(history, job, event)
        history.delete(job["pending_key"])
        retry = {
            **job,
            "state": "INCOMPLETE",
            "reason": "GAP",
            "next_attempt_ms": job["due_ms"] + 60000,
            "pending_key": pending_key(job["due_ms"] + 60000, job["outcome_id"]),
        }
        history.put("outcome-job:" + job["outcome_id"], retry)
        history.put(retry["pending_key"], {"outcome_id": job["outcome_id"]})
        value = telegram(history, event)
        history.put("pending:telegram:0000000000000001:codec", {"delivery_id": "codec"})
        history.put("pending:telegram:0000000000000001:orphan", {"delivery_id": "orphan"})
        history.put("digest-outbox:legacy", {"state": "PENDING"})
        history.put("digest-pending:legacy", {"delivery_id": "legacy"})
    # This is the production maintenance caller, not just direct service GC.
    BoundedRetention(store).work(event["reference_time_ms"])
    assert history.get(retry["pending_key"]) == {"outcome_id": job["outcome_id"]}
    assert history.get("outcome-job:" + job["outcome_id"]) == retry
    assert history.get("outbox:telegram:codec")["text"] == value["text"]
    assert history.count("pending:telegram:") == existing_pending + 1
    assert history.get("digest-pending:legacy") == {"delivery_id": "legacy"}
    assert history.get("orphaned-pending-summary") == {"pending:telegram:EXPIRED_MISSING_PARENT": 1}
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        history.delete("outcome-job:" + job["outcome_id"])
        assert history.prune_pending() == 0  # Derived queue vanishes with its owner.
    assert history.get(retry["pending_key"]) is None


def test_payload_roundtrip_plain_compatibility_frozen_retry_and_accounting(subject):
    from cryptowatcher.service_projections import transition
    from cryptowatcher.storage import StateError

    store, history, event, repo = subject
    existing = pending_usage(store)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        value = telegram(history, event)
        history.put("pending:telegram:0000000000000001:codec", {"delivery_id": "codec"})
        history.put("agent:intent-due:0000000000000001:legacy", {"key": "agent:intent:legacy"})
    payload, digest = store.db.execute("SELECT payload,digest FROM subject_delivery_payloads").fetchone()
    assert "_history_record_storage" in payload
    assert len(payload.encode()) < len(value["text"].encode())
    assert json.loads(decoded_payload(payload, digest))["text"] == value["text"]
    before = pending_usage(store)
    from cryptowatcher.storage import encode

    assert before["canonical_binding_bytes"] == len(encode({"delivery_id": "codec"}).encode())
    assert before["legacy_binding_bytes"] == existing["legacy_binding_bytes"] + len(
        encode({"key": "agent:intent:legacy"}).encode()
    )
    assert before["binding_bytes"] == before["legacy_binding_bytes"] + before["canonical_binding_bytes"]
    assert before["canonical_payload_bytes"] == len(payload.encode())
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        transition(history, "outbox:telegram:codec", {**value, "state": "RETRY", "attempts": 1})
    assert tuple(store.db.execute("SELECT payload,digest FROM subject_delivery_payloads").fetchone()) == (
        payload,
        digest,
    )
    assert history.get("outbox:telegram:codec")["text"] == value["text"]
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        assert History(reader).get("outbox:telegram:codec")["text"] == value["text"]
    # Legacy plain representation is still readable; no conversion is required.
    assert decoded_payload('{"body":"raw"}', checksum('{"body":"raw"}')) == '{"body":"raw"}'
    assert packed_payload('{"body":"raw"}') == '{"body":"raw"}'
    with pytest.raises(StateError, match="Corrupt subject"):
        decoded_payload(payload, "0" * 64)


def test_missing_canonical_table_is_error_not_permission_to_prune(subject):
    store, history, event, repo = subject
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        repo.reserve(history, job_for(event), event)
        store.db.execute("ALTER TABLE subject_deliveries RENAME TO broken_deliveries")
        with pytest.raises(sqlite3.OperationalError):
            history._pending_parent_exists("outbox:telegram:unknown")


def test_pending_accounting_does_not_hide_missing_payload(subject):
    from cryptowatcher.storage import StateError

    store, history, event, repo = subject
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        telegram(history, event)
        history.put("pending:telegram:0000000000000001:codec", {"delivery_id": "codec"})
        store.db.execute("DELETE FROM subject_delivery_payloads")
        with pytest.raises(StateError, match="missing its payload"):
            pending_usage(store)


def test_plain_frozen_payload_keeps_representation_and_terminal_release_is_once(subject):
    from cryptowatcher.service_projections import transition

    store, history, event, repo = subject
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        value = telegram(history, event)
        payload, digest = store.db.execute("SELECT payload,digest FROM subject_delivery_payloads").fetchone()
        plain = decoded_payload(payload, digest)
        store.db.execute("DELETE FROM subject_delivery_payloads")
        store.db.execute(
            "INSERT INTO subject_delivery_payloads VALUES(?,?,?,1)",
            ("outbox:telegram:codec", plain, checksum(plain)),
        )
        history.put("outbox:telegram:codec", value)
        assert store.db.execute("SELECT payload FROM subject_delivery_payloads").fetchone()[0] == plain
        transition(history, "outbox:telegram:codec", {**value, "state": "DELIVERED"})
        assert "text" not in history.get("outbox:telegram:codec")
        writes = repo.stats["payload_writes"]
        transition(history, "outbox:telegram:codec", {**value, "state": "DELIVERED"})
        assert repo.stats["payload_writes"] == writes
