"""Isolated capacity scenarios for the actual quality codec and SQLite layout.

No production DB scan. These are synthetic measurements, not observed signal
frequencies, guarantees of comparator reuse or a steady-state capacity proof.
"""

from __future__ import annotations

import hashlib
import sqlite3
from functools import lru_cache
from typing import Any

from .quality_layout import install as install_quality
from .quality_layout import put
from .quality_summary import create, encode_facts
from .storage import encode


@lru_cache(maxsize=1)
def quality_scenarios() -> tuple[dict[str, Any], ...]:
    scenarios = []
    for density, comparators in ((1, 0), (32, 0), (128, 0), (32, 20)):
        facts = []
        for i in range(density):
            metrics = {
                "price_return_pct": (i + 1) ** 0.5 / 13,
                "directed_return_pct": (i + 1) ** 0.5 / 13,
                "mfe_pct": (i + 2) ** 0.5 / 7,
                "mae_pct": -((i + 3) ** 0.5) / 29,
            }
            facts.append(
                {
                    "strength": (i + 2) ** 0.5,
                    "state": "COMPLETE",
                    "previous_state": "COMPLETE",
                    "metrics": metrics,
                    "classification": "CONTINUATION",
                    "noise": (i + 1) ** 0.5 / 100,
                    "comparator_metrics": [
                        {k: v * (j + 1) ** 0.5 for k, v in metrics.items()} for j in range(comparators)
                    ],
                    "comparator_sets": [
                        {
                            "methodology_version": "1",
                            "offset": 0,
                            "count": comparators,
                            "state": "COMPLETE" if comparators else "COMPLETE_LIMITED",
                            "sample_size": comparators,
                            "population_size": 300,
                            "limitations": [] if comparators else ["PARTIAL28DAY_SOURCE_HISTORY"],
                        }
                    ],
                    "limitations": [] if comparators else ["PARTIAL28DAY_SOURCE_HISTORY"],
                    "completeness": {"expected_candles": 15, "observed_candles": 15},
                }
            )
        blob = encode_facts(facts)
        db = sqlite3.connect(":memory:")
        try:
            create(db)
            db.execute(
                "CREATE TABLE market_counts(category TEXT PRIMARY KEY,records INTEGER,logical_bytes INTEGER,inserted_bytes INTEGER,deleted_bytes INTEGER)"
            )
            install_quality(db)
            page = db.execute("PRAGMA page_size").fetchone()[0]
            baseline = db.execute("PRAGMA page_count").fetchone()[0] * page
            for group in range(32):
                dimensions = {
                    "detector": "price",
                    "version": "1",
                    "settings": 3,
                    "profile": "custom",
                    "market": "spot",
                    "timeframe": "1m",
                    "instrument": f"SYNTHETIC{group}USDT",
                    "universe": "top",
                    "direction": "UP",
                    "horizon_min": 15,
                    "methodology_version": "1",
                }
                provenance = {"quality_provenance": {"mode": "value2", "id": "a" * 64}}
                for day in range(14):
                    put(
                        db,
                        (
                            20000 + day,
                            hashlib.sha256(encode(dimensions).encode()).hexdigest(),
                            0,
                            20366 + day,
                            encode(dimensions),
                            encode(provenance),
                            blob,
                            density,
                        ),
                    )
            physical = db.execute("PRAGMA page_count").fetchone()[0] * page - baseline
        finally:
            db.close()
        scenarios.append(
            {
                "facts_per_cohort_day": density,
                "comparators_per_fact": comparators,
                "encoded_bytes_per_fact": len(blob) / density,
                "physical_bytes_per_fact": physical / (32 * density * 14),
                "kind": "ISOLATED_CODEC_AND_SQLITE_TABLE_INDEX_MEASUREMENT",
                "recurring_cohort_days": 14,
            }
        )
    return tuple(scenarios)
