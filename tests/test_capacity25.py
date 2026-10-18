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
    assert state["state"] == "COMPLETE" and state["version"] == 6
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


def test_namespace_backfill_tracks_interleaved_mutations_restarts_and_rollback(store, monkeypatch):
    import time
    from collections import defaultdict

    from cryptowatcher import runtime_meter
    from cryptowatcher.market_storage import enable

    with monkeypatch.context() as patcher:
        patcher.setattr(runtime_meter, "install", lambda db: None)
        enable(store.db)
    store.bounded = True
    with store.db:
        for i in range(25):
            store.db.execute("INSERT INTO runtime VALUES(?,?)", (f"radar:v1:fixture:{i:04d}", "ж" * (i + 1)))
    runtime_meter.install(store.db)
    with store.db:
        assert not runtime_meter.drain(store.db, batch=3, deadline=time.monotonic() + 1)
    saved = tuple(store.db.execute("SELECT * FROM market_namespace_scan").fetchone())
    with pytest.raises(RuntimeError), store.db:
        runtime_meter.drain(store.db, batch=3, deadline=time.monotonic() + 1)
        store.db.execute("DELETE FROM runtime WHERE key='radar:v1:fixture:0000'")
        raise RuntimeError("rollback")
    assert tuple(store.db.execute("SELECT * FROM market_namespace_scan").fetchone()) == saved
    for i in range(40):
        with store.db:
            store.db.execute(
                "UPDATE runtime SET payload_json=? WHERE key=?",
                (b"updated" * (i + 1), f"radar:v1:fixture:{i % 25:04d}"),
            )
            store.db.execute("DELETE FROM runtime WHERE key=?", (f"radar:v1:fixture:{(i + 5) % 25:04d}",))
            store.put_runtime(f"new:{i}", {"unicode": "МСК", "i": i})
            done = runtime_meter.drain(store.db, batch=2, deadline=time.monotonic() + 1)
        if done:
            break
    assert done

    def direct():
        result = defaultdict(lambda: {"records": 0, "key_bytes": 0, "payload_bytes": 0})
        for key, size in store.db.execute("SELECT key,length(CAST(payload_json AS BLOB)) FROM runtime"):
            value = result[runtime_meter.namespace(key)]
            value["records"] += 1
            value["key_bytes"] += len(key.encode())
            value["payload_bytes"] += size
        return dict(result)

    assert runtime_meter.read(store.db) == direct()
    store.db.execute("CREATE TABLE unrelated_ddl(value)")
    assert runtime_meter.read(store.db) is None
    for _ in range(100):
        with store.db:
            done = runtime_meter.drain(store.db, batch=3, deadline=time.monotonic() + 1)
        if done:
            break
    assert done and runtime_meter.read(store.db) == direct()
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        assert runtime_meter.read(reader.db) == direct()


def test_namespace_scan_includes_negative_ids_and_counts_reused_tail_ids(store, monkeypatch):
    import time

    from cryptowatcher import runtime_meter
    from cryptowatcher.market_storage import enable

    with monkeypatch.context() as patcher:
        patcher.setattr(runtime_meter, "install", lambda db: None)
        enable(store.db)
    with store.db:
        for rid in (-(2**63), -1, 0, 50, 100):
            store.db.execute(
                "INSERT INTO runtime(rowid,key,payload_json) VALUES(?,?,?)", (rid, f"old:{rid}", "payload")
            )
    runtime_meter.install(store.db)
    with store.db:
        store.db.execute("DELETE FROM runtime WHERE rowid=100")
        for _ in range(10):
            if runtime_meter.drain(store.db, batch=2, deadline=time.monotonic() + 1):
                break
    assert sum(v["records"] for v in runtime_meter.read(store.db).values()) == 4
    with store.db:
        store.db.execute("INSERT INTO runtime(rowid,key,payload_json) VALUES(100,'new:reused','xx')")
    assert sum(v["records"] for v in runtime_meter.read(store.db).values()) == 5


