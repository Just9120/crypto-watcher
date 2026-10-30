import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cryptowatcher.analytics import ComparisonWorker, as_of, cohorts
from cryptowatcher.bot import Controller
from cryptowatcher.enrichment_runtime import Enrichment
from cryptowatcher.events import EventEngine, History, identity
from cryptowatcher.export import Selection, ndjson
from cryptowatcher.ingestion import BybitRadar
from cryptowatcher.market import MarketError
from cryptowatcher.outcomes import OutcomeWorker
from cryptowatcher.render import event_text, notification_text
from cryptowatcher.source_context import at, save
from cryptowatcher.storage import Store

from .conftest import update
from .test_enrichment import NOW, instruments, ticker
from .test_events import candidate
from .test_events import window as history


@pytest.fixture
async def enriched(store):
    market = BybitRadar(store)
    market.observe_clock(NOW)
    spot, perp = instruments()
    market.instruments = {s.key: s for s in (spot, perp)}
    market.streams = {s.key + "/1m" for s in (spot, perp)}
    market.server_now = lambda: NOW
    market.refresh_tickers = AsyncMock()
    market._get = AsyncMock(
        return_value={
            "time": NOW,
            "result": {
                "category": "linear",
                "symbol": perp.symbol,
                "list": [
                    {"timestamp": str(NOW), "openInterest": "120"},
                    {"timestamp": str(NOW - 300_000), "openInterest": "100"},
                ],
            },
        }
    )
    with store.db:
        for item in (spot, perp):
            save(store, "ticker", item.key, ticker())
    instance = Enrichment(store, market)
    yield instance
    await market.close()


async def test_off_zero_requests_enable_shared_cadence_restart_and_causal_cutoff(enriched, store):
    await enriched.work(NOW)
    enriched.market.refresh_tickers.assert_not_called()
    enriched.market._get.assert_not_called()
    assert enriched.context("spot/BTCUSDT", NOW)["state"] == "DISABLED"
    store.update(store.snapshot().revision, store.snapshot().settings.changed(enrichment_enabled=True))
    await enriched.work(NOW)
    assert enriched.market.refresh_tickers.call_count == 2
    assert all(c.kwargs == {"max_age": 60} for c in enriched.market.refresh_tickers.call_args_list)
    assert enriched.market._get.call_count == 1
    snapshot = enriched.context("spot/BTCUSDT", NOW)
    assert snapshot["state"] == "COMPLETE"
    assert snapshot["metrics"]["oi_change_pct_5min"]["value"] == pytest.approx(20)
    assert enriched.context("spot/BTCUSDT", NOW - 1)["state"] == "MISSING"
    assert enriched.context("linear/BTCUSDT", NOW)["reason"] == "INDEPENDENT_PERPETUAL"
    restarted = Enrichment(store, enriched.market)
    await restarted.work(NOW + 299_999)
    assert enriched.market._get.call_count == 1
    assert (
        restarted.context("spot/BTCUSDT", NOW + 60_001)["metrics"]["open_interest_base"]["state"] == "STALE"
    )


async def test_optional_failure_recovery_priority_and_owner_revision_cancel(enriched, store):
    store.update(store.snapshot().revision, store.snapshot().settings.changed(enrichment_enabled=True))
    enriched.market._pending["spot/BTCUSDT/1m"] = (0, 1)
    await enriched.work(NOW)
    enriched.market._get.assert_not_called()
    assert enriched.diagnostics()["worker"]["state"] == "WAITING_FOR_CORE_RECOVERY"
    enriched.market._pending.clear()
    enriched.market._get.side_effect = MarketError("test_only")
    await enriched.work(NOW)
    context = enriched.context("spot/BTCUSDT", NOW)
    assert context["state"] == "PARTIAL" and context["metrics"]["funding_rate_pct"]["state"] == "FRESH"
    assert context["metrics"]["oi_change_base_5min"]["state"] == "MISSING"
    assert at(store, "oi", "linear/BTCUSDT", NOW) is None
    assert enriched.diagnostics()["worker"]["errors"] == {"linear/BTCUSDT/oi": "MarketError"}

    async def disable(*_):
        store.update(store.snapshot().revision, store.snapshot().settings.changed(enrichment_enabled=False))
        return enriched.market._get.return_value

    enriched.market._get.side_effect = disable
    enriched.market.server_now = lambda: NOW + 300_000
    await enriched.work(NOW + 300_000)
    assert at(store, "oi", "linear/BTCUSDT", NOW + 300_000) is None
    assert enriched.context("spot/BTCUSDT", NOW)["state"] == "DISABLED"


