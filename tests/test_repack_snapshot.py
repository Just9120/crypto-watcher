import time
from contextlib import closing

import pytest

from cryptowatcher.events import DECISION_HEAD
from cryptowatcher.market_storage import decoded, enable, encoded
from cryptowatcher.quality_layout import put as put_quality
from cryptowatcher.quality_summary import encode_facts
from cryptowatcher.record_codec import pack_binary, pack_binary_v2
from cryptowatcher.storage import Store, encode
from cryptowatcher.storage_repack import record_rows, work

from .test_outcomes import NOW


def bounded(store):
    enable(store.db)
    store.bounded = True


def current(value):
    raw = encode(value)
    packed = pack_binary(raw)
    return packed if len(packed) < len(raw.encode()) else raw


def test_in_range_current_writes_cannot_extend_captured_records(store):
    bounded(store)
    raw = encode({"retained": "x" * 200, "metrics": {"return": -0.0}})
    with store.db:
        for i in range(4):
            store.db.execute(
                "INSERT INTO runtime VALUES (?,?)", (f"radar:v1:fixture:{i:04}", pack_binary_v2(raw))
            )
    state = work(store, NOW, batch=1, seconds=1)
    ceiling = state["records_rowid_upper"]
    for tick in range(40):
        with store.db:
            for offset in range(2):
                value = current({"new": tick * 2 + offset})
                store.db.execute(
                    "INSERT INTO runtime VALUES (?,?)",
                    (f"radar:v1:fixture:0001-{tick * 2 + offset:08}", value),
                )
        state = work(store, NOW + tick, batch=1, seconds=1)
    assert state["state"] == "COMPLETE" and state["records"] == 4
    assert state["records_rowid_upper"] == ceiling
    assert (
        store.db.execute(
            "SELECT count(*) FROM runtime WHERE key>=? AND key<?", ("radar:v1:fixture:", "radar:v1:fixture:~")
        ).fetchone()[0]
        == 84
    )
    assert [encode(store.get_runtime(f"radar:v1:fixture:{i:04}")) for i in range(4)] == [raw] * 4
    for tick in range(80):
        assert store.get_runtime(f"radar:v1:fixture:0001-{tick:08}") == {"new": tick}


def test_sparse_physical_order_keeps_legacy_floor_and_restart_progress(store):
    bounded(store)
    raw = encode({"retained": "sparse" * 40})
    with store.db:
        for rid, name in ((1000, "a"), (5, "c"), (100, "b")):
            store.db.execute(
                "INSERT INTO runtime(rowid,key,payload_json) VALUES (?,?,?)",
                (rid, f"radar:v1:fixture:{name}", pack_binary_v2(raw)),
            )
        store.put_runtime(
            "storage_repack",
            {"cursor": "radar:v1:fixture:a", "records": 1, "records_upper": "radar:v1:fixture:c"},
        )
    state = work(store, NOW, batch=1, seconds=1)
    assert state["cursor"] == "radar:v1:fixture:c" and state["records"] == 2
    next_rowid, ceiling = state["records_next_rowid"], state["records_rowid_upper"]
    with closing(Store(store.path, store.owner_id)) as reopened:
        assert reopened.get_runtime("storage_repack")["records_next_rowid"] == next_rowid
        for i in range(10):
            state = work(reopened, NOW + i, batch=1, seconds=1)
            assert state["records_rowid_upper"] == ceiling
            if state["state"] == "COMPLETE":
                break
        assert state["state"] == "COMPLETE" and state["records"] == 3
        assert state["records_key_floor"] == "radar:v1:fixture:a"
        assert [encode(reopened.get_runtime(f"radar:v1:fixture:{k}")) for k in "abc"] == [raw] * 3


