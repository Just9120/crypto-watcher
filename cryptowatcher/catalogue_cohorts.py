"""Always transactional accounting for a catalogue born empty.

Its generation is independent of the finite legacy rowid scan, so a VACUUM or
additive DDL cannot make cold events disappear from a rebuilt hot generation.
Readers select the combined source only after the complete runtime adapter is
explicitly activated. Installing these primitives never activates it.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import cohort_meter

TABLES = ("market_cohorts", "market_cohort_revisions", "market_cohort_strengths")


def cold(sql: str) -> str:
    for table in (*TABLES, "market_cohort_scan"):
        sql = sql.replace(table, table.replace("market_", "market_cold_", 1))
    return sql.replace("CREATE TRIGGER market_cohort_names_", "CREATE TRIGGER market_cold_cohort_names_")


def install(db: sqlite3.Connection) -> None:
    marker = db.execute("SELECT value FROM market_meta WHERE key='catalogue_cohorts'").fetchone()
    if marker is not None:
        if marker[0] != "1":
            raise ValueError("Unknown catalogue counters; database preserved")
        return
    version = db.execute("SELECT version FROM market_cohort_scan WHERE id=1").fetchone()
    if version is None or version[0] != 1:
        raise ValueError("Unsupported prospective source layout")
    if db.in_transaction or db.execute("SELECT 1 FROM market_records LIMIT 1").fetchone():
        raise ValueError("Initial catalogue counters require an empty outer boundary")
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE market_cold_cohort_scan(
            id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL CHECK(version=1),
            generation INTEGER NOT NULL CHECK(generation=1),unknown INTEGER NOT NULL DEFAULT 0 CHECK(unknown>=0))""")
        db.execute("INSERT INTO market_cold_cohort_scan VALUES(1,1,1,0)")
        db.execute("""CREATE TABLE market_catalogue_counts(
            kind INTEGER PRIMARY KEY CHECK(kind BETWEEN 0 AND 4),
            n INTEGER NOT NULL CHECK(typeof(n)='integer' AND n>=0),
            heads INTEGER NOT NULL CHECK(typeof(heads)='integer' AND heads BETWEEN 0 AND n))""")
        db.executemany("INSERT INTO market_catalogue_counts VALUES(?,0,0)", [(i,) for i in range(5)])
        for table in TABLES:
            definition = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if definition is None:
                raise ValueError("Missing prospective source table")
            db.execute(cold(definition[0]))
            triggers = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name=? ORDER BY name", (table,)
            ).fetchall()
            for trigger in triggers:
                db.execute(cold(trigger[0]))
            target = table.replace("market_", "market_cold_", 1)
            for operation in ("INSERT", "UPDATE"):
                db.execute(
                    f"CREATE TRIGGER {target}_guard_{operation} BEFORE {operation} ON {target} "
                    "WHEN typeof(NEW.n)<>'integer' OR NEW.n<0 OR NEW.generation<>1 BEGIN "
                    "SELECT RAISE(ABORT,'Invalid catalogue cohort counter'); END"
                )
        for operation, sign, alias in (("INSERT", 1, "NEW"), ("DELETE", -1, "OLD")):
            expression = (
                f"cw_cohort_record(cw_catalogue_key({alias}.kind,{alias}.public_uuid,{alias}.revision),"
                f"cw_catalogue_record({alias}.kind,{alias}.public_uuid,{alias}.revision,{alias}.packet,{alias}.slot))"
            )
            commands = ";".join(cold(v) for v in cohort_meter.modifications(expression, sign, "1"))
            db.execute(
                f"CREATE TRIGGER market_catalogue_cohort_{operation} AFTER {operation} ON market_records WHEN {alias}.kind=0 BEGIN {commands}; END"
            )
        db.execute("""CREATE TRIGGER market_catalogue_count_insert BEFORE INSERT ON market_records BEGIN
            SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM market_catalogue_counts WHERE kind=NEW.kind)
                OR NOT EXISTS(SELECT 1 FROM market_cold_cohort_scan WHERE id=1 AND version=1 AND generation=1)
                THEN RAISE(ABORT,'Missing catalogue namespace counter') END;
        END""")
        db.execute("""CREATE TRIGGER market_catalogue_count_added AFTER INSERT ON market_records BEGIN
            UPDATE market_catalogue_counts SET n=n+1,heads=heads+NEW.current WHERE kind=NEW.kind;
        END""")
        db.execute("""CREATE TRIGGER market_catalogue_count_delete BEFORE DELETE ON market_records BEGIN
            SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM market_catalogue_counts WHERE kind=OLD.kind AND n>0 AND heads>=OLD.current)
                OR NOT EXISTS(SELECT 1 FROM market_cold_cohort_scan WHERE id=1 AND version=1 AND generation=1)
                THEN RAISE(ABORT,'Missing catalogue namespace counter') END;
        END""")
        db.execute("""CREATE TRIGGER market_catalogue_count_removed AFTER DELETE ON market_records BEGIN
            UPDATE market_catalogue_counts SET n=n-1,heads=heads-OLD.current WHERE kind=OLD.kind;
        END""")
        db.execute("""CREATE TRIGGER market_catalogue_head_guard BEFORE UPDATE OF current ON market_records BEGIN
            SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM market_catalogue_counts WHERE kind=OLD.kind AND n>0 AND heads>=OLD.current)
                THEN RAISE(ABORT,'Missing catalogue head counter') END;
        END""")
        db.execute("""CREATE TRIGGER market_catalogue_head_changed AFTER UPDATE OF current ON market_records BEGIN
            UPDATE market_catalogue_counts SET heads=heads+NEW.current-OLD.current WHERE kind=OLD.kind;
        END""")
        db.execute("INSERT INTO market_meta VALUES('catalogue_cohorts','1')")
        db.commit()
    except BaseException:
        db.rollback()
        raise


