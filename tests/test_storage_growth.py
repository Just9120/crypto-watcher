import hashlib
import json
import math
import random
from contextlib import closing
from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest

from cryptowatcher import (
    compact_json,
    expiry_index,
    quality_layout,
    quality_meter,
    quality_packets,
    reference_layout,
)
from cryptowatcher.analytics import cohort_rows
from cryptowatcher.analytics_recovery import AnalyticsRecovery
from cryptowatcher.events import History
from cryptowatcher.ingestion import BybitRadar
from cryptowatcher.market_storage import decoded, enable
from cryptowatcher.outcomes import OutcomeWorker, Retention
from cryptowatcher.quality_summary import encode_facts
from cryptowatcher.record_codec import binary_payload, pack_binary, pack_binary_v2
from cryptowatcher.sources import Candle, Sources
from cryptowatcher.storage import Store, encode
from cryptowatcher.storage_repack import work

from .test_bounded_storage import legacy_method  # noqa: F401
from .test_outcomes import DAY, MINUTE, NOW, future, observe


def test_typed_records_keep_exact_numeric_types_unicode_and_legacy_bytes():
    rng = random.Random(1729)
    for _ in range(100):
        value = {
            "numbers": [rng.getrandbits(90), rng.random(), -0.0, 2.0, 2, None, True],
            "unknown": {"unicode": "РџСЂРёРјРµСЂ рџЊђ", "id": "12345678-1234-1234-1234-123456789abc"},
            "sha256": hashlib.sha256(b"source").hexdigest(),
        }
        raw = encode(value)
        assert binary_payload(pack_binary(raw)) == raw
        assert binary_payload(pack_binary_v2(raw)) == raw
    huge = encode({"exact_integer": 1 << 9000})
    assert binary_payload(pack_binary(huge)) == huge
    noncanonical = '{ "x": 0.0175454454105349, "x2": -0.0 }'
    assert binary_payload(pack_binary(noncanonical)) == noncanonical


@pytest.mark.parametrize("raw", [b"\xff", b"\x06\xff\xff\xff\x7f", b"\x08\xff\x7f", b"\x04", b"\x00extra"])
def test_typed_decoder_rejects_invalid_tags_bounds_and_truncation(raw):
    with pytest.raises(ValueError):
        compact_json.unpack(raw, 1000)


def test_quality_columns_preserve_all_facts_and_reduce_dense_comparator_payloads():
    from cryptowatcher.market_storage import encoded

    rng = random.Random(41)
    facts = [
        {
            "metrics": {"price_return_pct": rng.random(), "mfe_pct": rng.random(), "mae_pct": -rng.random()},
            "strength": 1 + rng.random(),
            "state": "COMPLETE",
            "previous_state": "COMPLETE",
            "noise": 0.1,
            "classification": "CONTINUATION",
            "completeness": {"expected_candles": 15, "observed_candles": 15},
            "comparator_metrics": [{"price_return_pct": rng.random()} for _ in range(20)],
            "comparator_sets": [{"methodology_version": "1", "count": 20, "offset": 0, "sample_size": 20}],
            "limitations": [],
        }
        for _ in range(128)
    ]
    packed = encode_facts(facts)
    assert encode(decoded(packed, 1024 * 1024)) == encode(facts)
    assert len(packed) < len(encoded(facts)) * 0.8
    assert math.copysign(1, decoded(encode_facts([{"v": -0.0}]), 1000)[0]["v"]) == -1


