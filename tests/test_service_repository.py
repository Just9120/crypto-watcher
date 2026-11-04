"""Real SQLite subject job/delivery contracts; no runtime or VPS activation."""

import json
import sqlite3
from contextlib import closing

import pytest

from cryptowatcher.domain_repository import History
from cryptowatcher.domain_repository import install as install_domain
from cryptowatcher.events import EventEngine, identity
from cryptowatcher.service_repository import install, validate
from cryptowatcher.storage import StateError
from tests.test_events import NOW, window
from tests.test_runtime_repository import opened


@pytest.fixture
def subject(tmp_path):
    with closing(opened(tmp_path / "subject.sqlite3", True)) as store:
        install_domain(store)
        history = History(store)
        engine = EventEngine(store)
        engine.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True)
        event = next(v for _, v in history.items("event:") if v["detector_id"] == "price")
        repository = install(store)
        yield store, history, event, repository


def job_for(event):
    identifier = identity("subject-test-outcome")
    from cryptowatcher.outcomes import pending_key

    due = event["reference_time_ms"] + 5 * 60_000
    return dict(
        outcome_id=identifier,
        event_id=event["event_id"],
        event_revision=event["revision"],
        horizon_min=5,
        methodology_version="2",
        stream="spot/BTCUSDT/1m",
        state="PENDING",
        next_attempt_ms=due,
        due_ms=due,
        pending_key=pending_key(due, identifier),
        hold_key=f"hold:spot/BTCUSDT/1m:{identifier}",
    )


def test_canonical_job_exact_origin_and_savepoint_rollback(subject):
    store, history, event, repo = subject
    job = job_for(event)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        assert repo.reserve(history, job, event)
        assert repo.get_job(job["outcome_id"]) == job
        assert repo.pending_jobs(4) == [(job["pending_key"], {"outcome_id": job["outcome_id"]})]
        store.db.execute("SAVEPOINT inside")
        changed = {**job, "state": "INCOMPLETE", "next_attempt_ms": job["due_ms"] + 60_000, "reason": "GAP"}
        repo.update_job(changed)
        assert repo.get_job(job["outcome_id"])["reason"] == "GAP"
        store.db.execute("ROLLBACK TO inside")
        store.db.execute("RELEASE inside")
        assert repo.get_job(job["outcome_id"]) == job
    with closing(sqlite3.connect(store.path)) as reader:
        assert reader.execute("SELECT COUNT(*) FROM subject_outcomes").fetchone()[0] == 1
        assert (
            reader.execute(
                "SELECT COUNT(*) FROM runtime WHERE key LIKE 'radar:v1:outcome-pending:%'"
            ).fetchone()[0]
            == 0
        )
    with pytest.raises(sqlite3.IntegrityError), store.db:
        store.db.execute("UPDATE subject_outcomes SET due_ms=due_ms+1")
    with pytest.raises(StateError, match="immutable"), store.db:
        store.db.execute("BEGIN IMMEDIATE")
        repo.update_job({**job, "event_revision": job["event_revision"] + 1})
    assert repo.get_job(job["outcome_id"]) == job


def test_reservation_does_not_move_late_reference_or_accept_horizon_over72h(subject):
    store, history, event, repo = subject
    for changed in ({"due_ms": NOW + 999}, {"horizon_min": 4321}):
        with pytest.raises(StateError), store.db:
            store.db.execute("BEGIN IMMEDIATE")
            repo.reserve(history, {**job_for(event), **changed}, event)
    assert store.db.execute("SELECT COUNT(*) FROM subject_outcomes").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM subject_sources").fetchone()[0] == 0


def test_frozen_delivery_status_does_not_rewrite_payload_and_errors_rollback(subject):
    store, history, event, repo = subject
    key = "agent:outbox:" + identity("subject-delivery")
    mutable = {
        "delivery_id": key.rsplit(":", 1)[1],
        "event_id": event["event_id"],
        "episode_id": event["episode_id"],
        "state": "PENDING",
        "next_ms": NOW,
        "attempts": 0,
    }
    payload = {
        "body": "immutable-body",
        "body_sha256": "hash",
        "intent": {"event_id": event["event_id"]},
        "notice": {},
    }
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        assert repo.write_delivery(
            history,
            key,
            {**mutable, **payload},
            channel="agent",
            stage="outbox",
            event_id=event["event_id"],
            revision=event["revision"],
            historical=False,
            due_ms=NOW,
            expires_ms=event["fresh_until_ms"],
            frozen=True,
        )
    before = tuple(store.db.execute("SELECT payload,digest FROM subject_delivery_payloads").fetchone())
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        assert repo.update_delivery(key, {**mutable, "attempts": 1, "state": "IN_FLIGHT"})
    assert repo.get_delivery(key) == {**mutable, **payload, "attempts": 1, "state": "IN_FLIGHT"}
    assert (
        tuple(store.db.execute("SELECT payload,digest FROM subject_delivery_payloads").fetchone()) == before
    )
    assert repo.stats["payload_writes"] == 1 and repo.stats["status_only_updates"] == 1
    with pytest.raises(StateError, match="payload fields"), store.db:
        store.db.execute("BEGIN IMMEDIATE")
        repo.update_delivery(key, {**mutable, **payload})
    with pytest.raises(sqlite3.IntegrityError, match="retry payload"), store.db:
        store.db.execute("BEGIN IMMEDIATE")
        repo.write_delivery(
            history,
            key,
            {**mutable, **payload, "body": "changed"},
            channel="agent",
            stage="outbox",
            event_id=event["event_id"],
            revision=event["revision"],
            historical=False,
            due_ms=NOW,
            expires_ms=event["fresh_until_ms"],
            frozen=True,
        )
    assert repo.get_delivery(key)["body"] == "immutable-body"
    assert repo.get_delivery(key)["state"] == "IN_FLIGHT"


