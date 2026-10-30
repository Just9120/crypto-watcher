"""Controlled-clock rolling retention, episode numeric equality and bounded-population evidence.

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

from cryptowatcher import episode_summary
from cryptowatcher.config import TIMEFRAMES, OwnerSettings
from cryptowatcher.events import EventEngine, History
from cryptowatcher.market_storage import enable
from cryptowatcher.outcomes import DAY, MINUTE, OutcomeWorker, Retention
from cryptowatcher.quality_summary import month_boundary
from cryptowatcher.reset_history import private_state, signature
from cryptowatcher.source_context import save
from cryptowatcher.sources import Candle, Sources
from cryptowatcher.storage import Store, encode
from cryptowatcher.storage_repack import VERSION

NOW = 1_800_000_000_000


def fingerprint(episode: str, group: dict[str, Any], fact: dict[str, Any]) -> str:
    return encode(
        {
            "episode_id": episode,
            "group": group,
            "metrics": fact["metrics"],
            "complete": fact["state"] == "COMPLETE",
        }
    )


def detail_facts(h: History, event: dict[str, Any]) -> list[str]:
    return [
        fingerprint(event["episode_id"], group, fact)
        for _, group, _, fact in episode_summary.prepare(h.store, event)
    ]


def retained_facts(store: Store) -> Counter[str]:
    h = History(store)
    # A membership/group counts the episode once even while its root is already
    # archived and later confirmations remain live at the30-day boundary.
    facts = set()
    for key, _ in store.scan_runtime("radar:v1:event-head:"):
        event = h.metadata(key.removeprefix("radar:v1:"))
        facts.update(detail_facts(h, event))
    for episode, horizon, method, raw, dimensions in store.db.execute(
        "SELECT r.episode_id,r.horizon,r.method,r.fact,m.dimensions "
        "FROM market_episode_results r JOIN market_episode_members m ON m.episode_id=r.episode_id"
    ):
        group = {**json.loads(dimensions), "horizon_min": horizon, "methodology_version": method}
        facts.add(fingerprint(episode, group, json.loads(raw)))
    return Counter(facts)


def measure(path: Path, weeks: int = 62, *, compact: bool = False) -> dict[str, Any]:
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
    compaction: dict[str, Any] = {}
    try:
        worker = OutcomeWorker(store)
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
            worker.work(now + 61 * MINUTE, batch=1000)
            epoch_facts = []
            for eid in born:
                event = history.metadata("event-head:" + eid)
                epoch_facts += detail_facts(history, event)
                if history.count("comparison-job:"):
                    raise ValueError("Automatic N20 in current episode rehearsal")
            manifests.append((now // DAY, epoch_facts))
            private = signature(private_state(store))
            if compact and week >= 8:
                from cryptowatcher.catalogue_repack import busy
                from cryptowatcher.storage_repack import work as repack

                # Upgrade an already populated mixture of details and archived
                # quality, then keep writing/expiring through the new adapters.
                for tick in range(1000):
                    compaction = repack(store, now + 61 * MINUTE + tick, seconds=0.04)
                    if compaction["state"] == "COMPLETE" and not busy(compaction):
                        break
                else:
                    raise ValueError("Finite block repack did not finish")
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
                    expected.update(set(facts))
            if retained_facts(store) != expected:
                raise ValueError(f"Episode numeric/membership equality failed at epoch{week}")
            live_events = history.count("event-head:")
            expected_events = sum(
                len(facts) // 4 for day, facts in manifests if day * DAY + 30 * DAY > now + 61 * MINUTE
            )
            if live_events != expected_events:
                raise ValueError("Detail population did not follow30day TTL")
            store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            source_rows = store.db.execute("SELECT COUNT(*) FROM market_candles").fetchone()[0]
            if compact and compaction:
                from cryptowatcher.candle_blocks import summary as source_summary

                source_rows += source_summary(store.db)["versions"]
            samples.append(
                {
                    "week": week,
                    "events": live_events,
                    "quality_facts": store.db.execute(
                        "SELECT COUNT(*) FROM market_episode_results"
                    ).fetchone()[0],
                    "quality_memberships": store.db.execute(
                        "SELECT COUNT(*) FROM market_episode_members"
                    ).fetchone()[0],
                    "source_rows": source_rows,
                    "allocated_bytes": store.db.execute("PRAGMA page_count").fetchone()[0] * 4096,
                    "free_bytes": store.db.execute("PRAGMA freelist_count").fetchone()[0] * 4096,
                }
            )
            if week == 31:
                store.close()
                store = Store(path, 233382575)
                worker = OutcomeWorker(store)
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
            "market_episode_results",
            "market_episode_members",
            "market_episode_provenance",
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
        numeric_issued = 0
        if compact:
            from cryptowatcher.candle_blocks import summary as source_summary
            from cryptowatcher.numeric_blocks import summary as numeric_summary

            empty_tables += (
                "market_candle_blocks",
                "market_candle_fallbacks",
                "market_candle_gc",
                "market_numeric_blocks",
                "market_numeric_block_refs",
                "market_numeric_block_lookup",
                "market_numeric_transfer",
                "market_numeric_gc",
                "market_record_pending",
                "market_records",
                "market_record_parents",
                "market_record_owners",
                "market_record_members",
                "market_record_bodies",
                "market_packets",
                "market_packet_arenas",
                "market_cold_cohorts",
                "market_cold_cohort_revisions",
                "market_cold_cohort_strengths",
            )
            numeric = numeric_summary(store.db)
            numeric_issued = numeric["issued"]
            if any(numeric[k] for k in ("live", "details", "quality", "detail_bytes", "quality_bytes")):
                raise ValueError("Expired numeric block/count survived final drain")
            source = source_summary(store.db)
            if any(source.values()):
                raise ValueError("Expired source block/count survived final drain")
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
            "storage_layout": f"MIXED_BLOCKS_REPACK{VERSION}" if compact else "LEGACY_NORMALIZED",
            "compaction": compaction,
            "issued_numeric_ids_after_final_gc": numeric_issued,
            "families": sorted(families),
            "markets": 2,
            "timeframes": list(TIMEFRAMES),
            "episode_numeric_and_membership_equality": "PASS",
            "outcome_unit": "EPISODE",
            "private_equality": "PASS",
            "restarts": restarts,
            "nonempty_comparator_events": comparator_nonempty,
            "final_remaining": remaining,
            "steady_detail_population": samples[10:][-12:],
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "limitations": [
                "Weekly complete local windows, not continuous market/load model",
                "File pages include indexes; no multiplication into production35GB claim",
                "Calendar12month expiry and partial outcomes are checked; real annual mix remains unobserved",
            ],
        }
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    print(json.dumps(measure(args.database, compact=args.compact)))


if __name__ == "__main__":
    main()