async def test_arrived_candles_retire_existing_recovery_during_live_priority_and_restart(store, monkeypatch):
    enable(store.db)
    store.bounded = True
    store.clock_ms = lambda: NOW
    radar = BybitRadar(store)
    monkeypatch.setattr(radar, "server_now", lambda: NOW + 6 * MINUTE)
    radar._get = AsyncMock(side_effect=AssertionError("No REST for locally complete data"))
    stream = "spot/BTCUSDT/1m"
    h = History(store)
    with store.db:
        h.put("hold:" + stream + ":owner", {"start": NOW, "end": NOW + 5 * MINUTE})
    recovery = AnalyticsRecovery(store, radar)
    recovery.request(stream, NOW, NOW + 5 * MINUTE)
    assert h.count("analytics-gap-pending:") == 1
    Sources(store).put(stream, future())
    before = [asdict(c) for c in future()]
    radar._pending["spot/ETHUSDT/1m"] = (0, 1)
    await AnalyticsRecovery(store, radar).work(NOW + 6 * MINUTE)
    assert h.count("analytics-gap-pending:") == 0
    assert recovery.request(stream, NOW, NOW + 5 * MINUTE) is None
    assert h.count("analytics-gap-pending:") == 0
    assert h.items("analytics-gap:")[0][1]["completion"] == "LOCAL_CLOSED_DATA"
    from cryptowatcher.outcomes import read_candles

    assert [asdict(c) for c in read_candles(store, stream, NOW, NOW + 5 * MINUTE)] == before
    radar._get.assert_not_awaited()
    await radar.close()


async def test_overlapping_recovery_ranges_share_one_durable_request(store, monkeypatch):
    enable(store.db)
    store.bounded = True
    store.clock_ms = lambda: NOW
    radar = BybitRadar(store)
    monkeypatch.setattr(radar, "server_now", lambda: NOW)
    stream = "linear/BTCUSDT/1m"
    h = History(store)
    with store.db:
        h.put("hold:" + stream + ":owner", {"start": NOW - 10 * MINUTE, "end": NOW})
    recovery = AnalyticsRecovery(store, radar)
    recovery.request(stream, NOW - 10 * MINUTE, NOW - 5 * MINUTE)
    recovery.request(stream, NOW - 7 * MINUTE, NOW - 2 * MINUTE)
    assert h.count("analytics-gap-pending:") == 1
    pending = [v for _, v in h.items("analytics-gap:") if v["state"] == "PENDING"]
    assert (
        len(pending) == 1
        and pending[0]["start"] == NOW - 10 * MINUTE
        and pending[0]["end"] == NOW - 2 * MINUTE
    )
    AnalyticsRecovery(store, radar).request(stream, NOW - 8 * MINUTE, NOW - 3 * MINUTE)
    assert h.count("analytics-gap-pending:") == 1
    assert h.count("event:") == 0
    await radar.close()


