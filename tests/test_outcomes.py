import json
import sqlite3
from dataclasses import asdict

import pytest

from cryptowatcher.config import ConfigError
from cryptowatcher.events import EventEngine, History
from cryptowatcher.outcomes import DAY, MINUTE, OutcomeWorker, Retention, calculate
from cryptowatcher.sources import Candle, Sources
from cryptowatcher.storage import Store, encode

NOW = 1_800_000_000_000


def test_upgrade_g2b_payload_and_history_preserves_original_and_all_row_backup(tmp_path):
    from cryptowatcher.config import OwnerSettings

    path = tmp_path / "old.sqlite"
    store = Store(path, 233382575)
    store.initialize(OwnerSettings())
    worker = OutcomeWorker(store)
    observe(store, worker)
    history = History(store)
    # A saved G2B owner/event has neither of the new G3 fields.
    with store.db:
        owner = store.snapshot().settings.payload()
        owner.pop("outcome_horizons")
        owner.pop("digest_interval_min")
        store.db.execute("UPDATE owner_settings SET settings_json=?", (encode(owner),))
        for key, value in history.items("event:"):
            value["settings_snapshot"] = owner
            store.db.execute(
                "UPDATE runtime SET payload_json=? WHERE key=?", (encode(value), "radar:v1:" + key)
            )
        for key, _ in history.items("outcome-job:") + history.items("outcome-pending:"):
            history.delete(key)
        history.delete("outcome-admission")
    original = store.db.execute(
        "SELECT key,payload_json FROM runtime WHERE key LIKE 'radar:v1:event:%' ORDER BY key"
    ).fetchall()
    store.close()
    store = Store(path, 233382575)
    worker = OutcomeWorker(store)
    worker.bootstrap()
    assert store.snapshot().settings.outcome_horizons == (5, 15, 30, 60)
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert (
        store.db.execute(
            "SELECT key,payload_json FROM runtime WHERE key LIKE 'radar:v1:event:%' ORDER BY key"
        ).fetchall()
        == original
    )
    assert History(store).count("outcome-job:") == len(original) * 4
    backup = tmp_path / "backup.sqlite"
    store.backup(backup)
    restored = Store(backup, store.owner_id, readonly=True)
    assert (
        restored.db.execute("SELECT key,payload_json FROM runtime ORDER BY key").fetchall()
        == store.db.execute("SELECT key,payload_json FROM runtime ORDER BY key").fetchall()
    )
    restored.close()


def event(direction="UP"):
    return {
        "reference_time_ms": NOW,
        "reference_price": 100,
        "direction": direction,
        "continuation_threshold_pct": 0.1,
    }


def future(start=NOW):
    return [
        Candle.parse(
            [start + i * MINUTE, 100, 104 + i, 96 + i, 101 + i, 1, 100],
            "1m",
            "rest",
            NOW + 10 * MINUTE,
            NOW + 10 * MINUTE,
        )
        for i in range(5)
    ]


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True])
def test_invalid_reference_is_an_error_not_a_zero_or_fabricated_outcome(value):
    with pytest.raises(ValueError, match="reference"):
        calculate({**event(), "reference_price": value}, 5, future())


def observe(store, worker, now=NOW):
    engine = EventEngine(store, on_event=worker.admit)
    candles = [
        Candle.parse(
            [
                now - (24 - i) * MINUTE,
                100,
                104 if i == 23 else 101,
                99,
                102 if i == 23 else 100,
                1,
                1200 if i == 23 else 400,
            ],
            "1m",
            "websocket",
            now,
            now,
        )
        for i in range(24)
    ]
    engine.observe("spot/BTCUSDT/1m", candles, 1e6, 0.01, now, live=True)
    return engine


