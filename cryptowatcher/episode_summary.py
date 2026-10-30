"""Exact episode outcomes and group membership after 30-day detail expiry.

Ordinary SQLite rows, one numeric result per horizon and one membership per
distinct frozen group. No candle arrays, decision contexts or new binary format.
Legacy event summaries remain readable through quality_summary.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from .storage import StateError, Store, encode

if TYPE_CHECKING:
    from .quality_summary import ArchiveFact


def installed(db: sqlite3.Connection) -> bool:
    marker = db.execute("SELECT value FROM market_meta WHERE key='episode_summary'").fetchone()
    tables = db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
        "AND name IN ('market_episode_results','market_episode_members','market_episode_provenance')"
    ).fetchone()[0]
    if marker is None and not tables:
        return False
    if marker is None or marker[0] != "1" or tables != 3:
        raise StateError("Unknown or incomplete episode summary layout; database preserved")
    return True


def install(db: sqlite3.Connection) -> None:
    if installed(db):
        return
    if db.in_transaction:
        raise StateError("Episode summary installation requires an outer boundary")
    try:
        db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE market_episode_provenance(
                id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                refs INTEGER NOT NULL DEFAULT 0 CHECK(refs>=0)) WITHOUT ROWID;
            CREATE INDEX market_episode_provenance_unused ON market_episode_provenance(id) WHERE refs=0;
            CREATE TABLE market_episode_results(
                episode_id TEXT NOT NULL, horizon INTEGER NOT NULL,
                method TEXT NOT NULL, day INTEGER NOT NULL, expires_day INTEGER NOT NULL,
                fact TEXT NOT NULL, provenance TEXT NOT NULL REFERENCES market_episode_provenance(id),
                PRIMARY KEY(episode_id,horizon,method)) WITHOUT ROWID;
            CREATE INDEX market_episode_results_expiry ON market_episode_results(expires_day,episode_id);
            CREATE INDEX market_episode_results_day ON market_episode_results(day,episode_id);
            CREATE TABLE market_episode_members(
                episode_id TEXT NOT NULL, signature TEXT NOT NULL,
                dimensions TEXT NOT NULL, provenance TEXT NOT NULL REFERENCES market_episode_provenance(id),
                PRIMARY KEY(episode_id,signature)) WITHOUT ROWID;
            INSERT INTO market_meta VALUES('episode_summary','1');
        """)
        for table in ("market_episode_results", "market_episode_members"):
            db.execute(
                f"CREATE TRIGGER {table}_provenance_insert AFTER INSERT ON {table} BEGIN "
                "UPDATE market_episode_provenance SET refs=refs+1 WHERE id=NEW.provenance; END"
            )
            db.execute(
                f"CREATE TRIGGER {table}_provenance_delete AFTER DELETE ON {table} BEGIN "
                "UPDATE market_episode_provenance SET refs=refs-1 WHERE id=OLD.provenance; END"
            )
            db.execute(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE ON {table} BEGIN "
                "SELECT RAISE(ABORT,'Immutable episode summary'); END"
            )
        # Small exact counters; diagnostics never scan the summary population.
        for table, columns in (
            ("market_episode_provenance", ("id", "payload")),
            ("market_episode_results", ("episode_id", "method", "fact", "provenance")),
            ("market_episode_members", ("episode_id", "signature", "dimensions", "provenance")),
        ):

            def size(alias: str) -> str:
                return "+".join(f"length(CAST({alias}.{c} AS BLOB))" for c in columns)

            for event, statement in (
                (
                    "INSERT",
                    f"INSERT INTO market_counts VALUES('quality_episodes',1,{size('NEW')},{size('NEW')},0) "
                    "ON CONFLICT(category) DO UPDATE SET records=records+1,"
                    "logical_bytes=logical_bytes+excluded.logical_bytes,"
                    "inserted_bytes=inserted_bytes+excluded.inserted_bytes;",
                ),
                (
                    "DELETE",
                    f"UPDATE market_counts SET records=records-1,logical_bytes=logical_bytes-({size('OLD')}),"
                    f"deleted_bytes=deleted_bytes+({size('OLD')}) WHERE category='quality_episodes';",
                ),
            ):
                db.execute(
                    f"CREATE TRIGGER {table}_{event.lower()} AFTER {event} ON {table} BEGIN {statement} END"
                )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def prepare(store: Store, event: dict[str, Any]) -> list[ArchiveFact]:
    from .config import OwnerSettings
    from .events import History, identity
    from .quality_summary import DAY

    history = History(store)
    member = history.metadata(f"event:{event['event_id']}:0000000001")
    if member is None:
        raise StateError("Missing frozen episode membership; details preserved")
    if member.get("outcome_methodology_version", "2") != "2":
        raise StateError("Unsupported frozen episode summary method; details preserved")
    rows = store.db.execute(
        "SELECT r.day,r.horizon,r.fact,p.payload FROM market_episode_results r "
        "JOIN market_episode_provenance p ON p.id=r.provenance WHERE r.episode_id=? ORDER BY r.horizon",
        (event["episode_id"],),
    ).fetchall()
    values = [(r[0], r[1], json.loads(r[2]), json.loads(r[3])) for r in rows]
    if not values:
        episode = history.metadata("episode-head:" + event["episode_id"])
        origin_id = episode.get("origin_event_id") if episode else None
        origin = history.metadata(f"event:{origin_id}:0000000001") if origin_id else None
        if origin is None:
            raise StateError("Missing episode outcome origin during aggregation; details preserved")
        horizons = OwnerSettings.from_payload(origin["settings_snapshot"]).outcome_horizons
        # Expiry needs saved numbers only. Source windows may already have
        # reached their lease boundary and must never be expanded here.
        results = [
            history.metadata("outcome-head:" + identity(f"outcome/{event['episode_id']}/{horizon}/2"))
            for horizon in horizons
        ]
        provenance = {
            "settings_snapshot": origin["settings_snapshot"],
            "settings_revision": origin["settings_revision"],
            "outcome_definition": history.get("outcome-method:2"),
            "effective_parameters": origin["effective_parameters"],
            "producer_source_revision": origin.get("producer_source_revision", "UNSET"),
            "settings_sha256": hashlib.sha256(encode(origin["settings_snapshot"]).encode()).hexdigest(),
        }
        by_horizon = {r["horizon_min"]: r for r in results if r is not None}
        for horizon in horizons:
            outcome = (
                by_horizon.get(horizon)
                or history.metadata("outcome-job:" + identity(f"outcome/{event['episode_id']}/{horizon}/2"))
                or {}
            )
            state = outcome.get("state", "EXPIRED_INCOMPLETE")
            fact = {
                "episode_id": event["episode_id"],
                "state": "COMPLETE" if state == "COMPLETE" else "EXPIRED_RETENTION_INCOMPLETE",
                "previous_state": state,
                "metrics": outcome.get("metrics", {}),
                "classification": outcome.get("classification", "UNCLASSIFIED"),
                "noise": outcome.get("noise_threshold_pct"),
                "completeness": {
                    k: outcome[k] for k in ("expected_candles", "observed_candles", "reason") if k in outcome
                },
            }
            values.append((origin["reference_time_ms"] // DAY, horizon, fact, provenance))
    dimensions = {
        "detector": member["detector_id"],
        "version": member["detector_version"],
        "settings": member["settings_revision"],
        "profile": member["settings_snapshot"].get("profile", "custom"),
        "market": member["market_type"],
        "timeframe": member["timeframe"],
        "instrument": member["symbol"],
        "universe": member["settings_snapshot"]["alert_universe_mode"],
        "direction": member["direction"],
        "strength": member["strength"],
    }
    member_provenance = {
        "settings_snapshot": member["settings_snapshot"],
        "detector_definition": history.get(
            f"detector-metadata:{member['detector_id']}:{member['detector_version']}"
        ),
        "effective_parameters": member["effective_parameters"],
        "producer_source_revision": member.get("producer_source_revision", "UNSET"),
    }
    return [
        (
            day,
            {**dimensions, "horizon_min": horizon, "methodology_version": "2"},
            {"membership": member_provenance, "origin": provenance},
            fact,
        )
        for day, horizon, fact, provenance in values
    ]


def provenance_key(value: Any) -> tuple[str, str]:
    payload = encode(value)
    return hashlib.sha256(payload.encode()).hexdigest(), payload


def retain_provenance(db: sqlite3.Connection, value: Any) -> str:
    identity, payload = provenance_key(value)
    existing = db.execute("SELECT payload FROM market_episode_provenance WHERE id=?", (identity,)).fetchone()
    if existing is not None and existing[0] != payload:
        raise StateError("Conflicting episode provenance; details preserved")
    db.execute("INSERT OR IGNORE INTO market_episode_provenance(id,payload) VALUES(?,?)", (identity, payload))
    return identity


def archive(store: Store, prepared: list[ArchiveFact]) -> None:
    from .quality_summary import month_boundary

    for day, group, provenance, fact in prepared:
        episode = fact["episode_id"]
        identity = (episode, group["horizon_min"], "2")
        provenance_id, _ = provenance_key(provenance["origin"])
        serialized = (day, month_boundary(day, 12), encode(fact), provenance_id)
        existing = store.db.execute(
            "SELECT day,expires_day,fact,provenance FROM market_episode_results "
            "WHERE episode_id=? AND horizon=? AND method=?",
            identity,
        ).fetchone()
        if existing is not None and tuple(existing) != serialized:
            raise StateError("Conflicting immutable episode summary; details preserved")
        if existing is None:
            retain_provenance(store.db, provenance["origin"])
            store.db.execute(
                "INSERT INTO market_episode_results VALUES(?,?,?,?,?,?,?)", (*identity, *serialized)
            )
        dimensions = {k: v for k, v in group.items() if k not in ("horizon_min", "methodology_version")}
        packed = encode(dimensions)
        member_provenance = retain_provenance(store.db, provenance["membership"])
        signature = hashlib.sha256(encode([dimensions, provenance["membership"]]).encode()).hexdigest()
        store.db.execute(
            "INSERT OR IGNORE INTO market_episode_members VALUES(?,?,?,?)",
            (episode, signature, packed, member_provenance),
        )


def membership_sql(*, provenance: bool = False) -> str:
    paths = {
        "detector": "detector_id",
        "version": "detector_version",
        "settings": "settings_revision",
        "profile": "settings_snapshot.profile",
        "market": "market_type",
        "timeframe": "timeframe",
        "instrument": "symbol",
        "universe": "settings_snapshot.alert_universe_mode",
        "direction": "direction",
        "strength": "strength",
    }
    live_fields = [
        part
        for key, path in paths.items()
        for part in (f"'{key}'", f"json_extract(cw_record_json(payload_json),'$.{path}')")
    ]
    extra = ""
    if provenance:
        extra = (
            ",json_object('settings_snapshot',json_extract(cw_record_json(payload_json),'$.settings_snapshot'),"
            "'detector_definition',json(cw_runtime_payload('radar:v1:detector-metadata:'||"
            "json_extract(cw_record_json(payload_json),'$.detector_id')||':'||"
            "json_extract(cw_record_json(payload_json),'$.detector_version'))),"
            "'effective_parameters',json_extract(cw_record_json(payload_json),'$.effective_parameters'),"
            "'producer_source_revision',COALESCE(json_extract(cw_record_json(payload_json),"
            "'$.producer_source_revision'),'UNSET')) AS provenance"
        )
    # A root can expire before later confirmations. Retain those live groups
    # through the same archived numeric result during that boundary interval.
    return (
        "(SELECT episode_id,dimensions"
        + (
            ",p.payload AS provenance FROM market_episode_members a JOIN market_episode_provenance p ON p.id=a.provenance"
            if provenance
            else " FROM market_episode_members"
        )
        + " UNION ALL SELECT episode_id,json_object("
        + ",".join(live_fields)
        + ") AS dimensions"
        + extra
        + " FROM episode_members)"
    )


def union_sql(
    groups: tuple[str, ...],
    start_ms: int,
    end_ms: int,
    detector: str | None,
    version: str | None,
    market: str | None,
) -> tuple[str, list[Any]]:
    from .quality_summary import DAY, period_metadata

    fields = [part for g in groups for part in (f"'{g}'", f"json_extract(m.dimensions,'$.{g}')")]
    fields += ["'horizon_min'", "r.horizon", "'methodology_version'", "r.method"]
    period = period_metadata(start_ms, end_ms)
    args: list[Any] = [period["archive_start_ms"] // DAY, period["archive_end_ms"] // DAY]
    terms = ["r.day>=?", "r.day<?", "json_extract(r.fact,'$.state')='COMPLETE'"]
    for key, value in (("detector", detector), ("version", version), ("market", market)):
        if value is not None:
            terms.append(f"json_extract(m.dimensions,'$.{key}')=?")
            args.append(value)
    memberships = membership_sql()
    return (
        "SELECT json_object(" + ",".join(fields) + ") AS group_key,"
        "COALESCE(json_extract(r.fact,'$.metrics.directed_return_pct'),"
        "json_extract(r.fact,'$.metrics.price_return_pct')) AS value,"
        "json_extract(r.fact,'$.classification') AS class,json_extract(r.fact,'$.noise') AS noise,"
        "'episode:'||r.episode_id AS event_id,r.horizon AS horizon "
        "FROM market_episode_results r JOIN "
        + memberships
        + " m USING(episode_id) WHERE "
        + " AND ".join(terms),
        args,
    )


def collect(db: sqlite3.Connection, now_day: int, batch: int) -> None:
    episodes = db.execute(
        "SELECT episode_id FROM market_episode_results WHERE expires_day<=? ORDER BY expires_day LIMIT ?",
        (now_day, batch),
    ).fetchall()
    for (episode,) in episodes:
        db.execute("DELETE FROM market_episode_members WHERE episode_id=?", (episode,))
        db.execute("DELETE FROM market_episode_results WHERE episode_id=?", (episode,))
    db.execute(
        "DELETE FROM market_episode_provenance WHERE id IN "
        "(SELECT id FROM market_episode_provenance WHERE refs=0 LIMIT ?)",
        (batch,),
    )


def records(store: Store, selection: Any) -> Iterator[dict[str, Any]]:
    from .catalogue_sql import source
    from .events import PREFIX
    from .quality_summary import DAY, period_metadata

    period = period_metadata(selection.start_ms, selection.end_ms)
    args: list[Any] = [
        PREFIX + "event:",
        PREFIX + "event:~",
        period["archive_start_ms"] // DAY,
        period["archive_end_ms"] // DAY,
    ]
    terms = ["r.day>=?", "r.day<?"]
    for key, value in (
        ("detector", selection.detector),
        ("version", selection.version),
        ("market", selection.market),
    ):
        if value is not None:
            terms.append(f"json_extract(m.dimensions,'$.{key}')=?")
            args.append(value)
    for row in store.db.execute(
        "WITH episode_members AS MATERIALIZED (SELECT "
        "json_extract(cw_record_json(payload_json),'$.episode_id') AS episode_id,payload_json "
        "FROM " + source(store, "event") + " WHERE key>=? AND key<? "
        "AND json_extract(cw_record_json(payload_json),'$.revision')=1 "
        "AND json_extract(cw_record_json(payload_json),'$.outcome_unit')='EPISODE') "
        "SELECT DISTINCT r.episode_id,r.day,r.horizon,r.method,r.fact,p.payload,m.dimensions,m.provenance "
        "FROM market_episode_results r JOIN market_episode_provenance p ON p.id=r.provenance JOIN "
        + membership_sql(provenance=True)
        + " m USING(episode_id) WHERE "
        + " AND ".join(terms)
        + " ORDER BY r.day,r.episode_id,r.horizon,m.dimensions",
        args,
    ):
        dimensions = json.loads(row[6])
        yield {
            "event_day_utc": row[1],
            "aggregation_unit": "UNIQUE_EPISODES",
            "dimensions": {**dimensions, "horizon_min": row[2], "methodology_version": row[3]},
            "provenance": {**json.loads(row[7]), "outcome_origin": json.loads(row[5])},
            "facts": [{**json.loads(row[4]), "strength": dimensions["strength"]}],
            "detail_available": False,
        }