def test_expiry_extension_and_repack_preserve_history_with_rollback_and_restart(store, monkeypatch):
    # Construct the real old schema2 layout, then open it with the current writer.
    with monkeypatch.context() as m:
        m.setattr(expiry_index, "install", lambda db: None)
        m.setattr(reference_layout, "install", lambda db: None)
        enable(store.db)
    store.bounded = True
    store.clock_ms = lambda: NOW
    worker = OutcomeWorker(store)
    observe(store, worker)
    Sources(store).put(
        "spot/BTCUSDT/1m",
        [
            Candle.parse(
                [NOW + i * MINUTE, 100, 104, 96, 101, 1, 100],
                "1m",
                "fixture",
                NOW + 61 * MINUTE,
                NOW + 61 * MINUTE,
            )
            for i in range(60)
        ],
    )
    worker.work(NOW + 61 * MINUTE, batch=200)
    h = History(store)
    before = h.events(50)
    cohorts = list(cohort_rows(store))
    private = tuple(store.db.execute("SELECT * FROM owner_settings").fetchone())
    expiry_before = [tuple(r) for r in store.db.execute("SELECT * FROM market_lifetimes ORDER BY key")]
    # Simulate actual retained CW2 bytes, rather than just testing new-to-new.
    with store.db:
        for key, raw in store.db.execute(
            "SELECT key,cw_record_json(payload_json) FROM runtime WHERE key>=? AND key<?",
            ("radar:v1:", "radar:v1:~"),
        ).fetchall():
            store.db.execute("UPDATE runtime SET payload_json=? WHERE key=?", (pack_binary_v2(raw), key))
    expiry_index.install(store.db)
    reference_layout.install(store.db)
    assert [
        tuple(r) for r in store.db.execute("SELECT * FROM market_lifetimes ORDER BY key")
    ] == expiry_before
    from cryptowatcher import record_codec

    source_rows_before = store.db.execute("SELECT COUNT(*) FROM market_candles").fetchone()[0]
    with monkeypatch.context() as m:
        m.setattr(record_codec, "pack_binary", lambda value: (_ for _ in ()).throw(RuntimeError("interrupt")))
        with pytest.raises(RuntimeError):
            # Reach record packing after this small fixture's finite source
            # phase, so the fault also tests rollback of the preceding transfer.
            work(store, NOW, batch=256, seconds=1)
    assert store.get_runtime("storage_repack") is None
    assert store.db.execute("SELECT COUNT(*) FROM market_candles").fetchone()[0] == source_rows_before
    assert store.db.execute("SELECT COUNT(*) FROM market_candle_blocks").fetchone()[0] == 0
    assert (
        store.db.execute("SELECT value FROM market_meta WHERE key='candle_block_runtime'").fetchone() is None
    )
    assert h.events(50) == before
    from cryptowatcher.catalogue_repack import busy

    for _ in range(500):
        state = work(store, NOW, batch=3, seconds=1)
        if state["state"] == "COMPLETE" and not busy(state):
            break
    assert state["state"] == "COMPLETE" and state["initial_reclaim_complete"] and not busy(state)
    assert not store.db.execute("SELECT 1 FROM market_expiry_legacy LIMIT 1").fetchone()
    store.db.execute("VACUUM")
    assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert h.events(50) == before and list(cohort_rows(store)) == cohorts
    assert tuple(store.db.execute("SELECT * FROM owner_settings").fetchone()) == private
    with closing(Store(store.path, store.owner_id, readonly=True)) as fresh:
        refreshed = work(store, NOW, seconds=1)
        assert {key: value for key, value in refreshed.items() if key != "catalogue_maintenance"} == {
            key: value for key, value in state.items() if key != "catalogue_maintenance"
        }
        assert (
            refreshed["catalogue_maintenance"]["free_pages"]
            == store.db.execute("PRAGMA freelist_count").fetchone()[0]
        )
        assert refreshed["catalogue_maintenance"]["pending_work"] is False
        assert work(store, NOW) == refreshed
        assert History(fresh).events(50) == before
    # Aggregate after migration, preserving frozen source/method/strength and N.
    for _ in range(100):
        Retention(store).work(NOW + 31 * DAY)
        if h.count("history:") == 0:
            break
    assert h.count("history:") == 0 and list(cohort_rows(store)) == cohorts
    assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_forecast_does_not_turn_balanced_queue_churn_into_thirty_day_growth(store):
    from cryptowatcher.storage_meter import report, sample

    enable(store.db)
    store.bounded = True
    store.clock_ms = lambda: NOW
    h = History(store)
    sample(store, NOW)
    with store.db:
        for i in range(100):
            key = f"hold:spot/BTCUSDT/1m:{i}"
            h.put(key, {"start": NOW, "end": NOW + MINUTE})
            h.delete(key)
    value = report(store, NOW + MINUTE)
    for category in ("pending_holds", "service_indexes"):
        row = next(r for r in value["forecast"] if r["category"] == category)
        assert row["gross_bytes_per_day"] > 0
        assert row["projected_logical_bytes"] == 0
        assert row["net_bytes_per_day_excluding_repack"] == 0


