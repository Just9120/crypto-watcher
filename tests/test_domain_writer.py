"""Typed writer correctness checks; local small cases are not capacity evidence."""

import json
from contextlib import closing
from unittest.mock import patch

import pytest

from cryptowatcher.config import OwnerSettings
from cryptowatcher.events import EventEngine
from cryptowatcher.market_storage import enable
from cryptowatcher.storage import StateError, Store, encode
from tests import domain_writer as unit
from tests import sqlite_repository as repository
from tests.test_events import NOW, window


def sample(path, candidate):
    with (
        unit.installed(candidate) as stats,
        repository.repository_context(True),
        closing(Store(path, 233382575)) as store,
    ):
        store.initialize(OwnerSettings())
        enable(store.db)
        store.bounded = True
        store.clock_ms = lambda: NOW
        repository.install(store)
        engine = EventEngine(store)
        for market, tf, at, price in [
            ("spot", "1m", NOW, 104),
            ("spot", "3m", NOW + 180000, 110),
            ("linear", "5m", NOW, 104),
            ("spot", "1m", NOW + 240000, 120),
        ]:
            engine.observe(f"{market}/BTCUSDT/{tf}", window(at, tf, price), 1e6, 0.01, at, live=True)
        actual = {
            p: engine.history.items(p)
            for p in ("event:", "episode:", "history:", "outbox:telegram:", "pending:telegram:")
        }
        assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
        if candidate:
            rows = store.db.execute("SELECT payload FROM prototype_episode_revisions").fetchall()
            assert rows and all(
                "confirmations" not in json.loads(r[0]) and "conditions" not in json.loads(r[0]) for r in rows
            )
            assert store.db.execute("SELECT COUNT(*) FROM domain_members").fetchone()[0] > 0
        return encode(actual), dict(stats)


def test_domain_golden_exact_facts_episodes_inputs_and_notifications(tmp_path):
    expected, _ = sample(tmp_path / "base.sqlite3", False)
    actual, stats = sample(tmp_path / "candidate.sqlite3", True)
    assert actual == expected
    assert stats["observations"] == stats["pure_transitions"] == 4
    assert stats["facts"] > 0 and stats["failed"] == 0


def test_pure_state_never_falls_back_to_history():
    state = unit.PreparedState({}, {"condition:s:price:UP"})
    assert state.get("condition:s:price:UP", {}) == {}
    with pytest.raises(StateError, match="Unprepared"):
        state.get("unknown")
    assert not hasattr(state, "db") and not hasattr(state.store, "db")


def test_rollback_and_corruption_are_authoritative(tmp_path):
    with (
        unit.installed(),
        repository.repository_context(True),
        closing(Store(tmp_path / "rollback.sqlite3", 233382575)) as store,
    ):
        store.initialize(OwnerSettings())
        enable(store.db)
        store.bounded = True
        store.clock_ms = lambda: NOW
        repository.install(store)
        engine = EventEngine(store)
        engine.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True)
        episode = engine.history.items("episode:")[0][1]
        key = f"episode:{episode['episode_id']}:{episode['revision']:010d}"
        assert engine.history.get(key) == episode
        before = list(store.db.iterdump())
        with pytest.raises(RuntimeError), store.db:
            engine.observe("linear/ETHUSDT/1m", window(), 1e6, 0.01, NOW, live=True, commit=False)
            raise RuntimeError("rollback")
        assert list(store.db.iterdump()) == before
        with pytest.raises(StateError, match="immutable"), store.db:
            engine.history.version(
                "episode", {**episode, "revision": episode["revision"] + 1, "reference_price": 1}
            )
        assert list(store.db.iterdump()) == before
        with store.db:
            store.db.execute("DROP TRIGGER domain_member_immutable")
            store.db.execute("UPDATE domain_members SET payload=? WHERE id=?", ("{}", episode["episode_id"]))
        with pytest.raises(StateError, match="corrupt"):
            engine.history.get(key)


async def test_small_complete_sqlite_flow_and_both_transports(tmp_path):
    from tests.processing_replay import replay

    baseline = await replay(
        tmp_path / "reference-joint.sqlite3", instruments=3, seconds=12, direct_sqlite=True, bounded_pool=True
    )
    with unit.installed() as stats:
        result = await replay(
            tmp_path / "joint.sqlite3",
            instruments=3,
            seconds=12,
            direct_sqlite=True,
            bounded_pool=True,
            domain_roles=True,
        )
    assert result["seed_sha256"] == baseline["seed_sha256"]
    assert result["offered_sha256"] == baseline["offered_sha256"]
    assert result["numeric_sha256"] == baseline["numeric_sha256"]
    assert result["producer_errors"] == result["worker_errors"] == result["receiver_errors"] == []
    assert result["offered_closed"] == result["accepted_closed"] > 0
    assert result["bounded_pool"]["global_expected_sha256"] == result["bounded_pool"]["global_actual_sha256"]
    assert result["bounded_pool"]["actual_outcome_ids"] == result["bounded_pool"]["selected_outcome_ids"]
    assert result["completed"] > 0 and result["due_pending"] == 0
    assert result["telegram_attempts"] > 0 and result["signed_agent_receipts"] > 0
    assert result["isolated_ingress"]["state"] == "COMPLETE" and result["isolated_ingress"]["joined"]
    assert stats["facts"] > 0 and stats["failed"] == 0


