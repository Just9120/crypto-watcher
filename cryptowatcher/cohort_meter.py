"""Exact prospective cohorts without decoding millions of events in a probe.

Auxiliary generations are disposable accounting, never history. Initialization,
concurrent runtime changes and its checkpoint share the writer transaction.
"""

from __future__ import annotations

import math
import sqlite3
import struct
import time
from collections import OrderedDict
from typing import Any

NAMES = ("detector", "version", "profile", "market", "timeframe", "instrument", "universe", "direction")
FIELDS = (
    "day",
    "detector",
    "version",
    "settings",
    "profile",
    "market",
    "timeframe",
    "instrument",
    "universe",
    "direction",
    "horizon",
    "methodology",
)
GEN = "(SELECT generation FROM market_cohort_scan WHERE id=1)"
MAX_CACHE = 1024 * 1024


class Projector:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self.cache: OrderedDict[Any, Any] = OrderedDict()
        self.size = 0

    def read(self, key: str, raw: Any) -> str:
        from .history_codec import BLOB_PREFIX, MARKER, SCHEMA, Codec
        from .storage import StateError, encode, json_payload

        if not key.startswith("radar:v1:event:"):
            return '{"known":0,"names":[]}'
        token = (key, raw)
        cached = self.cache.get(token)
        if cached is not None:
            cached_bodies, result, _ = cached
            if all(
                (found := self.db.execute("SELECT payload_json FROM runtime WHERE key=?", (body,)).fetchone())
                is not None
                and found[0] == payload
                for body, payload in cached_bodies
            ):
                self.cache.move_to_end(token)
                return result
        event = Codec.record_value(json_payload(raw))
        result = None
        bodies: list[tuple[str, Any]] = []
        cacheable = True
        if isinstance(event, dict):
            settings = event.get("settings_snapshot")
            storage = event.get(MARKER)
            if storage is not None:
                if (
                    not isinstance(storage, dict)
                    or set(storage) != {"schema", "fields"}
                    or storage["schema"] != SCHEMA
                    or not isinstance(storage["fields"], dict)
                ):
                    raise StateError("Invalid prospective shared fields")
                reference = storage["fields"].get("settings_snapshot")
                if reference is not None:
                    restored = Codec(self.db).field(reference)
                    projection = (
                        {k: restored[k] for k in ("profile", "alert_universe_mode") if k in restored}
                        if isinstance(restored, dict)
                        else None
                    )
                    if settings is not None and settings != projection:
                        raise StateError("Conflicting prospective settings projection")
                    settings = restored
                    cacheable = reference.get("mode") in ("value", "value2")
                    ids = [reference["id"]] if reference.get("mode") == "value2" else reference.get("ids", [])
                    for identity in ids:
                        body = BLOB_PREFIX + identity
                        value = self.db.execute(
                            "SELECT payload_json FROM runtime WHERE key=?", (body,)
                        ).fetchone()
                        if value is None:
                            raise StateError("Missing prospective settings")
                        bodies.append((body, value[0]))
            required = {
                "revision",
                "event_id",
                "market_observation_ms",
                "detector_id",
                "detector_version",
                "settings_revision",
                "market_type",
                "timeframe",
                "symbol",
                "direction",
                "strength",
            }
            if (
                required <= event.keys()
                and isinstance(settings, dict)
                and {"outcome_horizons", "alert_universe_mode"} <= settings.keys()
            ):
                names = dict(
                    zip(
                        NAMES,
                        (
                            event["detector_id"],
                            event["detector_version"],
                            settings.get("profile", "custom"),
                            event["market_type"],
                            event["timeframe"],
                            event["symbol"],
                            settings["alert_universe_mode"],
                            event["direction"],
                        ),
                    )
                )
                horizons = settings["outcome_horizons"]
                strength = event["strength"]
                try:
                    converted = float(strength)
                except (TypeError, ValueError, OverflowError):
                    converted = float("nan")
                if (
                    all(isinstance(v, str) and v for v in names.values())
                    and type(event["revision"]) is int
                    and event["revision"] > 0
                    and event["revision"] < 2**63
                    and type(event["settings_revision"]) is int
                    and event["settings_revision"] > 0
                    and event["settings_revision"] < 2**63
                    and type(event["market_observation_ms"]) is int
                    and event["market_observation_ms"] >= 0
                    and key == f"radar:v1:event:{event['event_id']}:{event['revision']:010d}"
                    and type(strength) in (int, float)
                    and (type(strength) is int or math.isfinite(converted))
                    and isinstance(horizons, list)
                    and horizons
                    and all(type(h) is int and 0 < h < 2**63 for h in horizons)
                    and len(set(horizons)) == len(horizons)
                ):
                    strength_key = (
                        struct.pack(">d", 0.0 if strength == 0 else converted).hex()
                        if converted == strength
                        else "I" + str(strength)
                    )
                    result = {
                        "known": 1,
                        "day": event["market_observation_ms"] // 86400000,
                        "revision": event["revision"],
                        "settings": event["settings_revision"],
                        "strength": strength_key,
                        "horizons": horizons,
                        **names,
                        "names": list(names.values()),
                    }
        answer = encode(result or {"known": 0, "names": []})
        charge = (
            len(key.encode())
            + len(raw.encode() if isinstance(raw, str) else raw)
            + len(answer.encode())
            + sum(len(p.encode() if isinstance(p, str) else p) for _, p in bodies)
            + 256
        )
        if cacheable and charge <= MAX_CACHE:
            if token in self.cache:
                self.size -= self.cache.pop(token)[2]
            while self.cache and self.size + charge > MAX_CACHE:
                self.size -= self.cache.popitem(last=False)[1][2]
            self.cache[token] = (bodies, answer, charge)
            self.size += charge
        return answer


