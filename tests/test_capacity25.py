import json
import math
import struct
from contextlib import closing
from unittest.mock import patch

import pytest

from cryptowatcher.analytics import prior_population
from cryptowatcher.config import ConfigError
from cryptowatcher.detectors import DetectorUnavailable, atr14
from cryptowatcher.digest import aggregate, text
from cryptowatcher.events import History, identity
from cryptowatcher.sources import Candle
from cryptowatcher.storage import Store
from cryptowatcher.telegram_views import source_summary


def test_prior_population_matches_original_float_bits_with_gaps_and_lookbacks(store):
    candles = [
        Candle.parse(
            [i * 60000, 100, 102 + math.sin(i), 98 - math.cos(i), 100 + math.sin(i / 7), 1, 100],
            "1m",
            "fixture",
            (i + 1) * 60000,
            (i + 1) * 60000,
        )
        for i in range(500)
        if i not in (71, 72, 201)
    ]
    start, reference = 40 * 60000, 480 * 60000
    for needed in (16, 21, 256):
        with patch("cryptowatcher.analytics.read_candles", return_value=candles):
            _, actual, population = prior_population(
                store, "spot/BTCUSDT/1m", start, reference, needed, 60000
            )
        expected = []
        for i, candle in enumerate(candles):
            if not start <= candle.end < reference:
                continue
            try:
                value = 100 * atr14(candles[max(0, i - needed + 1) : i + 1]) / candle.close
            except DetectorUnavailable:
                continue
            expected.append((candle, i, value))
        assert [(c.start, i, struct.pack(">d", v)) for c, i, v in actual] == [
            (c.start, i, struct.pack(">d", v)) for c, i, v in expected
        ]
        assert population == sorted(v for _, _, v in expected)


def test_population_cache_is_one_entry_and_only_in_own_read_snapshot(store):
    with patch("cryptowatcher.analytics.read_candles", return_value=[]) as read:
        with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
            reader.db.execute("BEGIN")
            prior_population(reader, "spot/BTCUSDT/1m", 0, 60000, 16, 60000)
            prior_population(reader, "spot/BTCUSDT/1m", 0, 60000, 21, 60000)
            assert read.call_count == 1
            prior_population(reader, "spot/ETHUSDT/1m", 0, 60000, 16, 60000)
            prior_population(reader, "spot/BTCUSDT/1m", 0, 60000, 16, 60000)
            assert read.call_count == 3
        for _ in range(2):
            prior_population(store, "spot/BTCUSDT/1m", 0, 60000, 16, 60000)
        assert read.call_count == 5


def test_effective_settings_rejection_preserves_revision_and_old_settings_readable(store):
    before = store.snapshot()
    bad = before.settings.changed(watchlist=("spot/BTCUSDT",), watchlist_multiplier=0.25)
    for value in (
        bad,
        before.settings.with_parameter("turnover_lookback", 300),
        before.settings.with_parameter("volatility_lookback", 255),
    ):
        with pytest.raises(ConfigError):
            store.update(before.revision, value)
        assert store.snapshot() == before
    # Admission is stricter; immutable snapshots do not acquire new validation rules.
    from cryptowatcher.config import OwnerSettings

    assert OwnerSettings.from_payload(bad.payload()) == bad
    store.update(before.revision, before.settings.with_parameter("turnover_lookback", 255))


def test_digest_activity_cannot_be_displaced_by_five_stronger_singletons(store):
    h = History(store)
    start = 1800000000000
    with store.db:
        for i in range(25):
            h.version(
                "event",
                dict(
                    event_id=identity("activity/" + str(i)),
                    revision=1,
                    episode_id=identity("activity-episode/" + str(i)),
                    market_observation_ms=start + 1,
                    available_at_ms=start + 1,
                    market_type="spot",
                    detector_id="price",
                    symbol="BUSYUSDT" if i < 20 else "RARE" + str(i) + "USDT",
                    timeframe="1m",
                    direction="UP",
                    strength=1 if i < 20 else 100,
                    reference_price=100,
                    reference_time_ms=start,
                ),
            )
    result = aggregate(store, start, start + 60000, start + 60000)
    assert result["most_active_instruments"][0] == {"instrument": "spot/BUSYUSDT", "events": 20}
    assert len(result["strongest_episodes"]) == 5
    rendered = text(result)
    assert "BUSYUSDT" in rendered and "МСК" in rendered and "2027" in rendered
    assert "strength" not in rendered and " events" not in rendered