@pytest.mark.parametrize("direction", ["UP", "DOWN", "NEUTRAL"])
def test_full_window_exact_target_direction_excursions_and_missing(direction):
    result = calculate(event(direction), 5, list(reversed(future())))
    assert result["state"] == "COMPLETE" and result["metrics"]["price_return_pct"] == pytest.approx(5)
    if direction == "UP":
        assert result["metrics"]["mfe_pct"] == pytest.approx(8)
        assert result["metrics"]["mae_pct"] == pytest.approx(-4)
        assert result["classification"] == "CONTINUATION"
    elif direction == "DOWN":
        assert result["metrics"]["mfe_pct"] == pytest.approx(4)
        assert result["metrics"]["mae_pct"] == pytest.approx(-8)
        assert result["classification"] == "REVERSAL"
    else:
        assert result["metrics"]["max_rise_pct"] == pytest.approx(8)
        assert result["metrics"]["max_fall_pct"] == pytest.approx(-4)
        assert "directed_return_pct" not in result["metrics"] and result["classification"] == "UNCLASSIFIED"
    missing = calculate(event(direction), 5, future()[:2] + future()[3:])
    assert missing["state"] == "INCOMPLETE" and missing["metrics"] == {}
    assert missing["missing_ranges"] == [(NOW + 2 * MINUTE, NOW + 3 * MINUTE)]
    assert calculate(event(direction), 5, [])["metrics"] == {}
    with pytest.raises(ValueError):
        calculate(event(direction), 5, future() + [future()[-1]])


def test_noise_boundary_and_missing_context():
    row = future()[:1]
    threshold = 100 * (row[0].close / 100 - 1)
    result = calculate({**event(), "continuation_threshold_pct": threshold}, 1, row)
    assert result["classification"] == "NEUTRAL_MOVEMENT"
    result = calculate({**event(), "continuation_threshold_pct": None}, 1, row)
    assert result["state"] == "COMPLETE" and result["classification_reason"] == "MISSING_REFERENCE_CONTEXT"


def test_pending_restart_gaps_recovery_reference_and_no_silent_recalculation(store):
    snapshot = store.snapshot()
    store.update(snapshot.revision, snapshot.settings.changed(outcome_horizons=(5,)))
    gaps = []
    worker = OutcomeWorker(store, lambda stream, start, end: gaps.append((stream, start, end)))
    engine = observe(store, worker)
    worker.work(NOW + 4 * MINUTE)
    assert not engine.history.items("outcome-head:")
    worker.work(NOW + 6 * MINUTE)
    incomplete = engine.history.items("outcome-head:")
    assert all(v["state"] == "INCOMPLETE" and not v["metrics"] for _, v in incomplete)
    assert gaps and all(g[1:] == (NOW, NOW + 5 * MINUTE) for g in gaps)
    worker.work(NOW + 7 * MINUTE)
    assert all(v["revision"] == 1 for _, v in engine.history.items("outcome-head:"))
    Sources(store).put("spot/BTCUSDT/1m", future())
    snapshot = store.snapshot()
    store.update(snapshot.revision, snapshot.settings.changed(outcome_horizons=(15,)))
    backup = store.path.with_name("reopened.sqlite3")
    store.backup(backup)
    restored = Store(backup, store.owner_id)
    try:
        restarted = OutcomeWorker(restored)
        restarted.work(NOW + 30 * MINUTE)
        complete = History(restored).items("outcome-head:")
        assert all(
            v["state"] == "COMPLETE" and v["horizon_min"] == 5 and v["reference_time_ms"] == NOW
            for _, v in complete
        )
        assert all(v["revision"] == 2 for _, v in complete)
        # A later source correction never rewrites a finished methodology result.
        before = complete
        corrected = [Candle(**{**asdict(c), "high": c.high + 1}) for c in future()]
        Sources(restored).put("spot/BTCUSDT/1m", corrected, recovery=True)
        restarted.work(NOW + 31 * MINUTE)
        assert before == History(restored).items("outcome-head:")
    finally:
        restored.close()


