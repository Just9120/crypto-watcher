import time

import pytest

from cryptowatcher.market_storage import enable
from cryptowatcher.quality_layout import drain as drain_quality
from cryptowatcher.quality_layout import put
from cryptowatcher.quality_meter import drain, install, read
from cryptowatcher.quality_summary import encode_facts


def test_archived_fact_meter_counts_backfill_concurrent_writes_rollback_and_schema_changes(store):
    enable(store.db)
    for name in ("INSERT", "DELETE", "UPDATE"):
        store.db.execute(f"DROP TRIGGER market_quality_tally_{name}")
    store.db.execute("DROP TABLE market_quality_tally")
    payload = encode_facts([])

    def add(day, n):
        put(store.db, (day, "cohort", 0, day + 366, "{}", "{}", payload, n))

    with store.db:
        for i in range(30):
            add(20000 + i, i)
        cohort = store.db.execute("SELECT id FROM market_quality_cohorts").fetchone()[0]
        provenance = store.db.execute("SELECT id FROM market_quality_provenance").fetchone()[0]
        store.db.execute(
            "INSERT INTO market_quality_blocks(rowid,day,cohort,chunk,expires_day,provenance,facts,n) VALUES(-10,30000,?,0,30366,?,?,7)",
            (cohort, provenance, payload),
        )
    install(store.db)
    assert read(store.db) is None
    with store.db:
        assert not drain(store.db, 3, time.monotonic() + 1)
        store.db.execute("DELETE FROM market_quality WHERE day IN(30000,20028)")
        add(20000, 57)  # Already scanned.
        add(20020, 99)  # Not scanned yet.
        add(40000, 100)  # Beyond the fixed ceiling.
    with pytest.raises(RuntimeError), store.db:
        add(50000, 1000)
        drain(store.db, 3, time.monotonic() + 1)
        raise RuntimeError("rollback counters and rows")
    for _ in range(30):
        with store.db:
            if drain(store.db, 3, time.monotonic() + 1):
                break
    expected = tuple(store.db.execute("SELECT COUNT(*),SUM(n) FROM market_quality").fetchone())
    assert read(store.db) == expected
    queries = []
    store.db.set_trace_callback(queries.append)
    assert read(store.db) == expected
    store.db.set_trace_callback(None)
    assert not any("FROM market_quality_blocks" in q for q in queries)
    store.db.execute("CREATE TABLE scratch_schema_change(id INTEGER)")
    assert read(store.db) is None
    with store.db:
        assert drain(store.db, 100, time.monotonic() + 1)
    assert read(store.db) == expected
    with store.db:
        store.db.execute(
            "INSERT INTO market_quality_legacy VALUES(60000,'legacy',0,60366,'{}','{}',?,11)", (payload,)
        )
    assert read(store.db) is None
    with store.db:
        assert drain_quality(store.db, 100) == 1
    assert read(store.db) == (expected[0] + 1, expected[1] + 11)