def install_functions(db: sqlite3.Connection) -> None:
    db.create_function("cw_cohort_record", 2, Projector(db).read)


def modifications(expression: str, sign: int, condition: str) -> list[str]:
    def value(field: str) -> str:
        return f"json_extract({expression},'$.{field}')"

    valid = f"({condition}) AND {value('known')}=1"
    coordinates = {field: value(field) for field in ("day", "settings")}
    coordinates.update(
        {field: f"(SELECT id FROM market_cohort_names WHERE value={value(field)})" for field in NAMES}
    )
    coordinates.update(horizon="h.value", methodology="1")
    select = (
        f"SELECT {GEN},"
        + ",".join(coordinates[field] for field in FIELDS)
        + f" FROM json_each({expression},'$.horizons') h WHERE {valid}"
    )
    where = f"(generation,{','.join(FIELDS)}) IN({select})"
    statements = [
        f"UPDATE market_cohort_scan SET unknown=unknown+({sign}) WHERE id=1 AND ({condition}) AND {value('known')}=0"
    ]
    if sign > 0:
        statements += [
            f"INSERT INTO market_cohort_names(value) SELECT value FROM json_each({expression},'$.names') WHERE {valid} ON CONFLICT(value) DO NOTHING",
            f"INSERT INTO market_cohort_revisions SELECT {GEN},{value('revision')},1 WHERE {valid} ON CONFLICT(generation,revision) DO UPDATE SET n=n+1",
            f"INSERT INTO market_cohort_strengths SELECT {GEN},{value('strength')},1 WHERE {valid} ON CONFLICT(generation,value) DO UPDATE SET n=n+1",
            "INSERT INTO market_cohorts SELECT "
            + f"{GEN},"
            + ",".join(coordinates[field] for field in FIELDS)
            + f",1 FROM json_each({expression},'$.horizons') h WHERE {valid} ON CONFLICT(generation,{','.join(FIELDS)}) DO UPDATE SET n=n+1",
        ]
    else:
        statements += [
            f"SELECT CASE WHEN {valid} AND ((SELECT COUNT(*) FROM market_cohorts WHERE {where} AND n>0)<>json_array_length({expression},'$.horizons') OR NOT EXISTS(SELECT 1 FROM market_cohort_revisions WHERE generation={GEN} AND revision={value('revision')} AND n>0) OR NOT EXISTS(SELECT 1 FROM market_cohort_strengths WHERE generation={GEN} AND value={value('strength')} AND n>0)) THEN RAISE(ABORT,'Missing prospective counter') END",
            f"UPDATE market_cohorts SET n=n-1 WHERE {where}",
            f"UPDATE market_cohort_revisions SET n=n-1 WHERE generation={GEN} AND revision={value('revision')} AND {valid}",
            f"UPDATE market_cohort_strengths SET n=n-1 WHERE generation={GEN} AND value={value('strength')} AND {valid}",
            f"DELETE FROM market_cohorts WHERE generation={GEN} AND n=0",
            f"DELETE FROM market_cohort_revisions WHERE generation={GEN} AND n=0",
            f"DELETE FROM market_cohort_strengths WHERE generation={GEN} AND n=0",
        ]
    return statements


