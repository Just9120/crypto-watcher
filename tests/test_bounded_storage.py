from dataclasses import asdict

import pytest

from cryptowatcher.events import History
from cryptowatcher.history_codec import MARKER, Codec
from cryptowatcher.market_storage import enable, read
from cryptowatcher.outcomes import OutcomeWorker, calculate, read_candles
from cryptowatcher.sources import Sources
from cryptowatcher.storage import Store, encode, json_payload

from .test_outcomes import MINUTE, NOW, future, observe


def bounded(store):
    enable(store.db)
    store.bounded = True
    store.clock_ms = lambda: NOW
    return store


def test_normalized_overlapping_windows_replay_and_restart_keep_exact_analytics(store):
    bounded(store)
    worker = OutcomeWorker(store)
    observe(store, worker)
    history = History(store)
    original = history.events(20)
    assert original
    count = store.db.execute("SELECT count(*) FROM market_candles").fetchone()[0]
    assert 0 < count <= 24
    observe(store, worker)
    assert history.events(20) == original
    assert store.db.execute("SELECT count(*) FROM market_candles").fetchone()[0] == count
    sources = Sources(store)
    sources.put("spot/BTCUSDT/1m", future())
    worker.work(NOW + 6 * MINUTE)
    first = history.events(1)[0]
    for _, outcome in history.items("outcome-head:"):
        if outcome["horizon_min"] == 5:
            expected = calculate(history.get("event-head:" + outcome["event_id"]), 5, future())
            assert outcome["metrics"] == expected["metrics"]
    stored = store.get_runtime("radar:v1:event:" + first["event_id"] + ":0000000001")
    reference = stored[MARKER]["fields"]["source_inputs"]
    assert reference["mode"] == "source-ref" and type(reference["id"]) is int
    assert (
        store.db.execute("SELECT refs FROM market_source_windows WHERE id=?", (reference["id"],)).fetchone()[
            0
        ]
        > 0
    )
    assert all(
        "source_inputs" not in json_payload(r[0])
        for r in store.db.execute("SELECT payload_json FROM runtime WHERE key LIKE 'radar:v1:event:%'")
    )
    fresh = Store(store.path, store.owner_id, readonly=True)
    try:
        assert History(fresh).events(20) == history.events(20)
        assert [asdict(c) for c in read_candles(fresh, "spot/BTCUSDT/1m", NOW, NOW + 5 * MINUTE)] == [
            asdict(c) for c in future()
        ]
        assert fresh.db.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        fresh.close()


def test_source_reference_preserves_used_revision_when_recovery_changes_current_values(store):
    bounded(store)
    codec = Codec(store.db)
    codec.source_stream = "spot/BTCUSDT/1m"
    codec.source_expiry = NOW + 30 * 86400000
    with store.db:
        packed = codec.pack({"source_inputs": [asdict(c) for c in future()]})
    originals = codec.unpack(packed)
    values = future()
    with store.db:
        from cryptowatcher.market_storage import admit

        changed = {**asdict(values[0]), "received_ms": values[0].received_ms + 1}
        admit(store.db, "spot/BTCUSDT/1m", changed)
    assert read(store.db, "spot/BTCUSDT/1m", NOW, NOW + MINUTE)[0] == changed
    assert encode(codec.unpack(packed)) == encode(originals)


