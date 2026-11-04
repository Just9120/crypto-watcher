"""Constant-size byte/flow ledgers for the explicitly installed subject tables.

Fresh installs are empty. A small compatible pre-release population can be
accounted atomically; a large unmetered database is preserved and rejected,
never scanned implicitly at application startup.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .storage import StateError, Store

DIRECT = {
    **{
        f"direct_{kind}_revisions": "direct_revisions"
        for kind in ("event", "episode", "outcome", "comparison")
    },
    **{f"direct_{kind}_heads": "direct_indexes" for kind in ("event", "episode", "outcome", "comparison")},
    "direct_snapshots": "direct_shared",
    "direct_links": "direct_shared",
    "domain_conditions": "direct_state",
    "domain_state": "direct_state",
    "domain_members": "direct_members",
}
POOL = {
    "analytics_pool_choices": "pool_choices",
    **{
        f"analytics_pool_{name}": "pool_state"
        for name in ("policy", "slots", "jobs", "rate", "legacy", "cutover", "totals")
    },
}


def quoted(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def size_sql(columns: list[str], alias: str = "") -> str:
    parts = []
    for column in columns:
        field = (alias + "." if alias else "") + quoted(column)
        parts.append(
            f"CASE typeof({field}) WHEN 'null' THEN 0 WHEN 'integer' THEN 8 "
            f"WHEN 'real' THEN 8 ELSE length(CAST({field} AS BLOB)) END"
        )
    return "(" + "+".join(parts) + ")"


def installed(store: Store, label: str) -> bool:
    if not store.bounded:
        return False
    row = store.db.execute("SELECT value FROM market_meta WHERE key=?", (label + "_flow_meter",)).fetchone()
    return row is not None and row[0] == "1"


def unchanged(column: str) -> str:
    field = quoted(column)
    new, old = "NEW." + field, "OLD." + field
    # INTEGER and REAL both occupy8 logical bytes, including cross-type changes.
    return (
        f"({new} IS {old} OR (typeof({new}) IN ('integer','real') AND typeof({old}) IN ('integer','real')))"
    )


def trigger_sql(table: str, category: str, columns: list[str], *, legacy: bool = False) -> dict[str, str]:
    new, old = size_sql(columns, "NEW"), size_sql(columns, "OLD")
    guard = f"SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM market_counts WHERE category='{category}' AND records>=0 AND logical_bytes>=0) THEN RAISE(ABORT,'Missing subject storage counter') END;"
    if legacy:
        # Exact v1 definitions are retained only to verify ownership before the
        # bounded transactional upgrade. Existing counters are never re-seeded.
        changes = {
            "INSERT": (
                "",
                f"records=records+1,logical_bytes=logical_bytes+{new},inserted_bytes=inserted_bytes+{new}",
            ),
            "DELETE": (
                "",
                f"records=records-1,logical_bytes=logical_bytes-{old},deleted_bytes=deleted_bytes+{old}",
            ),
            "UPDATE": (
                f" WHEN {new}<>{old}",
                f"logical_bytes=logical_bytes+{new}-{old},inserted_bytes=inserted_bytes+max(0,{new}-{old}),deleted_bytes=deleted_bytes+max(0,{old}-{new})",
            ),
        }
        statements = {
            operation: (condition, f"UPDATE market_counts SET {change} WHERE category='{category}';")
            for operation, (condition, change) in changes.items()
        }
    else:
        delta = (
            "("
            + "+".join(
                f"CASE WHEN {unchanged(column)} THEN 0 ELSE {size_sql([column], 'NEW')}-{size_sql([column], 'OLD')} END"
                for column in columns
            )
            + ")"
        )
        # LIMIT prevents subquery flattening: the byte amount/delta is evaluated
        # once, then reused for all ledger columns. No Python UDF or row cache.
        statements = {
            "INSERT": (
                "",
                f"UPDATE market_counts SET (records,logical_bytes,inserted_bytes)=(SELECT records+1,logical_bytes+amount,inserted_bytes+amount FROM (SELECT {new} AS amount LIMIT -1)) WHERE category='{category}';",
            ),
            "DELETE": (
                "",
                f"UPDATE market_counts SET (records,logical_bytes,deleted_bytes)=(SELECT records-1,logical_bytes-amount,deleted_bytes+amount FROM (SELECT {old} AS amount LIMIT -1)) WHERE category='{category}';",
            ),
            "UPDATE": (
                " WHEN NOT (" + " AND ".join(unchanged(column) for column in columns) + ")",
                f"UPDATE market_counts SET logical_bytes=logical_bytes+measured.amount,inserted_bytes=inserted_bytes+max(0,measured.amount),deleted_bytes=deleted_bytes+max(0,-measured.amount) FROM (SELECT {delta} AS amount LIMIT -1) AS measured WHERE category='{category}' AND measured.amount<>0;",
            ),
        }
    return {
        "cw_meter_" + table + "_" + operation.lower(): (
            f"CREATE TRIGGER {quoted('cw_meter_' + table + '_' + operation.lower())} AFTER {operation} ON {quoted(table)}{condition} BEGIN "
            + guard
            + statement
            + " END"
        )
        for operation, (condition, statement) in statements.items()
    }


def install(store: Store, label: str, tables: dict[str, str]) -> None:
    if not store.bounded:
        return
    if label not in ("direct", "pool", "subject") or store.readonly or store.db.in_transaction:
        raise StateError("Subject meter installation requires its idle writable owner")
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        row = store.db.execute(
            "SELECT value FROM market_meta WHERE key=?", (label + "_flow_meter",)
        ).fetchone()
        version = store.db.execute(
            "SELECT value FROM market_meta WHERE key=?", (label + "_flow_meter_triggers",)
        ).fetchone()
        if row is not None and row[0] != "1" or version is not None and (row is None or version[0] != "2"):
            raise StateError("Unknown subject meter epoch; data preserved")
        measured = []
        for table, category in tables.items():
            if (
                store.db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                is not None
            ):
                columns = [r[1] for r in store.db.execute("PRAGMA table_info(" + quoted(table) + ")")]
                measured.append((table, category, columns))
        if row is not None:
            categories = {r[0] for r in store.db.execute("SELECT category FROM market_counts")}
            if not {category for _, category, _ in measured} <= categories:
                raise StateError("Missing subject storage counter; data preserved")
            # Validate every owned definition before replacing any. Unknown or
            # missing triggers fail closed; the encompassing transaction rolls back.
            for table, category, columns in measured:
                for name, expected in trigger_sql(table, category, columns, legacy=version is None).items():
                    actual = store.db.execute(
                        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
                    ).fetchone()
                    if actual is None or " ".join(actual[0].split()) != " ".join(expected.split()):
                        raise StateError("Unknown subject meter trigger; data preserved")
            if version is not None:
                return
            for table, category, columns in measured:
                for name in trigger_sql(table, category, columns):
                    store.db.execute("DROP TRIGGER " + quoted(name))
        else:
            counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
            for table, category, columns in measured:
                values = [
                    r[0]
                    for r in store.db.execute(
                        "SELECT " + size_sql(columns) + " FROM " + quoted(table) + " LIMIT 513"
                    )
                ]
                if len(values) > 512:
                    raise StateError(
                        "Large unmetered subject population requires explicit initialization; data preserved"
                    )
                counts[category][0] += len(values)
                counts[category][1] += sum(values)
            for category, (records, logical) in counts.items():
                if store.db.execute("SELECT 1 FROM market_counts WHERE category=?", (category,)).fetchone():
                    raise StateError("Conflicting subject meter epoch; data preserved")
                store.db.execute(
                    "INSERT INTO market_counts VALUES(?,?,?,?,0)", (category, records, logical, logical)
                )
            store.db.execute("INSERT INTO market_meta VALUES(?,?)", (label + "_flow_meter", "1"))
        for table, category, columns in measured:
            for statement in trigger_sql(table, category, columns).values():
                store.db.execute(statement)
        store.db.execute("INSERT INTO market_meta VALUES(?,?)", (label + "_flow_meter_triggers", "2"))


def state(store: Store) -> dict[str, Any]:
    if not store.bounded:
        return {"state": "N/A_LEGACY_PHYSICAL_SCHEMA"}
    present = {
        r[0]
        for r in store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('direct_snapshots','analytics_pool_choices')"
        )
    }
    labels = [("direct", DIRECT)] if "direct_snapshots" in present else []
    if "analytics_pool_choices" in present:
        labels.append(("pool", POOL))
    if getattr(store, "services", None) is not None:
        from .service_repository import TABLES

        labels.append(("subject", TABLES))
    missing = [name for name, _ in labels if not installed(store, name)]
    categories = {r[0] for r in store.db.execute("SELECT category FROM market_counts")}
    existing = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing += [
        name + ":categories"
        for name, tables in labels
        if not {category for table, category in tables.items() if table in existing} <= categories
    ]
    return {
        "state": "INCOMPLETE" if missing else "MEASURED_FLOW" if labels else "NOT_INSTALLED",
        "missing": missing,
        "tables": sorted({table for _, tables in labels for table in tables if table in existing}),
        "basis": "transactional logical column bytes; physical pages/indexes/free space accounted separately",
    }
