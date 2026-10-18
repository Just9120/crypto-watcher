"""Controlled-clock rolling retention, numeric equality and bounded-population evidence.

Synthetic weekly closed windows cover both markets/five detectors/four TF. This
is not a continuous market/load model and never predicts production bytes/day.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

from cryptowatcher.analytics import ComparisonWorker
from cryptowatcher.config import TIMEFRAMES, OwnerSettings
from cryptowatcher.events import EventEngine, History, identity
from cryptowatcher.market_storage import enable
from cryptowatcher.outcomes import DAY, MINUTE, OutcomeWorker, Retention
from cryptowatcher.quality_packets import unpack as unpack_quality
from cryptowatcher.quality_summary import month_boundary
from cryptowatcher.reset_history import private_state, signature
from cryptowatcher.source_context import save
from cryptowatcher.sources import Candle, Sources
from cryptowatcher.storage import Store, encode

NOW = 1_800_000_000_000


def fingerprint(
    group: dict[str, Any], strength: float, metrics: dict[str, Any], comparators: list[Any], state: str
) -> str:
    return encode(
        {
            "group": group,
            "strength": strength,
            "metrics": metrics,
            "comparator_metrics": comparators,
            "complete": state == "COMPLETE",
        }
    )


def detail_facts(h: History, event: dict[str, Any]) -> list[str]:
    settings = event["settings_snapshot"]
    result = []
    comparison = h.metadata("comparison-head:" + identity(f"comparison/{event['event_id']}/1")) or {}
    for horizon in settings["outcome_horizons"]:
        outcome = h.metadata("outcome-head:" + identity(f"outcome/{event['event_id']}/{horizon}/1"))
        if outcome is None:
            raise ValueError("Missing admitted outcome")
        group = {
            "detector": event["detector_id"],
            "version": event["detector_version"],
            "settings": event["settings_revision"],
            "profile": settings["profile"],
            "market": event["market_type"],
            "timeframe": event["timeframe"],
            "instrument": event["symbol"],
            "universe": settings["alert_universe_mode"],
            "direction": event["direction"],
            "horizon_min": horizon,
            "methodology_version": outcome["methodology_version"],
        }
        metrics = [
            s["outcomes"][str(horizon)]["metrics"]
            for s in comparison.get("samples", [])
            if str(horizon) in s["outcomes"]
        ]
        result.append(fingerprint(group, event["strength"], outcome["metrics"], metrics, outcome["state"]))
    return result


def retained_facts(store: Store) -> Counter[str]:
    h = History(store)
    result: Counter[str] = Counter()
    for _, event in h.items("event-head:"):
        result.update(detail_facts(h, event))
    for row in store.db.execute("SELECT dimensions,facts FROM market_quality"):
        group = json.loads(row[0])
        for fact in unpack_quality(store.db, bytes(row[1])):
            result[
                fingerprint(
                    group, fact["strength"], fact["metrics"], fact["comparator_metrics"], fact["state"]
                )
            ] += 1
    return result


def measure(path: Path, weeks: int = 62) -> dict[str, Any]:
    if path.exists() or not 58 <= weeks <= 64:
        raise ValueError("New disposable DB path and58..64 simulated weeks required")
    started = time.monotonic()
    store = Store(path, 233382575)
    store.initialize(OwnerSettings())
    enable(store.db)
    store.bounded = True
    manifests: list[tuple[int, list[str]]] = []
    samples = []
    families: set[str] = set()
    comparator_nonempty = 0
    restarts = 0
    try:
        worker = OutcomeWorker(store)
        comparison = ComparisonWorker(store)
        engine = EventEngine(store, on_event=worker.admit)
        for week in range(weeks):
            now = NOW + week * 7 * DAY
            store.clock_ms = lambda: now
            if week and week % 8 == 0:
                saved = store.snapshot()
                store.update(saved.revision, saved.settings.changed(liquidity_floor_24h=1e6 + week))
            born = []
            for market in ("spot", "linear"):
                for tf in TIMEFRAMES if week == 0 else (TIMEFRAMES[week % 4],):
                    step = int(tf[:-1]) * MINUTE
                    close = 110 if week % 2 == 0 else 90
                    candles = [
                        Candle.parse(
                            [
                                now - (24 - i) * step,
                                100,
                                max(101, close) if i == 23 else 100.1,
                                min(99, close) if i == 23 else 99.9,
                                close if i == 23 else 100,
                                1,
                                4000 if i == 23 else 400,
                            ],
                            tf,
                            "fixture",
                            now,
                            now,
                        )
                        for i in range(24)
                    ]
                    Sources(store).put(f"{market}/FIXTUREUSDT/{tf}", candles)
                    with store.db:
                        save(
                            store,
                            "instrument",
                            f"{market}/FIXTUREUSDT",
                            {"tick_size": 0.01, "available_at_ms": now - 1, "market_observation_ms": now - 1},
                        )
                    engine.observe(f"{market}/FIXTUREUSDT/{tf}", candles, 2e6, 0.01, now, live=False)
                future = [
                    Candle.parse(
                        [now + i * MINUTE, close, close + 1, close - 1, close, 1, 200],
                        "1m",
                        "fixture",
                        now + 60 * MINUTE,
                        now + 60 * MINUTE,
                    )
                    for i in range(60)
                ]
                # First epoch deliberately leaves a long horizon incomplete. Its
                # retry/hold must become explicit incomplete summary at detail TTL.
                Sources(store).put(f"{market}/FIXTUREUSDT/1m", future[:-2] if week == 0 else future)
            history = History(store)
            for key, event in history.items("event-head:"):
                if event["market_observation_ms"] == now:
                    born.append(event["event_id"])
                    families.add(event["detector_id"])
            for _ in range(history.count("comparison-pending:")):
                comparison.work(now + 61 * MINUTE)
            worker.work(now + 61 * MINUTE, batch=1000)
            epoch_facts = []
            for eid in born:
                event = history.metadata("event-head:" + eid)
                epoch_facts += detail_facts(history, event)
                comp = history.metadata("comparison-head:" + identity(f"comparison/{eid}/1")) or {}
                comparator_nonempty += int(comp.get("sample_size", 0) > 0)
            manifests.append((now // DAY, epoch_facts))
            private = signature(private_state(store))
            retention = Retention(store)
            for _ in range(500):
                removed = retention.work(now + 61 * MINUTE)
                if not removed:
                    break
            else:
                raise ValueError("Retention did not finish bounded batches")
            if signature(private_state(store)) != private:
                raise ValueError("Retention changed private settings")
            expected: Counter[str] = Counter()
            for day, facts in manifests:
                if month_boundary(day, 12) > (now + 61 * MINUTE) // DAY:
                    expected.update(facts)
            if retained_facts(store) != expected:
                raise ValueError(f"Numeric/comparator/group equality failed at epoch{week}")
            live_events = history.count("event-head:")
            expected_events = sum(
                len(facts) // 4 for day, facts in manifests if day * DAY + 30 * DAY > now + 61 * MINUTE
            )
            if live_events != expected_events:
                raise ValueError("Detail population did not follow30day TTL")
            store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            samples.append(
                {
                    "week": week,
                    "events": live_events,
                    "quality_facts": store.db.execute(
                        "SELECT COALESCE(SUM(n),0) FROM market_quality"
                    ).fetchone()[0],
                    "source_rows": store.db.execute("SELECT COUNT(*) FROM market_candles").fetchone()[0],
                    "allocated_bytes": store.db.execute("PRAGMA page_count").fetchone()[0] * 4096,
                    "free_bytes": store.db.execute("PRAGMA freelist_count").fetchone()[0] * 4096,
                }
            )
            if week == 31:
                store.close()
                store = Store(path, 233382575)
                worker = OutcomeWorker(store)
                comparison = ComparisonWorker(store)
                engine = EventEngine(store, on_event=worker.admit)
                restarts += 1
        # All generations of dependent windows, jobs, summaries and shared bodies
        # must disappear after their finite lifetimes, without resetting the DB.
        end = now + 400 * DAY
        for _ in range(1000):
            if not Retention(store).work(end):
                break
        empty_tables = (
            "market_candles",
            "market_context",
            "market_context_bodies",
            "market_holds",
            "market_quality",
            "market_quality_cohorts",
            "market_quality_provenance",
            "market_quality_dictionary",
            "market_quality_hints",
            "market_numeric",
            "market_source_windows",
            "market_references",
            "market_body_ids",
            "market_cohorts",
            "market_cohort_strengths",
            "market_cohort_revisions",
            "market_cohort_names",
            "market_body_links",
        )
        remaining = {
            name: store.db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in empty_tables
        }
        if (
            any(remaining.values())
            or History(store).count("event-head:")
            or History(store).count("outcome-job:")
        ):
            raise ValueError("Expired dependency survived final controlled-clock drain")
        return {
            "schema": "cryptowatcher.retention-rehearsal.v1",
            "kind": "CONTROLLED_CLOCK_SYNTHETIC",
            "simulated_weeks": weeks,
            "families": sorted(families),
            "markets": 2,
            "timeframes": list(TIMEFRAMES),
            "numeric_and_comparator_multiset_equality": "PASS",
            "private_equality": "PASS",
            "restarts": restarts,
            "nonempty_comparator_events": comparator_nonempty,
            "final_remaining": remaining,
            "steady_detail_population": samples[10:][-12:],
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "limitations": [
                "Weekly complete local windows, not continuous market/load model",
                "File pages include indexes; no multiplication into production20GB claim",
                "Calendar12month expiry and partial outcomes are checked; real annual mix remains unobserved",
            ],
        }
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(measure(args.database)))


if __name__ == "__main__":
    main()