def test_event_admission_atomic_and_horizon_numeric_order(store, monkeypatch):
    snapshot = store.snapshot()
    # D22 rejects horizons incompatible with retained inputs/detail. Keep the
    # numeric-order and atomic rollback regression within the supported window.
    with pytest.raises(ConfigError, match="retention incompatible"):
        snapshot.settings.changed(outcome_horizons=(10**100, 5))
    store.update(snapshot.revision, snapshot.settings.changed(outcome_horizons=(10_000, 5)))
    worker = OutcomeWorker(store)
    engine = observe(store, worker)
    Sources(store).put("spot/BTCUSDT/1m", future())
    worker.work(NOW + 6 * MINUTE)
    assert all(v["state"] == "COMPLETE" for _, v in engine.history.items("outcome-head:"))
    assert all(v["horizon_min"] == 5 for _, v in engine.history.items("outcome-head:"))
    previous = store.db.execute("SELECT count(*) FROM runtime").fetchone()[0]
    original = worker.history.put

    def fail(key, value):
        if key.startswith("outcome-job:"):
            raise sqlite3.OperationalError("injected disk failure")
        original(key, value)

    monkeypatch.setattr(worker.history, "put", fail)
    with pytest.raises(sqlite3.OperationalError):
        observe(store, worker, NOW + MINUTE)
    assert store.db.execute("SELECT count(*) FROM runtime").fetchone()[0] == previous


def test_retention_preserves_snapshots_protected_keys_and_pending_ranges(store):
    worker = OutcomeWorker(store)
    engine = observe(store, worker)
    rows = future()
    Sources(store).put("spot/BTCUSDT/1m", rows)
    ordinary = Candle.parse(
        [NOW + 40 * DAY, 100, 104, 96, 101, 1, 100], "1m", "rest", NOW + 41 * DAY, NOW + 41 * DAY
    )
    Sources(store).put("spot/BTCUSDT/1m", [ordinary])
    # Initial admission must finish before deleting any raw records.
    retention = Retention(store)
    assert retention.work(NOW + 100 * DAY) == 0
    worker.work(NOW + 100 * DAY)
    history = engine.history
    held = history.get("outcome-job:" + history.items("outcome-job:")[0][1]["outcome_id"])
    with store.db:
        history.put("hold:spot/BTCUSDT/1m:test", {"start": rows[3].start, "end": rows[3].end})
        history.delete("protected:" + Sources.prefix("spot/BTCUSDT/1m") + f"{rows[3].start:016d}")
    assert held and retention.work(NOW + 100 * DAY) == 0  # 40-day-old ordinary remains inside90days.
    retention.work(NOW + 140 * DAY)
    assert store.get_runtime(Sources.prefix("spot/BTCUSDT/1m") + f"{ordinary.start:016d}") is None
    assert all(store.get_runtime(Sources.prefix("spot/BTCUSDT/1m") + f"{c.start:016d}") for c in rows)
    assert history.events() and history.items("outcome-head:")


@pytest.mark.parametrize("values", [(0,), (), (5, 5), (True,), (1.5,)])
def test_horizon_validation_preserves_saved_owner(store, values):
    snapshot = store.snapshot()
    with pytest.raises(ConfigError):
        snapshot.settings.changed(outcome_horizons=values)
    assert store.snapshot() == snapshot


def test_finite_source_overflow_does_not_abort_other_detector_events(store):
    worker = OutcomeWorker(store)
    engine = EventEngine(store, on_event=worker.admit)
    candles = [
        Candle.parse(
            [NOW - (24 - i) * MINUTE, 100, 1e308, 99, 102 if i == 23 else 100, 1, 1200 if i == 23 else 400],
            "1m",
            "websocket",
            NOW,
            NOW,
        )
        for i in range(24)
    ]
    engine.observe("spot/BTCUSDT/1m", candles, 1e6, 0.01, NOW, live=True)
    assert any(e["detector_id"] == "price" for e in engine.history.events())
    assert all(e["continuation_threshold_pct"] is None for e in engine.history.events())
    assert engine.history.diagnostics()["detector_errors"]
    assert all(json.loads(encode(e)) for e in engine.history.events())