def test_recovery_summary_distinguishes_waiting_and_running(store):
    summary = source_summary(
        store.snapshot(), dict(at=100, active_streams=4, recovery_pending=0, recovery_working=1), 100
    )
    assert "ожидают: 0" in summary and "в работе: 1" in summary


def test_archived_intraday_csv_ndjson_and_cohorts_share_explicit_bounds(store):
    import csv
    import io
    import json

    from cryptowatcher.analytics import cohort_rows
    from cryptowatcher.export import Selection, csv_summary, ndjson
    from cryptowatcher.outcomes import Retention
    from tests.test_capacity import bounded
    from tests.test_outcomes import DAY, MINUTE, NOW, OutcomeWorker, Sources, future, observe

    bounded(store)
    worker = OutcomeWorker(store)
    observe(store, worker)
    Sources(store).put("spot/BTCUSDT/1m", future())
    worker.work(NOW + 61 * MINUTE, batch=200)
    selection = Selection(start_ms=NOW - MINUTE, end_ms=NOW + 1)
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        before = list(csv.DictReader(io.StringIO("".join(csv_summary(reader, selection)))))
    n = sum(int(r["sample_size"]) for r in before)
    assert n > 0
    for _ in range(100):
        Retention(store).work(NOW + 31 * DAY)
        if History(store).count("history:") == 0:
            break
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        after = list(csv.DictReader(io.StringIO("".join(csv_summary(reader, selection)))))
        exported = [json.loads(line) for line in ndjson(reader, selection)]
        grouped = list(cohort_rows(reader, start_ms=selection.start_ms, end_ms=selection.end_ms))
    assert sum(int(r["sample_size"]) for r in after) == sum(r["sample_size"] for r in grouped) == n
    manifest = exported[0]["selection_period"]
    assert manifest["archive_start_ms"] == selection.start_ms // DAY * DAY
    assert all(int(r["archive_start_ms"]) == manifest["archive_start_ms"] for r in after)
    assert manifest["requested_start_ms"] == selection.start_ms


def test_queue_health_separates_future_current_overdue_and_unknown(store):
    from cryptowatcher.queue_health import snapshot

    now = 1800000000000
    history = History(store)
    with store.db:
        history.put("outcome-pending:one", {"outcome_id": "one"})
        history.put("outcome-job:one", {"state": "PENDING", "next_attempt_ms": now + 1000})
        history.put("analytics-gap-pending:one", {"id": "one"})
        history.put(
            "analytics-gap:one",
            {"state": "PENDING", "next_attempt_ms": now, "end": now - 5000, "attempts": 0},
        )
    assert snapshot(store, now)["outcome"]["state"] == "FUTURE"
    assert snapshot(store, now)["analytics-gap"]["state"] == "CURRENT"
    assert snapshot(store, now + 62000)["outcome"]["state"] == "OVERDUE"
    assert snapshot(store, now + 62000)["analytics-gap"]["state"] == "REFERENCED_RECOVERY"
    history.delete("outcome-job:one")
    assert snapshot(store, now)["outcome"]["state"] == "ORPHAN_OR_UNKNOWN"


def test_completed_lag_and_app_restart_cannot_hide_between_samples(store):
    from cryptowatcher.queue_health import completed_batch
    from scripts.observe_capacity import advance

    completed_batch(store, "outcome", 100000, [61000], 1)
    value = {
        "at_ms": 100000,
        "stability_blockers": [],
        "runtime_instance": {"id": "one"},
        "analytics_progress": {"outcome": store.get_runtime("analytics_progress:outcome")},
    }
    state = {"state": "OBSERVING", "baseline_ms": 1000, "baseline_instance": {"id": "one"}}
    first = advance(state, value, 100)
    assert first["interrupted_samples"] == 1 and "ANALYTICS_SLO_VIOLATION" in first["last_blockers"]
    second = advance(first, value, 101)
    assert second["interrupted_samples"] == 1
    changed = advance(second, {**value, "runtime_instance": {"id": "two"}}, 102)
    assert changed["interrupted_samples"] == 2 and "RUNTIME_INSTANCE_CHANGED" in changed["last_blockers"]


