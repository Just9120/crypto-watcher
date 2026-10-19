"""Reproducible isolated measurements; never run a synthetic population in the bot.

Coefficients are scenarios, not an observed population mix, an upper bound or a
mature capacity proof. Regenerate using python -m cryptowatcher.storage_model.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from typing import Any

from . import numeric_blocks, numeric_pool, quality_packets
from .quality_layout import install as install_quality
from .quality_layout import put
from .quality_summary import create
from .storage import encode

MODEL_VERSION = "CQ4_NUMERIC2_SPARSE1_NB1_V3"
# Bounded qualification includes public facts and every private quality pool/index.
COEFFICIENTS: tuple[dict[str, Any], ...] = (
    {
        "facts_per_cohort_day": 1,
        "comparators_per_fact": 0,
        "comparator_reuse": "INDEPENDENT_FIXTURE",
        "encoded_bytes_per_fact": 140.44642857142858,
        "packet_bytes_per_fact": 139.0,
        "physical_bytes_per_fact": 219.42857142857142,
        "kind": "ISOLATED_CURRENT_CODEC_ALL_QUALITY_TABLES_AND_INDEXES",
        "recurring_cohort_days": 7,
        "cohorts": 8,
        "facts": 56,
        "model_version": MODEL_VERSION,
        "sqlite_version": "3.49.1",
        "public_sha256": "38ae547a7f2b16bd3cb61634442396961050dca7f2c79c5ae3eaae440c52b5e0",
    },
    {
        "facts_per_cohort_day": 32,
        "comparators_per_fact": 0,
        "comparator_reuse": "INDEPENDENT_FIXTURE",
        "encoded_bytes_per_fact": 40.361607142857146,
        "packet_bytes_per_fact": 40.361607142857146,
        "physical_bytes_per_fact": 48.0,
        "kind": "ISOLATED_CURRENT_CODEC_ALL_QUALITY_TABLES_AND_INDEXES",
        "recurring_cohort_days": 7,
        "cohorts": 8,
        "facts": 1792,
        "model_version": MODEL_VERSION,
        "sqlite_version": "3.49.1",
        "public_sha256": "d195564194c302fdd5942df81cf36c1ca533088f47c7d683511a5e39811bce99",
    },
    {
        "facts_per_cohort_day": 128,
        "comparators_per_fact": 0,
        "comparator_reuse": "INDEPENDENT_FIXTURE",
        "encoded_bytes_per_fact": 35.2431640625,
        "packet_bytes_per_fact": 35.2431640625,
        "physical_bytes_per_fact": 37.714285714285715,
        "kind": "ISOLATED_CURRENT_CODEC_ALL_QUALITY_TABLES_AND_INDEXES",
        "recurring_cohort_days": 7,
        "cohorts": 8,
        "facts": 7168,
        "model_version": MODEL_VERSION,
        "sqlite_version": "3.49.1",
        "public_sha256": "2bbcc4f6baba4250b85851d9590ef3b2186effb57feb0336b291c519c09b8069",
    },
    {
        "facts_per_cohort_day": 32,
        "comparators_per_fact": 20,
        "comparator_reuse": "EXPLICIT_OVERLAPPING_FIXTURE",
        "encoded_bytes_per_fact": 67.84430803571429,
        "packet_bytes_per_fact": 23.904575892857142,
        "physical_bytes_per_fact": 89.14285714285714,
        "kind": "ISOLATED_CURRENT_CODEC_ALL_QUALITY_TABLES_AND_INDEXES",
        "recurring_cohort_days": 7,
        "cohorts": 8,
        "facts": 1792,
        "model_version": MODEL_VERSION,
        "sqlite_version": "3.49.1",
        "public_sha256": "875e502841cf5d2c0fa48b84622f90435047547d63b12042341fff3941b25a2d",
    },
    {
        "facts_per_cohort_day": 32,
        "comparators_per_fact": 20,
        "comparator_reuse": "INDEPENDENT_FIXTURE",
        "encoded_bytes_per_fact": 956.0641741071429,
        "packet_bytes_per_fact": 25.491071428571427,
        "physical_bytes_per_fact": 1248.0,
        "kind": "ISOLATED_CURRENT_CODEC_ALL_QUALITY_TABLES_AND_INDEXES",
        "recurring_cohort_days": 7,
        "cohorts": 8,
        "facts": 1792,
        "model_version": MODEL_VERSION,
        "sqlite_version": "3.49.1",
        "public_sha256": "17f0950098e9181bc3ba6436ae9764ad0dcdfe4422652a38c29e3a5b40062763",
    },
)


def quality_scenarios() -> tuple[dict[str, Any], ...]:
    return tuple(dict(value) for value in COEFFICIENTS)


def metric(seed: int) -> dict[str, float]:
    return {
        "price_return_pct": math.sqrt(seed + 1) / 13,
        "directed_return_pct": math.sqrt(seed + 1) / 13,
        "mfe_pct": math.sqrt(seed + 2) / 7,
        "mae_pct": -math.sqrt(seed + 3) / 29,
    }


def measure(
    density: int, comparators: int, *, shared: bool, groups: int = 8, days: int = 7
) -> dict[str, Any]:
    """Materialize all tables; numerical entropy differs across dates/cohorts.

    Shared fixtures explicitly overlap comparator populations. Independent ones
    do not. Neither represents observed market behavior.
    """
    db = sqlite3.connect(":memory:")
    started = time.monotonic()
    try:
        db.executescript("""
            CREATE TABLE runtime(key TEXT PRIMARY KEY,payload_json BLOB);
            CREATE TABLE market_meta(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE market_counts(category TEXT PRIMARY KEY,records INTEGER,
                logical_bytes INTEGER,inserted_bytes INTEGER,deleted_bytes INTEGER);
        """)
        db.create_function("cw_numeric_refs", 2, numeric_pool.references)
        create(db)
        install_quality(db)
        numeric_pool.install(db)
        quality_packets.install(db)
        numeric_blocks.install(db)
        with db:
            db.execute("INSERT INTO market_meta VALUES('numeric_block_runtime','1')")
        page = db.execute("PRAGMA page_size").fetchone()[0]
        baseline = db.execute("PRAGMA page_count").fetchone()[0] * page
        public = hashlib.sha256()
        encoded = 0
        for group in range(groups):
            dimensions = {
                "detector": "price",
                "version": "1",
                "settings": 3,
                "profile": "custom",
                "market": "spot" if group % 2 else "linear",
                "timeframe": ("1m", "3m", "5m", "15m")[group % 4],
                "instrument": f"SYNTHETIC{group}USDT",
                "universe": "top",
                "direction": "UP",
                "horizon_min": 15,
                "methodology_version": "1",
            }
            cohort = hashlib.sha256(encode(dimensions).encode()).hexdigest()
            for day in range(days):
                facts = []
                for index in range(density):
                    seed = 1 + group * 100000 + day * 10000 + index * 32
                    comparisons = [
                        metric(1 + group * 100000 + (day // 2) * 10000 + (index // 4 + j) % 128)
                        if shared
                        else metric(seed + j)
                        for j in range(comparators)
                    ]
                    facts.append(
                        {
                            "strength": math.sqrt(seed + 2),
                            "state": "COMPLETE",
                            "previous_state": "COMPLETE",
                            "metrics": metric(seed),
                            "classification": "CONTINUATION",
                            "noise": math.sqrt(seed + 1) / 100,
                            "comparator_metrics": comparisons,
                            "comparator_sets": [
                                {
                                    "methodology_version": "1",
                                    "offset": 0,
                                    "count": comparators,
                                    "state": "COMPLETE" if comparators else "COMPLETE_LIMITED",
                                    "sample_size": comparators,
                                    "population_size": 300 + day,
                                    "limitations": [] if comparators else ["PARTIAL28DAY_SOURCE_HISTORY"],
                                }
                            ],
                            "limitations": [] if comparators else ["PARTIAL28DAY_SOURCE_HISTORY"],
                            "completeness": {"expected_candles": 15, "observed_candles": 15},
                        }
                    )
                blob = quality_packets.pack(db, facts, cohort)
                restored = quality_packets.unpack(db, blob)
                assert encode(restored) == encode(facts)
                public.update(encode(restored).encode())
                encoded += len(blob)
                put(db, (20000 + day, cohort, 0, 20366 + day, encode(dimensions), "{}", blob, density))
        physical = db.execute("PRAGMA page_count").fetchone()[0] * page - baseline
        logical = db.execute("SELECT SUM(logical_bytes) FROM market_counts").fetchone()[0]
        n = groups * days * density
        return {
            "facts_per_cohort_day": density,
            "comparators_per_fact": comparators,
            "comparator_reuse": "EXPLICIT_OVERLAPPING_FIXTURE" if shared else "INDEPENDENT_FIXTURE",
            "encoded_bytes_per_fact": logical / n,
            "packet_bytes_per_fact": encoded / n,
            "physical_bytes_per_fact": physical / n,
            "kind": "ISOLATED_CURRENT_CODEC_ALL_QUALITY_TABLES_AND_INDEXES",
            "recurring_cohort_days": days,
            "cohorts": groups,
            "facts": n,
            "model_version": MODEL_VERSION,
            "sqlite_version": sqlite3.sqlite_version,
            "public_sha256": public.hexdigest(),
            "seconds": round(time.monotonic() - started, 3),
        }
    finally:
        db.close()


def main() -> None:
    scenarios = [
        measure(density, comparators, shared=shared)
        for density, comparators, shared in (
            (1, 0, False),
            (32, 0, False),
            (128, 0, False),
            (32, 20, True),
            (32, 20, False),
        )
    ]
    print(json.dumps(scenarios, indent=2))


if __name__ == "__main__":
    main()