def install(db: sqlite3.Connection) -> None:
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='market_cohort_scan'").fetchone():
        version = db.execute("SELECT version FROM market_cohort_scan WHERE id=1").fetchone()
        if version is None or version[0] != 1:
            raise ValueError("Unknown prospective counter; database preserved")
        return
    if db.in_transaction:
        raise ValueError("Cohort meter installation requires an outer boundary")
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "CREATE TABLE market_cohort_scan(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL CHECK(version=1),generation INTEGER NOT NULL CHECK(generation>0),cursor INTEGER,ceiling INTEGER NOT NULL,schema_version INTEGER NOT NULL,complete INTEGER NOT NULL CHECK(complete IN(0,1)),unknown INTEGER NOT NULL DEFAULT 0 CHECK(unknown>=0))"
        )
        db.execute(
            "CREATE TABLE market_cohort_names(id INTEGER PRIMARY KEY,value TEXT NOT NULL UNIQUE,refs INTEGER NOT NULL DEFAULT 0 CHECK(refs>=0))"
        )
        db.execute("CREATE INDEX market_cohort_names_unused ON market_cohort_names(id) WHERE refs=0")
        columns = ",".join(
            field + " INTEGER NOT NULL" + (" REFERENCES market_cohort_names(id)" if field in NAMES else "")
            for field in FIELDS
        )
        db.execute(
            f"CREATE TABLE market_cohorts(generation INTEGER NOT NULL,{columns},n INTEGER NOT NULL CHECK(n>=0),PRIMARY KEY(generation,{','.join(FIELDS)})) WITHOUT ROWID"
        )
        db.execute("CREATE INDEX market_cohorts_zero ON market_cohorts(generation) WHERE n=0")
        db.execute(
            "CREATE TABLE market_cohort_strengths(generation INTEGER,value TEXT,n INTEGER NOT NULL CHECK(n>=0),PRIMARY KEY(generation,value)) WITHOUT ROWID"
        )
        db.execute(
            "CREATE INDEX market_cohort_strengths_zero ON market_cohort_strengths(generation) WHERE n=0"
        )
        db.execute(
            "CREATE TABLE market_cohort_revisions(generation INTEGER,revision INTEGER,n INTEGER NOT NULL CHECK(n>=0),PRIMARY KEY(generation,revision)) WITHOUT ROWID"
        )
        for table, size in (
            ("market_cohorts", "112"),
            ("market_cohort_strengths", "length(CAST({alias}.value AS BLOB))+16"),
            ("market_cohort_revisions", "24"),
            ("market_cohort_names", "length(CAST({alias}.value AS BLOB))+16"),
        ):
            new, old = size.format(alias="NEW"), size.format(alias="OLD")
            db.execute(f"""CREATE TRIGGER {table}_bytes_insert AFTER INSERT ON {table} BEGIN
                INSERT INTO market_counts VALUES('prospective_indexes',1,{new},{new},0)
                ON CONFLICT(category) DO UPDATE SET records=records+1,
                    logical_bytes=logical_bytes+excluded.logical_bytes,inserted_bytes=inserted_bytes+excluded.inserted_bytes;
            END""")
            db.execute(f"""CREATE TRIGGER {table}_bytes_delete AFTER DELETE ON {table} BEGIN
                UPDATE market_counts SET records=records-1,logical_bytes=logical_bytes-{old},
                    deleted_bytes=deleted_bytes+{old} WHERE category='prospective_indexes';
            END""")
        for operation, sign, alias in (("INSERT", 1, "NEW"), ("DELETE", -1, "OLD")):
            db.execute(
                f"CREATE TRIGGER market_cohort_names_{operation} AFTER {operation} ON market_cohorts BEGIN UPDATE market_cohort_names SET refs=refs+({sign}) WHERE id IN({','.join(alias + '.' + field for field in NAMES)}); END"
            )
        for operation in ("INSERT", "DELETE", "UPDATE"):
            statements = []
            for alias, sign in (("OLD", -1), ("NEW", 1)):
                if (alias == "OLD" and operation == "INSERT") or (alias == "NEW" and operation == "DELETE"):
                    continue
                condition = f"{alias}.key GLOB 'radar:v1:event:*' AND EXISTS(SELECT 1 FROM market_cohort_scan WHERE id=1 AND (complete=1 OR {alias}.rowid<=cursor OR {alias}.rowid>ceiling))"
                statements.extend(
                    modifications(f"cw_cohort_record({alias}.key,{alias}.payload_json)", sign, condition)
                )
            when = (
                "NEW.key GLOB 'radar:v1:event:*'"
                if operation == "INSERT"
                else "OLD.key GLOB 'radar:v1:event:*'"
                if operation == "DELETE"
                else "OLD.key GLOB 'radar:v1:event:*' OR NEW.key GLOB 'radar:v1:event:*'"
            )
            db.execute(
                f"CREATE TRIGGER market_cohort_{operation} AFTER {operation} ON runtime WHEN {when} BEGIN {';'.join(statements)}; END"
            )
        last = db.execute("SELECT rowid FROM runtime ORDER BY rowid DESC LIMIT 1").fetchone()
        db.execute(
            "INSERT INTO market_cohort_scan VALUES(1,1,1,NULL,?,?,?,0)",
            (last[0] if last else 0, db.execute("PRAGMA schema_version").fetchone()[0], int(last is None)),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def collect(db: sqlite3.Connection, generation: int, batch: int) -> int:
    removed = 0
    for table, columns in (
        ("market_cohorts", ("generation", *FIELDS)),
        ("market_cohort_strengths", ("generation", "value")),
        ("market_cohort_revisions", ("generation", "revision")),
    ):
        names = ",".join(columns)
        removed += db.execute(
            f"DELETE FROM {table} WHERE ({names}) IN(SELECT {names} FROM {table} WHERE generation<? ORDER BY {names} LIMIT ?)",
            (generation, batch),
        ).rowcount
    removed += db.execute(
        "DELETE FROM market_cohort_names WHERE id IN(SELECT id FROM market_cohort_names WHERE refs=0 LIMIT ?)",
        (batch,),
    ).rowcount
    return removed


def drain(db: sqlite3.Connection, batch: int, deadline: float) -> bool:
    state = db.execute(
        "SELECT generation,cursor,ceiling,schema_version,complete FROM market_cohort_scan WHERE id=1"
    ).fetchone()
    schema = db.execute("PRAGMA schema_version").fetchone()[0]
    if state[3] != schema:
        last = db.execute("SELECT rowid FROM runtime ORDER BY rowid DESC LIMIT 1").fetchone()
        upper = last[0] if last else 0
        db.execute(
            "UPDATE market_cohort_scan SET generation=generation+1,cursor=NULL,ceiling=?,schema_version=?,complete=?,unknown=0 WHERE id=1",
            (upper, schema, int(last is None)),
        )
        state = (state[0] + 1, None, upper, schema, int(last is None))
    collect(db, state[0], batch)
    if state[4]:
        return True
    rows = db.execute(
        "SELECT rowid,key,CASE WHEN key GLOB 'radar:v1:event:*' THEN payload_json END FROM runtime WHERE rowid"
        + (">=?" if state[1] is None else ">?")
        + " AND rowid<=? ORDER BY rowid LIMIT ?",
        (-(2**63) if state[1] is None else state[1], state[2], batch),
    ).fetchall()
    cursor = state[1]
    processed = 0
    commands = modifications("cw_cohort_record(?,?)", 1, "1")
    for identity, key, raw in rows:
        if processed and time.monotonic() >= deadline:
            break
        if raw is not None:
            for command in commands:
                db.execute(command, (key, raw) * (command.count("?") // 2))
        cursor = identity
        processed += 1
    complete = processed == len(rows) and (len(rows) < batch or cursor >= state[2])
    db.execute("UPDATE market_cohort_scan SET cursor=?,complete=? WHERE id=1", (cursor, int(complete)))
    return complete


def align_empty(db: sqlite3.Connection) -> None:
    # Subsequent additive installers cannot relocate an empty runtime. Do not
    # apply this shortcut to a previously populated counter or a VACUUM cursor.
    if not db.execute("SELECT 1 FROM runtime LIMIT 1").fetchone():
        db.execute(
            "UPDATE market_cohort_scan SET schema_version=? WHERE id=1 AND complete=1 AND unknown=0",
            (db.execute("PRAGMA schema_version").fetchone()[0],),
        )
        db.commit()


def report(db: sqlite3.Connection, seconds: float = 60) -> dict[str, Any] | None:
    from collections import Counter, defaultdict

    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='market_cohort_scan'").fetchone():
        return None
    state = db.execute(
        "SELECT generation,schema_version,complete,unknown FROM market_cohort_scan WHERE id=1"
    ).fetchone()
    if (
        state is None
        or type(state[0]) is not int
        or state[0] < 1
        or state[2] not in (0, 1)
        or type(state[3]) is not int
        or state[3] < 0
    ):
        return {"state": "UNSET_COUNTER_HEADER"}
    if not state[2] or state[1] != db.execute("PRAGMA schema_version").fetchone()[0]:
        return {"state": "PENDING_FINITE_COHORT_BACKFILL", "method": "TRANSACTIONAL_COHORT_COUNTERS"}
    if state[3]:
        return {"state": "UNSET_EVENT_METADATA", "unclassified_event_records": state[3]}
    from . import catalogue_cohorts

    if catalogue_cohorts.active(db):
        catalogue_state = catalogue_cohorts.status(db)
        if catalogue_state["state"] != "COMPLETE":
            return catalogue_state
    methods = [
        row[0]
        for row in db.execute(
            "SELECT key FROM runtime WHERE key>=? AND key<? ORDER BY key",
            ("radar:v1:outcome-method:", "radar:v1:outcome-method:~"),
        )
    ]
    if methods != ["radar:v1:outcome-method:1"]:
        return {"state": "UNSET_MULTIPLE_OR_UNKNOWN_METHODS", "published_methods": methods}
    deadline = time.monotonic() + seconds
    marginal: dict[str, set[Any]] = defaultdict(set)
    without: dict[str, int] = {name: 0 for name in FIELDS}
    no_day: set[tuple[Any, ...]] = set()
    density: Counter[int] = Counter()
    day_rows: list[tuple[Any, ...]] = []
    day = None
    groups = 0
    facts = 0
    complete = False
    revisions: dict[int, int] = {}
    strengths = 0

    def finish_day() -> None:
        # Every other projection retains day, so disjoint dates can be counted
        # separately. Peak Python memory follows one day, not the retained month.
        for index, name in enumerate(FIELDS[1:]):
            without[name] += len({key[:index] + key[index + 1 :] for key in day_rows})
        day_rows.clear()

    db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    try:
        source, parameters = catalogue_cohorts.query(db, "market_cohorts", state[0])
        for row in db.execute(source + f" ORDER BY {','.join(FIELDS)}", parameters):
            if time.monotonic() >= deadline:
                break
            if day is not None and row[0] != day:
                finish_day()
            day = row[0]
            key = tuple(row[:-1])
            day_rows.append(key[1:])
            no_day.add(key[1:])
            for field, value in zip(FIELDS, key):
                marginal[field].add(value)
            density[row[-1]] += 1
            groups += 1
            facts += row[-1]
        else:
            complete = True
        finish_day()
        without["day"] = len(no_day)
        if time.monotonic() < deadline:
            source, parameters = catalogue_cohorts.query(db, "market_cohort_revisions", state[0])
            revisions = dict(db.execute("SELECT revision,n FROM (" + source + ") WHERE n>0", parameters))
        if time.monotonic() < deadline:
            source, parameters = catalogue_cohorts.query(db, "market_cohort_strengths", state[0])
            strengths = db.execute("SELECT COUNT(*) FROM (" + source + ") WHERE n>0", parameters).fetchone()[
                0
            ]
    except sqlite3.OperationalError as exc:
        if getattr(exc, "sqlite_errorcode", None) != sqlite3.SQLITE_INTERRUPT:
            raise
        # Preserve the public report shape under a zero/expired SQL budget.
        # Any counters read before interruption remain explicitly partial.
        complete = False
    finally:
        db.set_progress_handler(None, 0)
    median = None
    seen = 0
    for value, n in sorted(density.items()):
        seen += n
        if seen > groups // 2:
            median = value
            break
    complete = complete and time.monotonic() < deadline
    return {
        "state": "MEASURED_PROSPECTIVE" if complete else "PARTIAL_BUDGET",
        "method": "TRANSACTIONAL_COHORT_COUNTERS_AFTER_COMPLETE_FINITE_SCAN",
        "event_revisions": revisions,
        "event_records": sum(revisions.values()),
        "facts_if_archived": facts,
        "cohort_days_if_archived": groups,
        "density_histogram": dict(density),
        "density_mean": facts / max(1, groups),
        "density_median": median,
        "dimension_cardinality": {key: len(values) for key, values in marginal.items()},
        "distinct_without_dimension": without,
        "distinct_strengths": strengths,
        "limitations": [
            "Frozen event horizons and published outcome methodology1; prospective, not an archived production population",
            "One calendar-day density may be partial; additional settings/methods are not a future capacity guarantee",
        ],
    }


def ready(db: sqlite3.Connection) -> bool:
    row = db.execute("SELECT schema_version,complete FROM market_cohort_scan WHERE id=1").fetchone()
    if not (row and row[1] and row[0] == db.execute("PRAGMA schema_version").fetchone()[0]):
        return False
    from . import catalogue_cohorts

    return not catalogue_cohorts.active(db) or catalogue_cohorts.status(db)["state"] == "COMPLETE"


def collect_current(db: sqlite3.Connection, batch: int) -> int:
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='market_cohort_scan'").fetchone():
        generation = db.execute("SELECT generation FROM market_cohort_scan WHERE id=1").fetchone()[0]
        return collect(db, generation, batch)
    return 0