def test_sample_failures_are_durable_sanitized_and_never_overwritten(tmp_path):
    import json
    import subprocess

    from scripts import observe_capacity as observer

    state = {"state": "OBSERVING"}
    for _ in range(2):
        with patch.object(
            observer, "sample_for_state", side_effect=subprocess.TimeoutExpired("secret command", 15)
        ):
            with pytest.raises(subprocess.TimeoutExpired):
                observer.sample_probe(tmp_path, state, "f" * 40)
    attempts = sorted(tmp_path.glob("sample-attempt-*.json"))
    assert len(attempts) == 2
    for path in attempts:
        value = json.loads(path.read_text())
        assert value["decision"] == "ERROR" and value["phase"] == "SAMPLE"
        assert value["error_class"] == "TimeoutExpired" and value["timeout_seconds"] == 15
        assert "started_at" in value and "finished_at" in value and "secret" not in path.read_text()
    pending = tmp_path / "sample-attempt-0003.json"
    observer.atomic(pending, {"decision": "PENDING"})
    state["active_sample_attempt"] = pending.name
    observer.recover_sample(tmp_path, state)
    assert json.loads(pending.read_text())["decision"] == "INTERRUPTED"
    assert state["interrupted_samples"] == 1


def test_window_repack_preserves_public_values_and_head_projections_across_restart(store):
    from contextlib import closing

    from cryptowatcher.events import PREFIX
    from cryptowatcher.market_storage import enable
    from cryptowatcher.source_windows import load
    from cryptowatcher.storage import Store, encode
    from cryptowatcher.storage_repack import work
    from tests.test_outcomes import NOW, OutcomeWorker, observe

    enable(store.db)
    store.bounded = True
    store.clock_ms = lambda: NOW
    observe(store, OutcomeWorker(store))
    history = History(store)
    before = {k: encode(v) for k, v in history.items("event-head:")}
    with store.db:
        # Retained production v3 contains inline references and full metadata heads.
        for key, _ in list(history.items("event:")):
            raw = store.get_runtime(PREFIX + key)
            fields = raw["_history_storage"]["fields"]
            from base64 import b64encode

            from cryptowatcher.market_storage import encoded, reference_points

            shared = load(store.db, fields["source_inputs"]["id"])
            fields["source_inputs"] = {
                "mode": "source",
                "stream": shared["stream"],
                "sha256": shared["sha256"],
                "points": b64encode(encoded(reference_points(shared))).decode(),
            }
            store.put_runtime(PREFIX + key, raw)
        for key, _ in list(history.items("event-head:")):
            event = history.get(key)
            head = History._head(
                "event", event, f"event:{event['event_id']}:{event['revision']:010d}", bounded=True
            )
            store.put_runtime(PREFIX + key, head)
        store.put_runtime(
            "storage_repack", {"version": 3, "state": "COMPLETE", "records": 999, "saved_payload_bytes": 42}
        )
    for _ in range(100):
        state = work(store, NOW, batch=3, seconds=1)
        if state["state"] == "COMPLETE":
            break
    assert state["state"] == "COMPLETE" and state["version"] == 4
    assert state["previous_run"]["records"] == 999 and state["saved_payload_bytes"] > 42
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        assert {k: encode(v) for k, v in History(reader).items("event-head:")} == before
        for key in before:
            assert reader.get_runtime(PREFIX + key)["schema"] == "cryptowatcher.history-head.v1"


def test_shared_window_refs_are_transactional_and_gc_cannot_remove_retained_inputs(store):
    import sqlite3

    from cryptowatcher.market_storage import enable
    from cryptowatcher.source_windows import collect, load
    from cryptowatcher.storage import encode
    from tests.test_outcomes import NOW, OutcomeWorker, observe

    enable(store.db)
    store.bounded = True
    store.clock_ms = lambda: NOW
    observe(store, OutcomeWorker(store))
    key, _ = History(store).items("event:")[0]
    raw = store.get_runtime("radar:v1:" + key)
    window_id = raw["_history_storage"]["fields"]["source_inputs"]["id"]
    original = load(store.db, window_id)
    refs = store.db.execute("SELECT refs FROM market_source_windows WHERE id=?", (window_id,)).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError), store.db:
        store.db.execute("DELETE FROM market_source_windows WHERE id=?", (window_id,))
    assert collect(store.db, NOW + 10**12, 1000) >= 0 and load(store.db, window_id) == original
    raw["_history_storage"]["fields"]["source_inputs"]["id"] = 10**12
    with pytest.raises(sqlite3.IntegrityError), store.db:
        store.db.execute("UPDATE runtime SET payload_json=? WHERE key=?", (encode(raw), "radar:v1:" + key))
    assert (
        store.db.execute("SELECT refs FROM market_source_windows WHERE id=?", (window_id,)).fetchone()[0]
        == refs
    )
    assert History(store).get(key)["source_inputs"]


