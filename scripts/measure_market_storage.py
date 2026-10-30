"""Offline measured storage fixture. This does not predict production event frequency."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from cryptowatcher.analytics import cohort_rows
from cryptowatcher.config import OwnerSettings
from cryptowatcher.episode_summary import installed as episode_archive_installed
from cryptowatcher.events import EventEngine, History
from cryptowatcher.market_storage import enable
from cryptowatcher.outcomes import DAY, MINUTE, OutcomeWorker, Retention
from cryptowatcher.sources import Candle, Sources
from cryptowatcher.storage import Store
from cryptowatcher.storage_meter import measure_indexes, report
from cryptowatcher.telegram_delivery import TelegramDelivery

NOW = 1_800_000_000_000


def measure(path: Path, instruments: int = 2) -> dict[str, Any]:
    if path.exists() or not 1 <= instruments <= 20:
        raise ValueError("Measurement requires a new fixture path and1..20 instruments")
    start = time.monotonic()
    with closing(Store(path, 233382575)) as store:
        store.initialize(OwnerSettings())
        enable(store.db)
        store.bounded = True
        store.clock_ms = lambda: NOW
        worker = OutcomeWorker(store)
        engine = EventEngine(store, on_event=worker.admit)
        for market in ("spot", "linear"):
            for instrument in range(instruments):
                symbol = f"TEST{instrument}USDT"
                for tf in ("1m", "3m", "5m", "15m"):
                    step = int(tf[:-1]) * MINUTE
                    candles = [
                        Candle.parse(
                            [
                                NOW - (24 - i) * step,
                                100,
                                104 if i == 23 else 100.1,
                                99 if i == 23 else 99.9,
                                103 if i == 23 else 100,
                                1,
                                1600 if i == 23 else 400,
                            ],
                            tf,
                            "fixture",
                            NOW,
                            NOW,
                        )
                        for i in range(24)
                    ]
                    Sources(store).put(f"{market}/{symbol}/{tf}", candles)
                    engine.observe(f"{market}/{symbol}/{tf}", candles, 2e6, 0.01, NOW, live=True)
                future = [
                    Candle.parse(
                        [NOW + i * MINUTE, 103, 105 + i * 0.01, 102, 104 + i * 0.01, 1, 200],
                        "1m",
                        "fixture",
                        NOW + 60 * MINUTE,
                        NOW + 60 * MINUTE,
                    )
                    for i in range(60)
                ]
                Sources(store).put(f"{market}/{symbol}/1m", future)
        count = History(store).count("event-head:")
        history = History(store)
        episode_count = history.count("episode-head:")
        worker.work(NOW + 61 * MINUTE, batch=episode_count * 4 + 1)
        results = history.items("outcome-head:")
        complete_count = sum(row["state"] == "COMPLETE" for _, row in results)
        if history.count("comparison-job:") or history.count("comparison-pending:"):
            raise ValueError("Automatic legacy comparison admission in episode fixture")

        async def send() -> None:
            class Bot:
                async def send_message(self, *args: Any, **kwargs: Any) -> Any:
                    assert not store.db.in_transaction
                    return SimpleNamespace(message_id=1)

            delivery = TelegramDelivery(
                store, store.owner_id, lambda *args: "measured fixture", lambda event: None
            )
            for _ in range(instruments * 2 + 1):
                await delivery.send_due(Bot(), NOW + 2001)

        asyncio.run(send())
        store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        before = report(store, NOW)
        indexes = measure_indexes(store, seconds=1)
        cohorts_before = list(cohort_rows(store))
        families = {r["detector_id"] for r in History(store).events(count + 1)}
        retention = Retention(store)
        for _ in range(count + 20):
            retention.work(NOW + 30 * DAY + 1)
            if History(store).count("history:") == 0:
                break
        cohorts_after = list(cohort_rows(store))
        if cohorts_before != cohorts_after or len(families) != 5:
            raise ValueError("Measurement analytics/family equality failed")
        after = report(store, NOW + 30 * DAY + 1)
        quality_bytes = after["categories"].get("quality_episodes", {}).get("logical_bytes", 0)
        assert episode_archive_installed(store.db)
        return {
            "schema": "cryptowatcher.storage-measurement.v1",
            "kind": "ISOLATED_SYNTHETIC_MEASUREMENT",
            "events": count,
            "episodes": episode_count,
            "outcome_unit": "EPISODE",
            "automatic_comparison_jobs": 0,
            "markets": 2,
            "timeframes": ["1m", "3m", "5m", "15m"],
            "families": sorted(families),
            "outcomes": len(results),
            "all_outcomes_complete": complete_count == len(results) == episode_count * 4,
            "group_sample_sizes_are_non_additive": True,
            "analytics_equality": "PASS",
            "details_working_bytes": before["working_bytes"],
            "details_bytes_per_event_including_sources_services_indexes": before["working_bytes"] / count,
            "quality_encoded_bytes": quality_bytes,
            "quality_encoded_bytes_per_event": quality_bytes / count,
            "quality_encoded_bytes_per_episode": quality_bytes / episode_count,
            "categories_before_expiry": before["categories"],
            "categories_after_expiry": after["categories"],
            "indexes": indexes,
            "elapsed_seconds": round(time.monotonic() - start, 3),
            "limitations": [
                "Synthetic values and event mix, not production signal frequency",
                "Fixed file/page overhead matters on this small fixture",
                "No automatic N20; overlapping detector/TF groups reuse the same episode outcome",
                "Source sharing and service cost vary with market activity",
                "12month capacity projection must use actual production arrival rates",
            ],
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--instruments", type=int, default=2)
    args = parser.parse_args()
    print(json.dumps(measure(args.database, args.instruments)))


if __name__ == "__main__":
    main()