@pytest.mark.parametrize(
    "result",
    [
        {"category": "spot", "symbol": "BTCUSDT", "list": []},
        {"category": "linear", "symbol": "ETHUSDT", "list": []},
        {
            "category": "linear",
            "symbol": "BTCUSDT",
            "list": [{"timestamp": str(NOW + 1), "openInterest": "1"}],
        },
        {"category": "linear", "symbol": "BTCUSDT", "list": [{"timestamp": str(NOW), "openInterest": "nan"}]},
        {
            "category": "linear",
            "symbol": "BTCUSDT",
            "list": [{"timestamp": str(NOW), "openInterest": "1"}] * 2,
        },
    ],
)
async def test_invalid_optional_pages_do_not_publish_oi_or_hide_other_metrics(enriched, store, result):
    store.update(store.snapshot().revision, store.snapshot().settings.changed(enrichment_enabled=True))
    enriched.market._get.return_value = {"time": NOW, "result": result}
    await enriched.work(NOW)
    assert at(store, "oi", "linear/BTCUSDT", NOW) is None
    assert enriched.context("spot/BTCUSDT", NOW)["metrics"]["funding_rate_pct"]["value"] == pytest.approx(
        0.01
    )


async def test_ticker_refresh_coalesces_concurrent_clients_and_respects_age(enriched, monkeypatch):
    market = enriched.market
    del market.refresh_tickers
    elapsed = 1.0
    monkeypatch.setattr("cryptowatcher.ingestion.time.monotonic", lambda: elapsed)
    market._get = AsyncMock(
        return_value={"time": NOW, "result": {"list": [{"symbol": "BTCUSDT", **ticker()["values"]}]}}
    )
    market.server_now = lambda: NOW + 1
    await asyncio.gather(market.refresh_tickers("linear", 60), market.refresh_tickers("linear", 300))
    assert market._get.call_count == 1
    elapsed += 59.9
    await market.refresh_tickers("linear", 60)
    assert market._get.call_count == 1
    elapsed += 0.1
    market.server_now = lambda: NOW + 60_000
    await market.refresh_tickers("linear", 60)
    assert market._get.call_count == 2


async def test_owner_controls_frozen_event_independent_perpetual_and_no_threshold_changes(
    enriched, store, infrastructure
):
    controller = Controller(infrastructure, store, enriched.market)
    baseline = store.snapshot().settings
    for command in ("/enrichment on", "/derivatives spot/BTC", "/compare", "/recompare bad"):
        before = store.db.total_changes
        await controller.handle(update(command, owner=False), None)
        await controller.handle(update(command, group=True), None)
        assert store.db.total_changes == before
    for callback in (
        "enrichment:on",
        "enrichment:off",
        "enrichment:status",
        "derivatives:spot/BTC",
        "recompare:" + identity("missing"),
    ):
        before = store.db.total_changes
        await controller.handle(update(callback=callback, owner=False), None)
        await controller.handle(update(callback=callback, group=True), None)
        assert store.db.total_changes == before
    await controller.handle(update("/enrichment on"), None)
    assert store.snapshot().settings.changed(enrichment_enabled=False) == baseline
    await controller.enrichment_job(None)
    engine = EventEngine(store)
    value = controller.enrichment_context("spot/BTCUSDT", NOW)
    for market in ("spot", "linear"):
        engine.observe(
            market + "/BTCUSDT/1m",
            history(),
            1e6,
            0.01,
            NOW,
            live=True,
            enrichment=controller.enrichment_context(market + "/BTCUSDT", NOW),
        )
    events = History(store).events(20)
    assert {e["market_type"] for e in events} == {"spot", "linear"}
    spot = next(e for e in events if e["market_type"] == "spot")
    assert spot["enrichment"] == value
    old = spot["enrichment"]["metrics"]["funding_rate_pct"]["value"]
    with store.db:
        save(store, "ticker", "linear/BTCUSDT", {**ticker(fundingRate="0.9"), "available_at_ms": NOW + 1})
    shown = event_text(History(store).get("event-head:" + spot["event_id"]), NOW + 60_001)
    assert "Funding: 0.01 % · устарел" in shown and "BYBIT_REST_TICKERS" in shown
    assert (
        History(store).get("event-head:" + spot["event_id"])["enrichment"]["metrics"]["funding_rate_pct"][
            "value"
        ]
        == old
    )
    before = store.snapshot()
    await controller.handle(update(callback="enrichment:status"), None)
    await controller.handle(update(callback="derivatives:spot/BTC"), None)
    await controller.handle(update("/compare"), None)
    assert store.snapshot() == before
    assert "profile" in cohorts(store, ("profile",))["groups"]


