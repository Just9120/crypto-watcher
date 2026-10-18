import time
from contextlib import closing

import pytest

from cryptowatcher.history_codec import Codec
from cryptowatcher.market_storage import enable
from cryptowatcher.reference_layout import drain
from cryptowatcher.storage import Store, encode
from cryptowatcher.storage_lifecycle import body_links


def reference(body):
    return {
        "_history_storage": {
            "schema": "cryptowatcher.shared-fields.v1",
            "fields": {"settings_snapshot": {"mode": "value2", "id": body}},
        }
    }


def test_legacy_reference_drain_is_exact_atomic_indexed_and_preserves_shared_bodies(store):
    enable(store.db)
    store.bounded = True
    codec = Codec(store.db)
    with store.db:
        body = codec.save({"settings": "frozen"})
        for i in range(100):
            owner = f"radar:v1:test-owner:{i:010d}"
            store.put_runtime(owner, {"kept": True})
            store.db.execute("INSERT INTO market_body_links_legacy VALUES(?,?)", (owner, body))
    expected = sorted(tuple(row) for row in store.db.execute("SELECT * FROM market_body_links"))
    assert store.db.execute("SELECT refs FROM market_body_counts WHERE body=?", (body,)).fetchone()[0] == 100
    with pytest.raises(RuntimeError), store.db:
        drain(store.db, 17, time.monotonic() + 1)
        raise RuntimeError("rollback links and counters")
    assert store.db.execute("SELECT COUNT(*) FROM market_references").fetchone()[0] == 0
    for _ in range(20):
        with store.db:
            if drain(store.db, 17, time.monotonic() + 1):
                break
    assert sorted(tuple(row) for row in store.db.execute("SELECT * FROM market_body_links")) == expected
    assert store.db.execute("SELECT COUNT(*) FROM market_body_links_legacy").fetchone()[0] == 0
    assert store.db.execute("SELECT refs FROM market_body_counts WHERE body=?", (body,)).fetchone()[0] == 100
    assert store.db.execute("SELECT COUNT(*) FROM market_body_ids").fetchone()[0] == 1
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        assert sorted(tuple(row) for row in reader.db.execute("SELECT * FROM market_body_links")) == expected
        assert Codec(reader.db).load(body) == {"settings": "frozen"}
    # Deleting runtime directly still cascades and counts the retained body.
    with store.db:
        for owner, _ in expected:
            store.db.execute("DELETE FROM runtime WHERE key=?", (owner,))
    assert store.db.execute("SELECT refs FROM market_body_counts WHERE body=?", (body,)).fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM market_references").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM market_body_ids").fetchone()[0] == 0
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()


def test_quality_provenance_owns_one_reference_until_its_last_block_expires(store):
    from cryptowatcher.quality_layout import collect_garbage, put
    from cryptowatcher.quality_summary import encode_facts, provenance_links

    enable(store.db)
    store.bounded = True
    codec = Codec(store.db)
    with store.db:
        packed = codec.pack({"quality_provenance": {"settings": "frozen" * 100}})
        body = packed["_history_storage"]["fields"]["quality_provenance"]["id"]
        for day in range(20000, 20020):
            put(
                store.db,
                (
                    day,
                    "cohort",
                    0,
                    day + 366,
                    "{}",
                    encode(packed),
                    encode_facts([]),
                    0,
                ),
            )
            provenance_links(store, day, "cohort", 0, packed)
    assert store.db.execute("SELECT refs FROM market_body_counts WHERE body=?", (body,)).fetchone()[0] == 1
    with store.db:
        store.db.execute("DELETE FROM market_quality WHERE day<20019")
        collect_garbage(store.db)
    assert store.db.execute("SELECT refs FROM market_body_counts WHERE body=?", (body,)).fetchone()[0] == 1
    assert codec.unpack(packed)["quality_provenance"] == {"settings": "frozen" * 100}
    with store.db:
        store.db.execute("DELETE FROM market_quality")
        collect_garbage(store.db)
    assert store.db.execute("SELECT refs FROM market_body_counts WHERE body=?", (body,)).fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM market_references").fetchone()[0] == 0


def test_body_link_refresh_does_not_write_unchanged_refs_and_unknown_body_is_rejected(store):
    import sqlite3

    enable(store.db)
    store.bounded = True
    with store.db:
        body = Codec(store.db).save({"fixture": "exact"})
        body_links(store, "quality-provenance:123", reference(body))
    before = store.db.total_changes
    with store.db:
        body_links(store, "quality-provenance:123", reference(body))
    assert store.db.total_changes == before
    with pytest.raises(sqlite3.IntegrityError, match="Missing shared body"), store.db:
        body_links(store, "unowned", reference("f" * 64))
    assert store.db.execute("SELECT refs FROM market_body_counts WHERE body=?", (body,)).fetchone()[0] == 1