def test_schema_corruption_and_owner_fail_closed(subject):
    store, history, event, repo = subject
    with pytest.raises(StateError, match="owner"), store.db:
        store.db.execute("BEGIN IMMEDIATE")
        store.owner_id += 1
        repo.reserve(history, job_for(event), event)
    store.owner_id -= 1
    with store.db:
        store.db.execute("DROP TRIGGER subject_payload_frozen")
    with pytest.raises(StateError, match="schema"):
        validate(store)
    assert history.get(f"event:{event['event_id']}:{event['revision']:010d}")["event_id"] == event["event_id"]


def test_existing_history_not_converted_and_new_job_survives_reopen(subject):
    store, history, event, repo = subject
    before = [tuple(r) for r in store.db.execute("SELECT key,payload_json FROM runtime ORDER BY key")]
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        repo.reserve(history, job_for(event), event)
    after = [tuple(r) for r in store.db.execute("SELECT key,payload_json FROM runtime ORDER BY key")]
    assert before == after
    with closing(sqlite3.connect(store.path)) as other:
        assert other.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert other.execute("PRAGMA foreign_key_check").fetchall() == []
        row = other.execute("SELECT due_ms,state,extra FROM subject_outcomes").fetchone()
        assert row[:2] == (job_for(event)["due_ms"], "PENDING") and json.loads(row[2]) == {}


async def test_subject_services_run_actual_runtime_ingestion_outcomes_and_both_deliveries(tmp_path):
    from tests.processing_replay import replay

    result = await replay(tmp_path / "joint.sqlite3", instruments=3, seconds=8, subject_services=True)
    pool = result["bounded_pool"]
    assert pool["global_expected_sha256"] == pool["global_actual_sha256"]
    assert pool["actual_outcome_ids"] == pool["selected_outcome_ids"]
    assert result["telegram_attempts"] and result["signed_agent_receipts"]
    assert result["completed"] > 0 and result["future_jobs"] > 0
    assert result["producer_errors"] == result["worker_errors"] == result["receiver_errors"] == []
    report = result["subject_services"]
    assert report["foreign_keys"] == []
    assert report["accounting"]["state"] == "MEASURED_FLOW"
    assert report["operations"]["job_reservations"] > 0
    assert report["operations"]["attempt_writes"] > 0
    assert report["operations"]["status_only_updates"] > 0


@pytest.mark.parametrize(
    "scenario",
    [
        "test_restart_ambiguous_ack_reuses_identical_bytes_and_success_state",
        "test_expired_retry_is_explicit_historical_revision_without_freshening",
        "test_config_race_and_future_clock_do_not_publish_live_old_revision",
        "test_atomic_admission_coalescing_and_telegram_independence",
    ],
)
async def test_existing_agent_contract_on_subject_repository(tmp_path, scenario):
    from tests import test_agent_delivery as existing

    with closing(opened(tmp_path / "agent.sqlite3", True)) as store:
        install_domain(store)
        install(store)
        await getattr(existing, scenario)(store)
        assert store.db.execute("SELECT COUNT(*) FROM subject_attempts").fetchone()[0] > 0
        assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_subject_mixed_pagination_numeric_due_readonly_reopen_and_exact_meter(subject):
    from cryptowatcher.outcomes import pending_key
    from cryptowatcher.repository_meter import size_sql
    from cryptowatcher.service_repository import TABLES
    from cryptowatcher.storage import Store

    store, history, event, repo = subject
    job = job_for(event)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        repo.reserve(history, job, event)
        history.put("outcome-pending:0001:1:old", {"outcome_id": "old"})
    expected = [
        ("outcome-pending:0001:1:old", {"outcome_id": "old"}),
        (job["pending_key"], {"outcome_id": job["outcome_id"]}),
    ]
    assert history.get("outcome-pending:0000000000000001:legacy") is None
    assert history.items("outcome-pending:") == expected
    assert history.items("outcome-pending:", 1, reverse=True) == expected[-1:]
    assert list(store.scan_runtime("radar:v1:outcome-pending:", after="radar:v1:" + expected[0][0])) == [
        ("radar:v1:" + expected[1][0], expected[1][1])
    ]
    with store.db:
        history.delete(job["pending_key"])
        changed = {
            **job,
            "next_attempt_ms": job["due_ms"] + 60_000,
            "pending_key": pending_key(job["due_ms"] + 60_000, job["outcome_id"]),
            "state": "INCOMPLETE",
            "reason": "GAP",
        }
        history.put(changed["pending_key"], {"outcome_id": job["outcome_id"]})
        history.put("outcome-job:" + job["outcome_id"], changed)
    assert history.get("outcome-job:" + job["outcome_id"]) == changed
    assert history.items("analytics-blocked:outcome:")
    for table, category in TABLES.items():
        columns = [row[1] for row in store.db.execute("PRAGMA table_info(" + table + ")")]
        actual = tuple(
            store.db.execute(
                "SELECT COUNT(*),COALESCE(SUM(" + size_sql(columns) + "),0) FROM " + table
            ).fetchone()
        )
        saved = tuple(
            store.db.execute(
                "SELECT records,logical_bytes FROM market_counts WHERE category=?", (category,)
            ).fetchone()
        )
        assert actual == saved
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        assert reader.services is not None
        assert History(reader).get("outcome-job:" + job["outcome_id"]) == changed
        with pytest.raises(StateError, match="permissions"):
            reader.services.update_job(changed)
        with pytest.raises(sqlite3.OperationalError):
            reader.db.execute("DELETE FROM subject_outcomes")