@pytest.fixture
def domain_core(tmp_path):
    with (
        unit.installed(),
        repository.repository_context(True),
        closing(Store(tmp_path / "contracts.sqlite3", 233382575)) as store,
    ):
        store.initialize(OwnerSettings())
        enable(store.db)
        store.bounded = True
        store.clock_ms = lambda: NOW
        repository.install(store)
        yield store


async def test_existing_unknown_ack_restart_uses_identical_bytes(domain_core):
    from tests.test_agent_delivery import test_restart_ambiguous_ack_reuses_identical_bytes_and_success_state

    await test_restart_ambiguous_ack_reuses_identical_bytes_and_success_state(domain_core)


def test_existing_consumer_failure_rolls_back_complete_source(domain_core):
    from tests.test_agent_delivery import (
        test_atomic_callback_failure_rolls_back_all_source_episode_and_outcome_admission,
    )

    test_atomic_callback_failure_rolls_back_all_source_episode_and_outcome_admission(domain_core)


def test_notify_rules_differ_only_by_bounded_snapshot_copy():
    import ast
    import inspect
    import textwrap

    original = inspect.getsource(EventEngine._notify).replace(
        "json.loads(encode(episode))", "freeze_episode(episode)"
    )
    candidate = inspect.getsource(unit.Kernel._notify)
    assert ast.dump(ast.parse(textwrap.dedent(original))) == ast.dump(ast.parse(textwrap.dedent(candidate)))


def test_kernel_has_no_writable_or_readable_database_handle(domain_core):
    seen = []
    original_prepare = unit.prepare

    def prepared(*args):
        value, snapshot = original_prepare(*args)
        original_get = value.get

        def get(key, default=None):
            assert not hasattr(value, "db") and not hasattr(value.store, "db")
            seen.append(key)
            return original_get(key, default)

        value.get = get
        return value, snapshot

    with patch.object(unit, "prepare", prepared):
        EventEngine(domain_core).observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True)
    assert seen


def test_readonly_projection_restart_and_maintenance(domain_core):
    import sqlite3

    engine = EventEngine(domain_core)
    engine.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True)
    event = engine.history.events()[0]
    episode = engine.history.get("episode-head:" + event["episode_id"])
    with closing(Store(domain_core.path, domain_core.owner_id, readonly=True)) as reader:
        history = unit.History(reader)
        assert history.get("episode-head:" + event["episode_id"]) == episode
        assert history.get("event-head:" + event["event_id"]) == event
        with pytest.raises(StateError, match="Readonly"):
            history.version("episode", episode)
        reader.db.set_authorizer(
            lambda action, table, *_: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and table == "domain_members"
                else sqlite3.SQLITE_OK
            )
        )
        with pytest.raises(sqlite3.DatabaseError):
            history.get("episode-head:" + event["episode_id"])
    assert unit.maintenance(domain_core, NOW)["state"] == "DIRECT_SQLITE_CORE"
    assert engine.history.get("episode-head:" + event["episode_id"]) == episode


@pytest.mark.parametrize("receipt_delay,expected_live", [(0, True), (70000, False)])
def test_ingress_clock_does_not_hide_processing_delay_or_make_late_arrival_live(
    domain_core, receipt_delay, expected_live
):
    from dataclasses import replace

    candles = [replace(c, source="websocket", received_ms=NOW + receipt_delay) for c in window()]
    engine = EventEngine(domain_core)
    engine.observe("spot/BTCUSDT/1m", candles, 1e6, 0.01, NOW + 70000, live=True)
    facts = engine.history.events()
    assert facts
    assert all(f["live"] is (expected_live or f["detector_id"] == "breakout") for f in facts)
    assert all(f["detection_ms"] == f["available_at_ms"] == NOW + 70000 for f in facts)
    assert all(f["receipt_ms"] == NOW + receipt_delay for f in facts)
    assert all(f["fresh_until_ms"] <= NOW + 120000 for f in facts)


