"""Lossless archive packets with bounded, immutable dictionary dependencies.

Only final leaf blocks retain numeric bodies. A delta ancestor retains integer
coordinates, never old metrics that the final ordered list no longer contains.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import sqlite3
from typing import Any

from . import compact_json
from .market_storage import decoded
from .record_codec import json_value, pack_binary

LEGACY_MAGIC = b"CQ1\x00"
PARAMETER_MAGIC = b"CQ2\x00"
MAGIC = b"CQ4\x00"
MAXIMUM = 1024 * 1024
MAX_CHAIN = 8


def packed(value: Any) -> bytes:
    from .storage import encode

    return compact_json.pack(json.loads(encode(value)))


def signature(raw: bytes) -> bytes:
    return hashlib.sha256(raw).digest()[:8]


def positive(value: Any) -> int:
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError("Invalid quality dictionary coordinate")
    return value


def install_functions(db: sqlite3.Connection) -> None:
    from .storage import encode

    db.create_function("cw_quality_json", 1, lambda raw: encode(unpack(db, bytes(raw))))
    db.create_function("cw_quality_refs", 1, lambda raw: json.dumps(ownership(db, bytes(raw))))


def install(db: sqlite3.Connection) -> None:
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='market_quality_dictionary'").fetchone():
        marker = db.execute("SELECT value FROM market_meta WHERE key='quality_packets'").fetchone()
        if marker is None or marker[0] not in ("1", "2"):
            raise ValueError("Unknown quality packets; database preserved")
        if marker[0] == "2":
            return
        if db.in_transaction:
            raise ValueError("Quality packet upgrade requires an outer boundary")
        try:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT sql FROM sqlite_master WHERE name='market_quality_dictionary_insert'"
            ).fetchone()
            if old is None or "kind='set'" not in old[0]:
                raise ValueError("Unknown quality ancestor trigger; database preserved")
            db.execute("DROP TRIGGER market_quality_dictionary_insert")
            db.execute(old[0].replace("kind='set'", "kind=NEW.kind AND kind IN('set','set4')"))
            db.execute("UPDATE market_meta SET value='2' WHERE key='quality_packets'")
            db.commit()
            return
        except BaseException:
            db.rollback()
            raise
    if db.in_transaction:
        raise ValueError("Quality packet installation requires an outer boundary")
    try:
        db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE market_quality_dictionary(
                id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,hash BLOB NOT NULL,checksum BLOB NOT NULL,
                payload BLOB NOT NULL,parent INTEGER,depth INTEGER NOT NULL CHECK(depth BETWEEN 0 AND 8),
                refs INTEGER NOT NULL DEFAULT 0 CHECK(refs>=0));
            CREATE INDEX market_quality_dictionary_lookup ON market_quality_dictionary(kind,hash);
            CREATE INDEX market_quality_dictionary_unused ON market_quality_dictionary(id) WHERE refs=0;
            CREATE TABLE market_quality_hints(cohort TEXT PRIMARY KEY,identity INTEGER NOT NULL) WITHOUT ROWID;
            CREATE TRIGGER market_quality_dictionary_immutable BEFORE UPDATE OF kind,hash,checksum,payload,parent,depth
                ON market_quality_dictionary WHEN NEW.kind<>OLD.kind OR NEW.hash<>OLD.hash OR
                NEW.checksum<>OLD.checksum OR NEW.payload<>OLD.payload OR NEW.parent IS NOT OLD.parent OR NEW.depth<>OLD.depth BEGIN
                SELECT RAISE(ABORT,'Immutable quality dictionary');
            END;
            CREATE TRIGGER market_quality_dictionary_keep BEFORE DELETE ON market_quality_dictionary
                WHEN OLD.refs>0 BEGIN SELECT RAISE(ABORT,'Retained quality dictionary'); END;
            CREATE TRIGGER market_quality_dictionary_insert AFTER INSERT ON market_quality_dictionary BEGIN
                SELECT CASE WHEN NEW.parent IS NOT NULL AND NOT EXISTS(
                    SELECT 1 FROM market_quality_dictionary WHERE id=NEW.parent
                    AND kind=NEW.kind AND kind IN('set','set4') AND depth=NEW.depth-1)
                    THEN RAISE(ABORT,'Missing quality delta ancestor') END;
                UPDATE market_quality_dictionary SET refs=refs+1 WHERE id=NEW.parent;
                INSERT INTO market_counts VALUES('quality_components',1,length(NEW.payload),length(NEW.payload),0)
                    ON CONFLICT(category) DO UPDATE SET records=records+1,
                    logical_bytes=logical_bytes+excluded.logical_bytes,inserted_bytes=inserted_bytes+excluded.inserted_bytes;
            END;
            CREATE TRIGGER market_quality_dictionary_delete AFTER DELETE ON market_quality_dictionary BEGIN
                UPDATE market_quality_dictionary SET refs=refs-1 WHERE id=OLD.parent;
                UPDATE market_counts SET records=records-1,logical_bytes=logical_bytes-length(OLD.payload),
                    deleted_bytes=deleted_bytes+length(OLD.payload) WHERE category='quality_components';
            END;
        """)
        for operation in ("INSERT", "DELETE", "UPDATE"):
            statements = []
            for field, table, column in (
                ("pool", "market_quality_dictionary", "refs"),
                ("numeric", "market_numeric", "quality_refs"),
            ):
                new = f"SELECT value FROM json_each(cw_quality_refs(NEW.facts),'$.{field}')"
                old = f"SELECT value FROM json_each(cw_quality_refs(OLD.facts),'$.{field}')"
                if operation != "DELETE":
                    statements.append(
                        f"SELECT CASE WHEN EXISTS(SELECT 1 FROM ({new}) r LEFT JOIN {table} t ON t.id=r.value "
                        "WHERE t.id IS NULL) THEN RAISE(ABORT,'Missing retained quality value') END;"
                    )
                if operation != "INSERT":
                    difference = old + (" EXCEPT " + new if operation == "UPDATE" else "")
                    statements.append(f"UPDATE {table} SET {column}={column}-1 WHERE id IN({difference});")
                if operation != "DELETE":
                    difference = new + (" EXCEPT " + old if operation == "UPDATE" else "")
                    statements.append(f"UPDATE {table} SET {column}={column}+1 WHERE id IN({difference});")
            db.execute(
                f"CREATE TRIGGER market_quality_packet_{operation} AFTER {operation}"
                + (" OF facts" if operation == "UPDATE" else "")
                + " ON market_quality_blocks BEGIN "
                + " ".join(statements)
                + " END"
            )
        db.execute("INSERT INTO market_meta VALUES('quality_packets','2')")
        db.commit()
    except BaseException:
        db.rollback()
        raise