def test_new_pointer_heads_inside_the_old_upper_key_cannot_keep_decision_pass_open(store):
    bounded(store)
    prefix = "radar:v1:decision:telegram:"
    keys = [prefix + "00000000-0000-0000-0000-000000000000", prefix + "ffffffff-ffff-ffff-ffff-ffffffffffff"]
    payload = current({DECISION_HEAD: 1})
    with store.db:
        for key in keys:
            store.db.execute("INSERT INTO runtime VALUES (?,?)", (key, payload))
            store.db.execute(
                "INSERT INTO runtime VALUES (?,?)", (key + ":0000000001", current({"revision": 1}))
            )
    state = work(store, NOW, batch=1, seconds=1)
    ceiling = state["decisions_rowid_upper"]
    for tick in range(30):
        with store.db:
            for offset in range(2):
                identity = f"00000001-0000-0000-0000-{tick * 2 + offset:012}"
                store.db.execute("INSERT INTO runtime VALUES (?,?)", (prefix + identity, payload))
                store.db.execute(
                    "INSERT INTO runtime VALUES (?,?)",
                    (prefix + identity + ":0000000001", current({"revision": 1})),
                )
        state = work(store, NOW + tick, batch=1, seconds=1)
    assert state["state"] == "COMPLETE" and state["decisions_rowid_upper"] == ceiling
    assert state["decision_heads_scanned"] == 2
    assert (
        store.db.execute(
            "SELECT count(*) FROM runtime WHERE key>=? AND key<?", (prefix, prefix + "~")
        ).fetchone()[0]
        == 124
    )
    assert all(store.get_runtime(key) == {DECISION_HEAD: 1} for key in keys)


@pytest.mark.parametrize("rebuild", ["vacuum", "ddl"])
def test_schema_rebuild_recaptures_physical_range_without_skipping_public_data(store, rebuild):
    bounded(store)
    raw = encode({"retained": "version" * 40})
    with store.db:
        for name in "hgfedcba":
            store.db.execute(
                "INSERT INTO runtime VALUES (?,?)", ("radar:v1:fixture:" + name, pack_binary_v2(raw))
            )
    state = work(store, NOW, batch=1, seconds=1)
    version = state["records_schema_version"]
    store.db.execute("VACUUM" if rebuild == "vacuum" else "CREATE INDEX test_schema_change ON runtime(key)")
    changed = store.db.execute("PRAGMA schema_version").fetchone()[0]
    assert changed != version
    for i in range(25):
        state = work(store, NOW + i, batch=1, seconds=1)
        if state["state"] == "COMPLETE":
            break
    assert state["state"] == "COMPLETE" and state["records_scan_restarts"] == 1
    assert store.db.execute("PRAGMA schema_version").fetchone()[0] == changed
    assert [encode(store.get_runtime("radar:v1:fixture:" + k)) for k in "abcdefgh"] == [raw] * 8


def test_reused_rowid_after_retirement_cannot_reopen_or_extend_the_interval(store):
    bounded(store)
    store.put_runtime("storage_repack", {"cursor": "", "records": 0})
    with store.db:
        for name in "az":
            store.db.execute(
                "INSERT INTO runtime VALUES (?,?)", ("radar:v1:fixture:" + name, current({"key": name}))
            )
    state = work(store, NOW, batch=1, seconds=1)
    ceiling = state["records_rowid_upper"]
    for i in range(20):
        with store.db:
            store.db.execute(
                "DELETE FROM runtime WHERE key='radar:v1:fixture:z' OR key GLOB 'radar:v1:fixture:y:*'"
            )
            store.db.execute(
                "INSERT INTO runtime VALUES (?,?)", (f"radar:v1:fixture:y:{i:04}", current({"new": i}))
            )
        state = work(store, NOW + i, batch=1, seconds=1)
        assert state["records_rowid_upper"] == ceiling
    assert state["state"] == "COMPLETE"
    assert store.get_runtime("radar:v1:fixture:a") == {"key": "a"}
    assert store.get_runtime("radar:v1:fixture:y:0019") == {"new": 19}


def test_non_target_headers_advance_without_decoding_or_changing_shared_values(store):
    bounded(store)
    untouched = b"not-a-history-envelope" * 300
    with store.db:
        store.db.execute("INSERT INTO runtime VALUES (?,?)", ("historyblob:v1:untouched", untouched))
        store.db.execute(
            "INSERT INTO runtime VALUES (?,?)", ("radar:v1:fixture:a", current({"retained": True}))
        )
    first = work(store, NOW, batch=1, seconds=1)
    assert first["records"] == 0 and first["records_rows_scanned"] == 1
    for i in range(5):
        state = work(store, NOW + i, batch=1, seconds=1)
        if state["state"] == "COMPLETE":
            break
    assert state["state"] == "COMPLETE"
    assert (
        store.db.execute("SELECT payload_json FROM runtime WHERE key='historyblob:v1:untouched'").fetchone()[
            0
        ]
        == untouched
    )
    assert store.get_runtime("radar:v1:fixture:a") == {"retained": True}