async def test_legacy_retried_outcome_uses_original_due_for_completion_lag(store):
    from cryptowatcher.market_storage import enable
    from tests.test_outcomes import NOW, OutcomeWorker, Sources, future, observe

    enable(store.db)
    store.bounded = True
    store.clock_ms = lambda: NOW
    worker = OutcomeWorker(store)
    observe(store, worker)
    history = History(store)
    selected = next(v for _, v in history.items("outcome-job:") if v["horizon_min"] == 5)
    original_due = selected.pop("due_ms")
    clock = original_due + 120000
    selected["next_attempt_ms"] = clock
    with store.db:
        for key, _ in history.items("outcome-pending:") + history.items("outcome-job:"):
            history.delete(key)
        history.put("outcome-admission", {"complete": True})
        history.put("outcome-job:" + selected["outcome_id"], selected)
        history.put(selected["pending_key"], {"outcome_id": selected["outcome_id"]})
    Sources(store).put("spot/BTCUSDT/1m", future())
    assert await worker.async_work(lambda: clock) == 1
    value = store.get_runtime("analytics_progress:outcome")
    assert value["batch_completed"] == 1 and value["max_completed_due_lag_ms"] == 120000
    assert value["last_slo_violation_ms"] == clock


def test_numeric_metric_candidate_preserves_float_bits_integer_types_and_zero_signs():
    import random

    from cryptowatcher.metric_codec import pack, unpack
    from cryptowatcher.storage import encode

    rng = random.Random(3291)
    cases = []
    for value in (0.0, -0.0, 2.0, -2.0, 2**80, -(2**80), 1e-300, 1e300):
        for sign in (-1, 1):
            cases.append(
                {
                    "price_return_pct": value,
                    "directed_return_pct": value * sign,
                    "mfe_pct": 0,
                    "mae_pct": -0.0,
                }
            )
    for _ in range(1000):
        price = rng.uniform(-1e30, 1e30)
        cases.append(
            {
                "price_return_pct": price,
                "directed_return_pct": price * rng.choice((-1, 1)),
                "mfe_pct": rng.random(),
                "mae_pct": -rng.random(),
            }
        )
        cases.append(
            {
                "price_return_pct": price,
                "max_rise_pct": rng.random(),
                "max_fall_pct": -rng.random(),
                "range_pct": rng.random(),
            }
        )
    for original in cases:
        raw = pack(original)
        assert raw is not None and len(raw) in (26, 34)
        assert encode(unpack(raw)) == encode(original)
    for invalid in ({"other": 1}, {"price_return_pct": float("nan")}, {"price_return_pct": 1 << 9000}):
        assert pack(invalid) is None
    for raw in (
        b"",
        b"\x01\x00",
        b"\x04\x00" + b"\x00" * 24,
        b"\x01\x80" + b"\x00" * 24,
        b"\x01\x00" + struct.pack(">ddd", float("inf"), 0, 0),
    ):
        with pytest.raises(ValueError):
            unpack(raw)


def test_numeric_pool_collision_rollback_gc_and_identity_are_lossless(store, monkeypatch):
    from cryptowatcher import numeric_pool
    from cryptowatcher.market_storage import enable
    from cryptowatcher.storage import encode

    enable(store.db)
    store.bounded = True
    first = {
        "price_return_pct": 0.0175454454105349,
        "directed_return_pct": 0.0175454454105349,
        "mfe_pct": 0,
        "mae_pct": -0.0,
    }
    second = {**first, "mfe_pct": 2.75}
    with monkeypatch.context() as patcher:
        patcher.setattr(numeric_pool, "fingerprint", lambda raw: b"collision")
        with store.db:
            one = numeric_pool.intern(store.db, first)
            two = numeric_pool.intern(store.db, second)
        assert one != two
        assert encode(numeric_pool.load(store.db, one)) == encode(first)
        assert encode(numeric_pool.load(store.db, two)) == encode(second)
        with pytest.raises(RuntimeError), store.db:
            numeric_pool.intern(store.db, {**first, "mae_pct": -2.5})
            raise RuntimeError("rollback")
        assert store.db.execute("SELECT COUNT(*) FROM market_numeric").fetchone()[0] == 2
        with store.db:
            assert numeric_pool.collect(store.db) == 2
            three = numeric_pool.intern(store.db, first)
        assert three > max(one, two)
        with store.db:
            numeric_pool.collect(store.db)