def test_retention_keeps_exact_cohorts_and_method_settings_after_detail_expiry(store):
    from cryptowatcher.analytics import cohorts
    from cryptowatcher.outcomes import DAY, Retention

    bounded(store)
    worker = OutcomeWorker(store)
    observe(store, worker)
    Sources(store).put("spot/BTCUSDT/1m", future())
    worker.work(NOW + 6 * MINUTE)
    before = cohorts(store)
    assert before["cohorts"]
    retention = Retention(store)
    retention.work(NOW + 30 * DAY - 1)
    assert History(store).events(20)
    # One bounded timer slice need not retire every eligible parent.
    for _ in range(100):
        retention.work(NOW + 30 * DAY + 1)
        if not History(store).events(1):
            break
    assert not History(store).events(20)
    after = cohorts(store)
    assert after["cohorts"] == before["cohorts"]
    assert after["incomplete_outcomes"] == before["incomplete_outcomes"]
    assert store.db.execute("SELECT sum(n) FROM market_quality").fetchone()[0] > 0
    assert History(store).count("protected:") == 0
    # Retry of cleanup is idempotent, no duplicate summary admissions.
    total = store.db.execute("SELECT sum(n) FROM market_quality").fetchone()[0]
    retention.work(NOW + 31 * DAY)
    assert store.db.execute("SELECT sum(n) FROM market_quality").fetchone()[0] == total
    retention.work(NOW + 36 * DAY)
    assert store.db.execute("SELECT count(*) FROM market_candles").fetchone()[0] == 0
    retention.work(NOW + 400 * DAY)
    assert store.db.execute("SELECT count(*) FROM market_quality").fetchone()[0] == 0


def test_reset_preparation_preserves_private_configuration_and_refuses_repeat(store, tmp_path):
    import pytest

    from cryptowatcher.reset_history import prepare, private_state, signature
    from cryptowatcher.storage import StateError

    with store.db:
        store.put_runtime(
            "radar:v1:digest-preferences", {"enabled": True, "next_end_ms": 10, "changed_ms": 1}
        )
        store.put_runtime("radar:v1:agent:preference", {"enabled": False, "schema": 1})
        store.put_runtime("old-market-event", {"history": "discardable test"})
    store.mute("spot/BTCUSDT", NOW / 1000 + 10000)
    preserved = private_state(store)
    source_bytes = store.path.read_bytes()
    candidate = (tmp_path / "fresh.sqlite3").resolve()
    receipt = prepare(
        store.path.resolve(), candidate, (tmp_path / "private.json").resolve(), store.owner_id, now_ms=NOW
    )
    assert receipt["private_source_sha256"] == signature(preserved)
    assert store.path.read_bytes() == source_bytes
    restored = Store(candidate, store.owner_id)
    try:
        assert restored.snapshot() == store.snapshot()
        assert restored.enabled() == store.enabled()
        assert list(map(tuple, restored.db.execute("SELECT * FROM mutes"))) == list(
            map(tuple, store.db.execute("SELECT * FROM mutes"))
        )
        assert restored.get_runtime("old-market-event") is None
        assert restored.get_runtime("radar:v1:digest-preferences")["enabled"] is True
        assert restored.get_runtime("radar:v1:digest-preferences")["next_end_ms"] > NOW
        assert restored.get_runtime("history_generation")["started_ms"] == NOW
        with pytest.raises(StateError, match="already performed"):
            prepare(
                candidate,
                (tmp_path / "again.sqlite3").resolve(),
                (tmp_path / "again.json").resolve(),
                store.owner_id,
            )
    finally:
        restored.close()
    assert not (tmp_path / "again.sqlite3").exists()


def test_compact_context_reuses_stable_metadata_and_freezes_causal_snapshot(store):
    from cryptowatcher.source_context import at, save

    bounded(store)
    codec = Codec(store.db)
    codec.source_stream = "spot/BTCUSDT/1m"
    codec.source_expiry = NOW
    value = {
        "schema": "metadata",
        "tick_size": 0.01,
        "available_at_ms": NOW - 60000,
        "market_observation_ms": NOW - 60000,
        "metadata": {"base": "BTC", "details": "same" * 300},
    }
    with store.db:
        save(store, "instrument", "spot/BTCUSDT", value)
        save(
            store,
            "instrument",
            "spot/BTCUSDT",
            {**value, "available_at_ms": NOW, "market_observation_ms": NOW},
        )
        packed = codec.pack({"source_context": {"instrument": value, "ticker": None}})
    assert store.db.execute("SELECT count(*) FROM market_context_bodies").fetchone()[0] == 1
    assert codec.unpack(packed)["source_context"]["instrument"] == value
    assert at(store, "instrument", "spot/BTCUSDT", NOW - 1) == value
    assert at(store, "instrument", "spot/BTCUSDT", NOW)["available_at_ms"] == NOW
    assert "metadata" not in encode(packed)