def test_slow_read_budget_cannot_starve_the_first_captured_header(store):
    bounded(store)
    raw = encode({"retained": "slow-read" * 30})
    with store.db:
        for i in range(3):
            store.db.execute(
                "INSERT INTO runtime VALUES (?,?)", (f"radar:v1:fixture:{i}", pack_binary_v2(raw))
            )
    state = work(store, NOW, batch=1, seconds=1)
    previous = state["records_next_rowid"]
    with store.db:
        record_rows(store, state, batch=1, deadline=time.monotonic() - 1)
    assert state["records_next_rowid"] > previous and state["records"] == 2
    assert encode(store.get_runtime("radar:v1:fixture:1")) == raw


def test_incremental_vacuum_does_not_discard_the_saved_interval(store):
    bounded(store)
    store.db.execute("CREATE TABLE disposable_fixture(payload BLOB)")
    raw = encode({"retained": "incremental" * 30})
    with store.db:
        store.db.executemany("INSERT INTO disposable_fixture VALUES (?)", [(b"x" * 4000,)] * 500)
        store.db.execute("DELETE FROM disposable_fixture")
        for i in range(4):
            store.db.execute(
                "INSERT INTO runtime VALUES (?,?)", (f"radar:v1:fixture:{i}", pack_binary_v2(raw))
            )
    assert store.db.execute("PRAGMA freelist_count").fetchone()[0] > 256
    previous = 0
    for i in range(10):
        state = work(store, NOW + i, batch=1, seconds=1)
        assert state["records_next_rowid"] >= previous
        assert state.get("records_scan_restarts", 0) == 0
        assert state["records_schema_version"] == store.db.execute("PRAGMA schema_version").fetchone()[0]
        previous = state["records_next_rowid"]
        if state["state"] == "COMPLETE":
            break
    assert state["state"] == "COMPLETE"
    assert [encode(store.get_runtime(f"radar:v1:fixture:{i}")) for i in range(4)] == [raw] * 4


def test_quality_interval_finishes_with_new_facts_and_keeps_every_numeric_result(store):
    bounded(store)
    day = NOW // 86400000

    def put(i, legacy=False):
        facts = [{"metrics": {"price_return_pct": i / 17, "mae_pct": -0.0}, "strength": 1.25}]
        value = encoded(facts) if legacy else encode_facts(facts)
        put_quality(
            store.db,
            (
                day + i,
                "cohort",
                0,
                day + i + 366,
                encode({"detector": "price"}),
                encode({"method": "1"}),
                value,
                1,
            ),
        )

    with store.db:
        for i in range(4):
            put(i, legacy=True)
        store.put_runtime(
            "storage_repack",
            {"records_complete": True, "decisions_complete": True, "expiry_legacy_empty": True},
        )
    state = work(store, NOW, batch=1, seconds=1)
    ceiling = state["quality_rowid_upper"]
    for tick in range(40):
        with store.db:
            for offset in range(2):
                put(100 + tick * 2 + offset)
        state = work(store, NOW + tick, batch=1, seconds=1)
    assert state["state"] == "COMPLETE" and state["quality_rowid_upper"] == ceiling
    rows = store.db.execute("SELECT day,facts,n FROM market_quality ORDER BY day").fetchall()
    assert len(rows) == 84 and sum(row[2] for row in rows) == 84
    for date, payload, _ in rows:
        i = date - day
        expected = [{"metrics": {"price_return_pct": i / 17, "mae_pct": -0.0}, "strength": 1.25}]
        assert encode(decoded(bytes(payload), 1024 * 1024)) == encode(expected)


@pytest.mark.parametrize(
    "checkpoint",
    [
        {"version": 13},
        {"version": "12"},
        {"version": 12.0},
        {"version": None},
        {"version": True},
        ["unknown"],
    ],
)
def test_unknown_repack_checkpoint_is_rejected_before_schema_or_data_changes(store, checkpoint):
    bounded(store)
    store.put_runtime("storage_repack", checkpoint)
    before = list(store.db.iterdump())
    with pytest.raises(ValueError, match="Unknown storage repack"):
        work(store, NOW)
    assert list(store.db.iterdump()) == before


def test_missing_price_writer_checkpoint_is_not_recreated_after_a_version12_commit(store):
    bounded(store)
    initial = work(store, NOW, seconds=0)
    assert initial["version"] == 12
    with store.db:
        store.db.execute("DELETE FROM market_meta WHERE key='numeric_price_runtime'")
    before = list(store.db.iterdump())
    with pytest.raises(ValueError, match="price writer checkpoint"):
        work(store, NOW + 1)
    assert list(store.db.iterdump()) == before