def test_numeric_metric_owner_projection_and_missing_body_fail_closed(store):
    import sqlite3

    from cryptowatcher.history_codec import Codec
    from cryptowatcher.market_storage import enable
    from cryptowatcher.numeric_pool import collect
    from cryptowatcher.storage import StateError, encode
    from tests.test_outcomes import NOW, OutcomeWorker, Sources, future, observe

    enable(store.db)
    store.bounded = True
    store.clock_ms = lambda: NOW
    worker = OutcomeWorker(store)
    observe(store, worker)
    Sources(store).put("spot/BTCUSDT/1m", future())
    worker.work(NOW + 6 * 60000)
    history = History(store)
    outcome = next(v for _, v in history.items("outcome-head:") if v["state"] == "COMPLETE")
    key = f"radar:v1:outcome:{outcome['outcome_id']}:{outcome['revision']:010d}"
    raw = store.get_runtime(key)
    metric_id = raw["_history_storage"]["fields"]["metrics"]["id"]
    assert (
        store.db.execute("SELECT detail_refs FROM market_numeric WHERE id=?", (metric_id,)).fetchone()[0] > 0
    )
    assert collect(store.db) >= 0
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        projected = json.loads(
            reader.db.execute(
                "SELECT cw_numeric_json(payload_json) FROM runtime WHERE key=?", (key,)
            ).fetchone()[0]
        )
        assert encode(projected["metrics"]) == encode(outcome["metrics"])
        assert encode(History(reader).get(key[len("radar:v1:") :])) == encode(outcome)
    broken = json.loads(encode(raw))
    broken["_history_storage"]["fields"]["metrics"]["id"] = 10**12
    with pytest.raises(sqlite3.IntegrityError), store.db:
        store.db.execute("UPDATE runtime SET payload_json=? WHERE key=?", (encode(broken), key))
    assert encode(history.get(key[len("radar:v1:") :])) == encode(outcome)
    # Corruption injection is confined to this disposable fixture.
    store.db.execute("DROP TRIGGER market_numeric_retained")
    with store.db:
        store.db.execute("DELETE FROM market_numeric WHERE id=?", (metric_id,))
    with pytest.raises(StateError, match="numeric"):
        history.get(key[len("radar:v1:") :])
    assert "metrics" not in Codec(store.db).unpack(raw, omit_fields=frozenset({"metrics"}))


def test_unsupported_large_metric_shapes_keep_existing_inline_encoding(store):
    from cryptowatcher.history_codec import Codec
    from cryptowatcher.market_storage import enable

    enable(store.db)
    store.bounded = True
    value = {"metrics": {f"custom_metric_{i}": i + 0.25 for i in range(40)}}
    with store.db:
        packed = Codec(store.db).pack(value)
    assert packed == value
    assert store.db.execute("SELECT COUNT(*) FROM market_numeric").fetchone()[0] == 0


def test_meter_preserves_first_new_epoch_sample_and_does_not_mix_legacy_flow(store):
    from cryptowatcher.market_storage import enable
    from cryptowatcher.storage_meter import report, sample
    from tests.test_outcomes import NOW

    enable(store.db)
    store.bounded = True
    start = NOW // 3600000 * 3600000 + 600000
    legacy = [
        {
            "at_ms": start - (25 - i) * 3600000,
            "format_version": 3,
            "working_bytes": 0,
            "categories": {},
            "event_total": 0,
        }
        for i in range(25)
    ]
    store.put_runtime("storage_samples", legacy)
    before = report(store, start)
    assert before["sample_seconds"] == 0 and before["net_bytes_per_second"] is None
    assert store.get_runtime("storage_samples") == legacy
    for offset in (0, 60000, 120000):
        sample(store, start + offset)
    current = [s for s in store.get_runtime("storage_samples") if s["format_version"] == 4]
    assert [s["at_ms"] for s in current] == [start, start + 120000]
    assert report(store, start + 120000)["sample_seconds"] == 120


def test_candle_range_cache_is_fixed_snapshot_only_and_cannot_hide_source_changes(store):
    from cryptowatcher.market_storage import enable
    from cryptowatcher.outcomes import read_candles
    from cryptowatcher.sources import Sources
    from tests.test_outcomes import MINUTE, NOW

    enable(store.db)
    store.bounded = True
    stream = "spot/CACHEUSDT/1m"
    candles = [
        Candle.parse(
            [NOW + i * MINUTE, 100, 102, 99, 101, 1, 100], "1m", "fixture", NOW + 5 * MINUTE, NOW + 5 * MINUTE
        )
        for i in range(5)
    ]
    Sources(store).put(stream, candles)
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        reader.db.execute("BEGIN")
        first = read_candles(reader, stream, NOW, NOW + 5 * MINUTE)
        assert first == candles and reader.analytics_range_bytes > 0
        first.clear()
        assert read_candles(reader, stream, NOW, NOW + 5 * MINUTE) == candles
        replacement = Candle.parse(
            [NOW, 100, 102, 99, 100.75, 1, 100], "1m", "fixture", NOW + 5 * MINUTE, NOW + 5 * MINUTE
        )
        Sources(store).put(stream, [replacement], recovery=True)
        assert read_candles(reader, stream, NOW, NOW + 5 * MINUTE) == candles
        reader.db.rollback()
        reader.db.execute("BEGIN")
        assert read_candles(reader, stream, NOW, NOW + 5 * MINUTE)[0] == replacement
    assert read_candles(store, stream, NOW, NOW + 5 * MINUTE)[0] == replacement
    assert store.analytics_range_bytes == 0


