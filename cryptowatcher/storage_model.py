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

MODEL_VERSION = "CQ5_NUMERIC2_SPARSE1_NB1_V4"
# Bounded qualification includes public facts and every private quality pool/index.
COEFFICIENTS: tuple[dict[str, Any], ...] = (
    {
        "facts_per_cohort_day": 1,
        "comparators_per_fact": 0,
        "comparator_reuse": "INDEPENDENT_FIXTURE",
        "encoded_bytes_per_fact": 139.41071428571428,
        "packet_bytes_per_fact": 138.0,
        "physical_bytes_per_fact": 219.42857142857142,
        "kind": "ISOLATED_CURRENT_CODEC_ALL_QUALITY_TABLES_AND_INDEXES",
        "recurring_cohort_days": 7,
        "cohorts": 8,
        "facts": 56,
        "model_version": "CQ5_NUMERIC2_SPARSE1_NB1_V4",
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
        "model_version": "CQ5_NUMERIC2_SPARSE1_NB1_V4",
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
        "model_version": "CQ5_NUMERIC2_SPARSE1_NB1_V4",
        "sqlite_version": "3.49.1",
        "public_sha256": "2bbcc4f6baba4250b85851d9590ef3b2186effb57feb0336b291c519c09b8069",
    },
    {
        "facts_per_cohort_day": 32,
        "comparators_per_fact": 20,
        "comparator_reuse": "EXPLICIT_OVERLAPPING_FIXTURE",
        "encoded_bytes_per_fact": 64.18582589285714,
        "packet_bytes_per_fact": 23.719866071428573,
        "physical_bytes_per_fact": 75.42857142857143,
        "kind": "ISOLATED_CURRENT_CODEC_ALL_QUALITY_TABLES_AND_INDEXES",
        "recurring_cohort_days": 7,
        "cohorts": 8,
        "facts": 1792,
        "model_version": "CQ5_NUMERIC2_SPARSE1_NB1_V4",
        "sqlite_version": "3.49.1",
        "public_sha256": "875e502841cf5d2c0fa48b84622f90435047547d63b12042341fff3941b25a2d",
    },
    {
        "facts_per_cohort_day": 32,
        "comparators_per_fact": 20,
        "comparator_reuse": "INDEPENDENT_FIXTURE",
        "encoded_bytes_per_fact": 875.7745535714286,
        "packet_bytes_per_fact": 26.05078125,
        "physical_bytes_per_fact": 1106.2857142857142,
        "kind": "ISOLATED_CURRENT_CODEC_ALL_QUALITY_TABLES_AND_INDEXES",
        "recurring_cohort_days": 7,
        "cohorts": 8,
        "facts": 1792,
        "model_version": "CQ5_NUMERIC2_SPARSE1_NB1_V4",
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


EPISODE_MODEL_VERSION = "EPISODE_SQLITE_V1"
# Rounded above isolated 512-episode, 1/4/20-membership all-table/page results.
# Full unshared provenance per origin/member; actual production reuse is not
# assumed. These are explicit model coefficients, not a mathematical bound.
EPISODE_RESULT_PHYSICAL = 2048
EPISODE_MEMBER_PHYSICAL = 2048
EPISODE_RESULT_LOGICAL = 1024
EPISODE_MEMBER_LOGICAL = 1536


def measure_episodes(*, episodes: int = 512, members: int = 4) -> dict[str, Any]:
    """Isolated exact writer/index qualification, never called by runtime.

    Every origin/member has distinct provenance: deliberately no saving from
    settings/definition reuse. Values vary at full float precision. Source
    arrays are absent because 12-month results do not own them.
    """
    from dataclasses import asdict
    from uuid import NAMESPACE_URL, uuid5

    from .config import OwnerSettings
    from .episode_summary import archive_records, install
    from .outcomes import DEFINITION

    if not 1 <= episodes <= 4096 or not 1 <= members <= 20:
        raise ValueError("Bounded qualification requires 1..4096 episodes and 1..20 members")
    db = sqlite3.connect(":memory:")
    try:
        db.executescript("""
            CREATE TABLE market_meta(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE market_counts(category TEXT PRIMARY KEY,records INTEGER,
                logical_bytes INTEGER,inserted_bytes INTEGER,deleted_bytes INTEGER);
        """)
        install(db)
        empty_pages = db.execute("PRAGMA page_count").fetchone()[0]
        settings = asdict(OwnerSettings())
        digest = hashlib.sha256()
        with db:
            for i in range(episodes):
                episode = str(uuid5(NAMESPACE_URL, f"episode-storage/{i}"))
                origin = {
                    "settings_snapshot": settings,
                    "settings_revision": i + 1,
                    "outcome_definition": DEFINITION,
                    "effective_parameters": {"price_pct": 2.0, "turnover_ratio": 3.0},
                    "producer_source_revision": hashlib.sha256(f"revision/{i}".encode()).hexdigest(),
                    "settings_sha256": hashlib.sha256(encode(settings).encode()).hexdigest(),
                }
                prepared = []
                for j in range(members):
                    dimensions = {
                        "detector": ("price", "turnover", "breakout", "volatility", "composite")[j % 5],
                        "version": "1",
                        "settings": i + 1,
                        "profile": "custom",
                        "market": ("spot", "linear")[i % 2],
                        "timeframe": ("1m", "3m", "5m", "15m")[j % 4],
                        "instrument": f"ASSET{i % 512}USDT",
                        "universe": "top",
                        "direction": "up",
                        "strength": math.sqrt(i + j + 1) / 7,
                    }
                    membership = {
                        "settings_snapshot": settings,
                        "detector_definition": {
                            "id": dimensions["detector"],
                            "version": "1",
                            "semantics": "closed candle; frozen inputs; prior-only baseline",
                        },
                        "effective_parameters": {"price_pct": 2.0, "turnover_ratio": 3.0},
                        "producer_source_revision": hashlib.sha256(f"member/{i}/{j}".encode()).hexdigest(),
                    }
                    for h in (5, 15, 30, 60):
                        fact = {
                            "episode_id": episode,
                            "state": "COMPLETE",
                            "previous_state": "COMPLETE",
                            "metrics": metric(i * 60 + h),
                            "classification": "CONTINUATION",
                            "noise": math.sqrt(i + h + 1) / 100,
                            "completeness": {"expected_candles": h, "observed_candles": h},
                        }
                        row = (
                            20756 + i % 30,
                            {**dimensions, "horizon_min": h, "methodology_version": "2"},
                            {"origin": origin, "membership": membership},
                            fact,
                        )
                        prepared.append(row)
                        digest.update(encode(row).encode())
                archive_records(db, prepared)
        physical = (db.execute("PRAGMA page_count").fetchone()[0] - empty_pages) * db.execute(
            "PRAGMA page_size"
        ).fetchone()[0]
        return {
            "model_version": EPISODE_MODEL_VERSION,
            "sqlite_version": sqlite3.sqlite_version,
            "episodes": episodes,
            "members_per_episode": members,
            "horizons": 4,
            "encoded_bytes_per_episode": db.execute(
                "SELECT logical_bytes FROM market_counts WHERE category='quality_episodes'"
            ).fetchone()[0]
            / episodes,
            "physical_bytes_per_episode": physical / episodes,
            "public_sha256": digest.hexdigest(),
            "empty_layout_bytes": empty_pages * db.execute("PRAGMA page_size").fetchone()[0],
            "assumption": "ONE_UNSHARED_FULL_PROVENANCE_PER_ORIGIN_AND_MEMBERSHIP",
        }
    finally:
        db.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy", action="store_true", help="Explicit CQ5/N20 compatibility qualification")
    if parser.parse_args().legacy:
        main()
    else:
        print(json.dumps([measure_episodes(members=n) for n in (1, 4, 20)], indent=2))