def test_settings_owner_change_during_transition_rejects_whole_write(domain_core):
    engine = EventEngine(domain_core)
    original = engine.detector
    owner = domain_core.owner_id
    before = list(domain_core.db.iterdump())

    def detect(*args):
        domain_core.owner_id = owner + 1
        return original(*args)

    engine.detector = detect
    try:
        with pytest.raises(StateError, match="owner/settings"):
            engine.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True)
    finally:
        domain_core.owner_id = owner
    assert list(domain_core.db.iterdump()) == before


def test_pending_ingress_prevents_timer_from_overtaking_accepted_frames():
    from tests import domain_roles as role

    with role.LOCK:
        role.PENDING[123] = NOW
    try:
        assert role.watermark(NOW + 120000) == NOW
    finally:
        with role.LOCK:
            role.PENDING.clear()
    assert role.watermark(NOW + 120000) == NOW + 120000


def test_image_context_and_report_fit_are_explicit():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    copies = [
        line
        for line in (root / "tests/domain_writer.Dockerfile").read_text().splitlines()
        if line.startswith("COPY")
    ]
    assert copies == [
        "COPY tests/domain_writer.py tests/domain_roles.py tests/processing_replay.py /app/tests/"
    ]
    workflow = (root / ".github/workflows/ci.yml").read_text()
    assert (
        'cp tests/domain_writer.py tests/domain_roles.py tests/processing_replay.py "$OVERLAY/tests/"'
        in workflow
    )
    assert '--candidate-image "cw-domain:$REVISION"' in workflow
    # All10300 fact IDs/hashes/clocks and expected IDs fit the unchanged2MiB report guard.
    facts = ["|".join(("a" * 36, "b" * 64, "129999", "129999", "R", "0"))] * 10300
    expected = ["c" * 36] * 10300
    assert len(json.dumps({"actual": facts, "expected": expected}, indent=2).encode()) + 150000 < 2 * 1024**2


def test_consumer_cannot_see_later_confirmation_revision(domain_core):
    seen = []
    engine = None

    def published(event, episode):
        head = engine.history.get("episode-head:" + episode["episode_id"])
        assert head["revision"] == episode["revision"] == event["episode_revision"]
        assert head["confirmations"] == episode["confirmations"]
        seen.append(event["event_id"])

    engine = EventEngine(domain_core, on_event=lambda e: None, on_event_snapshot=published)
    engine.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True)
    assert len(seen) > 1


async def test_partial_frames_keep_socket_alive_without_becoming_closed_work():
    import asyncio
    from types import SimpleNamespace

    from tests import domain_roles as role

    calls = []

    class Wire:
        async def recv(self):
            calls.append(1)
            if len(calls) <= 3:
                return role.Heartbeat(NOW + len(calls) * 5000)
            raise asyncio.CancelledError

    market = SimpleNamespace(
        connections={"group": {}},
        accept_message=lambda *a, **k: pytest.fail("Heartbeat reached detector/source writer"),
    )
    with pytest.raises(asyncio.CancelledError):
        await role.receive(market, Wire(), "group", "spot", set())
    assert market.connections["group"]["last_received_ms"] == NOW + 15000


def test_large_identity_evidence_keeps_core_report_small_and_validated(tmp_path):
    import hashlib

    from scripts.qualify_processing import save_fact_evidence

    result = {
        "bounded_pool": {
            "global_fact_evidence": ["a" * 160] * 12000,
            "global_expected_ids": ["b" * 36] * 10300,
            "global_actual": 12000,
            "global_expected": 10300,
        }
    }
    save_fact_evidence(result, tmp_path)
    meta = result["fact_evidence"]
    raw = (tmp_path / meta["file"]).read_bytes()
    assert meta["sha256"] == hashlib.sha256(raw).hexdigest()
    assert meta["actual_count"] == 12000 and meta["expected_count"] == 10300
    assert len(json.dumps(result)) < 1000
    assert json.loads(raw)["actual"] == ["a" * 160] * 12000
    with pytest.raises(FileExistsError):
        save_fact_evidence(
            {"bounded_pool": {"global_fact_evidence": [], "global_expected_ids": []}}, tmp_path
        )


def test_external_context_is_only_captured_for_meaningful_changes(domain_core):
    from cryptowatcher.detectors import Detection

    calls = []

    def provider():
        calls.append(1)
        return {}, {}

    silent = EventEngine(
        domain_core, detector=lambda family, *a: Detection(family, False, "NEUTRAL", 0, {}, ())
    )
    silent.observe("linear/ETHUSDT/1m", window(), 1e6, 0.01, NOW, live=True, context_provider=provider)
    assert calls == []
    engine = EventEngine(domain_core)
    engine.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True, context_provider=provider)
    assert calls == [1]
    assert all(e["source_context"] == {"ticker": None, "instrument": None} for e in engine.history.events())
    assert all(
        e["enrichment"] == {"state": "UNAVAILABLE", "detector_input": False} for e in engine.history.events()
    )