def row(
    db: sqlite3.Connection, identity: int, kind: str, cache: dict[Any, Any] | None = None
) -> tuple[Any, int | None, int]:
    if cache is not None and (identity, kind) in cache:
        return cache[(identity, kind)]
    found = db.execute(
        "SELECT kind,checksum,payload,parent,depth,hash FROM market_quality_dictionary WHERE id=?",
        (positive(identity),),
    ).fetchone()
    if found is None or found[0] != kind or signature(bytes(found[2])) != bytes(found[1]):
        raise ValueError("Missing or corrupt quality dictionary")
    value = compact_json.unpack(bytes(found[2]), MAXIMUM)
    if kind not in ("set", "set4") and signature(packed(value)) != bytes(found[5]):
        raise ValueError("Quality dictionary content mismatch")
    result = (value, found[3], found[4])
    if cache is not None:
        cache[(identity, kind)] = result
        cache[(identity, "hash")] = bytes(found[5])
    return result


def sequence(
    db: sqlite3.Connection,
    identity: int,
    seen: tuple[int, ...] = (),
    cache: dict[Any, Any] | None = None,
    *,
    kind: str = "set",
) -> list[Any]:
    if cache is None:
        cache = {}
    if identity in seen or len(seen) > MAX_CHAIN:
        raise ValueError("Quality delta chain exceeds bound")
    if (identity, "sequence") in cache:
        return cache[(identity, "sequence")]
    value, parent, depth = row(db, identity, kind, cache)
    if not isinstance(value, list) or len(value) > 4096 or not 0 <= depth <= MAX_CHAIN:
        raise ValueError("Invalid quality comparison list")
    if parent is None:
        if depth:
            raise ValueError("Invalid quality literal depth")
        result = value
    else:
        ancestor, _, ancestor_depth = row(db, parent, kind, cache)
        if ancestor is None or ancestor_depth + 1 != depth:
            raise ValueError("Invalid quality ancestor depth")
        base = sequence(db, parent, (*seen, identity), cache, kind=kind)
        result = []
        for instruction in value:
            if not isinstance(instruction, list):
                raise ValueError("Invalid quality delta instruction")
            if len(instruction) == 2 and instruction[0] == 1 and isinstance(instruction[1], list):
                result.extend(instruction[1])
            elif (
                len(instruction) == 3
                and instruction[0] == 0
                and type(instruction[1]) is int
                and type(instruction[2]) is int
                and 0 <= instruction[1] < len(base)
                and 0 < instruction[2] <= len(base) - instruction[1]
            ):
                result.extend(base[instruction[1] : instruction[1] + instruction[2]])
            else:
                raise ValueError("Invalid quality delta range")
            if len(result) > 4096:
                raise ValueError("Quality comparison bound")
    for value in result:
        metric_reference(value, views=kind == "set4")
    if signature(packed(result)) != cache[(identity, "hash")]:
        raise ValueError("Quality comparison content mismatch")
    cache[(identity, "sequence")] = result
    return result


