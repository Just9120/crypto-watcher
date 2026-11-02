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


def install(store: Store, label: str, tables: dict[str, str]) -> None:
    if not store.bounded:
        return
    if label not in ("direct", "pool") or store.readonly or store.db.in_transaction:
        raise StateError("Subject meter installation requires its idle writable owner")
    if installed(store, label):
        categories = {r[0] for r in store.db.execute("SELECT category FROM market_counts")}
        required = {
            category
            for table, category in tables.items()
            if store.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            is not None
        }
        if not required <= categories:
            raise StateError("Missing subject storage counter; data preserved")
        return
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    measured: list[tuple[str, str, list[str]]] = []
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        for table, category in tables.items():
            if (
                store.db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                is None
            ):
                continue
            columns = [r[1] for r in store.db.execute("PRAGMA table_info(" + quoted(table) + ")")]
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
            measured.append((table, category, columns))
        for category, (records, logical) in counts.items():
            if store.db.execute("SELECT 1 FROM market_counts WHERE category=?", (category,)).fetchone():
                raise StateError("Conflicting subject meter epoch; data preserved")
            store.db.execute(
                "INSERT INTO market_counts VALUES(?,?,?,?,0)", (category, records, logical, logical)
            )
        for table, category, columns in measured:
            new, old = size_sql(columns, "NEW"), size_sql(columns, "OLD")
            guard = f"SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM market_counts WHERE category='{category}' AND records>=0 AND logical_bytes>=0) THEN RAISE(ABORT,'Missing subject storage counter') END;"
            operations = {
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
            for operation, (condition, change) in operations.items():
                store.db.execute(
                    f"CREATE TRIGGER {quoted('cw_meter_' + table + '_' + operation.lower())} AFTER {operation} ON {quoted(table)}{condition} BEGIN "
                    + guard
                    + f"UPDATE market_counts SET {change} WHERE category='{category}'; END"
                )
        store.db.execute("INSERT INTO market_meta VALUES(?,?)", (label + "_flow_meter", "1"))


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