@pytest.mark.usefixtures("legacy_method")
def test_legacy_quality_rows_normalize_without_changing_exports_or_twelve_month_expiry(store, monkeypatch):
    from cryptowatcher.export import Selection, ndjson
    from cryptowatcher.market_storage import encoded
    from cryptowatcher.quality_summary import month_boundary

    with monkeypatch.context() as m:
        m.setattr(expiry_index, "install", lambda db: None)
        m.setattr(quality_layout, "install", lambda db: None)
        m.setattr(reference_layout, "install", lambda db: None)
        m.setattr(quality_packets, "install", lambda db: None)
        m.setattr(quality_meter, "install", lambda db: None)
        enable(store.db)
    store.bounded = True
    store.clock_ms = lambda: NOW
    worker = OutcomeWorker(store)
    observe(store, worker)
    Sources(store).put("spot/BTCUSDT/1m", future())
    worker.work(NOW + 6 * MINUTE)
    from cryptowatcher import quality_summary
    from cryptowatcher.storage_lifecycle import body_links

    # Reproduce the old writer's self-contained facts/per-block body ownership.
    # New packet/ref installers require the normalized tables intentionally absent here.
    with monkeypatch.context() as m:
        m.setattr(quality_packets, "pack", lambda db, facts, cohort: encode_facts(facts))
        m.setattr(quality_packets, "collect", lambda db, batch: 0)
        m.setattr(
            quality_summary,
            "provenance_links",
            lambda s, day, cohort, chunk, packed: body_links(s, f"quality:{day}:{cohort}:{chunk}", packed),
        )
        for _ in range(100):
            Retention(store).work(NOW + 31 * DAY)
            if History(store).count("history:") == 0:
                break
    with store.db:
        for row in store.db.execute("SELECT day,cohort,chunk,facts FROM market_quality").fetchall():
            store.db.execute(
                "UPDATE market_quality SET facts=? WHERE day=? AND cohort=? AND chunk=?",
                (encoded(decoded(bytes(row[3]), 1024 * 1024)), *row[:3]),
            )

    def export():
        with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
            result = [json.loads(row) for row in ndjson(reader, Selection())]
            result[0].pop("snapshot_at_ms")
            return result

    before = export()
    counts = [tuple(r) for r in store.db.execute("SELECT * FROM market_quality ORDER BY day,cohort,chunk")]
    assert counts
    expiry_index.install(store.db)
    quality_layout.install(store.db)
    quality_packets.install(store.db)
    reference_layout.install(store.db)
    quality_meter.install(store.db)
    for _ in range(500):
        if work(store, NOW + 31 * DAY, batch=8, seconds=1)["state"] == "COMPLETE":
            break
    assert export() == before
    assert not store.db.execute("SELECT 1 FROM market_quality_legacy").fetchone()
    actual = store.db.execute("SELECT SUM(n),COUNT(*) FROM market_quality").fetchone()
    assert actual[0] == sum(r[7] for r in counts) and actual[1] == len(counts)
    assert store.db.execute("SELECT COUNT(*) FROM market_quality_provenance").fetchone()[0] < actual[1]
    for _ in range(100):
        Retention(store).work(month_boundary(NOW // DAY, 12) * DAY + DAY)
        if not store.db.execute("SELECT 1 FROM market_quality").fetchone():
            break
    assert not store.db.execute("SELECT 1 FROM market_quality").fetchone()
    assert not store.db.execute("SELECT 1 FROM market_quality_cohorts").fetchone()
    assert not store.db.execute("SELECT 1 FROM market_quality_provenance").fetchone()


def test_due_service_lookup_skips_large_expired_detail_population(store):
    enable(store.db)
    store.bounded = True
    store.clock_ms = lambda: NOW
    h = History(store)
    with store.db:
        for i in range(1000):
            h.put(f"event-head:{i}", {"event_id": str(i)})
        h.put("outbox:telegram:one", {"state": "DELIVERED", "event_id": "1", "completed_ms": NOW})
    from cryptowatcher.expiry_index import due_services

    steps = 0

    def budget():
        nonlocal steps
        steps += 1
        return int(steps > 10)

    store.db.set_progress_handler(budget, 100)
    try:
        rows = due_services(store.db, NOW + 31 * DAY, 20)
    finally:
        store.db.set_progress_handler(None, 0)
    assert len(rows) == 1 and rows[0][0] == "radar:v1:outbox:telegram:one"


def test_pre_extension_index_snapshot_is_explicitly_stale_without_losing_evidence(store):
    from cryptowatcher.storage_meter import report

    enable(store.db)
    store.bounded = True
    old = {"state": "PASS", "at_ms": NOW, "tables": {"runtime": 4096}, "total_index_bytes": 4096}
    with store.db:
        store.put_runtime("storage_index_measurement", old)
    value = report(store, NOW + MINUTE)
    assert value["indexes"]["state"] == "STALE"
    assert value["indexes"]["at_ms"] == NOW
    assert store.get_runtime("storage_index_measurement") == old