async def test_export_contains_frozen_enrichment_method_and_no_future_replacement(enriched, store):
    store.update(store.snapshot().revision, store.snapshot().settings.changed(enrichment_enabled=True))
    await enriched.work(NOW)
    frozen = enriched.context("spot/BTCUSDT", NOW)
    EventEngine(store).observe("spot/BTCUSDT/1m", history(), 1e6, 0.01, NOW, live=True, enrichment=frozen)
    with store.db:
        save(store, "ticker", "linear/BTCUSDT", {**ticker(fundingRate="0.5"), "available_at_ms": NOW + 1})
    assert enriched.context("spot/BTCUSDT", NOW)["metrics"]["funding_rate_pct"]["value"] == pytest.approx(
        0.01
    )
    assert enriched.context("spot/BTCUSDT", NOW + 1)["metrics"]["funding_rate_pct"]["value"] == 50
    reader = Store(store.path, store.owner_id, readonly=True)
    records = [json.loads(line) for line in ndjson(reader, Selection())]
    reader.close()
    events = [r["record"] for r in records if r["type"] == "event"]
    assert events and all(e["enrichment"] == frozen for e in events)
    assert any(r["type"] == "methodology" and "oi_change_cadence_ms" in r["record"] for r in records)
    assert records[-1]["type"] == "complete"


async def test_context_plus_all_timeframes_confirmation_fits_telegram_limit(enriched, store):
    store.update(store.snapshot().revision, store.snapshot().settings.changed(enrichment_enabled=True))
    await enriched.work(NOW)
    context = enriched.context("spot/BTCUSDT", NOW)
    engine = EventEngine(store)
    for tf in ("1m", "3m", "5m", "15m"):
        engine.observe(
            "spot/BTCUSDT/" + tf, history(tf=tf, close=108), 1e6, 0.01, NOW, live=True, enrichment=context
        )
    pending = candidate(engine.history)
    event = engine.history.get("event-head:" + pending["event_ids"][0])
    text = notification_text(event, pending, NOW + 2000)
    assert "Есть дополнительный контекст деривативов — в подробностях." in text
    details = event_text(event, NOW + 2000)
    assert "Funding: 0.01 % · свежий" in details and "Разница Spot/Perpetual:" in details
    assert "BYBIT_REST_TICKERS" in details
    assert event["enrichment"] == context
    assert len(text.encode("utf-16-le")) // 2 < 4096


def test_explicit_recompare_keeps_old_revision_and_original_event_settings(store):
    worker = OutcomeWorker(store)
    engine = EventEngine(store, on_event=worker.admit)
    engine.observe("spot/BTCUSDT/1m", history(), 1e6, 0.01, NOW, live=True)
    h = History(store)
    event = h.events(10)[0]
    comparator = ComparisonWorker(store)
    # Reproduce an already-admitted legacy job without restoring runtime admission.
    with store.db:
        h.put(
            "comparison-job:" + event["event_id"],
            {"event_id": event["event_id"], "event_revision": event["revision"], "pending_key": ""},
        )
    assert comparator.requeue(event["event_id"], NOW) == "PENDING"
    while h.count("comparison-pending:"):
        comparator.work(NOW)
    key = identity(f"comparison/{event['event_id']}/1")
    old = h.get("comparison-head:" + key)
    assert old["revision"] == 1
    owner = store.snapshot()
    assert comparator.requeue(event["event_id"], NOW + 1) == "PENDING"
    assert comparator.requeue(event["event_id"], NOW + 1) == "ALREADY_PENDING"
    comparator.work(NOW + 1)
    new = h.get("comparison-head:" + key)
    assert new["revision"] == 2 and new["recalculation_requested_ms"] == NOW + 1
    assert as_of(h, "comparison", key, NOW) == old
    assert h.get("comparison-job:" + event["event_id"])["event_revision"] == event["revision"]
    assert h.get("event-head:" + event["event_id"]) == event and store.snapshot() == owner
    with pytest.raises(ValueError):
        comparator.requeue("missing", NOW + 2)


def test_g3_settings_default_off_readback_backup_and_method_change_fail_closed(store, tmp_path):
    settings = store.snapshot().settings.payload()
    settings.pop("enrichment_enabled")
    from cryptowatcher.config import OwnerSettings

    assert OwnerSettings.from_payload(settings).enrichment_enabled is False
    store.update(store.snapshot().revision, store.snapshot().settings.changed(enrichment_enabled=True))
    path = tmp_path / "backup.sqlite"
    store.backup(path)
    restored = Store(path, store.owner_id)
    assert restored.snapshot() == store.snapshot()
    restored.close()
    from cryptowatcher.storage import StateError

    market = SimpleNamespace()
    with store.db:
        History(store).put("enrichment-method:1", {"version": "1", "mapping": "changed"})
    with pytest.raises(StateError, match="without a new version"):
        Enrichment(store, market)