def metric_reference(value: Any, *, views: bool = False) -> None:
    if type(value) is int:
        if views:
            from .numeric_pool import reference_id

            reference_id({"mode": "numeric2", "id": value})
        else:
            positive(value)
    elif not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise ValueError("Invalid archived metric reference")


def intern(db: sqlite3.Connection, kind: str, value: Any, *, hint: str | None = None) -> int:
    raw = packed(value)
    digest = signature(raw)
    for (identity,) in db.execute(
        "SELECT id FROM market_quality_dictionary WHERE kind=? AND hash=?", (kind, digest)
    ):
        # Set hashes identify the full ordered values, not the chosen delta.
        existing = (
            sequence(db, identity, kind=kind) if kind in ("set", "set4") else row(db, identity, kind)[0]
        )
        if packed(existing) == raw:
            return int(identity)
    parent = None
    depth = 0
    payload = raw
    if hint is not None and kind in ("set", "set4") and value and all(type(v) is int for v in value):
        previous = db.execute(
            "SELECT d.id,d.depth FROM market_quality_hints h JOIN market_quality_dictionary d ON d.id=h.identity "
            "WHERE h.cohort=? AND d.kind=? AND d.depth<?",
            (hint, kind, MAX_CHAIN),
        ).fetchone()
        if previous:
            base = sequence(db, previous[0], kind=kind)
            if all(type(v) is int for v in base):
                instructions = []
                for op, first, end, left, right in difflib.SequenceMatcher(
                    a=base, b=value, autojunk=False
                ).get_opcodes():
                    if op == "equal":
                        instructions.append([0, first, end - first])
                    elif op in ("replace", "insert"):
                        instructions.append([1, value[left:right]])
                candidate = packed(instructions)
                if len(candidate) + 8 < len(raw):
                    payload, parent, depth = candidate, previous[0], previous[1] + 1
    result = db.execute(
        "INSERT INTO market_quality_dictionary(kind,hash,checksum,payload,parent,depth) VALUES(?,?,?,?,?,?)",
        (kind, digest, signature(payload), payload, parent, depth),
    )
    identity = int(result.lastrowid or 0)
    if hint is not None and kind in ("set", "set4"):
        db.execute(
            "INSERT INTO market_quality_hints VALUES(?,?) ON CONFLICT(cohort) DO UPDATE SET identity=excluded.identity",
            (hint, identity),
        )
    return identity