def test_typed_lease_is_used_by_candle_recovery_and_retired_after_archive(subject):
    from types import SimpleNamespace

    from cryptowatcher.analytics_recovery import AnalyticsRecovery
    from cryptowatcher.market_storage import candle_deadline
    from cryptowatcher.service_retention import retire, work
    from cryptowatcher.storage_lifecycle import DAY

    store, history, event, repo = subject
    job = job_for(event)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        repo.reserve(history, job, event)
    row = store.db.execute("SELECT stream,hold_start,hold_end,expires_ms FROM subject_outcomes").fetchone()
    sid, start, end, expiry = row
    assert (
        AnalyticsRecovery(store, SimpleNamespace()).owner_deadline(job["stream"], start, end, NOW) == expiry
    )
    assert candle_deadline(store.db, sid, start, 60_000, 0, NOW, 0, 0) >= min(expiry, NOW + 3600000)
    # Generic service cleanup cannot delete a valid accepted future obligation.
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        work(repo, NOW + DAY)
    assert repo.get_job(job["outcome_id"]) == job
    terminal = []
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        retire(repo, event["event_id"], lambda category, state: terminal.append((category, state)))
    assert terminal == [("outcome-job", "PENDING")]
    assert repo.get_job(job["outcome_id"]) is None
    assert store.db.execute("SELECT COUNT(*) FROM subject_sources").fetchone()[0] == 0
    assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_callback_sees_committed_unit_and_outer_rollback_removes_subject_state(tmp_path):
    from cryptowatcher.agent_delivery import AgentDelivery
    from cryptowatcher.events import EventEngine
    from tests.test_agent_delivery import ENV, AgentConfig

    with closing(opened(tmp_path / "callback.sqlite3", True)) as store:
        install_domain(store)
        install(store)
        agent = AgentDelivery(store, None, AgentConfig.from_env(ENV))
        agent.enable(True)
        seen = []

        def observe(event, episode):
            agent.admit(event, episode=episode)

            # A normal callback sees the actual canonical rows and the same
            # public History projection; no pending in-memory service overlay.
            def inspect():
                saved = agent.history.items("agent:intent:")
                actual = store.db.execute(
                    "SELECT event_id FROM subject_deliveries WHERE stage='intent'"
                ).fetchall()
                assert saved and actual
                seen.append(len(actual))

            inspect()

        engine = EventEngine(store, on_event_snapshot=observe)
        before = list(store.db.iterdump())
        with pytest.raises(RuntimeError, match="rollback source"), store.db:
            store.db.execute("BEGIN IMMEDIATE")
            engine.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True, commit=False)
            raise RuntimeError("rollback source")
        assert seen
        assert list(store.db.iterdump()) == before


def test_caught_subject_operation_failure_does_not_commit_partial_state(subject):
    store, history, event, repo = subject
    job = job_for(event)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        before = list(store.db.iterdump())
        with pytest.raises(StateError):
            repo.reserve(history, {**job, "due_ms": job["due_ms"] + 1}, event)
        assert list(store.db.iterdump()) == before
        repo.reserve(history, job, event)
        # A failed immutable-body rewrite cannot partially commit a new state.
        key = "agent:outbox:" + identity("caught-service")
        record = dict(delivery_id=key.rsplit(":", 1)[1], state="PENDING", next_ms=NOW, body="first")
        kwargs = dict(
            channel="agent",
            stage="outbox",
            event_id=event["event_id"],
            revision=event["revision"],
            historical=False,
            due_ms=NOW,
            expires_ms=event["fresh_until_ms"],
            frozen=True,
        )
        repo.write_delivery(history, key, record, **kwargs)
        before = list(store.db.iterdump())
        with pytest.raises(sqlite3.IntegrityError):
            repo.write_delivery(history, key, {**record, "state": "IN_FLIGHT", "body": "changed"}, **kwargs)
        assert list(store.db.iterdump()) == before
    assert repo.get_delivery(key) == record