def test_meter_counters_are_atomic_and_sampling_does_not_walk_large_tables(store):
    import pytest

    from cryptowatcher.storage_meter import report, sample

    bounded(store)
    Sources(store).put("spot/BTCUSDT/1m", future())
    before = report(store, NOW)
    assert before["categories"]["candles"]["records"] == 5
    with pytest.raises(RuntimeError), store.db:
        from cryptowatcher.market_storage import admit

        admit(store.db, "linear/BTCUSDT/1m", asdict(future()[0]))
        raise RuntimeError("rollback")
    assert report(store, NOW)["categories"]["candles"]["records"] == 5
    sample(store, NOW)
    sample(store, NOW + 60000)
    assert report(store, NOW + 60000)["sample_seconds"] == 60
    assert report(store, NOW + 60000)["projected_working_bytes"] is not None
    assert store.get_runtime("storage_index_measurement")["state"] in ("PASS", "UNAVAILABLE")


def test_generation_boundary_does_not_recreate_bootstrap_events_episodes_or_delivery(store):
    bounded(store)
    store.put_runtime("history_generation", {"started_ms": NOW + 1})
    worker = OutcomeWorker(store)
    observe(store, worker)
    history = History(store)
    assert history.events(20) == []
    assert history.count("episode:") == 0
    assert history.count("outbox:telegram:") == 0
    observe(store, worker, NOW + MINUTE)
    assert history.events(20)


async def test_bounded_signed_delivery_and_telegram_independence(store):
    from .test_agent_delivery import test_atomic_admission_coalescing_and_telegram_independence as scenario

    bounded(store)
    await scenario(store)


async def test_bounded_signed_retry_restart_reuses_identical_bytes(store):
    from .test_agent_delivery import (
        test_restart_ambiguous_ack_reuses_identical_bytes_and_success_state as scenario,
    )

    bounded(store)
    await scenario(store)


async def test_bounded_signed_late_outcome_preserves_causal_context(store):
    from .test_agent_delivery import test_episode_closure_and_late_outcome_keep_original_context as scenario

    bounded(store)
    await scenario(store)


def test_finished_jobs_are_not_recreated_by_bootstrap_and_owner_can_recompare(store):
    from cryptowatcher.analytics import ComparisonWorker

    bounded(store)
    worker = OutcomeWorker(store)
    observe(store, worker)
    Sources(store).put("spot/BTCUSDT/1m", future())
    worker.work(NOW + 6 * MINUTE)
    comparison = ComparisonWorker(store)
    eid = History(store).events(1)[0]["event_id"]
    for _ in range(10):
        comparison.work(NOW + 6 * MINUTE)
    assert History(store).get("comparison-job:" + eid) is None
    completed = History(store).count("outcome-head:")
    with store.db:
        History(store).delete("outcome-admission")
    worker.bootstrap(100)
    assert all(v["horizon_min"] != 5 for _, v in History(store).items("outcome-job:"))
    assert History(store).count("outcome-head:") == completed
    assert comparison.requeue(eid, NOW + 7 * MINUTE) == "PENDING"
    assert History(store).get("comparison-job:" + eid)["event_revision"] == 1