def test_comparison_admission_finds_ready_canonical_siblings_without_processing_future_work(store):
    import copy

    from cryptowatcher.analytics_worker import prepare, read_snapshot
    from cryptowatcher.market_storage import enable
    from cryptowatcher.outcomes import pending_key
    from tests.test_outcomes import NOW, OutcomeWorker, observe

    enable(store.db)
    store.bounded = True
    store.clock_ms = lambda: NOW
    worker = OutcomeWorker(store)
    observe(store, worker)
    history = History(store)
    events = history.events(20)
    assert len(events) == 5
    anchor, future = events[:2]
    unrelated = copy.deepcopy(anchor)
    unrelated.update(event_id=identity("different-instrument"), symbol="OTHERUSDT")
    with store.db:
        history.version("event", unrelated)
        worker.admit(unrelated)
        for event, deadline in ((anchor, NOW - 2), (unrelated, NOW - 1), (future, NOW + 60000)):
            key = "comparison-job:" + event["event_id"]
            job = history.get(key)
            history.delete(job["pending_key"])
            job["next_attempt_ms"] = deadline
            job["pending_key"] = pending_key(deadline, event["event_id"]).replace(
                "outcome-pending:", "comparison-pending:"
            )
            history.put(key, job)
            history.put(job["pending_key"], {"event_id": event["event_id"]})
    first_two = [v["event_id"] for _, v in history.items("comparison-pending:", 2)]
    assert first_two == [anchor["event_id"], unrelated["event_id"]]
    prepared = read_snapshot(
        store.path,
        store.owner_id,
        lambda reader: prepare(
            reader,
            "comparison-pending:",
            "comparison-job:",
            "event_id",
            NOW,
            2,
            2,
            lambda _, job, event: {"frozen": event["event_id"]},
        ),
    )
    chosen = [e["event_id"] for _, e, _, _ in prepared]
    assert len(chosen) == len(set(chosen)) == 2 and chosen[0] == anchor["event_id"]
    assert unrelated["event_id"] not in chosen and future["event_id"] not in chosen
    assert all(e["symbol"] == anchor["symbol"] and job["next_attempt_ms"] <= NOW for job, e, _, _ in prepared)


def test_interval_reports_real_flow_deltas_and_rejects_counter_reset():
    import copy

    from cryptowatcher.capacity import interval

    before = {
        "at_ms": 100000,
        "configuration_revision": 5,
        "source_revision": "f" * 40,
        "history_generation": {"id": "preserved"},
        "runtime_instance": {"id": "one"},
        "runtime_ledger": {"state": "MEASURED", "namespaces": {}},
        "prospective_cohorts": {"state": "MEASURED_PROSPECTIVE"},
        "quality_actual": {"state": "MEASURED"},
        "physical_snapshot": {"state": "PASS", "tables": {}, "total_index_bytes": 0},
        "storage": {"working_bytes": 100, "files": {}},
        "analytics_progress": {
            "comparison": {
                "admitted_total": 100,
                "completed_total": 90,
                "work_seconds_total": 3.5,
                "read_seconds_total": 2.5,
                "apply_seconds_total": 0.5,
            }
        },
    }
    after = copy.deepcopy(before)
    after["at_ms"] += 86400000
    after["analytics_progress"]["comparison"].update(
        admitted_total=150,
        completed_total=150,
        work_seconds_total=5.5,
        read_seconds_total=3.5,
        apply_seconds_total=1.25,
    )
    result = interval(before, after)
    flow = result["analytics_flows"]["comparison"]
    assert result["state"] == "OBSERVED_24H"
    assert flow["admitted_total"] == 50 and flow["completed_total"] == 60
    assert flow["completed_per_work_second"] == 30
    assert flow["read_seconds_total"] == 1 and flow["apply_seconds_total"] == 0.75
    assert result["analytics_flows"]["outcome"]["state"] == "UNSET_NO_BOUNDARY_PROGRESS"
    after["analytics_progress"]["comparison"]["completed_total"] = 0
    result = interval(before, after)
    assert result["state"] == "PARTIAL_OR_UNSTABLE" and result["daily_extrapolation_bytes"] is None
    assert result["analytics_flows"]["comparison"]["state"] == "RESET_OR_INCOMPATIBLE"