def test_rescheduled_incomplete_job_is_not_hidden_by_future_pending_work(store):
    from cryptowatcher.queue_health import snapshot
    from tests.test_capacity import bounded

    bounded(store)
    history = History(store)
    with store.db:
        history.put("outcome-pending:a", {"outcome_id": "a"})
        history.put("outcome-job:a", {"state": "PENDING", "next_attempt_ms": 1000000})
        history.put("outcome-pending:z", {"outcome_id": "z"})
        history.put(
            "outcome-job:z", {"state": "INCOMPLETE", "next_attempt_ms": 2000000, "reason": "missing source"}
        )
    assert snapshot(store, 1000)["outcome"]["state"] == "SOURCE_LIMITED_OR_UNKNOWN"
    with pytest.raises(RuntimeError), store.db:
        history.delete("outcome-job:z")
        raise RuntimeError("rollback")
    assert snapshot(store, 1000)["outcome"]["state"] == "SOURCE_LIMITED_OR_UNKNOWN"
    with store.db:
        history.delete("outcome-job:z")
        history.delete("outcome-pending:z")
    assert snapshot(store, 1000)["outcome"]["state"] == "FUTURE"


def test_admission_and_completion_progress_never_commit_outer_transaction(store):
    from cryptowatcher.queue_health import arrival, completed_batch

    with pytest.raises(RuntimeError), store.db:
        arrival(store, "outcome")
        completed_batch(store, "outcome", 100, [50], 2, read_seconds=0.25)
        raise RuntimeError("rollback")
    assert store.get_runtime("analytics_progress:outcome") is None
    with store.db:
        arrival(store, "outcome")
        completed_batch(store, "outcome", 100, [50], 2, read_seconds=0.25)
    value = store.get_runtime("analytics_progress:outcome")
    assert value["admitted_total"] == value["completed_total"] == 1
    assert value["read_calculate_seconds"] == 0.25 and value["apply_seconds"] == 1.75


def test_bounded_ndjson_filters_keep_selected_event_and_record_counts(store):
    from cryptowatcher.export import Selection, ndjson
    from cryptowatcher.market_storage import enable
    from tests.test_outcomes import NOW, OutcomeWorker, observe

    enable(store.db)
    store.bounded = True
    store.clock_ms = lambda: NOW
    observe(store, OutcomeWorker(store))
    expected = {e["event_id"] for e in History(store).events(20) if e["detector_id"] == "price"}
    assert expected
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        rows = [json.loads(line) for line in ndjson(reader, Selection(detector="price", market="spot"))]
    assert {r["record"]["event_id"] for r in rows if r["type"] == "event"} == expected
    assert rows[-1]["selected_events"] == len(expected)
    assert rows[-1]["counts"]["event"] == len(expected)


def test_source_coordinate_runs_preserve_order_gaps_duplicates_and_revisions():
    import random

    from cryptowatcher.market_storage import compact_reference, reference_points

    rng = random.Random(724)
    cases = [[], [[12, 0]], [[60000 * i, 0] for i in range(256)], [[4, 0], [4, 0], [2, 3], [0, 4]]]
    cases.extend(
        [[[rng.randrange(10**12), rng.randrange(4)] for _ in range(rng.randrange(1, 200))] for _ in range(30)]
    )
    for points in cases:
        reference = compact_reference(1, points, "a" * 64)
        assert reference_points(reference) == points
    for runs in ([[0, 1, 65537, 0]], [[0, -1, 2, 0]], [[2**63 - 1, 1, 2, 0]], [[0, 1, True, 0]]):
        with pytest.raises(ValueError):
            reference_points({"mode": "source-runs", "stream": 1, "runs": runs, "sha256": "a" * 64})