def active(db: sqlite3.Connection) -> bool:
    value = db.execute("SELECT value FROM market_meta WHERE key='record_catalogue_runtime'").fetchone()
    if value is not None and value[0] != "1":
        raise ValueError("Unknown catalogue runtime adapter")
    return value is not None


def status(db: sqlite3.Connection) -> dict[str, Any]:
    state = db.execute("SELECT version,generation,unknown FROM market_cold_cohort_scan WHERE id=1").fetchone()
    counts = db.execute("SELECT kind,n,heads FROM market_catalogue_counts ORDER BY kind").fetchall()
    if (
        state is None
        or state[0] != 1
        or state[1] != 1
        or state[2] < 0
        or [r[0] for r in counts] != list(range(5))
    ):
        return {"state": "UNSET_CATALOGUE_COUNTER_HEADER"}
    events = db.execute(
        "SELECT COALESCE(SUM(n),0) FROM market_cold_cohort_revisions WHERE generation=1"
    ).fetchone()[0]
    if events + state[2] != counts[0][1]:
        return {"state": "UNSET_CATALOGUE_COUNTER_MISMATCH"}
    if state[2]:
        return {"state": "UNSET_EVENT_METADATA", "unclassified_event_records": state[2]}
    return {"state": "COMPLETE", "event_records": events, "namespaces": [tuple(r) for r in counts]}


def query(db: sqlite3.Connection, table: str, generation: int) -> tuple[str, tuple[int, ...]]:
    if table not in TABLES:
        raise ValueError("Unknown cohort query source")
    fields = (
        ",".join(cohort_meter.FIELDS)
        if table == "market_cohorts"
        else "revision"
        if table == "market_cohort_revisions"
        else "value"
    )
    if not active(db):
        return f"SELECT {fields},n FROM {table} WHERE generation=?", (generation,)
    cold_table = table.replace("market_", "market_cold_", 1)
    source = f"(SELECT {fields},n FROM {table} WHERE generation=? UNION ALL SELECT {fields},n FROM {cold_table} WHERE generation=1)"
    return f"SELECT {fields},SUM(n) AS n FROM {source} GROUP BY {fields}", (generation,)