@pytest.mark.parametrize("fault", ["trailer", "checksum", "size", "hash", "dictionary", "extra"])
def test_binary_records_refuse_corruption_and_preserve_original_json_types(fault):
    import pytest

    from cryptowatcher.record_codec import binary_payload, pack_binary
    from cryptowatcher.storage import StateError

    value = {"number": 0.0175454454105349, "integer": 2, "real": 2.0, "notes": "история" * 40}
    raw = encode(value)
    payload = pack_binary(raw)
    assert binary_payload(payload) == raw
    if fault == "trailer":
        damaged = payload[:-1]
    elif fault == "checksum":
        damaged = payload[:-1] + bytes([payload[-1] ^ 1])
    elif fault == "hash":
        damaged = payload[:4] + bytes([payload[4] ^ 1]) + payload[5:]
    elif fault == "dictionary":
        damaged = b"CW9\0" + payload[4:]
    elif fault == "size":
        damaged = payload[:36] + b"bad-frame"
    else:
        damaged = payload + b"extra"
    with pytest.raises(StateError):
        binary_payload(damaged)


def test_retention_archive_failure_rolls_back_details_and_aggregates(store, monkeypatch):
    import pytest

    import cryptowatcher.quality_summary as quality
    from cryptowatcher.outcomes import DAY, Retention

    bounded(store)
    worker = OutcomeWorker(store)
    observe(store, worker)
    before = History(store).events(100)
    real = quality.archive_event

    def fail(*args):
        real(*args)
        raise RuntimeError("injected aggregate failure")

    monkeypatch.setattr(quality, "archive_event", fail)
    with pytest.raises(RuntimeError):
        Retention(store).work(NOW + 31 * DAY)
    assert History(store).events(100) == before
    assert store.db.execute("SELECT count(*) FROM market_quality").fetchone()[0] == 0