def packet(raw: bytes) -> list[Any]:
    if len(raw) > MAXIMUM:
        raise ValueError("Quality packet exceeds bound")
    value = json_value(raw[len(MAGIC) :])
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not isinstance(value[0], list)
        or not 0 < len(value[0]) <= 128
    ):
        raise ValueError("Invalid quality packet")
    return value


def template_parts(fact: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    metadata = copy.deepcopy(
        {k: v for k, v in fact.items() if k not in ("strength", "noise", "metrics", "comparator_metrics")}
    )
    slots = []
    positions = []
    for index, comparator in enumerate(metadata.get("comparator_sets", [])):
        if isinstance(comparator, dict) and "population_size" in comparator:
            slots.append(comparator["population_size"])
            positions.append(index)
            comparator["population_size"] = None
    return [metadata, positions], slots


def restore_template(
    db: sqlite3.Connection, identity: int, slots: Any, cache: dict[Any, Any]
) -> dict[str, Any]:
    value, parent, depth = row(db, identity, "template2", cache)
    if (
        parent is not None
        or depth
        or not isinstance(value, list)
        or len(value) != 2
        or not isinstance(value[0], dict)
        or not isinstance(value[1], list)
        or not isinstance(slots, list)
        or len(slots) != len(value[1])
        or len(set(value[1])) != len(value[1])
    ):
        raise ValueError("Invalid quality parameterized template")
    result = copy.deepcopy(value[0])
    sets = result.get("comparator_sets", [])
    for index, slot in zip(value[1], slots):
        if (
            type(index) is not int
            or not 0 <= index < len(sets)
            or not isinstance(sets[index], dict)
            or "population_size" not in sets[index]
            or sets[index]["population_size"] is not None
        ):
            raise ValueError("Invalid quality population slot")
        sets[index]["population_size"] = slot
    return result


def ownership(
    db: sqlite3.Connection, raw: bytes, cache: dict[Any, Any] | None = None
) -> dict[str, list[int]]:
    if cache is None:
        cache = {}
    if not raw.startswith((MAGIC, PARAMETER_MAGIC, LEGACY_MAGIC)):
        return {"pool": [], "numeric": []}
    rows, columns = packet(raw)
    pool: set[int] = set()
    numbers: set[int] = set()
    if columns is not None:
        pool.add(positive(columns))
        values, parent, depth = row(db, columns, "columns", cache)
        if parent is not None or depth or not isinstance(values, list) or len(values) != len(rows):
            raise ValueError("Invalid quality columns")
    modern = raw.startswith((MAGIC, PARAMETER_MAGIC))
    views = raw.startswith(MAGIC)
    for value in rows:
        if not isinstance(value, list) or len(value) != (
            (4 if columns is not None else 6) if modern else (3 if columns is not None else 5)
        ):
            raise ValueError("Invalid quality fact row")
        template, own, comparisons = value[:3]
        pool.update((positive(template), positive(comparisons)))
        if modern:
            restore_template(db, template, value[3], cache)
        else:
            metadata, parent, depth = row(db, template, "template", cache)
            if parent is not None or depth or not isinstance(metadata, dict):
                raise ValueError("Invalid quality template")
        for reference in [own, *sequence(db, comparisons, cache=cache, kind="set4" if views else "set")]:
            metric_reference(reference, views=views)
            if type(reference) is int:
                numbers.add(reference // 4 if views else reference)
    return {"pool": sorted(pool), "numeric": sorted(numbers)}


def pack(db: sqlite3.Connection, facts: list[Any], cohort: str | None = None) -> bytes:
    from .numeric_pool import encode_reference, existing_reference
    from .storage import encode

    if not 0 < len(facts) <= 128 or not all(
        isinstance(f, dict)
        and {"strength", "noise", "metrics", "comparator_metrics"} <= f.keys()
        and isinstance(f["metrics"], dict)
        and isinstance(f["comparator_metrics"], list)
        and all(isinstance(m, dict) for m in f["comparator_metrics"])
        for f in facts
    ):
        from .quality_summary import encode_facts

        return encode_facts(facts)
    if not any(f["comparator_metrics"] for f in facts):
        from .quality_summary import encode_facts

        return encode_facts(facts)
    columns = intern(db, "columns", [[f["strength"], f["noise"]] for f in facts]) if len(facts) >= 8 else None
    rows = []
    for fact in facts:
        # A unique own result stays inline. Reuse an already retained quality
        # body, or a body shared by at least four live immutable records. This
        # avoids paying a separate SQLite row/index for every unique own value.
        found = existing_reference(db, fact["metrics"])
        own = found[0]["id"] if found is not None and (found[1] >= 4 or found[2] > 0) else [fact["metrics"]]
        values = []
        for metric in fact["comparator_metrics"]:
            reference = encode_reference(db, metric)
            values.append(reference["id"] if reference is not None else [metric])
        metadata, slots = template_parts(fact)
        value = [
            intern(db, "template2", metadata),
            own,
            intern(db, "set4", values, hint=cohort),
            slots,
        ]
        if columns is None:
            value.extend((fact["strength"], fact["noise"]))
        rows.append(value)
    result = MAGIC + pack_binary(encode([rows, columns]))
    if encode(unpack(db, result)) != encode(facts):
        raise ValueError("Quality packet changed public facts")
    return result


def unpack(db: sqlite3.Connection, raw: bytes) -> list[Any]:
    from .numeric_pool import load_many, reference_value
    from .storage import encode

    if not raw.startswith((MAGIC, PARAMETER_MAGIC, LEGACY_MAGIC)):
        return decoded(raw, MAXIMUM)
    cache: dict[Any, Any] = {}
    refs = ownership(db, raw, cache)
    numbers = load_many(db, set(refs["numeric"]))
    rows, column_id = packet(raw)
    modern = raw.startswith((MAGIC, PARAMETER_MAGIC))
    views = raw.startswith(MAGIC)
    columns = row(db, column_id, "columns", cache)[0] if column_id is not None else None
    result = []
    for index, value in enumerate(rows):
        template, own, comparisons = value[:3]
        fact = (
            restore_template(db, template, value[3], cache)
            if modern
            else copy.deepcopy(row(db, template, "template", cache)[0])
        )
        if any(key in fact for key in ("strength", "noise", "metrics", "comparator_metrics")):
            raise ValueError("Conflicting quality template")
        pair = columns[index] if columns is not None else value[4 if modern else 3 :]
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("Invalid quality strength/noise columns")
        fact["strength"], fact["noise"] = pair

        def metric(reference: Any) -> Any:
            if type(reference) is not int:
                return copy.deepcopy(reference[0])
            if views:
                return reference_value({"mode": "numeric2", "id": reference}, numbers[reference // 4])
            return copy.deepcopy(numbers[reference])

        fact["metrics"] = metric(own)
        fact["comparator_metrics"] = [
            metric(r) for r in sequence(db, comparisons, cache=cache, kind="set4" if views else "set")
        ]
        result.append(fact)
    if len(encode(result).encode()) > MAXIMUM:
        raise ValueError("Decoded quality exceeds bound")
    return result


def collect(db: sqlite3.Connection, batch: int = 128) -> int:
    count = db.execute(
        "DELETE FROM market_quality_dictionary WHERE id IN(SELECT id FROM market_quality_dictionary WHERE refs=0 ORDER BY id LIMIT ?)",
        (batch,),
    ).rowcount
    db.execute(
        "DELETE FROM market_quality_hints WHERE cohort IN(SELECT h.cohort FROM market_quality_hints h "
        "LEFT JOIN market_quality_cohorts c ON c.external=h.cohort WHERE c.id IS NULL LIMIT ?)",
        (batch,),
    )
    return count