def test_summary_expiry_is_twelve_calendar_months_and_context_inputs_are_leased(store):
    from datetime import datetime, timezone

    from cryptowatcher.outcomes import DAY, Retention
    from cryptowatcher.quality_summary import month_boundary
    from cryptowatcher.source_context import save

    assert (
        month_boundary(int(datetime(2024, 2, 29, tzinfo=timezone.utc).timestamp()) // 86400, 12)
        == int(datetime(2025, 2, 28, tzinfo=timezone.utc).timestamp()) // 86400
    )
    bounded(store)
    value = {"available_at_ms": NOW - 28 * DAY, "market_observation_ms": NOW - 28 * DAY, "tick_size": 0.01}
    codec = Codec(store.db)
    codec.source_stream = "spot/BTCUSDT/1m"
    codec.source_expiry = NOW
    with store.db:
        save(store, "instrument", "spot/BTCUSDT", value)
        reference = codec.pack({"source_context": {"instrument": value, "ticker": None}})
    Retention(store).work(NOW + 8 * DAY)
    assert codec.unpack(reference)["source_context"]["instrument"] == value
    Retention(store).work(NOW + 31 * DAY)
    assert store.db.execute("SELECT count(*) FROM market_context").fetchone()[0] == 0


def test_forecast_includes_annual_quality_before_first_retention_pass(store):
    from cryptowatcher.storage_meter import report, sample

    bounded(store)
    sample(store, NOW - 60000)
    worker = OutcomeWorker(store)
    observe(store, worker)
    sample(store, NOW)
    value = report(store, NOW)
    assert value["observed_events_total"] > 0
    assert value["observed_events_per_day_extrapolated"] > 0
    quality = next(row for row in value["forecast"] if row["category"] == "quality")
    assert quality["model_applied"] is True
    assert quality["projected_logical_bytes"] > 0
    assert value["quality_forecast_model"]["actual_production_measurement"] is False


def test_pending_delivery_expires_explicitly_without_retaining_event_or_sources(store):
    from cryptowatcher.outcomes import DAY, Retention

    bounded(store)
    observe(store, OutcomeWorker(store))
    h = History(store)
    eid = h.events(1)[0]["event_id"]
    with store.db:
        h.put("test-pending:delivery", {"state": "RETRY", "event_id": eid, "attempts": 3})
    Retention(store).work(NOW + 30 * DAY + 1)
    assert h.get("test-pending:delivery") is None
    row = store.db.execute("SELECT value FROM market_meta WHERE key='expired_work'").fetchone()
    assert json_payload(row[0])["test-pending:RETRY->EXPIRED_RETENTION_INCOMPLETE"] == 1
    Retention(store).work(NOW + 36 * DAY)
    assert store.db.execute("SELECT count(*) FROM market_candles").fetchone()[0] == 0
    assert store.db.execute("SELECT sum(n) FROM market_quality").fetchone()[0] > 0


def test_bounded_summary_preserves_nonzero_comparator_distributions_and_export(store):
    import json
    from contextlib import closing

    from cryptowatcher.analytics import cohorts
    from cryptowatcher.export import Selection, ndjson
    from cryptowatcher.outcomes import DAY, Retention

    from .test_export import populate

    bounded(store)
    store.update(store.snapshot().revision, store.snapshot().settings.changed(outcome_horizons=(5,)))
    # The export fixture is deliberately minimal; real comparisons carry this causal revision.
    from unittest.mock import patch

    original = History.version

    def version(h, kind, value):
        if kind == "comparison":
            value = {**value, "event_revision": 1}
        original(h, kind, value)

    with patch.object(History, "version", version):
        populate(store, 8)
    h = History(store)
    with store.db:
        for _, event in h.items("event-head:"):
            h.put(
                f"history:{event['market_observation_ms']:016d}:{event['event_id']}",
                {"event_id": event["event_id"]},
            )
    before = cohorts(store)["cohorts"]
    assert before[0]["comparator_sample_size"] == 8
    for _ in range(20):
        Retention(store).work(NOW + 31 * DAY)
        if not h.events(1):
            break
    assert not h.events(1)
    assert cohorts(store)["cohorts"] == before
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        exported = [json.loads(line) for line in ndjson(reader, Selection())]
    assert any(v["type"] == "quality_summary" for v in exported)
    assert not any(v.get("type") == "event" for v in exported)
    assert "SENTINEL_SECRET_DO_NOT_EXPORT" not in json.dumps(exported)


def test_expired_job_and_orphaned_indexes_retire_without_breaking_workers(store):
    from cryptowatcher.analytics_worker import prepare
    from cryptowatcher.outcomes import Retention

    bounded(store)
    h = History(store)
    with store.db:
        h.put("analytics-gap:gap", {"id": "gap", "state": "PENDING", "expires_ms": NOW + 1})
        h.put("analytics-gap-pending:1:gap", {"id": "gap"})
        h.put("outcome-pending:1:missing", {"outcome_id": "missing"})
        h.put("comparison-pending:1:missing", {"event_id": "missing"})
        h.put("pending:telegram:1:missing", {"delivery_id": "missing"})
        h.put("agent:send-due:1:missing", {"delivery_id": "missing"})
        h.put("digest-pending:missing", {"delivery_id": "missing"})
    assert (
        prepare(store, "outcome-pending:", "outcome-job:", "outcome_id", NOW, 20, 1, lambda *args: None) == []
    )
    Retention(store).work(NOW + 2)
    assert h.get("analytics-gap:gap") is None
    counts = h.get("orphaned-pending-summary")
    assert len(counts) == 6 and sum(counts.values()) == 6
    with store.db:
        assert h.prune_pending() == 0


@pytest.mark.parametrize(
    "state",
    [
        "EXPIRED_LIVE",
        "MUTED",
        "ALERTS_DISABLED",
        "SUPERSEDED",
        "DIGEST_DISABLED",
        "DELIVERED_PRIOR_PREFERENCE",
    ],
)
def test_terminal_delivery_receipts_drop_bodies_and_expire_while_event_remains(store, state):
    from cryptowatcher.outcomes import DAY, Retention

    bounded(store)
    h = History(store)
    observe(store, OutcomeWorker(store))
    eid = h.events(1)[0]["event_id"]
    with store.db:
        h.put(
            "outbox:telegram:done",
            {
                "state": state,
                "completed_ms": NOW,
                "event_id": eid,
                "body": "immutable delivered bytes",
                "intent": {"event_id": eid},
                "episode_snapshot": {"large": "snapshot"},
                "text": "rendered body",
                "body_sha256": "hash",
            },
        )
    receipt = h.get("outbox:telegram:done")
    assert (
        receipt["body_sha256"] == "hash"
        and not {"body", "intent", "episode_snapshot", "text"} & receipt.keys()
    )
    Retention(store).work(NOW + DAY + 1)
    assert h.get("outbox:telegram:done") is None and h.events(1)


def test_current_episode_pointer_renews_and_permanent_target_failure_is_not_auto_enabled(store):
    from cryptowatcher.outcomes import DAY, Retention

    bounded(store)
    h = History(store)
    with store.db:
        h.put("active:spot/BTCUSDT/UP", {"episode_id": "old"})
        h.put("target:telegram", {"state": "BLOCKED", "reason": "Forbidden"})
        store.db.execute(
            "INSERT INTO market_lifetimes VALUES (?,?,?,NULL)",
            ("radar:v1:target:telegram", NOW + 30 * DAY, "target"),
        )
    store.clock_ms = lambda: NOW + 29 * DAY
    with store.db:
        h.put("active:spot/BTCUSDT/UP", {"episode_id": "current"})
    Retention(store).work(NOW + 31 * DAY)
    assert h.get("active:spot/BTCUSDT/UP")["episode_id"] == "current"
    Retention(store).work(NOW + 400 * DAY)
    assert h.get("target:telegram")["state"] == "BLOCKED"


def test_retention_preserves_all_method_versions_and_frozen_strength_grouping(store, monkeypatch):
    from cryptowatcher import outcomes as outcome_module
    from cryptowatcher.analytics import cohort_rows
    from cryptowatcher.events import identity
    from cryptowatcher.outcomes import DAY, Retention
    from cryptowatcher.quality_packets import unpack as unpack_quality

    bounded(store)
    worker = OutcomeWorker(store)
    observe(store, worker)
    Sources(store).put("spot/BTCUSDT/1m", future())
    worker.work(NOW + 6 * MINUTE)
    h = History(store)
    original = h.items("outcome-head:")[0][1]
    source = h.get(f"event:{original['event_id']}:{original['event_revision']:010d}")
    with store.db:
        h.version(
            "event",
            {
                **source,
                "revision": 2,
                "available_at_ms": NOW + 7 * MINUTE,
                "strength": source["strength"] + 100,
            },
        )
        h.put(
            "outcome-method:2", {"version": "2", "description": "compatibility fixture; no new calculation"}
        )
        h.version(
            "outcome",
            {
                **original,
                "outcome_id": identity(f"outcome/{original['event_id']}/5/2"),
                "methodology_version": "2",
                "revision": 1,
                "available_at_ms": NOW + 7 * MINUTE,
            },
        )
    before = list(cohort_rows(store, ("strength", "settings", "version")))
    # Simulate deployment of a new default; retained v1/v2 records still define their methods.
    monkeypatch.setattr(outcome_module, "METHOD", "new-default")
    Retention(store).work(NOW + 31 * DAY)
    assert list(cohort_rows(store, ("strength", "settings", "version"))) == before
    rows = store.db.execute("SELECT dimensions,provenance,facts FROM market_quality").fetchall()
    assert {json_payload(r[0])["methodology_version"] for r in rows} == {"1", "2"}
    assert all(json_payload(r[0])["methodology_version"] != "new-default" for r in rows)
    facts = [v for r in rows for v in unpack_quality(store.db, bytes(r[2]))]
    assert source["strength"] + 100 not in [v["strength"] for v in facts]
    assert any(
        h.codec.unpack(json_payload(r[1]))["quality_provenance"]["outcome_definition"]
        == {"version": "2", "description": "compatibility fixture; no new calculation"}
        for r in rows
    )


def test_retention_keeps_all_comparator_versions_and_original_pending_method(store):
    from cryptowatcher.analytics import cohort_rows
    from cryptowatcher.events import identity
    from cryptowatcher.outcomes import DAY, Retention
    from cryptowatcher.quality_packets import unpack as unpack_quality

    bounded(store)
    worker = OutcomeWorker(store)
    observe(store, worker)
    Sources(store).put("spot/BTCUSDT/1m", future())
    worker.work(NOW + 6 * MINUTE)
    h = History(store)
    event = h.events(1)[0]
    with store.db:
        for version, value in (("1", -2), ("2", 3)):
            h.put("comparison-method:" + version, {"version": version})
            h.version(
                "comparison",
                {
                    "comparison_id": identity(f"comparison/{event['event_id']}/{version}"),
                    "event_id": event["event_id"],
                    "event_revision": 1,
                    "revision": 1,
                    "available_at_ms": NOW + 7 * MINUTE,
                    "methodology_version": version,
                    "samples": [
                        {
                            "outcomes": {
                                "5": {"metrics": {"price_return_pct": value, "directed_return_pct": value}}
                            }
                        }
                    ],
                    "limitations": ["fixture-" + version],
                },
            )
        h.put(
            "outcome-job:pending-old-method",
            {
                "outcome_id": "pending-old-method",
                "event_id": event["event_id"],
                "event_revision": 1,
                "methodology_version": "old-pending",
                "horizon_min": 10,
                "state": "INCOMPLETE",
            },
        )
    before = list(cohort_rows(store))
    # Drain this finite population without assuming one machine's slice speed.
    for _ in range(100):
        Retention(store).work(NOW + 31 * DAY)
        if not h.events(1):
            break
    assert not h.events(1)
    assert list(cohort_rows(store)) == before
    rows = store.db.execute("SELECT dimensions,provenance,facts FROM market_quality").fetchall()
    assert any(
        json_payload(r[0])["methodology_version"] == "old-pending" and json_payload(r[0])["horizon_min"] == 10
        for r in rows
    )
    relevant = [
        r
        for r in rows
        if h.codec.unpack(json_payload(r[1]))["quality_provenance"]["comparison_versions"] == ["1", "2"]
    ]
    assert relevant
    provenance = h.codec.unpack(json_payload(relevant[0][1]))["quality_provenance"]
    assert provenance["comparison_definitions"] == {"1": {"version": "1"}, "2": {"version": "2"}}
    facts = [v for r in relevant for v in unpack_quality(store.db, bytes(r[2]))]
    assert any(
        len(v["comparator_metrics"]) == 2 and v["limitations"] == ["fixture-1", "fixture-2"] for v in facts
    )

    complete = next(v for v in facts if len(v["comparator_metrics"]) == 2)
    assert {
        entry["methodology_version"]: complete["comparator_metrics"][entry["offset"]]["price_return_pct"]
        for entry in complete["comparator_sets"]
        if entry["count"]
    } == {"1": -2, "2": 3}


def test_split_retention_batches_keep_pending_versions_and_do_not_expand_expired_windows(store):
    from dataclasses import asdict

    from cryptowatcher.analytics import cohort_rows
    from cryptowatcher.events import identity
    from cryptowatcher.outcomes import DAY, Retention
    from cryptowatcher.sources import Candle
    from cryptowatcher.storage import StateError

    bounded(store)
    worker = OutcomeWorker(store)
    observe(store, worker)
    Sources(store).put("spot/BTCUSDT/1m", future())
    worker.work(NOW + 6 * MINUTE)
    h = History(store)
    event_id = h.items("history:")[-1][1]["event_id"]
    old = [
        Candle.parse(
            [NOW - 28 * DAY + i * MINUTE, 100, 101, 99, 100, 1, 20],
            "1m",
            "fixture",
            NOW - 28 * DAY + 5 * MINUTE,
            NOW - 28 * DAY + 5 * MINUTE,
        )
        for i in range(5)
    ]
    cid = identity(f"comparison/{event_id}/1")
    with store.db:
        h.version(
            "comparison",
            {
                "comparison_id": cid,
                "event_id": event_id,
                "event_revision": 1,
                "revision": 1,
                "available_at_ms": NOW,
                "methodology_version": "1",
                "samples": [
                    {
                        "source_inputs": [asdict(c) for c in old],
                        "future_inputs": [asdict(c) for c in old],
                        "outcomes": {"5": {"metrics": {"price_return_pct": -2}}},
                    }
                ],
                "limitations": [],
            },
        )
        h.put(
            "outcome-job:original-method",
            {
                "outcome_id": "original-method",
                "event_id": event_id,
                "event_revision": 1,
                "horizon_min": 10,
                "methodology_version": "original-pending",
                "state": "PENDING",
            },
        )
    before = list(cohort_rows(store))
    Retention(store).work(NOW + 31 * DAY, batch=1)
    assert h.get("outcome-job:original-method") is not None
    # Ordinary readers still validate raw source availability. Retention needs
    # only already persisted metrics and provenance from the expired detail.
    with pytest.raises(StateError):
        h.get("comparison-head:" + cid)
    assert "source_inputs" not in h.metadata("comparison-head:" + cid)["samples"][0]
    for _ in range(20):
        Retention(store).work(NOW + 31 * DAY, batch=1)
        if not h.events(1):
            break
    assert not h.events(1) and list(cohort_rows(store)) == before
    rows = store.db.execute("SELECT dimensions FROM market_quality").fetchall()
    assert any(json_payload(r[0])["methodology_version"] == "original-pending" for r in rows)


def test_pending_method_is_not_silently_replaced_and_recompare_keeps_original_event(store, monkeypatch):
    from cryptowatcher import analytics as analytics_module
    from cryptowatcher import outcomes as outcome_module
    from cryptowatcher.analytics import ComparisonWorker

    bounded(store)
    worker = OutcomeWorker(store)
    observe(store, worker)
    h = History(store)
    event = h.events(1)[0]
    job = next(j for _, j in h.items("outcome-job:") if j["event_id"] == event["event_id"])
    monkeypatch.setattr(outcome_module, "METHOD", "2")
    result = worker.calculate(store, job, event)
    assert result["state"] == "INVALID_SOURCE" and result["reason"] == "OUTCOME_METHODOLOGY_UNAVAILABLE"
    with store.db:
        worker.apply(job, event, {"outcome_id": job["outcome_id"]}, result, NOW + 6 * MINUTE)
    assert h.get("outcome-head:" + job["outcome_id"])["methodology_version"] == "1"
    comparison = ComparisonWorker(store)
    monkeypatch.setattr(analytics_module, "COMPARATOR_VERSION", "2")
    for frozen_job in ({"methodology_version": "1"}, {}):
        result = comparison.calculate(store, frozen_job, event)
        assert (
            result["state"] == "INVALID_SOURCE"
            and result["methodology_version"] == "1"
            and not result["samples"]
        )
    with store.db:
        h.delete("comparison-job:" + event["event_id"])
        h.version(
            "event",
            {**event, "revision": 2, "strength": event["strength"] + 10, "available_at_ms": NOW + MINUTE},
        )
    comparison.requeue(event["event_id"], NOW + 2 * MINUTE)
    assert h.get("comparison-job:" + event["event_id"])["event_revision"] == 1
    assert h.get("comparison-job:" + event["event_id"])["methodology_version"] == "2"
